"""Start and clean up an isolated SeaweedFS S3Tables test instance.

This helper intentionally creates only random, localhost-bound test resources:

* remote Docker container name: swc-test-catalog-<hex>
* remote data directory: /tmp/<container-name>-data
* local registry/state files under output/

It never reads or writes the production SeaweedFS credentials registry. The
registry written by `start` contains fresh random credentials for the temporary
container only and is locked down with Windows ACLs when icacls is available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
PREFIX = "swc-test-catalog-"
IMAGE = "chrislusf/seaweedfs:4.48"
REMOTE_HOST = "10.34.158.137"
REMOTE_PORTS = {
    "admin": 23646,
    "s3": 8333,
    "filer": 8888,
    "iceberg": 8181,
    "lance": 9101,
}
OWNED_NAME_RE = re.compile(r"^swc-test-catalog-[0-9a-f]{8}$")
SAFE_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage an isolated SeaweedFS S3Tables Docker test instance.")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="create a random isolated container and local SSH forwards")
    start.add_argument("--ssh-host", default=REMOTE_HOST)
    start.add_argument("--image", default=IMAGE)
    start.add_argument("--output-dir", type=Path, default=OUTPUT)
    start.add_argument("--memory", default="768m")
    start.add_argument("--cpus", default="1")
    start.add_argument("--volume-max", type=int, default=8)

    status = sub.add_parser("status", help="read-only status check for a state file")
    status.add_argument("--state-file", type=Path, required=True)

    cleanup = sub.add_parser("cleanup", help="remove only the container, directory and SSH process recorded in state")
    cleanup.add_argument("--state-file", type=Path, required=True)
    cleanup.add_argument("--keep-state", action="store_true")
    return parser.parse_args(argv)


def run_checked(cmd: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    input_bytes = None if input_text is None else input_text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    completed = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(f"command failed {completed.returncode}: {' '.join(cmd)}\nstdout={stdout[-1200:]}\nstderr={stderr[-1200:]}")
    return subprocess.CompletedProcess(cmd, completed.returncode, stdout, stderr)


def choose_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def validate_owned_state(state: dict[str, Any]) -> tuple[str, str, str]:
    name = str(state.get("name") or "")
    remote_dir = str(state.get("dir") or "")
    ssh_host = str(state.get("ssh_host") or REMOTE_HOST)
    expected_dir = f"/tmp/{name}-data"
    if not OWNED_NAME_RE.fullmatch(name):
        raise SystemExit(f"refusing to operate on non-owned container name: {name!r}")
    if remote_dir != expected_dir:
        raise SystemExit(f"refusing to remove unexpected data directory: {remote_dir!r}")
    validate_ssh_host(ssh_host)
    return name, remote_dir, ssh_host


def validate_ssh_host(value: str) -> str:
    if not value or value.startswith("-") or not SAFE_SSH_HOST_RE.fullmatch(value):
        raise SystemExit(f"invalid ssh host: {value!r}")
    return value


def validate_shell_token(value: str, pattern: str, label: str) -> str:
    if not re.fullmatch(pattern, value):
        raise SystemExit(f"invalid {label}: {value!r}")
    return value


def lock_down_file(path: Path) -> str:
    if os.name != "nt":
        path.chmod(0o600)
        return "chmod_0600"
    user = os.environ.get("USERNAME")
    if not user:
        raise RuntimeError("cannot lock registry ACL: USERNAME is not set")
    icacls = shutil.which("icacls")
    if not icacls:
        raise RuntimeError("cannot lock registry ACL: icacls is unavailable")
    command = [icacls, str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)", "SYSTEM:(R,W)"]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"cannot lock registry ACL: icacls failed {completed.returncode}: {completed.stderr[-400:]}")
    return "icacls_current_user_system_rw"


def ssh_command_forwards(local_ports: dict[str, int], remote_state: dict[str, Any], ssh_host: str) -> list[str]:
    forwards: list[str] = []
    for key, remote_key in [("admin", "remote_admin"), ("s3", "remote_s3"), ("filer", "remote_filer"), ("iceberg", "remote_iceberg"), ("lance", "remote_lance")]:
        forwards.extend(["-L", f"127.0.0.1:{local_ports[key]}:127.0.0.1:{remote_state[remote_key]}"])
    return ["ssh", "-N", *forwards, ssh_host]


def popen_no_window(argv: list[str]) -> subprocess.Popen:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)


def process_fingerprint(pid: int) -> dict[str, Any] | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\";"
            "if ($null -eq $p) { exit 1 };"
            "$o=[ordered]@{pid=$p.ProcessId; creation_date=$p.CreationDate; command_line=$p.CommandLine};"
            "$o | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", script], text=True, capture_output=True)
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return {"pid": int(data.get("pid") or pid), "creation_date": str(data.get("creation_date") or ""), "command_line": str(data.get("command_line") or "")}
    proc = Path(f"/proc/{pid}")
    try:
        command_line = proc.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        stat = proc.joinpath("stat").read_text(encoding="utf-8", errors="replace").split()
        creation_date = stat[21] if len(stat) > 21 else ""
    except OSError:
        return None
    return {"pid": pid, "creation_date": creation_date, "command_line": command_line}


def process_matches_state(state: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str]:
    pid = int(state.get("ssh_pid") or 0)
    current = process_fingerprint(pid)
    expected = state.get("ssh_tunnel_fingerprint")
    expected_argv = state.get("ssh_tunnel_argv")
    if current is None:
        return False, None, "pid_absent"
    if not isinstance(expected, dict) or not isinstance(expected_argv, list):
        return False, current, "missing_expected_fingerprint"
    if current.get("creation_date") != expected.get("creation_date"):
        return False, current, "creation_date_mismatch"
    command_line = str(current.get("command_line") or "")
    if "ssh" not in command_line.lower() or "-N" not in command_line:
        return False, current, "command_line_not_ssh_tunnel"
    for part in expected_argv[1:]:
        if str(part) not in command_line:
            return False, current, "command_line_mismatch"
    return True, current, "matched"


def cleanup_remote_owned(name: str, remote_dir: str, ssh_host: str) -> dict[str, Any]:
    validate_owned_state({"name": name, "dir": remote_dir, "ssh_host": ssh_host})
    docker_rm = subprocess.run(["ssh", ssh_host, "docker", "rm", "-f", name], text=True, capture_output=True)
    rm_dir = subprocess.run(["ssh", ssh_host, "rm", "-rf", remote_dir], text=True, capture_output=True)
    return {
        "docker_rm_returncode": docker_rm.returncode,
        "docker_rm_stdout": docker_rm.stdout[-400:],
        "docker_rm_stderr": docker_rm.stderr[-400:],
        "rm_dir_returncode": rm_dir.returncode,
        "rm_dir_stderr": rm_dir.stderr[-400:],
    }


def remote_start_script(name: str, image: str, access_key: str, secret_key: str, admin_password: str, memory: str, cpus: str, volume_max: int) -> str:
    s3_json = json.dumps(
        {
            "identities": [
                {
                    "name": name,
                    "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                    "actions": ["Admin", "Read", "List", "Tagging", "Write"],
                    "account": {"id": f"test-{name.removeprefix(PREFIX)}"},
                }
            ]
        },
        separators=(",", ":"),
    )
    return f"""set -eu
NAME={name}
DIR=/tmp/${{NAME}}-data
IMAGE={image}
mkdir -p "$DIR"
cat > "$DIR/s3.json" <<'JSON'
{s3_json}
JSON
chmod 700 "$DIR"
chmod 600 "$DIR/s3.json"
choose_port() {{ python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}}
ADMIN=$(choose_port)
S3=$(choose_port)
FILER=$(choose_port)
ICE=$(choose_port)
LANCE=$(choose_port)
IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{{{.Id}}}}')
docker run -d --name "$NAME" --memory {memory} --cpus {cpus} \\
  -v "$DIR:/data" -v "$DIR/s3.json:/etc/seaweedfs/s3.json:ro" \\
  -p 127.0.0.1:$ADMIN:{REMOTE_PORTS["admin"]} \\
  -p 127.0.0.1:$S3:{REMOTE_PORTS["s3"]} \\
  -p 127.0.0.1:$FILER:{REMOTE_PORTS["filer"]} \\
  -p 127.0.0.1:$ICE:{REMOTE_PORTS["iceberg"]} \\
  -p 127.0.0.1:$LANCE:{REMOTE_PORTS["lance"]} \\
  "$IMAGE" mini -dir=/data -ip=127.0.0.1 -ip.bind=0.0.0.0 \\
  -master.telemetry=false -master.defaultReplication=000 -master.volumeSizeLimitMB=256 \\
  -volume.max={volume_max} -volume.disk=ssd -volume.index=leveldb \\
  -admin.dataDir=/data/admin -admin.user=admin -admin.password='{admin_password}' \\
  -s3 -s3.config=/etc/seaweedfs/s3.json -s3.autoCreateBucket=false -s3.allowDeleteBucketNotEmpty=false \\
  -s3.port.iceberg={REMOTE_PORTS["iceberg"]} -s3.port.lance={REMOTE_PORTS["lance"]} -webdav=false >/dev/null
docker inspect "$NAME" --format '{{{{json .Config.Cmd}}}}' > "$DIR/cmd.json"
printf '{{"name":"%s","dir":"%s","remote_admin":%s,"remote_s3":%s,"remote_filer":%s,"remote_iceberg":%s,"remote_lance":%s,"image":"%s","image_id":"%s","cmd_json_path":"%s/cmd.json"}}\\n' "$NAME" "$DIR" "$ADMIN" "$S3" "$FILER" "$ICE" "$LANCE" "$IMAGE" "$IMAGE_ID" "$DIR"
"""


def start(argv: argparse.Namespace) -> int:
    OUTPUT.mkdir(exist_ok=True)
    ssh_host = validate_ssh_host(str(argv.ssh_host))
    image = validate_shell_token(str(argv.image), r"[A-Za-z0-9._:/@-]+", "image")
    memory = validate_shell_token(str(argv.memory), r"[0-9]+[kKmMgG]?", "memory")
    cpus = validate_shell_token(str(argv.cpus), r"[0-9]+(?:\.[0-9]+)?", "cpus")
    suffix = secrets.token_hex(4)
    name = f"{PREFIX}{suffix}"
    access_key = "swc" + secrets.token_hex(10)
    secret_key = secrets.token_urlsafe(32)
    admin_password = secrets.token_urlsafe(24)
    local_ports = {key: choose_local_port() for key in REMOTE_PORTS}

    remote_script = remote_start_script(name, image, access_key, secret_key, admin_password, memory, cpus, argv.volume_max)
    remote_state: dict[str, Any] | None = None
    tunnel: subprocess.Popen | None = None
    registry_path = argv.output_dir / f"{name}-registry.json"
    state_path = argv.output_dir / f"{name}-state.json"
    try:
        remote = run_checked(["ssh", "-o", "BatchMode=yes", ssh_host, "bash", "-s"], input_text=remote_script, timeout=180)
        remote_state = json.loads(remote.stdout.strip().splitlines()[-1])
        validate_owned_state({"name": name, "dir": remote_state["dir"], "ssh_host": ssh_host})

        tunnel_argv = ssh_command_forwards(local_ports, remote_state, ssh_host)
        tunnel = popen_no_window(tunnel_argv)
        time.sleep(0.5)
        if tunnel.poll() is not None:
            raise RuntimeError(f"ssh tunnel exited early with code {tunnel.returncode}")
        fingerprint = process_fingerprint(tunnel.pid)
        if fingerprint is None:
            raise RuntimeError("ssh tunnel started but process fingerprint could not be captured")

        registry = {
            "secrets": {
                "catalog-admin": {
                    "kind": "seaweed_admin",
                    "username": "admin",
                    "password": admin_password,
                    "allowed_endpoint_url": f"http://127.0.0.1:{local_ports['admin']}",
                    "allowed_endpoints": {
                        "s3": f"http://127.0.0.1:{local_ports['s3']}",
                        "filer": f"http://127.0.0.1:{local_ports['filer']}",
                        "iceberg": f"http://127.0.0.1:{local_ports['iceberg']}",
                        "lance": f"http://127.0.0.1:{local_ports['lance']}",
                    },
                },
                "catalog-s3": {
                    "access_key_id": access_key,
                    "secret_access_key": secret_key,
                    "allowed_endpoint_url": f"http://127.0.0.1:{local_ports['s3']}",
                },
            }
        }
        registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        acl_result = lock_down_file(registry_path)

        state = {
            "name": name,
            "dir": remote_state["dir"],
            "ssh_host": ssh_host,
            "image": remote_state["image"],
            "image_id": remote_state["image_id"],
            "cmd_json_path": remote_state["cmd_json_path"],
            "remote_ports": {key: remote_state[f"remote_{key}"] for key in REMOTE_PORTS},
            "local_urls": {key: f"http://127.0.0.1:{local_ports[key]}" for key in REMOTE_PORTS},
            "registry": str(registry_path),
            "registry_acl": acl_result,
            "ssh_pid": tunnel.pid,
            "ssh_tunnel_argv": tunnel_argv,
            "ssh_tunnel_fingerprint": fingerprint,
            "flags": {
                "memory": memory,
                "cpus": cpus,
                "volume_max": argv.volume_max,
                "s3_port_iceberg": REMOTE_PORTS["iceberg"],
                "s3_port_lance": REMOTE_PORTS["lance"],
                "s3_auto_create_bucket": False,
                "s3_allow_delete_bucket_not_empty": False,
            },
        }
        tmp_state_path = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_state_path.replace(state_path)
        print(json.dumps({"status": "started", "state_file": str(state_path), "registry_file": str(registry_path), "container": name, "ssh_pid": tunnel.pid, "local_urls": state["local_urls"], "image_id": remote_state["image_id"]}, indent=2))
        return 0
    except Exception:
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        if remote_state is not None:
            cleanup_remote_owned(name, str(remote_state.get("dir") or f"/tmp/{name}-data"), ssh_host)
        registry_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        raise


def status(argv: argparse.Namespace) -> int:
    state = json.loads(argv.state_file.read_text(encoding="utf-8"))
    name, remote_dir, ssh_host = validate_owned_state(state)
    pid = int(state.get("ssh_pid") or 0)
    pid_matches, fingerprint, pid_status = process_matches_state(state)
    docker = subprocess.run(["ssh", ssh_host, "docker", "inspect", name, "--format", "{{.State.Running}}"], text=True, capture_output=True)
    directory = subprocess.run(["ssh", ssh_host, "test", "-d", remote_dir], text=True, capture_output=True)
    print(json.dumps({
        "container": name,
        "remote_dir": remote_dir,
        "ssh_pid": pid,
        "local_pid_running": fingerprint is not None,
        "local_pid_matches_recorded_tunnel": pid_matches,
        "local_pid_status": pid_status,
        "remote_container_status_checked": docker.returncode == 0,
        "remote_container_running": docker.returncode == 0 and docker.stdout.strip() == "true",
        "remote_dir_exists": directory.returncode == 0,
    }, indent=2))
    return 0


def cleanup(argv: argparse.Namespace) -> int:
    state = json.loads(argv.state_file.read_text(encoding="utf-8"))
    name, remote_dir, ssh_host = validate_owned_state(state)
    pid = int(state.get("ssh_pid") or 0)
    pid_matches, fingerprint, pid_status = process_matches_state(state)
    tunnel_stop: dict[str, Any] = {"pid": pid, "matched_recorded_tunnel": pid_matches, "match_status": pid_status}
    if pid and pid_matches:
        if os.name == "nt":
            stopped = subprocess.run(["taskkill", "/PID", str(pid), "/F"], text=True, capture_output=True)
        else:
            stopped = subprocess.run(["kill", str(pid)], text=True, capture_output=True)
        tunnel_stop.update({"attempted": True, "returncode": stopped.returncode, "stdout": stopped.stdout[-400:], "stderr": stopped.stderr[-400:]})
    else:
        tunnel_stop.update({"attempted": False, "needs_review": fingerprint is not None})
    cleanup_result = cleanup_remote_owned(name, remote_dir, ssh_host)
    docker = subprocess.run(["ssh", ssh_host, "docker", "ps", "-a", "--format", "{{.Names}}"], text=True, capture_output=True)
    directory = subprocess.run(["ssh", ssh_host, "test", "!", "-e", remote_dir], text=True, capture_output=True)
    container_absent = docker.returncode == 0 and name not in docker.stdout.splitlines()
    remote_dir_absent = directory.returncode == 0
    evidence = {
        "container": name,
        "container_absent": container_absent,
        "remote_dir_absent": remote_dir_absent,
        "ssh_tunnel_stop": tunnel_stop,
        "remote_cleanup": cleanup_result,
        "docker_ps_returncode": docker.returncode,
        "docker_ps_stderr": docker.stderr[-400:],
        "remote_dir_check_returncode": directory.returncode,
        "remote_dir_check_stderr": directory.stderr[-400:],
        "cleaned": container_absent and remote_dir_absent and not tunnel_stop.get("needs_review", False),
    }
    evidence_path = argv.state_file.with_name(argv.state_file.stem.replace("-state", "-cleanup") + ".json")
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    if not argv.keep_state:
        argv.state_file.unlink(missing_ok=True)
    status_value = "cleaned" if evidence["cleaned"] else "needs_review"
    print(json.dumps({"status": status_value, "evidence_file": str(evidence_path), **evidence}, indent=2))
    return 0 if evidence["cleaned"] else 1


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "start":
        return start(args)
    if args.command == "status":
        return status(args)
    if args.command == "cleanup":
        return cleanup(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
