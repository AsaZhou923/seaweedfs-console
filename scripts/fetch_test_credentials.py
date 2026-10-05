"""Fetch a server-side registry through existing SSH without displaying secrets."""
import json
import getpass
import os
from pathlib import Path
import subprocess


def main():
    raw = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "10.34.158.137",
                          "cat /home/optiplex/apps/seaweedfs/credentials.json"],
                         capture_output=True, check=True).stdout
    credentials = json.loads(raw)
    # SeaweedFS S3 credential JSON uses identities[].credentials[].
    if isinstance(credentials, dict) and "identities" in credentials:
        entries = credentials["identities"]
        identity = next((item for item in entries if "Admin" in item.get("actions", [])), entries[0])
        entry = identity["credentials"][0]
        access = entry.get("accessKey")
        secret = entry.get("secretKey")
    elif isinstance(credentials, dict) and "s3_access_key_id" in credentials:
        access = credentials["s3_access_key_id"]
        secret = credentials["s3_secret_access_key"]
    else:
        raise RuntimeError("Unexpected server credential registry structure")
    if not access or not secret:
        raise RuntimeError("Incomplete server test credentials")
    path = Path(__file__).resolve().parents[1] / "output" / "server-secrets.json"
    path.parent.mkdir(exist_ok=True)
    registry = {"server-test": {"access_key_id": access, "secret_access_key": secret,
                                "allowed_endpoint_url": "http://10.34.158.137:8333"}}
    if credentials.get("admin_user") and credentials.get("admin_password"):
        registry["server-admin"] = {"kind": "seaweed_admin", "username": credentials["admin_user"],
                                     "password": credentials["admin_password"],
                                     "allowed_endpoint_url": "http://10.34.158.137:23646",
                                     "allowed_endpoints": {"filer": "http://127.0.0.1:28888",
                                                           "master": "http://127.0.0.1:29333",
                                                           "volume": "http://127.0.0.1:29340",
                                                           "s3": "http://10.34.158.137:8333"},
                                     "allowed_grpc_endpoints": {"filer": {"target": "127.0.0.1:31888", "transport": "plaintext"}}}
    path.write_text(json.dumps(registry), encoding="utf-8")
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        getpass.getuser() + ":(F)", "*S-1-5-18:(F)"],
                       capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        path.chmod(0o600)
    print("Server test registry saved (credentials redacted).")


if __name__ == "__main__":
    main()
