"""Regenerate the readonly pinned bindings; not required when running the app."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
backend = ROOT / "backend"
subprocess.run([sys.executable, "-m", "grpc_tools.protoc", "-I", str(backend),
                "--python_out", str(backend), "--grpc_python_out", str(backend),
                str(backend / "console/vendor/seaweed_mount.proto")], check=True)
print("Readonly SeaweedFS 4.48 Mount protobuf bindings generated.")
