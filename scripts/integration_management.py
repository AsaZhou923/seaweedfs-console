"""Actual read-only control-plane integration; credentials never printed."""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fastapi.testclient import TestClient
from console.config import Settings
from console.main import create_app


def main():
    with tempfile.TemporaryDirectory(prefix="swc-management-") as directory:
        data = Path(directory)
        cfg = Settings(data_dir=data, database_path=data / "console.db", admin_password="integration-only-password",
                       secrets_file=ROOT / "output" / "server-secrets.json", dev_insecure_cookie=True,
                       allowed_origins=["http://testserver"])
        client = TestClient(create_app(cfg))
        logged = client.post("/api/v1/auth/login", headers={"Origin": "http://testserver"}, json={"username": "admin", "password": cfg.admin_password})
        assert logged.status_code == 200
        headers = {"Origin": "http://testserver", "X-CSRF-Token": logged.json()["csrf_token"]}
        s3_connection = client.post("/api/v1/connections", headers=headers, json={"display_name": "OptiPlex S3",
                    "endpoint_url": "http://10.34.158.137:8333", "region": "us-east-1", "secret_ref": "server-test"})
        assert s3_connection.status_code == 200, s3_connection.text
        created = client.post("/api/v1/management/connections", headers=headers, json={
            "name": "OptiPlex read-only", "admin_url": "http://10.34.158.137:23646", "admin_secret_ref": "server-admin",
            "s3_connection_id": s3_connection.json()["id"],
            "endpoints": {"filer": "http://127.0.0.1:28888", "master": "http://127.0.0.1:29333", "volume": "http://127.0.0.1:29340", "s3": "http://10.34.158.137:8333"},
            "permissions": {"file.read_roots": ["/"]}})
        assert created.status_code == 200, created.text
        identifier = created.json()["id"]
        evidence = {"management_id": identifier, "production_mutations": 0, "routes": {}}
        for route in ["overview", "topology", "services", "services/health", "volumes", "collections", "ec", "buckets", "iam/users", "iam/groups", "iam/principals",
                      "iam/service-accounts", "iam/policies", "workers", "maintenance", "modules", "modules/mount-clients", "files"]:
            response = client.get(f"/api/v1/management/{identifier}/{route}")
            print(f"{route}: {response.status_code}")
            evidence["routes"][route] = {"status": response.status_code, "response": response.json()}
            assert response.status_code == 200, response.text
            if route == "services/health":
                services = response.json()["services"]
                assert all(services[name]["status"] == "healthy" for name in ("master", "filer", "volume", "s3")), response.text
            if route == "modules/mount-clients":
                assert response.json()["status"] == "supported", response.text
                assert response.json()["count"] == len(response.json()["items"]), response.text
        volume_page = client.get(f"/api/v1/management/{identifier}/volumes", params={"limit": 1})
        assert volume_page.status_code == 200, volume_page.text
        if volume_page.json().get("next_cursor"):
            volume_next = client.get(f"/api/v1/management/{identifier}/volumes", params={"limit": 1, "cursor": volume_page.json()["next_cursor"]})
            assert volume_next.status_code == 200, volume_next.text
            evidence["volume_pagination"] = {"first": volume_page.json(), "next": volume_next.json()}
        denied = client.post(f"/api/v1/management/{identifier}/buckets", headers=headers, json={"name": "must-not-be-created"})
        assert denied.status_code in (403, 422), denied.text
        fixture = json.loads((ROOT / "output/ui-fixture.json").read_text())
        project = client.post("/api/v1/projects", headers=headers, json={"project_key": "read-test", "display_name": "Read test"}).json()
        scope = client.post(f"/api/v1/projects/{project['id']}/scopes", headers=headers, json={
            "connection_id": s3_connection.json()["id"], "display_name": "Disposable UI samples",
            "bucket": fixture["bucket"], "prefix": "gallery/", "scope_policy": {"allow_original_download": True}}).json()
        live = client.get(f"/api/v1/management/{identifier}/objects", params={"scope_id": scope["id"], "limit": 2})
        assert live.status_code == 200, live.text
        assert len(live.json()["items"]) == 2 and live.json()["next_cursor"]
        next_page = client.get(f"/api/v1/management/{identifier}/objects", params={"scope_id": scope["id"], "limit": 2, "cursor": live.json()["next_cursor"]})
        assert next_page.status_code == 200 and next_page.json()["items"][0]["key"] != live.json()["items"][0]["key"]
        key = live.json()["items"][0]["key"]
        selected = client.post(f"/api/v1/management/{identifier}/objects/select", headers=headers, json={"scope_id": scope["id"], "key": key})
        assert selected.status_code == 200, selected.text
        download = client.get(f"/api/v1/management/{identifier}/objects/download", params={"scope_id": scope["id"], "key": key})
        assert download.status_code == 200 and download.content[:2] == b"\xff\xd8", download.status_code
        evidence["live_objects"] = {"listing": live.json(), "next_page": next_page.json(), "selected": selected.json(), "download_bytes": len(download.content)}
        path = ROOT / "output" / "management-integration-read.json"
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("MANAGEMENT_READ_PASS; live S3 pagination/select/download passed; production management writes denied.")


if __name__ == "__main__":
    main()
