from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from console import db, jobs, storage


class Body(io.BytesIO):
    pass


def image_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, format="JPEG")
    return out.getvalue()


class FakeS3:
    def __init__(self):
        self.objects = {
            "gallery/a.jpg": image_bytes(),
            "other/b.jpg": image_bytes(),
        }

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        contents = [
            {"Key": key, "Size": len(value), "ETag": f'"{key}"', "LastModified": datetime(2026, 10, 4, tzinfo=timezone.utc)}
            for key, value in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def head_object(self, **kwargs):
        key = kwargs["Key"]
        return {"ContentLength": len(self.objects[key]), "ContentType": "image/jpeg", "ETag": f'"{key}"', "LastModified": datetime(2026, 10, 4, tzinfo=timezone.utc)}

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        return {"Body": Body(self.objects[key]), "ContentType": "image/jpeg", "ContentLength": len(self.objects[key])}

    def get_bucket_cors(self, **_kwargs):
        return {"CORSRules": [{"AllowedOrigins": ["https://app.example"], "AllowedMethods": ["GET"]}]}


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(
        json.dumps({"local-file:test/admin": {"access_key_id": "a", "secret_access_key": "s", "allowed_endpoint_url": "http://seaweedfs.local:8333"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSOLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONSOLE_DATABASE_PATH", str(tmp_path / "data" / "console.db"))
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setenv("CONSOLE_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CONSOLE_CSRF_SIGNING_KEY", "csrf-key")
    monkeypatch.setenv("CONSOLE_CURSOR_SIGNING_KEY", "cursor-key")
    monkeypatch.setenv("CONSOLE_DEV_INSECURE_COOKIE", "1")
    monkeypatch.setenv("CONSOLE_SECRETS_FILE", str(secret_path))
    fake = FakeS3()
    monkeypatch.setattr(storage, "client", lambda *_args, **_kwargs: fake)
    from console.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client, app


def headers(csrf: str, origin: str = "http://testserver") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Origin": origin}


def login(client: TestClient) -> str:
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-pass"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def create_scope(client: TestClient, csrf: str, project_key: str, prefix: str) -> tuple[str, str, str]:
    connection = client.post(
        "/api/v1/connections",
        json={
            "display_name": f"conn-{project_key}",
            "endpoint_url": "http://seaweedfs.local:8333",
            "region": "us-east-1",
            "secret_ref": "local-file:test/admin",
            "approved_endpoints": {"distribution": {"url": "https://cdn.example/assets"}},
            "verify_tls": True,
        },
        headers=headers(csrf),
    )
    assert connection.status_code == 200, connection.text
    project = client.post("/api/v1/projects", json={"project_key": project_key, "display_name": project_key}, headers=headers(csrf))
    assert project.status_code == 200, project.text
    scope = client.post(
        f"/api/v1/projects/{project.json()['id']}/scopes",
        json={
            "connection_id": connection.json()["id"],
            "display_name": prefix,
            "bucket": "images",
            "prefix": prefix,
            "scope_policy": {"allow_preview": True, "allow_original_download": True},
        },
        headers=headers(csrf),
    )
    assert scope.status_code == 200, scope.text
    return connection.json()["id"], project.json()["id"], scope.json()["id"]


def run_jobs(app) -> None:
    for _ in range(10):
        if not jobs.run_once(app.state.settings):
            return


def scan(client: TestClient, app, csrf: str, scope_id: str) -> list[dict]:
    response = client.post(f"/api/v1/scopes/{scope_id}/scans", json={"max_objects": 10, "objects_per_second": 100}, headers=headers(csrf))
    assert response.status_code in (200, 202), response.text
    run_jobs(app)
    assets = client.get(f"/api/v1/scopes/{scope_id}/assets")
    assert assets.status_code == 200, assets.text
    return assets.json()["items"]


def test_asset_tags_detail_and_group_members_respect_active_scope(api):
    client, app = api
    csrf = login(client)
    _conn, project_id, scope_id = create_scope(client, csrf, "assets", "gallery/")
    item = scan(client, app, csrf, scope_id)[0]
    asset_id = item["asset_id"]

    updated = client.put(f"/api/v1/scopes/{scope_id}/assets/{asset_id}/tags", json={"tags": [" hero ", "product"]}, headers=headers(csrf))
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["hero", "product"]
    filtered = client.get(f"/api/v1/scopes/{scope_id}/assets?tag=hero")
    assert len(filtered.json()["items"]) == 1

    detail = client.get(f"/api/v1/scopes/{scope_id}/assets/{asset_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["tags"] == ["hero", "product"]
    assert len(detail.json()["visible_objects"]) == 1
    assert detail.json()["reference_status"] == "unknown"

    with db.connect(app.state.settings) as conn:
        conn.execute("INSERT INTO image_groups(id,project_id,name,source,source_ref,created_at) VALUES(?,?,?,?,?,?)", ("grp1", project_id, "Hero", "manual", None, jobs.now()))
        conn.execute("INSERT INTO image_group_members(group_id,object_id,relation_source,created_at) VALUES(?,?,?,?)", ("grp1", item["id"], "manual", jobs.now()))
    members = client.get(f"/api/v1/projects/{project_id}/groups/grp1/members")
    assert members.status_code == 200, members.text
    assert [member["id"] for member in members.json()["items"]] == [item["id"]]

    archived = client.post(f"/api/v1/scopes/{scope_id}/archive", headers=headers(csrf))
    assert archived.status_code == 200, archived.text
    assert client.get(f"/api/v1/projects/{project_id}/groups/grp1/members").json()["items"] == []
    assert client.get(f"/api/v1/scopes/{scope_id}/assets/{asset_id}").status_code == 403


def test_asset_tag_validation_and_cross_scope_denial(api):
    client, app = api
    csrf = login(client)
    _conn1, _project1, scope1 = create_scope(client, csrf, "p1", "gallery/")
    _conn2, _project2, scope2 = create_scope(client, csrf, "p2", "other/")
    other_asset = scan(client, app, csrf, scope2)[0]["asset_id"]
    too_many = client.put(f"/api/v1/scopes/{scope1}/assets/missing/tags", json={"tags": [str(i) for i in range(51)]}, headers=headers(csrf))
    assert too_many.status_code == 429
    cross = client.put(f"/api/v1/scopes/{scope1}/assets/{other_asset}/tags", json={"tags": ["x"]}, headers=headers(csrf))
    assert cross.status_code == 404


def test_cors_diagnostic_validates_origin_and_rejects_arbitrary_url(api):
    client, app = api
    csrf = login(client)
    _conn, _project, scope_id = create_scope(client, csrf, "cors", "gallery/")
    item = scan(client, app, csrf, scope_id)[0]

    invalid_origin = client.post(
        f"/api/v1/scopes/{scope_id}/diagnostics/cors-read",
        json={"origin": "https://app.example/path"},
        headers=headers(csrf),
    )
    assert invalid_origin.status_code == 400

    arbitrary = client.post(
        f"/api/v1/scopes/{scope_id}/diagnostics/cors-read",
        json={"origin": "https://app.example", "object_id": item["id"], "url": "https://evil.example/a.jpg"},
        headers=headers(csrf),
    )
    assert arbitrary.status_code == 400

    valid = client.post(
        f"/api/v1/scopes/{scope_id}/diagnostics/cors-read",
        json={"origin": "https://app.example"},
        headers=headers(csrf),
    )
    assert valid.status_code == 200
    assert valid.json()["evidence_level"] == "storage_configuration"
    assert valid.json()["business_browser_result"] == "unknown"
