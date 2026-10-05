"""Add a read-only official management connection to the existing UI fixture."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from console import db, management
from console.config import Settings
from console.main import create_app

record = json.loads((ROOT / "output/ui-fixture.json").read_text())
directory = Path(record["data_dir"])
cfg = Settings(data_dir=directory, database_path=directory / "console.db", admin_password=record["admin_password"],
               secrets_file=Path(record["secrets_file"]), dev_insecure_cookie=True,
               allowed_origins=["http://127.0.0.1:18765"])
create_app(cfg)
with db.connect(cfg) as conn:
    row = conn.execute("SELECT id,admin_secret_ref,endpoints_json FROM management_connections WHERE name='OptiPlex · SeaweedFS 4.48'").fetchone()
    if not row:
        scope = conn.execute("SELECT connection_id FROM scopes WHERE id=?", (record["scope_id"],)).fetchone()
        management.create_connection(conn, cfg, {"name": "OptiPlex · SeaweedFS 4.48", "admin_url": "http://10.34.158.137:23646",
                    "admin_secret_ref": "server-admin", "s3_connection_id": scope["connection_id"],
                    "endpoints": {"filer": "http://127.0.0.1:28888", "master": "http://127.0.0.1:29333", "volume": "http://127.0.0.1:29340", "s3": "http://10.34.158.137:8333"}})
    else:
        endpoints = json.loads(row["endpoints_json"])
        if "s3" not in endpoints:
            endpoints["s3"] = "http://10.34.158.137:8333"
            approved = management._validate_endpoints(endpoints, management._admin_secret(cfg, row["admin_secret_ref"]))
            conn.execute("UPDATE management_connections SET endpoints_json=? WHERE id=?", (json.dumps(approved), row["id"]))
print("Read-only management preview prepared; credentials remain server-side.")
