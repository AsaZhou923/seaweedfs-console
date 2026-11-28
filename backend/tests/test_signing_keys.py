from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from console.config import Settings, get_settings
from console import signing_keys


def settings(path, **overrides):
    return Settings(data_dir=path, database_path=path / "console.db", admin_password="same-password", **overrides)


def test_random_keys_are_independent_persistent_and_password_independent(tmp_path):
    original = settings(tmp_path)
    cursor, csrf = original.require_cursor_key(), original.require_csrf_key()
    assert len(cursor) == len(csrf) == 32
    assert cursor != csrf
    assert cursor != b"same-password" and csrf != b"same-password"
    restarted = Settings(data_dir=tmp_path, database_path=tmp_path / "console.db", admin_password="changed-password")
    assert restarted.require_cursor_key() == cursor
    assert restarted.require_csrf_key() == csrf
    separate = settings(tmp_path / "separate")
    assert separate.require_cursor_key() != cursor
    assert separate.require_csrf_key() != csrf
    assert not list(tmp_path.glob(".*.key"))
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "cursor-signing.key").stat().st_mode) == 0o600


def test_explicit_keys_and_environment_overrides_need_no_key_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONSOLE_CURSOR_SIGNING_KEY", "explicit-cursor")
    monkeypatch.setenv("CONSOLE_CSRF_SIGNING_KEY", "explicit-csrf")
    configured = get_settings()
    assert configured.require_cursor_key() == b"explicit-cursor"
    assert configured.require_csrf_key() == b"explicit-csrf"
    assert not list(tmp_path.glob("*.key"))


@pytest.mark.parametrize("content", [b"", b"password", b"a" * 63, b"a" * 65, b"z" * 64])
def test_malformed_existing_keys_fail_without_replacement(tmp_path, content):
    target = tmp_path / "cursor-signing.key"
    target.write_bytes(content)
    with pytest.raises(RuntimeError, match="Cannot initialize or read cursor signing key"):
        settings(tmp_path).require_cursor_key()
    assert target.read_bytes() == content


def test_unreadable_key_fails_without_password_fallback(tmp_path, monkeypatch):
    target = tmp_path / "csrf-signing.key"
    target.write_bytes(b"a" * 64)
    original_read = Path.read_bytes

    def read(path):
        if path == target:
            raise PermissionError("denied")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(RuntimeError, match="Cannot initialize or read csrf signing key"):
        settings(tmp_path).require_csrf_key()


@pytest.mark.parametrize("failure", ["restrict", "publish"])
def test_restrict_or_publish_failure_leaves_no_partial_key(tmp_path, monkeypatch, failure):
    def reject(*_args):
        raise PermissionError("denied")

    if failure == "restrict":
        monkeypatch.setattr(signing_keys, "_restrict_file", reject)
    else:
        monkeypatch.setattr(signing_keys.os, "link", reject)
    with pytest.raises(RuntimeError, match="Cannot initialize or read cursor signing key"):
        settings(tmp_path).require_cursor_key()
    assert not list(tmp_path.iterdir())


def test_concurrent_process_initialization_observes_identical_keys(tmp_path):
    code = ("import hashlib,json,sys; from pathlib import Path; from console.config import Settings; "
            "p=Path(sys.argv[1]); s=Settings(data_dir=p,database_path=p/'db'); "
            "print(json.dumps([hashlib.sha256(s.require_cursor_key()).hexdigest(),hashlib.sha256(s.require_csrf_key()).hexdigest()]))")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

    def initialize(_index):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(initialize, range(6)))
    assert all(result == results[0] for result in results)
    persisted = settings(tmp_path)
    assert results[0] == [hashlib.sha256(persisted.require_cursor_key()).hexdigest(),
                          hashlib.sha256(persisted.require_csrf_key()).hexdigest()]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["csrf-signing.key", "cursor-signing.key"]


def test_package_collection_excludes_default_and_configured_signing_keys(tmp_path):
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "package.py"
    spec = importlib.util.spec_from_file_location("signing_key_package_test", module_path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    default = settings(tmp_path / "data")
    configured = settings(tmp_path / "backend" / "custom-state")
    for configured_settings in (default, configured):
        configured_settings.require_cursor_key()
        configured_settings.require_csrf_key()
    source = tmp_path / "backend" / "keep.py"
    source.write_text("# package source\n", encoding="utf-8")
    files = builder.collect_files(tmp_path)
    assert source in files
    assert all(path.suffix != ".key" for path in files)
