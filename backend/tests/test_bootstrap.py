from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import core, db
from console.config import Settings


def make_settings(tmp_path: Path, *, username: str = "admin", password: str = "admin-pass") -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_username=username,
        admin_password=password,
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        dev_insecure_cookie=True,
        cookie_secure=False,
    )


def test_initialize_is_schema_only_and_custom_admin_login_has_single_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONSOLE_DATABASE_PATH", str(tmp_path / "data" / "console.db"))
    monkeypatch.setenv("CONSOLE_ADMIN_USERNAME", "operator")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "operator-pass")
    monkeypatch.setenv("CONSOLE_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CONSOLE_CSRF_SIGNING_KEY", "csrf-test-key")
    monkeypatch.setenv("CONSOLE_CURSOR_SIGNING_KEY", "cursor-test-key")
    monkeypatch.setenv("CONSOLE_DEV_INSECURE_COOKIE", "1")

    from console.main import create_app

    with TestClient(create_app()) as client:
        ok = client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": "operator-pass"},
            headers={"Origin": "http://testserver"},
        )
        assert ok.status_code == 200, ok.text
        default_admin = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "operator-pass"},
            headers={"Origin": "http://testserver"},
        )
        assert default_admin.status_code == 401

    settings = make_settings(tmp_path, username="operator", password="operator-pass")
    with db.connect(settings) as conn:
        rows = conn.execute("SELECT username FROM users ORDER BY username").fetchall()
    assert [row["username"] for row in rows] == ["operator"]


def test_bootstrap_restart_does_not_reset_existing_password(tmp_path: Path):
    settings = make_settings(tmp_path, password="first-pass")
    db.initialize(settings)
    with db.connect(settings) as conn:
        user = core.bootstrap_admin(conn, settings)

    restarted = make_settings(tmp_path, password="second-pass")
    db.initialize(restarted)
    with db.connect(restarted) as conn:
        same_user = core.bootstrap_admin(conn, restarted)
        assert same_user["id"] == user["id"]
        assert core.login(conn, restarted, "admin", "first-pass")["session_token"]
        with pytest.raises(core.AppError) as exc:
            core.login(conn, restarted, "admin", "second-pass")
        assert exc.value.status == 401


def test_bootstrap_rejects_different_configured_admin_when_user_exists(tmp_path: Path):
    settings = make_settings(tmp_path, username="operator", password="operator-pass")
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)

    changed = make_settings(tmp_path, username="admin", password="admin-pass")
    with db.connect(changed) as conn:
        with pytest.raises(RuntimeError, match="Configured admin user is missing"):
            core.bootstrap_admin(conn, changed)


def test_concurrent_same_config_bootstrap_returns_same_user(tmp_path: Path):
    settings = make_settings(tmp_path, username="operator", password="operator-pass")
    db.initialize(settings)
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def boot() -> None:
        try:
            barrier.wait(timeout=5)
            with db.connect(settings) as conn:
                row = core.bootstrap_admin(conn, settings)
            with lock:
                results.append(row["id"])
        except BaseException as exc:  # pragma: no cover - asserted below with full exception object
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    with db.connect(settings) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    assert count == 1
