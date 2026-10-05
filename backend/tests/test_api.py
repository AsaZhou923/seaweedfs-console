from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from console import db, jobs, storage
from console.config import Settings


class ClientError(Exception):
    def __init__(self, status: int, code: str):
        self.response = {
            "Error": {"Code": code, "Message": "storage failure should stay sanitized"},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": "req-secret-free"},
        }


class Body(io.BytesIO):
    pass


def image_bytes(mode="RGB", size=(12, 10), color="red", fmt="JPEG") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format=fmt)
    return out.getvalue()


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.get_calls: list[dict] = []

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        contents = [
            {
                "Key": key,
                "Size": len(data),
                "ETag": f'"etag-{key}"',
                "LastModified": datetime(2026, 10, 4, tzinfo=timezone.utc),
            }
            for key, data in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Contents": contents, "IsTruncated": False}

    def head_object(self, **kwargs):
        key = kwargs["Key"]
        if key.endswith("forbidden.jpg"):
            raise ClientError(403, "AccessDenied")
        if key not in self.objects:
            raise ClientError(404, "NoSuchKey")
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(self.objects[key]),
            "ContentType": "image/jpeg" if key.endswith(".jpg") else "image/png",
            "ETag": f'"etag-{key}"',
            "LastModified": datetime(2026, 10, 4, tzinfo=timezone.utc),
        }

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError(404, "NoSuchKey")
        expected = f'"etag-{key}"'
        if "IfMatch" in kwargs and kwargs["IfMatch"] != expected:
            raise ClientError(412, "PreconditionFailed")
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "Body": Body(self.objects[key]),
            "ContentType": "image/jpeg" if key.endswith(".jpg") else "image/png",
            "ContentLength": len(self.objects[key]),
        }

    def get_bucket_cors(self, **_kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "CORSRules": []}

    def get_bucket_versioning(self, **_kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Status": "Enabled"}


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(
        json.dumps(
            {
                "local-file:test/admin": {
                    "kind": "s3_access_key",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                    "allowed_endpoint_url": "http://seaweedfs.local:8333",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSOLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONSOLE_DATABASE_PATH", str(tmp_path / "data" / "console.db"))
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setenv("CONSOLE_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CONSOLE_CSRF_SIGNING_KEY", "csrf-test-key")
    monkeypatch.setenv("CONSOLE_CURSOR_SIGNING_KEY", "cursor-test-key")
    monkeypatch.setenv("CONSOLE_DEV_INSECURE_COOKIE", "1")
    monkeypatch.setenv("CONSOLE_SECRETS_FILE", str(secret_path))
    fake = FakeS3(
        {
            "gallery/a.jpg": image_bytes(),
            "gallery/b.png": image_bytes(mode="RGBA", color=(0, 255, 0, 100), fmt="PNG"),
            "other/c.jpg": image_bytes(color="blue"),
        }
    )
    monkeypatch.setattr(storage, "client", lambda *_args, **_kwargs: fake)
    from console.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client, app, fake


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin-pass"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def authed_headers(csrf: str, origin: str = "http://testserver") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Origin": origin}


def create_scope(client: TestClient, csrf: str, prefix="gallery/") -> tuple[str, str, str]:
    connection = client.post(
        "/api/v1/connections",
        json={
            "display_name": "mock-s3",
            "endpoint_url": "http://seaweedfs.local:8333",
            "region": "us-east-1",
            "secret_ref": "local-file:test/admin",
            "addressing_style": "path",
            "verify_tls": False,
        },
        headers=authed_headers(csrf),
    )
    assert connection.status_code == 200, connection.text
    project = client.post(
        "/api/v1/projects",
        json={"project_key": f"demo-{prefix.strip('/').replace('/', '-')}", "display_name": "Demo"},
        headers=authed_headers(csrf),
    )
    assert project.status_code == 200, project.text
    scope = client.post(
        f"/api/v1/projects/{project.json()['id']}/scopes",
        json={
            "connection_id": connection.json()["id"],
            "display_name": f"scope-{prefix}",
            "bucket": "images",
            "prefix": prefix,
            "scope_policy": {"allow_preview": True, "allow_original_download": True},
        },
        headers=authed_headers(csrf),
    )
    assert scope.status_code == 200, scope.text
    return connection.json()["id"], project.json()["id"], scope.json()["id"]


def run_all_jobs(app) -> None:
    for _ in range(20):
        if not jobs.run_once(app.state.settings):
            return


def scan_scope(client: TestClient, app, csrf: str, scope_id: str) -> list[dict]:
    submitted = client.post(
        f"/api/v1/scopes/{scope_id}/scans",
        json={"max_objects": 10, "objects_per_second": 100},
        headers=authed_headers(csrf),
    )
    assert submitted.status_code in (200, 202), submitted.text
    run_all_jobs(app)
    assets = client.get(f"/api/v1/scopes/{scope_id}/assets")
    assert assets.status_code == 200, assets.text
    return assets.json()["items"]


def test_auth_logout_expiry_csrf_and_same_origin(api):
    client, app, _fake = api
    assert client.get("/api/v1/auth/me").status_code == 401
    csrf = login(client)
    assert client.get("/api/v1/auth/me").status_code == 200

    blocked = client.post(
        "/api/v1/projects",
        json={"project_key": "evil", "display_name": "Evil"},
        headers=authed_headers(csrf, origin="http://testserver.evil"),
    )
    assert blocked.status_code == 403

    missing_csrf = client.post("/api/v1/projects", json={"project_key": "bad", "display_name": "Bad"})
    assert missing_csrf.status_code == 403

    logout = client.post("/api/v1/auth/logout", headers=authed_headers(csrf))
    assert logout.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401

    csrf = login(client)
    with db.connect(app.state.settings) as conn:
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00.000000Z'")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_scan_preview_archive_and_cached_media_denied(api):
    client, app, fake = api
    csrf = login(client)
    _connection_id, _project_id, scope_id = create_scope(client, csrf)
    items = scan_scope(client, app, csrf, scope_id)
    object_id = next(item["id"] for item in items if item["key"].endswith("a.jpg"))

    batch = client.post(
        f"/api/v1/scopes/{scope_id}/preview-batches",
        json={"object_ids": [object_id]},
        headers=authed_headers(csrf),
    )
    assert batch.status_code in (200, 202), batch.text
    run_all_jobs(app)

    preview = client.get(f"/api/v1/scopes/{scope_id}/objects/{object_id}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.headers["cache-control"] == "no-store"
    assert any(call.get("IfMatch") for call in fake.get_calls)

    archived = client.post(f"/api/v1/scopes/{scope_id}/archive", headers=authed_headers(csrf))
    assert archived.status_code == 200, archived.text
    assert client.get(f"/api/v1/scopes/{scope_id}/objects/{object_id}/preview").status_code == 403
    assert client.get(f"/api/v1/scopes/{scope_id}/assets").status_code == 403


def test_object_ids_cannot_cross_scope(api):
    client, app, _fake = api
    csrf = login(client)
    _conn1, _project1, scope1 = create_scope(client, csrf, "gallery/")
    _conn2, _project2, scope2 = create_scope(client, csrf, "other/")
    object_from_scope2 = scan_scope(client, app, csrf, scope2)[0]["id"]

    response = client.post(
        f"/api/v1/scopes/{scope1}/preview-batches",
        json={"object_ids": [object_from_scope2]},
        headers=authed_headers(csrf),
    )
    assert response.status_code in (403, 404)
    assert "secret" not in response.text.lower()


def test_storage_errors_are_sanitized_and_distinguish_403_404(api):
    client, app, fake = api
    csrf = login(client)
    _connection_id, _project_id, scope_id = create_scope(client, csrf)
    fake.objects["gallery/forbidden.jpg"] = image_bytes(color="black")
    items = scan_scope(client, app, csrf, scope_id)
    missing = client.get(f"/api/v1/scopes/{scope_id}/objects/not-a-real-object/preview")
    assert missing.status_code == 404
    assert "secret" not in missing.text.lower()

    forbidden = next((item for item in items if item["key"].endswith("forbidden.jpg")), None)
    if forbidden:
        response = client.post(
            f"/api/v1/scopes/{scope_id}/preview-batches",
            json={"object_ids": [forbidden["id"]]},
            headers=authed_headers(csrf),
        )
        run_all_jobs(app)
        assert response.status_code in (200, 202)
        job = client.get("/api/v1/jobs")
        assert "storage failure should stay sanitized" not in job.text


def test_secret_registry_no_credential_fallback_and_key_isolation(tmp_path):
    settings_type = type(
        "S",
        (),
        {
            "secrets_file": tmp_path / "secrets.json",
            "admin_password": "admin-pass",
            "cursor_signing_key": "cursor-key",
            "csrf_signing_key": "csrf-key",
        },
    )
    settings = settings_type()
    settings.secrets_file.write_text(json.dumps({"local-file:bad": {"access_key_id": "only-access"}}), encoding="utf-8")
    with pytest.raises(storage.StorageError) as exc:
        storage.secret_for_connection(settings, {"id": "con", "secret_ref": "local-file:bad"})
    assert exc.value.code == "SECRET_REF_INCOMPLETE"

    from console.security import sign_json, unsign_json

    cursor = sign_json({"kind": "cursor"}, settings.cursor_signing_key.encode(), "cursor:v1")
    with pytest.raises(ValueError):
        unsign_json(cursor, settings.csrf_signing_key.encode(), "csrf:v1")


def test_secret_registry_requires_approved_endpoint(tmp_path):
    settings_type = type("S", (), {"secrets_file": tmp_path / "secrets.json"})
    settings = settings_type()
    connection = {"id": "con", "secret_ref": "local-file:test", "endpoint_url": "http://seaweedfs.local:8333"}

    settings.secrets_file.write_text(
        json.dumps({"local-file:test": {"access_key_id": "access", "secret_access_key": "secret"}}),
        encoding="utf-8",
    )
    with pytest.raises(storage.StorageError) as missing:
        storage.secret_for_connection(settings, connection)
    assert missing.value.code == "SECRET_ENDPOINT_REQUIRED"

    settings.secrets_file.write_text(
        json.dumps({"local-file:test": {"access_key_id": "access", "secret_access_key": "secret", "allowed_endpoint_url": ""}}),
        encoding="utf-8",
    )
    with pytest.raises(storage.StorageError) as empty:
        storage.secret_for_connection(settings, connection)
    assert empty.value.code == "SECRET_ENDPOINT_REQUIRED"

    settings.secrets_file.write_text(
        json.dumps({"local-file:test": {"access_key_id": "access", "secret_access_key": "secret", "allowed_endpoint_url": "http://other.local:8333"}}),
        encoding="utf-8",
    )
    with pytest.raises(storage.StorageError) as mismatch:
        storage.secret_for_connection(settings, connection)
    assert mismatch.value.code == "SECRET_ENDPOINT_FORBIDDEN"

    settings.secrets_file.write_text(
        json.dumps({"local-file:test": {"access_key_id": "access", "secret_access_key": "secret", "allowed_endpoint_url": "http://seaweedfs.local:8333/"}}),
        encoding="utf-8",
    )
    assert storage.secret_for_connection(settings, connection)["aws_access_key_id"] == "access"


def test_connection_create_rejects_secret_without_approved_endpoint(tmp_path: Path):
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(
        json.dumps({"local-file:test/admin": {"access_key_id": "access", "secret_access_key": "secret"}}),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_password="admin-pass",
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        secrets_file=secret_path,
        dev_insecure_cookie=True,
        cookie_secure=False,
    )
    from console.main import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        csrf = login(client)
        response = client.post(
            "/api/v1/connections",
            json={
                "display_name": "mock-s3",
                "endpoint_url": "http://seaweedfs.local:8333",
                "region": "us-east-1",
                "secret_ref": "local-file:test/admin",
                "addressing_style": "path",
                "verify_tls": False,
            },
            headers=authed_headers(csrf),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "SECRET_ENDPOINT_REQUIRED"
        assert client.get("/api/v1/connections").json()["items"] == []
