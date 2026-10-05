"""Verify bundle integrity and exclusions without printing credential values."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"node_modules", "__pycache__", ".pytest_cache", ".venv", ".omx", "data", "output", "test-results"}
SENSITIVE_FIELDS = {"password", "adminpassword", "secretaccesskey", "awssecretaccesskey", "secretkey",
                    "accesskeyid", "awsaccesskeyid", "sessiontoken", "awssessiontoken"}
REQUIRED = {"frontend/dist/index.html", "backend/console/vendor/seaweed_mount_pb2.py",
            "backend/console/management_conditions.py", "backend/requirements.txt", "README.md",
            "README.zh-CN.md", "LICENSE", "NOTICE"}
PRIVATE_FILE_NAMES = {".env", "secrets.json", "credentials.json"}
PRIVATE_SUFFIXES = {".db", ".pyc", ".pem", ".key", ".pfx"}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class PackageVerificationError(RuntimeError):
    pass


def secret_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("_", "").replace("-", "")
            if normalized in SENSITIVE_FIELDS and isinstance(item, str) and item:
                yield item.encode()
            else:
                yield from secret_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from secret_values(item)


def _configured_registry_paths(root: Path, env: dict[str, str] | None = None, extra_paths: list[Path] | None = None) -> list[Path]:
    env = env if env is not None else os.environ
    candidates = [root / "secrets.json"]
    configured = env.get("CONSOLE_SECRETS_FILE")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(extra_paths or [])
    output = root / "output"
    candidates.extend(sorted(output.glob("*secrets*.json")))
    candidates.extend(sorted(output.glob("*-registry.json")))
    candidates.append(output / "ui-fixture.json")
    unique = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate.absolute()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def _scan_secret_sources(root: Path, env: dict[str, str] | None = None, extra_paths: list[Path] | None = None) -> tuple[set[bytes], list[str], list[str]]:
    secrets = set()
    scanned = []
    missing_required = []
    configured = (env if env is not None else os.environ).get("CONSOLE_SECRETS_FILE")
    required_sources = {Path(configured)} if configured else set()
    required_sources.update(extra_paths or [])
    for source in _configured_registry_paths(root, env, extra_paths):
        if source in required_sources and not source.exists():
            missing_required.append(str(source))
            continue
        if source.exists():
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise PackageVerificationError(f"Credential registry is not valid JSON: {source}") from exc
            scanned.append(str(source))
            secrets.update(secret_values(data))
    return secrets, scanned, missing_required


def _is_package_internal_link(target: str) -> bool:
    target = target.strip()
    if not target or target.startswith("#") or target.startswith(("http://", "https://", "mailto:")):
        return False
    if ":" in target:
        return False
    return True


def _linked_path(source: str, target: str) -> str:
    clean = target.strip().split("#", 1)[0].split("?", 1)[0]
    return (Path(source).parent / clean).as_posix()


def _verify_markdown_links(relative: str, content: bytes, payload: dict[str, str]) -> None:
    if Path(relative).suffix.lower() != ".md":
        return
    text = content.decode("utf-8", errors="ignore")
    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1).strip("<>")
        if not _is_package_internal_link(target):
            continue
        linked = _linked_path(relative, target)
        if linked and linked not in payload:
            raise PackageVerificationError(f"Package README link points to a missing packaged file: {relative} -> {target}")


def verify_package(package: Path | None = None, root: Path = ROOT, env: dict[str, str] | None = None, extra_registry_paths: list[Path] | None = None) -> dict[str, object]:
    package = package or root / "output/releases/seaweedfs-console-0.1.0-local.zip"
    secrets, scanned_sources, missing_required = _scan_secret_sources(root, env, extra_registry_paths)
    if missing_required:
        raise PackageVerificationError("Configured credential registry is missing; credential scan is insufficient")
    with zipfile.ZipFile(package) as archive:
        if archive.testzip() is not None:
            raise PackageVerificationError("Package ZIP integrity check failed")
        entries = archive.namelist()
        manifest = json.loads(archive.read("seaweedfs-console/PACKAGE-MANIFEST.json"))
        payload = {name.removeprefix("seaweedfs-console/"): name for name in entries
                   if name != "seaweedfs-console/PACKAGE-MANIFEST.json"}
        if set(payload) != set(manifest):
            raise PackageVerificationError("Package manifest does not cover exactly the packaged files")
        for relative, name in payload.items():
            parts = Path(relative).parts
            if EXCLUDED.intersection(parts) or ".." in parts or Path(relative).is_absolute():
                raise PackageVerificationError("Package includes an excluded or unsafe path")
            if Path(relative).name in PRIVATE_FILE_NAMES or Path(relative).suffix in PRIVATE_SUFFIXES:
                raise PackageVerificationError("Package includes a private file")
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != manifest[relative]:
                raise PackageVerificationError("Packaged file hash does not match its manifest")
            if any(value in content for value in secrets):
                raise PackageVerificationError("Package contains a real credential value")
            _verify_markdown_links(relative, content, payload)
        if not REQUIRED.issubset(payload):
            raise PackageVerificationError("Package is missing required runtime files")
    actual = hashlib.sha256(package.read_bytes()).hexdigest()
    declared = package.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    if actual != declared:
        raise PackageVerificationError("Package SHA256 file mismatch")
    credential_status = "absent" if secrets else ("no_secret_values" if scanned_sources else "not_scanned")
    return {"status": "PACKAGE_VERIFIED", "files": len(payload), "sha256": actual,
            "manifest": "passed", "private_paths": "excluded", "real_credentials": credential_status,
            "credential_sources_scanned": len(scanned_sources), "scanned_sources": len(scanned_sources)}


def main():
    parser = argparse.ArgumentParser(description="Verify the local SeaweedFS Console package without printing credential values.")
    parser.add_argument("--package", type=Path, default=None, help="Package zip to verify. Defaults to output/releases/seaweedfs-console-0.1.0-local.zip under --root.")
    parser.add_argument("--root", type=Path, default=ROOT, help="Project root used for package defaults and registry discovery.")
    parser.add_argument("--registry", type=Path, action="append", default=[], help="Additional credential registry JSON to scan. May be specified more than once.")
    args = parser.parse_args()
    print(json.dumps(verify_package(args.package, args.root, extra_registry_paths=args.registry)))


if __name__ == "__main__":
    main()
