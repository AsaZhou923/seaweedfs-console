from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_package(root: Path, files: dict[str, bytes]) -> Path:
    package = root / "output" / "releases" / "seaweedfs-console-0.1.0-local.zip"
    package.parent.mkdir(parents=True)
    manifest = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(f"seaweedfs-console/{name}", content)
        archive.writestr("seaweedfs-console/PACKAGE-MANIFEST.json", json.dumps(manifest))
    package_hash = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix(".zip.sha256").write_text(f"{package_hash}  {package.name}\n", encoding="utf-8")
    return package


def minimal_payload(**extra: bytes) -> dict[str, bytes]:
    payload = {
        "frontend/dist/index.html": b"<main></main>",
        "backend/console/vendor/seaweed_mount_pb2.py": b"# proto stub\n",
        "backend/console/management_conditions.py": b"# conditions\n",
        "backend/requirements.txt": b"fastapi\n",
        "README.md": b"Languages: English | [zh](README.zh-CN.md)\n",
        "README.zh-CN.md": b"[English](README.md)\n",
        "LICENSE": b"Synthetic license fixture\n",
        "NOTICE": b"Synthetic attribution fixture\n",
    }
    payload.update(extra)
    return payload


def write_packaging_fixture(root: Path) -> None:
    for relative, content in {
        "frontend/dist/index.html": "<main></main>",
        "backend/console/vendor/seaweed_mount_pb2.py": "# proto stub\n",
        "backend/console/management_conditions.py": "# conditions\n",
        "backend/requirements.txt": "fastapi\n",
        "scripts/start.ps1": "Write-Output start\n",
        "README.md": "Languages: English | [zh](README.zh-CN.md)\n[License](LICENSE) [Notice](NOTICE) [Guidelines](AGENTS.md)\n",
        "README.zh-CN.md": "[English](README.md)\n",
        "LICENSE": "Synthetic license fixture\n",
        "NOTICE": "Synthetic attribution fixture\n",
        "AGENTS.md": "# Synthetic project guidelines\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def run_package_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package.py"), *args],
        capture_output=True,
        text=True,
    )


def test_package_builder_includes_bilingual_readme_and_license_notices(tmp_path: Path):
    package_script = load_script("package.py")
    write_packaging_fixture(tmp_path)

    result = package_script.build_package(tmp_path, tmp_path / "out" / "bundle.zip")

    with zipfile.ZipFile(result["package"]) as archive:
        names = set(archive.namelist())
    assert "seaweedfs-console/README.md" in names
    assert "seaweedfs-console/README.zh-CN.md" in names
    assert "seaweedfs-console/LICENSE" in names
    assert "seaweedfs-console/NOTICE" in names
    assert "seaweedfs-console/AGENTS.md" in names


@pytest.mark.parametrize("missing", ["LICENSE", "NOTICE"])
def test_package_verifier_rejects_missing_license_notices(tmp_path: Path, missing: str):
    verifier = load_script("verify_package.py")
    payload = minimal_payload()
    del payload[missing]
    package = write_package(tmp_path, payload)

    with pytest.raises(verifier.PackageVerificationError, match="missing required runtime files"):
        verifier.verify_package(package, tmp_path, env={})


def test_package_cli_help_and_parse_errors_do_not_write_outputs(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    sentinel = tmp_path / "output" / "releases" / "seaweedfs-console-0.1.0-local.zip"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"historical package")
    sentinel.with_suffix(".zip.sha256").write_text("historical hash\n", encoding="utf-8")
    before = sentinel.read_bytes()
    sidecar_before = sentinel.with_suffix(".zip.sha256").read_text(encoding="utf-8")

    help_result = run_package_cli("--help")
    no_args_result = run_package_cli()
    unknown_result = run_package_cli("--root", str(tmp_path), "--unknown")

    assert help_result.returncode == 0
    assert "destination" in help_result.stdout
    assert no_args_result.returncode == 2
    assert unknown_result.returncode == 2
    assert sentinel.read_bytes() == before
    assert sentinel.with_suffix(".zip.sha256").read_text(encoding="utf-8") == sidecar_before
    assert not (tmp_path / "bundle.zip").exists()


def test_package_cli_writes_only_requested_fresh_destination(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "out" / "bundle.zip"

    result = run_package_cli("--root", str(tmp_path), "--destination", str(destination))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["package"] == str(destination.resolve())
    assert destination.is_file()
    assert destination.with_suffix(".zip.sha256").is_file()
    assert not (tmp_path / "output" / "releases").exists()
    assert not (destination.with_name(destination.name + ".tmp")).exists()
    assert not (destination.with_suffix(".zip.sha256.tmp")).exists()
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("seaweedfs-console/PACKAGE-MANIFEST.json"))
    assert "seaweedfs-console/README.md" in names
    assert "seaweedfs-console/README.zh-CN.md" in names
    assert manifest["README.md"] == hashlib.sha256((tmp_path / "README.md").read_bytes()).hexdigest()
    assert manifest["README.zh-CN.md"] == hashlib.sha256((tmp_path / "README.zh-CN.md").read_bytes()).hexdigest()


def test_package_cli_rejects_repeated_same_destination_without_modifying_it(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "out" / "bundle.zip"

    first = run_package_cli("--root", str(tmp_path), "--destination", str(destination))
    before_package = destination.read_bytes()
    before_sidecar = destination.with_suffix(".zip.sha256").read_text(encoding="utf-8")
    second = run_package_cli("--root", str(tmp_path), "--destination", str(destination))

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert destination.read_bytes() == before_package
    assert destination.with_suffix(".zip.sha256").read_text(encoding="utf-8") == before_sidecar


def test_package_cli_rejects_existing_destination_without_modifying_it(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "out" / "bundle.zip"
    destination.parent.mkdir()
    destination.write_bytes(b"existing package")

    result = run_package_cli("--root", str(tmp_path), "--destination", str(destination))

    assert result.returncode != 0
    assert destination.read_bytes() == b"existing package"
    assert not destination.with_suffix(".zip.sha256").exists()


def test_package_cli_rejects_existing_sidecar_without_modifying_it(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "out" / "bundle.zip"
    destination.parent.mkdir()
    destination.with_suffix(".zip.sha256").write_text("existing hash\n", encoding="utf-8")

    result = run_package_cli("--root", str(tmp_path), "--destination", str(destination))

    assert result.returncode != 0
    assert not destination.exists()
    assert destination.with_suffix(".zip.sha256").read_text(encoding="utf-8") == "existing hash\n"


def test_package_cli_rejects_historical_release_destination(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "output" / "releases" / "new-package.zip"

    result = run_package_cli("--root", str(tmp_path), "--destination", str(destination))

    assert result.returncode != 0
    assert "output/releases" in result.stderr.replace("\\", "/")
    assert not destination.exists()


def test_package_cli_rejects_destination_inside_payload_directory(tmp_path: Path):
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "scripts" / "bundle.zip"

    result = run_package_cli("--root", str(tmp_path), "--destination", str(destination))

    assert result.returncode != 0
    assert "payload source directory" in result.stderr
    assert not destination.exists()


def test_package_builder_does_not_overwrite_destination_created_after_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package_script = load_script("package.py")
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "out" / "bundle.zip"
    original_validate = package_script.validate_destination

    def validate_then_claim(root: Path, requested_destination: Path) -> Path:
        resolved = original_validate(root, requested_destination)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(b"claimed package")
        return resolved

    monkeypatch.setattr(package_script, "validate_destination", validate_then_claim)

    with pytest.raises(FileExistsError):
        package_script.build_package(tmp_path, destination)

    assert destination.read_bytes() == b"claimed package"
    assert not destination.with_suffix(".zip.sha256").exists()
    assert not list(destination.parent.glob(".*.tmp"))


def test_package_builder_cleans_owned_package_when_sidecar_created_after_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package_script = load_script("package.py")
    write_packaging_fixture(tmp_path)
    destination = tmp_path / "out" / "bundle.zip"
    sidecar = destination.with_suffix(".zip.sha256")
    original_validate = package_script.validate_destination

    def validate_then_claim_sidecar(root: Path, requested_destination: Path) -> Path:
        resolved = original_validate(root, requested_destination)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("claimed hash\n", encoding="utf-8")
        return resolved

    monkeypatch.setattr(package_script, "validate_destination", validate_then_claim_sidecar)

    with pytest.raises(FileExistsError):
        package_script.build_package(tmp_path, destination)

    assert not destination.exists()
    assert sidecar.read_text(encoding="utf-8") == "claimed hash\n"
    assert not list(destination.parent.glob(".*.tmp"))


def test_verify_package_scans_root_registry_for_copied_secret(tmp_path: Path):
    verify_script = load_script("verify_package.py")
    leaked = b"SYNTHETIC-ROOT-SECRET-123"
    (tmp_path / "secrets.json").write_text(json.dumps({"secret_access_key": leaked.decode()}), encoding="utf-8")
    package = write_package(tmp_path, minimal_payload(**{"backend/console/leak.txt": leaked}))

    with pytest.raises(verify_script.PackageVerificationError, match="credential"):
        verify_script.verify_package(package, tmp_path, env={})


def test_verify_package_scans_short_secret_values(tmp_path: Path):
    verify_script = load_script("verify_package.py")
    leaked = b"short"
    (tmp_path / "secrets.json").write_text(json.dumps({"password": leaked.decode()}), encoding="utf-8")
    package = write_package(tmp_path, minimal_payload(**{"backend/console/leak.txt": leaked}))

    with pytest.raises(verify_script.PackageVerificationError, match="credential"):
        verify_script.verify_package(package, tmp_path, env={})


def test_verify_package_scans_configured_external_registry(tmp_path: Path):
    verify_script = load_script("verify_package.py")
    leaked = b"SYNTHETIC-EXTERNAL-SECRET-123"
    external = tmp_path / "outside" / "registry.json"
    external.parent.mkdir()
    external.write_text(json.dumps({"password": leaked.decode()}), encoding="utf-8")
    package = write_package(tmp_path, minimal_payload(**{"backend/console/leak.txt": leaked}))

    with pytest.raises(verify_script.PackageVerificationError, match="credential"):
        verify_script.verify_package(package, tmp_path, env={"CONSOLE_SECRETS_FILE": str(external)})


def test_verify_package_scans_explicit_registry_argument(tmp_path: Path):
    verify_script = load_script("verify_package.py")
    leaked = b"SYNTHETIC-CLI-SECRET-123"
    external = tmp_path / "cli-registry.json"
    external.write_text(json.dumps({"password": leaked.decode()}), encoding="utf-8")
    package = write_package(tmp_path, minimal_payload(**{"backend/console/leak.txt": leaked}))

    with pytest.raises(verify_script.PackageVerificationError, match="credential"):
        verify_script.verify_package(package, tmp_path, env={}, extra_registry_paths=[external])


def test_verify_package_does_not_claim_credentials_absent_without_scan_input(tmp_path: Path):
    verify_script = load_script("verify_package.py")
    package = write_package(tmp_path, minimal_payload())

    result = verify_script.verify_package(package, tmp_path, env={})

    assert result["status"] == "PACKAGE_VERIFIED"
    assert result["scanned_sources"] == 0
    assert result["credential_sources_scanned"] == 0
    assert result["real_credentials"] == "not_scanned"


def test_verify_package_rejects_missing_bilingual_readme_link(tmp_path: Path):
    verify_script = load_script("verify_package.py")
    payload = minimal_payload()
    payload.pop("README.zh-CN.md")
    package = write_package(tmp_path, payload)

    with pytest.raises(verify_script.PackageVerificationError, match="README|runtime|link"):
        verify_script.verify_package(package, tmp_path, env={})
