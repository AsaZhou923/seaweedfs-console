"""Persist independent token-signing keys without publishing partial files."""

import csv
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


def _restrict_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    options = {"capture_output": True, "text": True, "check": True,
               "creationflags": subprocess.CREATE_NO_WINDOW}
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], **options)
    sid = next(csv.reader(identity.stdout.splitlines()))[1]
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F", "*S-1-5-18:F"], **options)


def persistent_key(data_dir: Path, purpose: str) -> bytes:
    path = data_dir / f"{purpose}-signing.key"
    temporary = None
    try:
        if not path.exists():
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{purpose}-signing-", suffix=".key", dir=data_dir)
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as output:
                _restrict_file(temporary)
                output.write(secrets.token_hex(32).encode("ascii"))
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        encoded = path.read_bytes()
        if len(encoded) != 64 or any(char not in b"0123456789abcdef" for char in encoded):
            raise ValueError("Signing key must contain exactly 64 lowercase hexadecimal characters")
        return bytes.fromhex(encoded.decode("ascii"))
    except (OSError, ValueError, subprocess.SubprocessError, IndexError) as exc:
        raise RuntimeError(f"Cannot initialize or read {purpose} signing key at {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
