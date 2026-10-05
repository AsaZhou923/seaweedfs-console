"""Build a local source+static bundle, excluding all data and credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"node_modules", "__pycache__", ".pytest_cache", ".venv", ".omx", "data", "output", "test-results"}
PRIVATE_NAMES = {".env", "secrets.json", "credentials.json"}
ROOT_FILES = ("README.md", "README.zh-CN.md", "DESIGN.md", ".gitignore", ".env.example", "secrets.example.json")
PAYLOAD_DIRECTORIES = ("backend", "frontend", "scripts", "docs")


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def collect_files(root: Path = ROOT) -> list[Path]:
    files = []
    for directory in PAYLOAD_DIRECTORIES:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if path.is_file() and path.name not in PRIVATE_NAMES and not set(relative.parts) & EXCLUDED and path.suffix not in (".db", ".pyc", ".tsbuildinfo", ".pem", ".key", ".pfx"):
                files.append(path)
    files.extend(root / name for name in ROOT_FILES if (root / name).is_file())
    return sorted(files)


def validate_destination(root: Path, destination: Path | None) -> Path:
    if destination is None:
        raise ValueError("Package destination is required; pass --destination or call build_package(root, destination)")
    root = root.resolve()
    destination = Path(destination).resolve()
    historical_releases = root / "output" / "releases"
    if is_relative_to(destination, historical_releases):
        raise ValueError("Refusing to write packages under output/releases; choose an isolated task destination")
    for directory in PAYLOAD_DIRECTORIES:
        if is_relative_to(destination, root / directory):
            raise ValueError("Refusing to write package inside a payload source directory")
    if destination.exists():
        raise FileExistsError(f"Package destination already exists: {destination}")
    sidecar = destination.with_suffix(".zip.sha256")
    if sidecar.exists():
        raise FileExistsError(f"Package hash sidecar already exists: {sidecar}")
    return destination


def unique_temporary_path(target: Path, suffix: str) -> Path:
    for _ in range(100):
        candidate = target.with_name(f".{target.name}.{uuid.uuid4().hex}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError("Could not allocate a unique temporary package output")


def publish_no_overwrite(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError("No-overwrite package publish is unsupported on this filesystem") from exc


def remove_if_owned_link(path: Path, owner: Path) -> None:
    if path.exists() and os.path.samefile(path, owner):
        path.unlink()


def build_package(root: Path = ROOT, destination: Path | None = None) -> dict[str, object]:
    root = Path(root).resolve()
    if not (root / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Build frontend before packaging")
    destination = validate_destination(root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = collect_files(root)
    manifest = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    temporary_destination = unique_temporary_path(destination, ".zip.tmp")
    sidecar = destination.with_suffix(".zip.sha256")
    temporary_sidecar = unique_temporary_path(sidecar, ".sha256.tmp")
    published_package = False
    try:
        with zipfile.ZipFile(temporary_destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, "seaweedfs-console/" + path.relative_to(root).as_posix())
            archive.writestr("seaweedfs-console/PACKAGE-MANIFEST.json", json.dumps(manifest, indent=2))
        package_hash = hashlib.sha256(temporary_destination.read_bytes()).hexdigest()
        temporary_sidecar.write_text(package_hash + "  " + destination.name + "\n", encoding="utf-8")
        publish_no_overwrite(temporary_destination, destination)
        published_package = True
        publish_no_overwrite(temporary_sidecar, sidecar)
    except Exception:
        if published_package:
            remove_if_owned_link(destination, temporary_destination)
        raise
    finally:
        temporary_destination.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
    return {"package": str(destination), "files": len(files), "sha256": package_hash,
            "credential_paths": "excluded", "credential_values": "not_scanned"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an isolated SeaweedFS Console source and static bundle.")
    parser.add_argument("--root", type=Path, default=ROOT, help="Project root to package. Defaults to this repository.")
    parser.add_argument("--destination", "--output", "-o", dest="destination", type=Path, required=True,
                        help="Fresh output zip path outside payload directories and output/releases.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(build_package(args.root, args.destination)))


if __name__ == "__main__":
    main()
