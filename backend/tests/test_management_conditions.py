from __future__ import annotations

import io
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from console import core, db, management, management_conditions
from console.config import Settings
from console.security import AppError, utc_now


class S3Error(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    def __init__(self, *, delete_honors_if_match: bool = True, cleanup_fails: bool = False, object_lock: str = "missing", put_error: S3Error | None = None, get_error: S3Error | None = None, cleanup_noop: bool = False, no_version: bool = False, null_version: bool = False, missing_head_etag: bool = False, replace_after_get: bool = False):
        self.delete_honors_if_match = delete_honors_if_match
        self.cleanup_fails = cleanup_fails
        self.object_lock = object_lock
        self.put_error = put_error
        self.get_error = get_error
        self.cleanup_noop = cleanup_noop
        self.no_version = no_version
        self.null_version = null_version
        self.missing_head_etag = missing_head_etag
        self.replace_after_get = replace_after_get
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def close(self):
        self.closed = True

    def get_object_lock_configuration(self, **kwargs):
        self.calls.append(("get_object_lock_configuration", kwargs))
        if self.object_lock == "enabled":
            return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}
        if self.object_lock == "unknown":
            raise S3Error("AccessDenied", 403)
        raise S3Error("ObjectLockConfigurationNotFoundError", 404)

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        if self.put_error is not None:
            raise self.put_error
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise S3Error("PreconditionFailed", 412)
        body = kwargs.get("Body", b"")
        etag = '"etag-' + key.rsplit("/", 1)[-1] + '"'
        version_id = None if self.no_version else ("null" if self.null_version else "v-" + key.rsplit("/", 1)[-1])
        self.objects[key] = {"body": body, "etag": etag, "version_id": version_id}
        response = {"ETag": etag, "ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}
        if version_id is not None:
            response["VersionId"] = version_id
        return response

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        key = kwargs["Key"]
        if kwargs.get("IfMatch") and self.delete_honors_if_match:
            item = self.objects.get(key)
            if item is None or kwargs["IfMatch"] != item["etag"]:
                raise S3Error("PreconditionFailed", 412)
        if self.cleanup_fails and "IfMatch" not in kwargs:
            raise S3Error("AccessDenied", 403)
        if self.cleanup_noop and "IfMatch" not in kwargs:
            return {"DeleteMarker": False}
        item = self.objects.get(key)
        if item and item.get("version_id") == "null" and "IfMatch" in kwargs and kwargs.get("VersionId") != "null":
            self.objects[key + "#delete-marker"] = {"body": b"", "etag": "delete-marker", "version_id": "dm"}
            return {"DeleteMarker": True, "VersionId": "dm"}
        self.objects.pop(key, None)
        return {"DeleteMarker": False}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        item = self.objects.get(kwargs["Key"])
        if not item:
            raise S3Error("NoSuchKey", 404)
        response = {"VersionId": item["version_id"], "ContentLength": len(item["body"]), "ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}
        if not self.missing_head_etag:
            response["ETag"] = item["etag"]
        return response

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        if self.get_error is not None:
            raise self.get_error
        item = self.objects.get(kwargs["Key"])
        if not item:
            raise S3Error("NoSuchKey", 404)
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != item["etag"]:
            raise S3Error("PreconditionFailed", 412)
        body = item["body"]
        if self.replace_after_get:
            self.objects[kwargs["Key"]] = {"body": b"replacement", "etag": '"replacement"', "version_id": None}
        return {"Body": io.BytesIO(body), "ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}


def make_settings(tmp_path: Path) -> Settings:
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(
        json.dumps(
            {
                "s3-secret": {"access_key_id": "ak", "secret_access_key": "sk", "allowed_endpoint_url": "http://s3.local"},
                "admin-secret": {"kind": "seaweed_admin", "username": "admin", "password": "pw", "allowed_endpoint_url": "http://admin.local"},
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_password="console-pass",
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        secrets_file=secret_path,
        dev_insecure_cookie=True,
        cookie_secure=False,
    )


def build_app(settings: Settings) -> FastAPI:
    db.initialize(settings)
    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management.router)
    app.include_router(management_conditions.router)
    return app


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)
        management.initialize(conn)
        management_conditions.initialize(conn)
        s3_conn = core.create_connection(conn, {"display_name": "S3", "endpoint_url": "http://s3.local", "secret_ref": "s3-secret", "verify_tls": False})
        project = core.create_project(conn, {"project_key": "p", "display_name": "Project"})
        scope = core.create_scope(conn, project["id"], {"connection_id": s3_conn["id"], "display_name": "Scope", "bucket": "bucket", "prefix": "raw/", "writable": True})
    app = build_app(settings)
    with db.connect(settings) as conn:
        login = core.login(conn, settings, "admin", "console-pass")
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, login["session_token"])
        response = client.post(
            "/api/v1/management/connections",
            json={
                "name": "Admin",
                "admin_url": "http://admin.local",
                "admin_secret_ref": "admin-secret",
                "s3_connection_id": s3_conn["id"],
                "management_write_enabled": True,
                "acknowledge_management_write": True,
                "permissions": {"object.manage": True},
            },
            headers={"Origin": "http://testserver", "X-CSRF-Token": login["csrf_token"]},
        )
        assert response.status_code == 200, response.text
        manager = response.json()
        yield client, settings, scope, manager, login["csrf_token"]
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", None)


def probe(client: TestClient, manager: dict, scope: dict, csrf: str, key: str = "probe-1"):
    return client.post(
        f"/api/v1/management/{manager['id']}/objects/check-conditional-delete",
        json={"scope_id": scope["id"]},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf, "Idempotency-Key": key},
    )


def test_supported_412_probe_updates_capability_after_cleanup(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3()
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["journal_status"] == "confirmed"
    assert body["capability_status"] == "supported"
    assert body["capability"] == "conditional_delete_if_match"
    assert body["replayed"] is False
    assert fake.objects == {}
    put_calls = [call for call in fake.calls if call[0] == "put_object"]
    assert len(put_calls) == 1
    probe_key = put_calls[0][1]["Key"]
    assert probe_key.startswith("raw/.console-probe/")
    conditional_delete = [kwargs for name, kwargs in fake.calls if name == "delete_object" and "IfMatch" in kwargs][0]
    assert conditional_delete["VersionId"].startswith("v-")
    assert conditional_delete["IfMatch"] == '"swc-never-match"'

    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"]["conditional_delete_if_match"] == "supported"
    evidence = stored["capabilities"]["evidence"]["conditional_delete_if_match"]
    assert evidence["endpoint"] == "http://s3.local"
    assert evidence["verifyTLS"] is False
    assert evidence["serverHeader"] == "SeaweedFS 4.48"
    assert evidence["probeKey"] == probe_key
    assert management_conditions.supported(settings, scope, {"ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}) is True


def test_idempotent_replay_does_not_create_second_probe(env, monkeypatch):
    client, _settings, scope, manager, _csrf = env
    fake = FakeS3()
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    first = probe(client, manager, scope, _csrf, "same")
    second = probe(client, manager, scope, _csrf, "same")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert len([call for call in fake.calls if call[0] == "put_object"]) == 1


def test_ignored_ifmatch_reports_unsupported_after_cleanup(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(delete_honors_if_match=False)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert response.json()["error_code"] == "CONDITIONAL_DELETE_IGNORED"
    assert fake.objects == {}
    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"].get("conditional_delete_if_match") is None


def test_cleanup_unknown_needs_review_and_does_not_mark_supported(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(cleanup_fails=True)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["journal_status"] == "needs_review"
    assert body["capability_status"] == "needs_review"
    assert body["error_code"] == "PROBE_CLEANUP_UNKNOWN"
    assert body["bucket"] == "bucket"
    assert body["probe_key"].startswith("raw/.console-probe/")
    assert body["version_id"].startswith("v-")
    assert len(fake.objects) == 1
    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"].get("conditional_delete_if_match") is None


def write_supported_evidence(settings, scope, *, checked_at=None, server="SeaweedFS 4.48"):
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE storage_connections SET capabilities_json=? WHERE id=?",
            (
                json.dumps({
                    "conditional_delete_if_match": "supported",
                    "evidence": {"conditional_delete_if_match": {
                        "endpoint": "http://s3.local", "verifyTLS": False, "serverHeader": server,
                        "checkedAt": checked_at or utc_now(), "probeKey": "raw/.console-probe/old",
                        "scopeId": scope["id"],
                    }},
                }),
                scope["connection_id"],
            ),
        )


def test_cleanup_requires_head_404(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(cleanup_noop=True)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "cleanup-noop")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["capability_status"] == "needs_review"
    assert body["error_code"] == "PROBE_CLEANUP_UNKNOWN"
    assert body["cleanup"]["error_code"] == "PROBE_STILL_EXISTS"
    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"].get("conditional_delete_if_match") is None


def test_put_success_then_readback_error_cleans_known_probe(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(get_error=S3Error("AccessDenied", 403))
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "readback-denied")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["capability_status"] == "unknown"
    assert body["error_code"] == "AccessDenied"
    assert fake.objects == {}
    cleanup_deletes = [kwargs for name, kwargs in fake.calls if name == "delete_object" and "IfMatch" not in kwargs]
    assert cleanup_deletes[-1]["VersionId"].startswith("v-")


def test_unversioned_cleanup_does_not_delete_replacement(env, monkeypatch):
    client, _settings, scope, manager, _csrf = env
    fake = FakeS3(no_version=True, replace_after_get=True)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "replacement")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["capability_status"] == "needs_review"
    assert body["cleanup"]["error_code"] == "PROBE_IDENTITY_CHANGED"
    assert list(fake.objects.values())[0]["etag"] == '"replacement"'


def test_unsupported_probe_clears_stale_supported_evidence(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    write_supported_evidence(settings, scope)
    fake = FakeS3(delete_honors_if_match=False)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "unsupported-clears")

    assert response.status_code == 200, response.text
    assert response.json()["capability_status"] == "unsupported"
    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"].get("conditional_delete_if_match") is None


def test_cleanup_already_missing_is_cleaned_for_ignored_ifmatch(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(delete_honors_if_match=False)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "ignored-already-missing")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["capability_status"] == "unsupported"
    assert fake.objects == {}


def test_null_version_cleanup_uses_mutable_identity_etag_guard(env, monkeypatch):
    client, _settings, scope, manager, _csrf = env
    fake = FakeS3(null_version=True)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "null-version")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["capability_status"] == "supported"
    cleanup_delete = [kwargs for name, kwargs in fake.calls if name == "delete_object" and "IfMatch" in kwargs and kwargs["IfMatch"] != '"swc-never-match"'][-1]
    assert cleanup_delete["VersionId"] == "null"
    assert cleanup_delete["IfMatch"].startswith('"etag-')
    cleanup_heads = [kwargs for name, kwargs in fake.calls if name == "head_object" and kwargs.get("VersionId") == "null"]
    assert len(cleanup_heads) >= 2
    assert not any(key.endswith("#delete-marker") for key in fake.objects)


def test_head_without_etag_is_not_supported(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(missing_head_etag=True)
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "missing-etag")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["capability_status"] == "unknown"
    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"].get("conditional_delete_if_match") is None


def test_write_gates_block_before_probe(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3()
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    with db.connect(settings) as conn:
        conn.execute("UPDATE management_connections SET permissions_json=? WHERE id=?", (json.dumps({"object.manage": False}), manager["id"]))
    response = probe(client, manager, scope, _csrf, "no-object-manage")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MANAGEMENT_WRITE_DISABLED"

    with db.connect(settings) as conn:
        conn.execute("UPDATE management_connections SET permissions_json=? WHERE id=?", (json.dumps({"object.manage": True}), manager["id"]))
        conn.execute("UPDATE scopes SET writable=0 WHERE id=?", (scope["id"],))
    response = probe(client, manager, scope, _csrf, "not-writable")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    assert [call for call in fake.calls if call[0] == "put_object"] == []


def test_dispatch_rechecks_revoked_permission_and_scope_epoch(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3()
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)
    original_bound = management_conditions._bound

    def bound_then_revoke(request, management_id, scope_id):
        result = original_bound(request, management_id, scope_id)
        with db.connect(settings) as conn:
            conn.execute("UPDATE management_connections SET permissions_json=? WHERE id=?", (json.dumps({"object.manage": False}), manager["id"]))
        return result

    monkeypatch.setattr(management_conditions, "_bound", bound_then_revoke)
    response = probe(client, manager, scope, _csrf, "revoke-before-dispatch")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MANAGEMENT_WRITE_DISABLED"
    assert fake.calls == []

    monkeypatch.setattr(management_conditions, "_bound", original_bound)
    with db.connect(settings) as conn:
        conn.execute("UPDATE management_connections SET permissions_json=? WHERE id=?", (json.dumps({"object.manage": True}), manager["id"]))

    def bound_then_bump_epoch(request, management_id, scope_id):
        result = original_bound(request, management_id, scope_id)
        with db.connect(settings) as conn:
            conn.execute("UPDATE scopes SET authz_epoch=authz_epoch+1 WHERE id=?", (scope["id"],))
        return result

    monkeypatch.setattr(management_conditions, "_bound", bound_then_bump_epoch)
    response = probe(client, manager, scope, _csrf, "epoch-before-dispatch")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    assert fake.calls == []


def test_object_lock_enabled_or_unknown_rejected_before_probe(env, monkeypatch):
    client, _settings, scope, manager, _csrf = env
    for mode, code in [("enabled", "OBJECT_LOCK_ENABLED"), ("unknown", "OBJECT_LOCK_UNKNOWN")]:
        fake = FakeS3(object_lock=mode)
        monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)
        response = probe(client, manager, scope, _csrf, f"lock-{mode}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code
        assert [call for call in fake.calls if call[0] == "put_object"] == []


def test_storage_permission_or_timeout_failure_is_unknown(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(put_error=S3Error("AccessDenied", 403))
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "put-denied")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["capability_status"] == "unknown"
    assert body["capability"] == "conditional_delete_if_match"
    assert body["error_code"] == "AccessDenied"
    assert body["bucket"] == "bucket"
    assert body["probe_key"].startswith("raw/.console-probe/")
    with db.connect(settings) as conn:
        stored = core.get_connection(conn, scope["connection_id"])
    assert stored["capabilities"].get("conditional_delete_if_match") is None


def test_failed_preflight_is_persisted_and_listed_without_raw_idempotency(env, monkeypatch):
    client, settings, scope, manager, _csrf = env
    fake = FakeS3(object_lock="enabled")
    monkeypatch.setattr(management_conditions, "_TEST_CLIENT", fake)

    response = probe(client, manager, scope, _csrf, "lock-persist")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OBJECT_LOCK_ENABLED"

    with db.connect(settings) as conn:
        rows = conn.execute("SELECT state,result_json,error_code FROM management_condition_probes").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert rows[0]["error_code"] == "OBJECT_LOCK_ENABLED"
    stored = json.loads(rows[0]["result_json"])
    assert stored["bucket"] == "bucket"
    assert stored["probe_key"].startswith("raw/.console-probe/")

    with db.connect(settings) as conn:
        conn.execute("UPDATE management_connections SET management_write_enabled=0 WHERE id=?", (manager["id"],))
    listed = client.get(
        f"/api/v1/management/{manager['id']}/objects/conditional-delete-probes",
        params={"scope_id": scope["id"]},
    )
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["journal_status"] == "failed"
    assert item["capability_status"] == "failed"
    assert item["error_code"] == "OBJECT_LOCK_ENABLED"
    assert item["bucket"] == "bucket"
    assert item["probe_key"].startswith("raw/.console-probe/")
    assert "idempotency_key" not in item
    assert "request_hash" not in item


def test_probe_list_requires_scope_association_and_read_access(env):
    client, settings, scope, manager, _csrf = env
    with db.connect(settings) as conn:
        other_conn = core.create_connection(conn, {"display_name": "Other", "endpoint_url": "http://s3.local", "secret_ref": "s3-secret", "verify_tls": False})
        other_project = core.create_project(conn, {"project_key": "other", "display_name": "Other"})
        other_scope = core.create_scope(conn, other_project["id"], {"connection_id": other_conn["id"], "display_name": "Other", "bucket": "bucket", "prefix": "other/", "writable": True})

    response = client.get(
        f"/api/v1/management/{manager['id']}/objects/conditional-delete-probes",
        params={"scope_id": other_scope["id"]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MANAGEMENT_SCOPE_MISMATCH"

    with db.connect(settings) as conn:
        conn.execute("UPDATE scopes SET archived_at=?,is_active=0 WHERE id=?", (utc_now(), scope["id"]))
    response = client.get(
        f"/api/v1/management/{manager['id']}/objects/conditional-delete-probes",
        params={"scope_id": scope["id"]},
    )
    assert response.status_code in {403, 404}


def test_supported_helper_fails_closed_for_expired_future_missing_or_mismatched_evidence(env):
    _client, settings, scope, _manager, _csrf = env
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE storage_connections SET capabilities_json=? WHERE id=?",
            (
                json.dumps({"conditional_delete_if_match": "supported", "evidence": {"conditional_delete_if_match": {"endpoint": "http://s3.local", "verifyTLS": False, "serverHeader": "SeaweedFS 4.48", "checkedAt": stale}}}),
                scope["connection_id"],
            ),
        )
    assert management_conditions.supported(settings, scope, {"ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}) is False

    fresh = utc_now()
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE storage_connections SET capabilities_json=? WHERE id=?",
            (
                json.dumps({"conditional_delete_if_match": "supported", "evidence": {"conditional_delete_if_match": {"endpoint": "http://s3.local", "verifyTLS": False, "serverHeader": "Other", "checkedAt": fresh}}}),
                scope["connection_id"],
            ),
        )
    assert management_conditions.supported(settings, scope, {"ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}) is False


def test_supported_helper_requires_head_server_header_and_rejects_future_evidence(env):
    _client, settings, scope, _manager, _csrf = env
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    write_supported_evidence(settings, scope, checked_at=future)
    assert management_conditions.supported(settings, scope, {"ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}) is False

    write_supported_evidence(settings, scope)
    assert management_conditions.supported(settings, scope, {}) is False

    write_supported_evidence(settings, scope, server=None)
    assert management_conditions.supported(settings, scope, {"ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 4.48"}}}) is False
