import io
import json
import sys
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from console import core, db, management, storage
from console.config import Settings
from console.main import create_app


@pytest.fixture
def browser(tmp_path, monkeypatch):
    registry = tmp_path / "secrets.json"
    registry.write_text(json.dumps({"a": {"kind": "seaweed_admin", "username": "u", "password": "p",
                                        "allowed_endpoint_url": "http://admin.local"}}))
    cfg = Settings(data_dir=tmp_path, database_path=tmp_path / "db", admin_password="test-password",
                   secrets_file=registry, dev_insecure_cookie=True, allowed_origins=["http://testserver"])
    client = TestClient(create_app(cfg))
    with db.connect(cfg) as conn:
        connection = core.create_connection(conn, {"display_name": "S3", "endpoint_url": "http://s3.local", "secret_ref": "s"})
        project = core.create_project(conn, {"project_key": "p", "display_name": "P"})
        scope = core.create_scope(conn, project["id"], {"connection_id": connection["id"], "display_name": "raw",
                                  "bucket": "bucket", "prefix": "raw/", "scope_policy": {"allow_original_download": True}})
        manager = management.create_connection(conn, cfg, {"name": "Admin", "admin_url": "http://admin.local",
                    "admin_secret_ref": "a", "s3_connection_id": connection["id"]})
    class S3:
        calls = []
        deleted = False
        delete_target = object()
        current_version_id = "null"
        versioning_status = None
        fail_precondition = False
        lock = False
        hold = "OFF"
        retention = {}
        missing_retention = False
        missing_hold = False
        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            return {"Contents": [{"Key": "raw/literal%..jpg", "Size": 3}], "CommonPrefixes": [{"Prefix": "raw/folder/"}],
                    "IsTruncated": True, "NextContinuationToken": "remote-token"}
        def get_bucket_versioning(self, **kwargs):
            self.calls.append({"versioning": kwargs})
            return {"Status": self.versioning_status} if self.versioning_status else {}
        def head_object(self, **kwargs):
            self.calls.append(kwargs)
            if self.deleted and kwargs.get("VersionId") == self.delete_target:
                raise S3Error("NoSuchVersion" if self.delete_target is not None else "NoSuchKey", 404)
            version_id = kwargs["VersionId"] if "VersionId" in kwargs else self.current_version_id
            result = {"ContentLength": 3, "ETag": '"etag"',
                    "LastModified": datetime(2026, 10, 4, tzinfo=timezone.utc)}
            if version_id is not None:
                result["VersionId"] = version_id
            return result
        def get_object(self, **kwargs):
            self.calls.append(kwargs)
            return {"Body": io.BytesIO(b"abc")}
        def get_object_lock_configuration(self, **kwargs):
            if not self.lock:
                raise S3Error("ObjectLockConfigurationNotFoundError", 404)
            return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}
        def get_object_retention(self, **kwargs):
            if self.missing_retention:
                raise S3Error("ObjectLockConfigurationNotFoundError", 404)
            return {"Retention": self.retention}
        def get_object_legal_hold(self, **kwargs):
            if self.missing_hold:
                raise S3Error("NoSuchObjectLegalHold", 404)
            return {"LegalHold": {"Status": self.hold}}
        def delete_object(self, **kwargs):
            self.calls.append({"delete": kwargs})
            if self.fail_precondition:
                raise S3Error("PreconditionFailed", 412)
            self.deleted = True
            self.delete_target = kwargs.get("VersionId")
            return {"VersionId": kwargs.get("VersionId")}
    s3 = S3()
    monkeypatch.setattr(storage, "client", lambda *args: s3)
    auth = client.post("/api/v1/auth/login", headers={"Origin": "http://testserver"}, json={"username": "admin", "password": cfg.admin_password})
    headers = {"Origin": "http://testserver", "X-CSRF-Token": auth.json()["csrf_token"]}
    return client, manager, scope, headers, s3, cfg


class S3Error(Exception):
    def __init__(self, code, status):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def writable(browser):
    client, manager, scope, headers, s3, cfg = browser
    with db.connect(cfg) as conn:
        conn.execute("UPDATE scopes SET writable=1 WHERE id=?", (scope["id"],))
    updated = client.put(f"/api/v1/management/connections/{manager['id']}/access-policy", headers=headers,
                        json={"management_write_enabled": True, "acknowledge_management_write": True, "permissions": {"object.manage": True}})
    assert updated.status_code == 200, updated.text
    return f"/api/v1/management/{manager['id']}/objects/delete-version", {
        "scope_id": scope["id"], "key": "raw/a.jpg", "version_id": "v1", "expected_etag": '"etag"',
        "confirm_version_delete": True, "acknowledge_unknown_references": True}, {**headers, "Idempotency-Key": "delete-one"}


def mutable_conditions(monkeypatch, supported=True):
    import console
    module = SimpleNamespace(supported=lambda settings, scope, head: supported)
    monkeypatch.setitem(sys.modules, "console.management_conditions", module)
    monkeypatch.setattr(console, "management_conditions", module, raising=False)


def test_manual_version_delete_readback_and_replay_are_safe(browser):
    client, _, _, _, s3, cfg = browser
    url, body, headers = writable(browser)
    with db.connect(cfg) as conn:
        deleted_obj = core.upsert_object(conn, body["scope_id"], {"key": body["key"], "version_id": "v1", "etag": '"etag"', "size": 3})
        replacement = core.upsert_object(conn, body["scope_id"], {"key": body["key"], "version_id": "v2", "etag": '"new"', "size": 4})
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "confirmed"
    repeat = client.post(url, headers=headers, json=body)
    assert repeat.json()["replayed"] is True
    assert len([call for call in s3.calls if "delete" in call]) == 1
    with db.connect(cfg) as conn:
        assert conn.execute("SELECT state FROM management_operations").fetchone()[0] == "confirmed"
        assert conn.execute("SELECT presence FROM objects WHERE id=?", (deleted_obj["id"],)).fetchone()[0] == "missing_confirmed"
        assert conn.execute("SELECT is_current FROM objects WHERE id=?", (replacement["id"],)).fetchone()[0] == 1
    assert [call["delete"] for call in s3.calls if "delete" in call][0]["IfMatch"] == '"etag"'


def test_null_version_delete_requires_conditions_and_sends_ifmatch(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, _ = browser
    url, body, headers = writable(browser)
    body["version_id"] = "null"
    headers["Idempotency-Key"] = "delete-null"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "confirmed"
    delete = [call["delete"] for call in s3.calls if "delete" in call][0]
    assert delete["VersionId"] == "null"
    assert delete["IfMatch"] == '"etag"'


def test_unversioned_delete_requires_unversioned_bucket_conditions_and_ifmatch(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, _ = browser
    s3.current_version_id = None
    url, body, headers = writable(browser)
    body.pop("version_id")
    headers["Idempotency-Key"] = "delete-unversioned"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    delete = [call["delete"] for call in s3.calls if "delete" in call][0]
    assert "VersionId" not in delete
    assert delete["IfMatch"] == '"etag"'
    assert any("versioning" in call for call in s3.calls)


@pytest.mark.parametrize("supported", [False])
def test_mutable_delete_without_fresh_conditions_proof_is_rejected(browser, monkeypatch, supported):
    mutable_conditions(monkeypatch, supported)
    client, _, _, _, s3, _ = browser
    url, body, headers = writable(browser)
    body["version_id"] = "null"
    headers["Idempotency-Key"] = "delete-null-no-proof"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 501, result.text
    assert result.json()["error"]["code"] == "MUTABLE_VERSION_DELETE_UNSUPPORTED"
    assert not any("delete" in call for call in s3.calls)


@pytest.mark.parametrize("status", ["Enabled", "Suspended"])
def test_unversioned_delete_refuses_versioned_buckets_to_avoid_delete_markers(browser, monkeypatch, status):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, _ = browser
    s3.current_version_id = None
    s3.versioning_status = status
    url, body, headers = writable(browser)
    body.pop("version_id")
    headers["Idempotency-Key"] = f"delete-unversioned-{status}"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "BUCKET_VERSIONING_ACTIVE"
    assert not any("delete" in call for call in s3.calls)


def test_unversioned_delete_rejects_unknown_versioning_and_object_lock(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, _ = browser
    s3.current_version_id = None
    url, body, headers = writable(browser)
    body.pop("version_id")
    s3.versioning_status = "Mystery"
    headers["Idempotency-Key"] = "delete-unversioned-unknown-versioning"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "BUCKET_VERSIONING_UNKNOWN"
    s3.versioning_status = None
    s3.lock = True
    headers["Idempotency-Key"] = "delete-unversioned-lock"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "OBJECT_RETENTION_UNKNOWN"
    assert not any("delete" in call for call in s3.calls)


def test_null_version_delete_checks_legal_hold(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, _ = browser
    url, body, headers = writable(browser)
    body["version_id"] = "null"
    headers["Idempotency-Key"] = "delete-null-hold"
    s3.lock = True
    s3.hold = "ON"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "OBJECT_LEGAL_HOLD"
    assert not any("delete" in call for call in s3.calls)


def test_delete_rejects_ifmatch_precondition_failure(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, _ = browser
    s3.current_version_id = None
    s3.fail_precondition = True
    url, body, headers = writable(browser)
    body.pop("version_id")
    headers["Idempotency-Key"] = "delete-unversioned-412"
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 412, result.text
    assert [call["delete"] for call in s3.calls if "delete" in call][0]["IfMatch"] == '"etag"'


def test_mutable_delete_marks_only_captured_revision_and_replay_preserves_replacement(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, _, _, _, s3, cfg = browser
    s3.current_version_id = None
    url, body, headers = writable(browser)
    body.pop("version_id")
    headers["Idempotency-Key"] = "delete-unversioned-index"
    old_head = {"key": body["key"], "version_id": None, "etag": '"etag"', "size": 3,
                "last_modified": "2026-10-04T00:00:00.000000Z"}
    with db.connect(cfg) as conn:
        deleted_obj = core.upsert_object(conn, body["scope_id"], old_head)
        replacement = core.upsert_object(conn, body["scope_id"], {"key": body["key"], "version_id": None,
                                          "etag": '"new"', "size": 4,
                                          "last_modified": "2026-10-04T00:00:01.000000Z"})
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    repeat = client.post(url, headers=headers, json=body)
    assert repeat.json()["replayed"] is True
    with db.connect(cfg) as conn:
        assert conn.execute("SELECT presence FROM objects WHERE id=?", (deleted_obj["id"],)).fetchone()[0] == "missing_confirmed"
        assert tuple(conn.execute("SELECT is_current,presence FROM objects WHERE id=?", (replacement["id"],)).fetchone()) == (1, "present")


def test_version_info_optional_version_reports_mutable_delete_reason(browser, monkeypatch):
    mutable_conditions(monkeypatch, False)
    client, manager, scope, _, s3, _ = browser
    s3.current_version_id = None
    result = client.get(f"/api/v1/management/{manager['id']}/objects/version-info",
                        params={"scope_id": scope["id"], "key": "raw/a.jpg"})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["version_id"] is None
    assert data["identity_strength"] == "mutable"
    assert data["can_delete"] is False
    assert data["delete_reason"] == "MUTABLE_VERSION_DELETE_UNSUPPORTED"


def test_version_info_blank_query_uses_current_strong_identity(browser):
    client, manager, scope, _, s3, _ = browser
    s3.current_version_id = "v-current"
    result = client.get(f"/api/v1/management/{manager['id']}/objects/version-info",
                        params={"scope_id": scope["id"], "key": "raw/a.jpg"})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["version_id"] == "v-current"
    assert data["identity_strength"] == "strong"
    assert data["can_delete"] is True
    assert data["requires_probe"] is False


def test_version_info_blank_query_uses_current_literal_null_identity(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, manager, scope, _, s3, _ = browser
    s3.current_version_id = "null"
    result = client.get(f"/api/v1/management/{manager['id']}/objects/version-info",
                        params={"scope_id": scope["id"], "key": "raw/a.jpg"})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["version_id"] == "null"
    assert data["identity_strength"] == "mutable"
    assert data["can_delete"] is True
    assert data["requires_probe"] is True
    assert not any("versioning" in call for call in s3.calls)


def test_version_info_explicit_null_queries_literal_null_identity(browser, monkeypatch):
    mutable_conditions(monkeypatch, True)
    client, manager, scope, _, s3, _ = browser
    s3.current_version_id = "v-current"
    result = client.get(f"/api/v1/management/{manager['id']}/objects/version-info",
                        params={"scope_id": scope["id"], "key": "raw/a.jpg", "version_id": "null"})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["version_id"] == "null"
    assert data["can_delete"] is True
    assert any(call.get("VersionId") == "null" for call in s3.calls if isinstance(call, dict))


def test_version_info_strong_version_reports_hold_block(browser):
    client, manager, scope, _, s3, _ = browser
    s3.lock = True
    s3.hold = "ON"
    result = client.get(f"/api/v1/management/{manager['id']}/objects/version-info",
                        params={"scope_id": scope["id"], "key": "raw/a.jpg", "version_id": "v1"})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["identity_strength"] == "strong"
    assert data["can_delete"] is False
    assert data["delete_block_reason"] == "OBJECT_LEGAL_HOLD"


@pytest.mark.parametrize("case", ["null", "wrong_etag", "hold", "retention", "unknown_hold", "outside"])
def test_version_delete_rejects_unsafe_targets_before_s3_delete(browser, case):
    from datetime import timedelta
    client, _, _, _, s3, _ = browser
    url, body, headers = writable(browser)
    if case == "null": body["version_id"] = "null"
    if case == "wrong_etag": body["expected_etag"] = '"other"'
    if case == "outside": body["key"] = "private/a.jpg"
    if case in {"hold", "retention", "unknown_hold"}:
        s3.lock = True
        s3.hold = "ON" if case == "hold" else "OFF" if case == "retention" else None
        if case == "retention":
            s3.retention = {"Mode": "GOVERNANCE", "RetainUntilDate": datetime.now(timezone.utc) + timedelta(days=1)}
    result = client.post(url, headers=headers, json=body)
    assert result.status_code in (403, 409, 501), result.text
    assert not any("delete" in call for call in s3.calls)


def test_version_delete_requires_permissions_and_explicit_confirmation(browser):
    client, manager, scope, headers, s3, _ = browser
    url = f"/api/v1/management/{manager['id']}/objects/delete-version"
    body = {"scope_id": scope["id"], "key": "raw/a", "version_id": "v1", "expected_etag": '"etag"',
            "confirm_version_delete": True, "acknowledge_unknown_references": True}
    assert client.post(url, headers={**headers, "Idempotency-Key": "d"}, json=body).status_code == 403
    url, body, headers = writable(browser)
    assert client.post(url, headers={key: value for key, value in headers.items() if key != "Idempotency-Key"}, json=body).status_code == 422
    assert client.post(url, headers=headers, json={**body, "confirm_version_delete": False}).status_code == 422
    assert not any("delete" in call for call in s3.calls)


@pytest.mark.parametrize("hold", ["ON", "OFF"])
def test_missing_retention_does_not_skip_legal_hold_check(browser, hold):
    client, _, _, _, s3, _ = browser
    url, body, headers = writable(browser)
    s3.lock = True
    s3.missing_retention = True
    s3.hold = hold
    result = client.post(url, headers=headers, json=body)
    assert result.status_code == (409 if hold == "ON" else 200), result.text
    assert s3.deleted is (hold == "OFF")


def test_live_pagination_binds_query_scope_and_preserves_literal_keys(browser):
    client, manager, scope, _, s3, _ = browser
    base = f"/api/v1/management/{manager['id']}/objects"
    query = {"scope_id": scope["id"], "prefix": "raw/", "limit": 1}
    page = client.get(base, params=query)
    assert page.status_code == 200
    assert page.json()["items"][0]["key"] == "raw/literal%..jpg"
    assert page.json()["total"] is None
    cursor = page.json()["next_cursor"]
    assert client.get(base, params={**query, "cursor": cursor}).status_code == 200
    assert s3.calls[-1]["ContinuationToken"] == "remote-token"
    assert client.get(base, params={**query, "prefix": "raw/folder/", "cursor": cursor}).status_code == 400
    assert client.get(base, params={**query, "prefix": "production/"}).status_code == 403


def test_literal_null_download_and_select_existing_revision_keep_properties(browser):
    client, manager, scope, headers, s3, cfg = browser
    base = f"/api/v1/management/{manager['id']}/objects"
    body = {"scope_id": scope["id"], "key": "raw/literal%..jpg"}
    selected = client.post(base + "/select", headers=headers, json=body)
    assert selected.status_code == 200
    obj = selected.json()["object"]
    with db.connect(cfg) as conn:
        conn.execute("UPDATE objects SET properties_json=? WHERE id=?", ('{"width":640}', obj["id"]))
    again = client.post(base + "/select", headers=headers, json=body)
    assert again.json()["object"]["properties"]["width"] == 640
    downloaded = client.get(base + "/download", params={**body, "version_id": "null"})
    assert downloaded.content == b"abc"
    assert s3.calls[-1]["VersionId"] == "null"
    assert s3.calls[-1]["IfMatch"] == '"etag"'
    assert client.post(base + "/select", json=body).status_code == 403


def test_unassociated_management_and_archived_scope_cannot_browse(browser):
    client, manager, scope, _, _, cfg = browser
    with db.connect(cfg) as conn:
        conn.execute("UPDATE management_connections SET s3_connection_id=NULL WHERE id=?", (manager["id"],))
    assert client.get(f"/api/v1/management/{manager['id']}/objects", params={"scope_id": scope["id"]}).status_code == 403


def test_s3_secret_endpoint_binding_rejects_another_host(tmp_path):
    registry = tmp_path / "secrets.json"
    registry.write_text(json.dumps({"s3": {"access_key_id": "public", "secret_access_key": "private",
                                          "allowed_endpoint_url": "http://s3.local:8333"}}))
    cfg = Settings(data_dir=tmp_path, database_path=tmp_path / "db", secrets_file=registry)
    connection = {"id": "connection", "secret_ref": "s3", "endpoint_url": "http://s3.local:8333"}
    assert storage.secret_for_connection(cfg, connection)["aws_access_key_id"] == "public"
    with pytest.raises(storage.StorageError) as error:
        storage.secret_for_connection(cfg, {**connection, "endpoint_url": "http://other-host:8333"})
    assert error.value.code == "SECRET_ENDPOINT_FORBIDDEN"


def test_delete_marker_response_is_not_confirmed_as_permanent_delete(browser, monkeypatch):
    client, _, _, _, s3, _ = browser
    mutable_conditions(monkeypatch, True)
    url, body, headers = writable(browser)
    body["version_id"] = None
    s3.current_version_id = None
    original = s3.delete_object

    def create_marker(**kwargs):
        original(**kwargs)
        return {"DeleteMarker": True, "VersionId": "new-delete-marker"}

    monkeypatch.setattr(s3, "delete_object", create_marker)
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert response.json()["result"]["confirmed"] is False
    replay = client.post(url, headers=headers, json=body)
    assert replay.json()["replayed"] is True
    assert len([call for call in s3.calls if "delete" in call]) == 1
