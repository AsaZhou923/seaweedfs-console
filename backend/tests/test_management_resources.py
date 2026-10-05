from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from console import db, management, management_ops, management_resources
from console.config import Settings
from console.security import AppError


class S3Error(Exception):
    def __init__(self, code: str, message: str = "sensitive url https://secret.example.invalid?X-Amz-Signature=hidden"):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeAdmin:
    def __init__(self):
        self.calls: list[tuple[str, str, Any]] = []
        self.close_count = 0
        self.responses: dict[str, Any] = {
            "/api/users": {
                "users": [
                    {"username": "alice", "email": "a@example.com", "access_keys": [{"access_key": "AKIA", "secret_key": "hidden"}]},
                    {"username": "static-user", "email": "s@example.com", "is_static": True},
                ]
            },
            "/api/users/alice": {
                "username": "alice",
                "email": "a@example.com",
                "access_keys": [{"access_key": "AKIA", "secret_key": "hidden", "created_at": "2099-01-01T00:00:00Z"}],
            },
            "/api/users/alice/policies": {"policies": ["Read"]},
            "/api/principals": {
                "principals": ["arn:aws:iam::user/alice", "arn:aws:iam::role/admin"],
                "secret_key": "hidden",
            },
            "/api/s3/buckets/photos": {"bucket": {"name": "photos", "owner": "alice"}},
            "/api/service-accounts": {"service_accounts": []},
            "/api/groups/team": {"name": "team", "status": "enabled"},
            "/api/groups/team/members": {"members": ["alice"]},
        }
        self.ignore_writes = False

    def get_json(self, path: str, params: dict[str, Any] | None = None):
        self.calls.append(("GET", path, params))
        return self.responses.get(path, {})

    def request_json(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ):
        self.calls.append((method, path, json_body))
        if path == "/api/object-store/policies/validate":
            return {"valid": True}
        if self.ignore_writes:
            return {"ignored": True}
        if path == "/api/users/alice/access-keys" and method == "POST":
            self.responses["/api/users/alice"]["access_keys"].append({"access_key": "new-public", "status": "Active"})
            return {"access_key": "new-public", "secret_key": "new-secret"}
        if path == "/api/users/alice/policies" and method == "PUT":
            self.responses[path] = {"policies": list((json_body or {}).get("actions", []))}
            return {"message": "User policies updated successfully"}
        if path == "/api/groups/team/status" and method == "PUT":
            enabled = (json_body or {}).get("enabled")
            self.responses["/api/groups/team"]["status"] = "enabled" if enabled else "disabled"
            return {"message": "Group status updated"}
        if path.startswith("/api/s3/buckets/") and path.endswith("/quota") and method == "PUT":
            bucket = path.removeprefix("/api/s3/buckets/").removesuffix("/quota")
            size = (json_body or {}).get("quota_size", 0)
            unit = (json_body or {}).get("quota_unit", "MB")
            multiplier = {"B": 1, "KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024, "TB": 1024 * 1024 * 1024 * 1024}[unit]
            quota = size * multiplier
            if not (json_body or {}).get("quota_enabled", True) and quota > 0:
                quota = -quota
            bucket_path = f"/api/s3/buckets/{bucket}"
            self.responses.setdefault(bucket_path, {"bucket": {"name": bucket}})
            self.responses[bucket_path]["bucket"].update({"quota": quota, "quota_enabled": (json_body or {}).get("quota_enabled", True)})
        if path.startswith("/api/s3/buckets/") and path.endswith("/owner") and method == "PUT":
            bucket = path.removeprefix("/api/s3/buckets/").removesuffix("/owner")
            bucket_path = f"/api/s3/buckets/{bucket}"
            self.responses.setdefault(bucket_path, {"bucket": {"name": bucket}})
            self.responses[bucket_path]["bucket"].update({"owner": (json_body or {}).get("owner", "")})
        if path == "/api/s3/buckets/photos/lifecycle" and method == "PUT":
            self.responses[path] = {"bucket": "photos", "rules": (json_body or {}).get("rules", [])}
        if path == "/api/s3/buckets/photos/lifecycle" and method == "DELETE":
            self.responses[path] = {}
        if path == "/api/s3/buckets/photos/policy" and method == "PUT":
            self.responses[path] = {"policy": (json_body or {}).get("policy")}
        if path == "/api/s3/buckets/photos/policy" and method == "DELETE":
            self.responses[path] = {}
        return {}

    def close(self):
        self.close_count += 1


class FakeS3:
    def __init__(self):
        self.deleted = False
        self.non_empty = False
        self.buckets = {"photos"}
        self.close_count = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.versioning = {"Status": "Suspended"}
        self.object_lock = {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}
        self.object_lock_error_code: str | None = None
        self.versioning_error_code: str | None = None
        self.fail_methods: set[str] = set()

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        return {"KeyCount": 1, "Contents": [{"Key": "x"}]} if self.non_empty else {"KeyCount": 0}

    def list_object_versions(self, **kwargs):
        self.calls.append(("list_object_versions", kwargs))
        return {}

    def list_multipart_uploads(self, **kwargs):
        self.calls.append(("list_multipart_uploads", kwargs))
        return {}

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        if self.deleted or kwargs.get("Bucket") not in self.buckets:
            raise AppError("BUCKET_NOT_FOUND", "bucket was not found", 404)
        return {}

    def create_bucket(self, **kwargs):
        self.calls.append(("create_bucket", kwargs))
        if "create_bucket" in self.fail_methods:
            raise S3Error("AccessDenied")
        self.buckets.add(kwargs["Bucket"])
        self.deleted = False
        return {"created": True}

    def delete_bucket(self, **kwargs):
        self.calls.append(("delete_bucket", kwargs))
        self.buckets.discard(kwargs["Bucket"])
        self.deleted = True
        return {"deleted": True}

    def get_bucket_versioning(self, **kwargs):
        self.calls.append(("get_bucket_versioning", kwargs))
        if self.versioning_error_code:
            raise S3Error(self.versioning_error_code)
        return dict(self.versioning)

    def put_bucket_versioning(self, **kwargs):
        self.calls.append(("put_bucket_versioning", kwargs))
        if "put_bucket_versioning" in self.fail_methods:
            raise S3Error("AccessDenied")
        self.versioning = dict(kwargs["VersioningConfiguration"])
        return {"ok": True}

    def get_object_lock_configuration(self, **kwargs):
        self.calls.append(("get_object_lock_configuration", kwargs))
        if self.object_lock_error_code:
            raise S3Error(self.object_lock_error_code)
        return dict(self.object_lock)

    def put_object_lock_configuration(self, **kwargs):
        self.calls.append(("put_object_lock_configuration", kwargs))
        if "put_object_lock_configuration" in self.fail_methods:
            raise S3Error("AccessDenied")
        self.object_lock = {"ObjectLockConfiguration": dict(kwargs["ObjectLockConfiguration"])}
        return {"ok": True}

    def close(self):
        self.close_count += 1


class HttpIamAdmin:
    def __init__(self):
        self.ignore_writes = False
        self.requests: list[tuple[str, str, Any]] = []
        self.users: dict[str, dict[str, Any]] = {
            "alice": {"username": "alice", "email": "a@example.com", "actions": ["Read"], "policy_names": [], "access_keys": [], "groups": []}
        }
        self.user_policies: dict[str, list[str]] = {"alice": ["Read"]}
        self.groups: dict[str, dict[str, Any]] = {"team": {"name": "team", "status": "enabled", "members": ["alice"], "policies": []}}
        self.service_accounts: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.key_counter = 0
        self.service_counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        if method == "GET" and path == "/login":
            return httpx.Response(200, text='<input type="hidden" name="csrf_token" value="login-csrf">', headers={"content-type": "text/html"})
        if method == "POST" and path == "/login":
            return httpx.Response(302, headers={"location": "/admin"})
        if method == "GET" and path == "/admin":
            return httpx.Response(200, text='<meta name="csrf-token" content="admin-csrf">', headers={"content-type": "text/html"})
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        self.requests.append((method, path, body if body != {} else None))
        try:
            return self._json(self._handle_api(method, path, body, dict(request.url.params)))
        except KeyError:
            return self._json({"error": "not found"}, status=404)

    def _json(self, value: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=value, headers={"content-type": "application/json"})

    def _user_details(self, username: str) -> dict[str, Any]:
        user = dict(self.users[username])
        user["access_keys"] = [dict(key) for key in user.get("access_keys", [])]
        return user

    def _service_list(self, parent_user: str | None = None) -> dict[str, Any]:
        accounts = [dict(account) for account in self.service_accounts.values() if not parent_user or account["parent_user"] == parent_user]
        return {"service_accounts": accounts}

    def _handle_api(self, method: str, path: str, body: dict[str, Any], params: dict[str, str]) -> Any:
        parts = [part for part in path.split("/") if part]
        if path == "/api/users" and method == "GET":
            return {"users": [self._user_details(name) for name in sorted(self.users)], "total_users": len(self.users)}
        if path == "/api/users" and method == "POST":
            username = body["username"]
            if not self.ignore_writes:
                self.users[username] = {"username": username, "email": body.get("email", ""), "actions": list(body.get("actions", [])), "policy_names": list(body.get("policy_names", [])), "access_keys": [], "groups": []}
                self.user_policies[username] = list(body.get("actions", []))
                if body.get("generate_key"):
                    self.key_counter += 1
                    key = {"access_key": f"AK{self.key_counter}", "secret_key": f"secret-{self.key_counter}", "status": "Active"}
                    self.users[username]["access_keys"].append({k: v for k, v in key.items() if k != "secret_key"})
                    return key
            return {"message": "User created successfully"}
        if len(parts) >= 3 and parts[:2] == ["api", "users"]:
            username = parts[2]
            if len(parts) == 3:
                if method == "GET":
                    return self._user_details(username)
                if method == "PUT":
                    if not self.ignore_writes:
                        self.users[username].update({"email": body.get("email", ""), "actions": list(body.get("actions", [])), "policy_names": list(body.get("policy_names", []))})
                    return {"message": "User updated successfully"}
                if method == "DELETE":
                    if not self.ignore_writes:
                        del self.users[username]
                    return {"message": "User deleted successfully"}
            if len(parts) == 4 and parts[3] == "policies":
                if method == "GET":
                    return {"policies": list(self.user_policies.get(username, []))}
                if method == "PUT":
                    if not self.ignore_writes:
                        self.user_policies[username] = list(body.get("actions", []))
                    return {"message": "User policies updated successfully"}
            if len(parts) == 4 and parts[3] == "access-keys" and method == "POST":
                self.key_counter += 1
                key = {"access_key": f"AK{self.key_counter}", "secret_key": f"secret-{self.key_counter}", "status": "Active"}
                if not self.ignore_writes:
                    self.users[username]["access_keys"].append({k: v for k, v in key.items() if k != "secret_key"})
                return key
            if len(parts) == 5 and parts[3] == "access-keys":
                access_key = parts[4]
                if method == "DELETE":
                    if not self.ignore_writes:
                        self.users[username]["access_keys"] = [key for key in self.users[username].get("access_keys", []) if key.get("access_key") != access_key]
                    return {"message": "Access key deleted successfully"}
            if len(parts) == 6 and parts[3] == "access-keys" and parts[5] == "status" and method == "PUT":
                if not self.ignore_writes:
                    for key in self.users[username].get("access_keys", []):
                        if key.get("access_key") == parts[4]:
                            key["status"] = body["status"]
                return {"message": "Access key status updated successfully"}
        if path == "/api/service-accounts" and method == "GET":
            return self._service_list(params.get("parent_user"))
        if path == "/api/service-accounts" and method == "POST":
            self.service_counter += 1
            account_id = f"sa-{self.service_counter}"
            account = {"id": account_id, "parent_user": body["parent_user"], "description": body.get("description", ""), "status": "Active", "access_key_id": f"SAK{self.service_counter}", "secret_access_key": f"sa-secret-{self.service_counter}", "expiration": body.get("expiration", "")}
            if not self.ignore_writes:
                self.service_accounts[account_id] = {k: v for k, v in account.items() if k != "secret_access_key"}
            return {"message": "Service account created successfully", "service_account": account}
        if len(parts) == 3 and parts[:2] == ["api", "service-accounts"]:
            account_id = parts[2]
            if method == "GET":
                return dict(self.service_accounts[account_id])
            if method == "PUT":
                if not self.ignore_writes:
                    self.service_accounts[account_id].update({"status": body.get("status", ""), "description": body.get("description", ""), "expiration": body.get("expiration", "")})
                return {"message": "Service account updated successfully", "service_account": dict(self.service_accounts.get(account_id, {"id": account_id}))}
            if method == "DELETE":
                if not self.ignore_writes:
                    del self.service_accounts[account_id]
                return {"message": "Service account deleted successfully"}
        if path == "/api/groups" and method == "GET":
            return {"groups": [dict(group) for group in self.groups.values()], "total_groups": len(self.groups)}
        if path == "/api/groups" and method == "POST":
            if not self.ignore_writes:
                self.groups[body["name"]] = {"name": body["name"], "status": "enabled", "members": [], "policies": []}
            return {"message": "Group created successfully"}
        if len(parts) >= 3 and parts[:2] == ["api", "groups"]:
            name = parts[2]
            if len(parts) == 3:
                if method == "GET":
                    return dict(self.groups[name])
                if method == "DELETE":
                    if not self.ignore_writes:
                        del self.groups[name]
                    return {"message": "Group deleted successfully"}
            if len(parts) == 4 and parts[3] == "status" and method == "PUT":
                if not self.ignore_writes:
                    self.groups[name]["status"] = "enabled" if body["enabled"] else "disabled"
                return {"message": "Group status updated"}
            if len(parts) == 4 and parts[3] == "members":
                if method == "GET":
                    return {"members": list(self.groups[name]["members"])}
                if method == "POST":
                    if not self.ignore_writes and body["username"] not in self.groups[name]["members"]:
                        self.groups[name]["members"].append(body["username"])
                    return {"message": "Group member added"}
            if len(parts) == 5 and parts[3] == "members" and method == "DELETE":
                if not self.ignore_writes:
                    self.groups[name]["members"] = [member for member in self.groups[name]["members"] if member != parts[4]]
                return {"message": "Group member removed"}
            if len(parts) == 4 and parts[3] == "policies":
                if method == "GET":
                    return {"policies": list(self.groups[name]["policies"])}
                if method == "POST":
                    if not self.ignore_writes and body["policy_name"] not in self.groups[name]["policies"]:
                        self.groups[name]["policies"].append(body["policy_name"])
                    return {"message": "Group policy attached"}
            if len(parts) == 5 and parts[3] == "policies" and method == "DELETE":
                if not self.ignore_writes:
                    self.groups[name]["policies"] = [policy for policy in self.groups[name]["policies"] if policy != parts[4]]
                return {"message": "Group policy detached"}
        if path == "/api/object-store/policies" and method == "GET":
            return {"policies": [dict(policy) for policy in self.policies.values()]}
        if path == "/api/principals" and method == "GET":
            return {"principals": ["arn:aws:iam::user/alice", "arn:aws:iam::role/admin"], "secret_access_key": "hidden"}
        if path == "/api/object-store/policies/validate" and method == "POST":
            return {"valid": True}
        if path == "/api/object-store/policies" and method == "POST":
            if not self.ignore_writes:
                self.policies[body["name"]] = {"name": body["name"], "document": dict(body["document"])}
            return {"message": "Policy created successfully"}
        if len(parts) == 4 and parts[:3] == ["api", "object-store", "policies"]:
            name = parts[3]
            if method == "GET":
                return dict(self.policies[name])
            if method == "PUT":
                if not self.ignore_writes:
                    self.policies[name] = {"name": name, "document": dict(body["document"])}
                return {"message": "Policy updated successfully"}
            if method == "DELETE":
                if not self.ignore_writes:
                    del self.policies[name]
                return {"message": "Policy deleted successfully"}
        raise KeyError(path)


def make_settings(tmp_path: Path) -> Settings:
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(json.dumps({}), encoding="utf-8")
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


def make_http_management_app(tmp_path: Path, monkeypatch, admin: HttpIamAdmin, *, iam_manage: bool = True):
    settings = make_settings(tmp_path)
    settings.secrets_file.write_text(
        json.dumps(
            {
                "server-admin": {
                    "kind": "seaweed_admin",
                    "username": "admin",
                    "password": "pass",
                    "allowed_endpoint_url": "http://admin.local:23646",
                }
            }
        ),
        encoding="utf-8",
    )
    db.initialize(settings)
    permissions = {"bucket.manage": False, "iam.manage": iam_manage, "bucket_write_prefixes": ["swc-management-"]}
    with db.connect(settings) as conn:
        management.initialize(conn)
        management_ops.initialize(conn)
        conn.execute(
            """
            INSERT INTO management_connections(
              id,name,admin_url,admin_secret_ref,s3_connection_id,protocol_baseline,
              endpoints_json,management_write_enabled,permissions_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mgmt_http",
                "Admin",
                "http://admin.local:23646",
                "server-admin",
                None,
                "4.48",
                "{}",
                1,
                json.dumps(permissions, sort_keys=True),
                "2026-10-04T00:00:00Z",
                "2026-10-04T00:00:00Z",
            ),
        )
        conn.commit()
    monkeypatch.setattr(management, "_TEST_TRANSPORT", admin.transport())
    monkeypatch.setattr(management, "mutation", lambda _request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(management, "current_user", lambda _request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(management_resources, "mutation", lambda _request: SimpleNamespace(id="admin"))
    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management_resources.router)
    return settings, TestClient(app)


def test_http_management_write_requires_idempotency_key_before_dispatch(tmp_path: Path, monkeypatch):
    admin = HttpIamAdmin()
    settings, client = make_http_management_app(tmp_path, monkeypatch, admin)
    response = client.post(
        "/api/v1/management/mgmt_http/iam/users",
        json={"username": "bob", "email": "", "actions": [], "policy_names": []},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert admin.requests == []
    with db.connect(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_operations").fetchone()[0] == 0


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    db.initialize(settings)
    admin = FakeAdmin()
    s3 = FakeS3()
    operations: list[dict[str, Any]] = []
    mgmt = {
        "id": "mgmt_1",
        "management_write_enabled": True,
        "permissions": {"bucket.manage": True, "iam.manage": True, "bucket_write_prefixes": ["photos", "test-"]},
    }

    def require_management(_request, management_id: str, write: bool = False):
        assert management_id == "mgmt_1"
        return dict(mgmt)

    def perform_operation(settings, management, actor_id, action, method, path, payload, **kwargs):
        before = None
        if kwargs.get("readback"):
            try:
                before = kwargs["readback"]()
            except AppError as exc:
                if exc.status != 404:
                    raise
        response = kwargs["invoke"]() if kwargs.get("invoke") else admin.request_json(method, path, payload)
        after = None
        if kwargs.get("readback"):
            try:
                after = kwargs["readback"]()
            except AppError as exc:
                if exc.status == 404 and method.upper() == "DELETE":
                    after = None
                else:
                    raise
        elif kwargs.get("readback_path"):
            after = admin.get_json(kwargs["readback_path"], params=kwargs.get("readback_params"))
        verify = kwargs.get("verify")
        confirmed = bool(verify and verify(before, after, response))
        record = {
            "management": management,
            "actor_id": actor_id,
            "action": action,
            "method": method,
            "path": path,
            "payload": payload,
            "kwargs": kwargs,
            "confirmed": confirmed,
        }
        operations.append(record)
        result = {"operation_id": f"op_{len(operations)}", "status": "confirmed" if confirmed else "needs_review", "result": {"confirmed": confirmed}}
        if kwargs.get("return_created_secret") and confirmed:
            result["created_credentials"] = response
            result["secret_receipt_available"] = True
        return result

    monkeypatch.setattr(management, "require_management", require_management)
    monkeypatch.setattr(management, "admin_client", lambda _settings, _conn, _mgmt: admin)
    monkeypatch.setattr(management, "associated_s3_client", lambda _settings, _mgmt: s3, raising=False)
    monkeypatch.setattr(management_ops, "perform_operation", perform_operation)
    monkeypatch.setattr(management_resources, "mutation", lambda _request: SimpleNamespace(id="admin"))

    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management_resources.router)
    with TestClient(app) as client:
        yield client, admin, s3, operations


def test_user_details_redact_secrets_and_mark_simulated_created_at(api):
    client, admin, _s3, _operations = api

    listed = client.get("/api/v1/management/mgmt_1/iam/users")
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["source"] == "object_store_users_api"
    item = listed_body["items"][0]
    assert item["access_keys"][0]["access_key"] == "AKIA"
    assert item["is_static"] is None
    assert item["is_static_source"] == "unknown"
    assert listed_body["items"][1]["is_static"] is True
    assert listed_body["items"][1]["is_static_source"] == "upstream"
    assert "secret_key" not in json.dumps(item).lower()

    details = client.get("/api/v1/management/mgmt_1/iam/users/alice")
    assert details.status_code == 200
    key = details.json()["access_keys"][0]
    assert key["access_key"] == "AKIA"
    assert key["created_at"] is None
    assert key["created_at_source"] == "upstream_simulated_not_authoritative"
    assert "hidden" not in details.text
    assert admin.close_count >= 2


def test_principals_proxy_uses_official_shape_and_redacts_secrets(api):
    client, admin, _s3, _operations = api

    response = client.get("/api/v1/management/mgmt_1/iam/principals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "official_admin_principals_api"
    assert body["principals"] == ["arn:aws:iam::user/alice", "arn:aws:iam::role/admin"]
    assert body["items"] == [
        {"principal": "arn:aws:iam::user/alice", "type": "user", "name": "alice"},
        {"principal": "arn:aws:iam::role/admin", "type": "role", "name": "admin"},
    ]
    assert "secret_key" not in json.dumps(body)
    assert ("GET", "/api/principals", None) in admin.calls


def test_principals_proxy_rejects_unexpected_shape(api):
    client, admin, _s3, _operations = api
    admin.responses["/api/principals"] = {"principals": [{"principal": "arn:aws:iam::user/alice", "secret_key": "hidden"}]}

    response = client.get("/api/v1/management/mgmt_1/iam/principals")
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "INVALID_PRINCIPALS_RESPONSE"


@pytest.mark.parametrize("candidates", [[], None])
def test_principals_proxy_accepts_empty_official_list(api, candidates):
    client, admin, _s3, _operations = api
    admin.responses["/api/principals"] = {"principals": candidates}

    response = client.get("/api/v1/management/mgmt_1/iam/principals")
    assert response.status_code == 200, response.text
    assert response.json()["principals"] == []
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    assert response.json()["coverage"] == "suggestions_only_not_identity_inventory"


def test_user_policies_use_official_readback_shape_and_allow_empty_clear(api):
    client, admin, _s3, operations = api

    read = client.get("/api/v1/management/mgmt_1/iam/users/alice/policies")
    assert read.status_code == 200
    assert read.json() == {"policies": ["Read"]}

    clear = client.put("/api/v1/management/mgmt_1/iam/users/alice/policies", json={"actions": []})
    assert clear.status_code == 200, clear.text
    assert clear.json()["status"] == "confirmed"
    assert operations[-1]["payload"] == {"actions": []}
    assert admin.responses["/api/users/alice/policies"] == {"policies": []}


def test_group_status_translates_client_status_to_official_enabled_field(api):
    client, _admin, _s3, operations = api

    response = client.put("/api/v1/management/mgmt_1/iam/groups/team/status", json={"status": "disabled"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert operations[-1]["payload"] == {"enabled": False}


def test_generated_secret_receipt_is_returned_once_and_manual_secret_is_rejected(api):
    client, _admin, _s3, operations = api

    response = client.post("/api/v1/management/mgmt_1/iam/users/alice/access-keys", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_credentials"] == {"access_key": "new-public", "secret_key": "new-secret"}
    assert operations[-1]["action"] == "iam.manage"
    assert operations[-1]["method"] == "POST"
    assert operations[-1]["path"] == "/api/users/alice/access-keys"
    assert operations[-1]["kwargs"]["return_created_secret"] is True

    rejected = client.post("/api/v1/management/mgmt_1/iam/users/alice/access-keys", json={"secret_key": "manual"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "MANUAL_SECRET_IMPORT_UNSUPPORTED"


def test_bucket_delete_requires_empty_s3_and_uses_standard_s3_delete(api):
    client, _admin, s3, operations = api

    response = client.delete("/api/v1/management/mgmt_1/buckets/photos")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert operations[-1]["method"] == "DELETE"
    assert operations[-1]["path"] == "/s3/buckets/photos"
    assert ("delete_bucket", {"Bucket": "photos"}) in s3.calls

    s3.deleted = False
    s3.non_empty = True
    rejected = client.delete("/api/v1/management/mgmt_1/buckets/photos")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "BUCKET_NOT_EMPTY"


def test_bucket_versioning_and_object_lock_use_associated_s3_with_verify(api):
    client, _admin, s3, operations = api

    read_versioning = client.get("/api/v1/management/mgmt_1/buckets/photos/versioning")
    assert read_versioning.status_code == 200
    assert read_versioning.json()["status"] == "Suspended"

    write_versioning = client.put("/api/v1/management/mgmt_1/buckets/photos/versioning", json={"status": "Enabled"})
    assert write_versioning.status_code == 200, write_versioning.text
    assert write_versioning.json()["status"] == "confirmed"
    assert operations[-1]["method"] == "PUT"
    assert operations[-1]["path"] == "/s3/buckets/photos/versioning"
    assert ("put_bucket_versioning", {"Bucket": "photos", "VersioningConfiguration": {"Status": "Enabled"}}) in s3.calls

    read_lock = client.get("/api/v1/management/mgmt_1/buckets/photos/object-lock")
    assert read_lock.status_code == 200
    assert read_lock.json()["object_lock_configuration"] == {"ObjectLockEnabled": "Enabled"}

    config = {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 7}}}
    write_lock = client.put("/api/v1/management/mgmt_1/buckets/photos/object-lock", json={"object_lock_configuration": config})
    assert write_lock.status_code == 200, write_lock.text
    assert write_lock.json()["status"] == "confirmed"
    assert operations[-1]["method"] == "PUT"
    assert operations[-1]["path"] == "/s3/buckets/photos/object-lock"
    assert s3.close_count >= 4


@pytest.mark.parametrize("code", ["NoSuchObjectLockConfiguration", "ObjectLockConfigurationNotFoundError"])
def test_object_lock_missing_configuration_is_not_unsupported(api, code):
    client, _admin, s3, _operations = api
    s3.object_lock_error_code = code

    response = client.get("/api/v1/management/mgmt_1/buckets/photos/object-lock")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "bucket": "photos",
        "status": "Off",
        "configuration_state": "not_configured",
        "object_lock_configuration": {},
        "source": "associated_s3",
        "capability": "not_checked",
    }


def test_object_lock_not_implemented_is_unsupported(api):
    client, _admin, s3, _operations = api
    s3.object_lock_error_code = "NotImplemented"

    response = client.get("/api/v1/management/mgmt_1/buckets/photos/object-lock")

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_s3_configuration_errors_do_not_expose_raw_exception_text(api):
    client, _admin, s3, _operations = api
    s3.versioning_error_code = "AccessDenied"

    response = client.get("/api/v1/management/mgmt_1/buckets/photos/versioning")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "S3_CONFIGURATION_UNAVAILABLE"
    assert body["error"]["detail"] == {"s3_error_code": "AccessDenied"}
    assert "secret.example" not in response.text
    assert "X-Amz-Signature" not in response.text


def test_service_accounts_params_and_policy_validate_are_readonly(api):
    client, admin, _s3, operations = api

    response = client.get("/api/v1/management/mgmt_1/iam/service-accounts", params={"parent_user": "alice"})
    assert response.status_code == 200
    assert admin.calls[-1] == ("GET", "/api/service-accounts", {"parent_user": "alice"})

    validation = client.post("/api/v1/management/mgmt_1/iam/policies/validate", json={"document": {"Statement": []}})
    assert validation.status_code == 200
    assert validation.json() == {"valid": True}
    assert operations == []


def test_bucket_quota_owner_lifecycle_policy_verify_exact_values(api):
    client, admin, _s3, operations = api

    invalid_quota = client.put("/api/v1/management/mgmt_1/buckets/photos/quota", json={"quota_size": "1", "quota_enabled": "false"})
    assert invalid_quota.status_code == 422
    assert invalid_quota.json()["error"]["code"] == "INVALID_REQUEST"
    assert operations == []

    zero_enabled = client.put("/api/v1/management/mgmt_1/buckets/photos/quota", json={"quota_size": 0, "quota_unit": "B", "quota_enabled": True})
    assert zero_enabled.status_code == 422
    assert zero_enabled.json()["error"]["field"] == "quota_size"
    assert operations == []

    overflow = client.put("/api/v1/management/mgmt_1/buckets/photos/quota", json={"quota_size": 9223372036854775808, "quota_unit": "B", "quota_enabled": True})
    assert overflow.status_code == 422
    assert overflow.json()["error"]["field"] == "quota_size"
    assert operations == []

    max_quota = client.put("/api/v1/management/mgmt_1/buckets/photos/quota", json={"quota_size": 9223372036854775807, "quota_unit": "B", "quota_enabled": True})
    assert max_quota.status_code == 200, max_quota.text
    assert max_quota.json()["status"] == "confirmed"
    assert admin.responses["/api/s3/buckets/photos"]["bucket"]["quota"] == 9223372036854775807

    quota = client.put("/api/v1/management/mgmt_1/buckets/photos/quota", json={"quota_size": 1, "quota_unit": "GB", "quota_enabled": True})
    assert quota.status_code == 200, quota.text
    assert quota.json()["status"] == "confirmed"
    assert operations[-1]["confirmed"] is True
    assert admin.responses["/api/s3/buckets/photos"]["bucket"]["quota"] == 1073741824
    assert admin.responses["/api/s3/buckets/photos"]["bucket"]["quota_enabled"] is True

    disabled_quota = client.put("/api/v1/management/mgmt_1/buckets/photos/quota", json={"quota_size": 1, "quota_unit": "MB", "quota_enabled": False})
    assert disabled_quota.status_code == 200, disabled_quota.text
    assert disabled_quota.json()["status"] == "confirmed"
    assert admin.responses["/api/s3/buckets/photos"]["bucket"]["quota"] == -1048576
    assert admin.responses["/api/s3/buckets/photos"]["bucket"]["quota_enabled"] is False

    owner = client.put("/api/v1/management/mgmt_1/buckets/photos/owner", json={"owner": "alice"})
    assert owner.status_code == 200, owner.text
    assert owner.json()["status"] == "confirmed"
    assert operations[-1]["confirmed"] is True

    lifecycle_body = {"rules": [{"id": "expire", "enabled": True, "prefix": "tmp/", "days": 30}]}
    lifecycle = client.put("/api/v1/management/mgmt_1/buckets/photos/lifecycle", json={"lifecycle": lifecycle_body})
    assert lifecycle.status_code == 200, lifecycle.text
    assert lifecycle.json()["status"] == "confirmed"
    assert operations[-1]["payload"] == {
        "bucket": "photos",
        "rules": [{"id": "expire", "status": "Enabled", "prefix": "tmp/", "expiration_days": 30}],
    }

    policy_body = {"Version": "2012-10-17", "Statement": []}
    policy = client.put("/api/v1/management/mgmt_1/buckets/photos/policy", json={"policy": policy_body})
    assert policy.status_code == 200, policy.text
    assert policy.json()["status"] == "confirmed"


def test_basic_bucket_create_uses_associated_s3_fallback_and_admin_fields_stay_admin(api):
    client, admin, s3, operations = api
    s3.buckets.discard("test-new")

    response = client.post("/api/v1/management/mgmt_1/buckets", json={"name": "test-new", "region": "us-east-1"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert operations[-1]["method"] == "POST"
    assert operations[-1]["path"] == "/s3/buckets/test-new"
    assert ("head_bucket", {"Bucket": "test-new"}) in s3.calls
    assert ("create_bucket", {"Bucket": "test-new"}) in s3.calls

    lock_config = {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 7}}}
    s3.buckets.discard("test-advanced")
    advanced = client.post(
        "/api/v1/management/mgmt_1/buckets",
        json={
            "name": "test-advanced",
            "region": "us-east-1",
            "quota_size": 1,
            "quota_unit": "GB",
            "quota_enabled": True,
            "owner": "alice",
            "object_lock_enabled": True,
            "set_default_retention": True,
            "object_lock_mode": "GOVERNANCE",
            "object_lock_duration": 7,
        },
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["status"] == "confirmed"
    assert operations[-1]["path"] == "/s3/buckets/test-advanced/advanced-create"
    assert ("create_bucket", {"Bucket": "test-advanced", "ObjectLockEnabledForBucket": True}) in s3.calls
    assert ("put_bucket_versioning", {"Bucket": "test-advanced", "VersioningConfiguration": {"Status": "Enabled"}}) in s3.calls
    assert ("put_object_lock_configuration", {"Bucket": "test-advanced", "ObjectLockConfiguration": lock_config}) in s3.calls
    assert admin.responses["/api/s3/buckets/test-advanced"]["bucket"]["quota"] == 1073741824
    assert admin.responses["/api/s3/buckets/test-advanced"]["bucket"]["owner"] == "alice"
    assert not any(call == ("POST", "/api/s3/buckets", {"name": "test-advanced", "region": "us-east-1"}) for call in admin.calls)

    advanced_zero_quota = client.post("/api/v1/management/mgmt_1/buckets", json={"name": "photos", "region": "us-east-1", "quota_size": 0, "quota_unit": "B", "quota_enabled": True})
    assert advanced_zero_quota.status_code == 422
    assert advanced_zero_quota.json()["error"]["field"] == "quota_size"

    advanced_overflow = client.post("/api/v1/management/mgmt_1/buckets", json={"name": "photos", "region": "us-east-1", "quota_size": 9223372036854775808, "quota_unit": "B", "quota_enabled": True})
    assert advanced_overflow.status_code == 422
    assert advanced_overflow.json()["error"]["field"] == "quota_size"


def test_advanced_s3_create_reports_partial_without_retry_or_delete(api):
    client, _admin, s3, operations = api
    s3.buckets.discard("test-partial")
    s3.fail_methods.add("put_object_lock_configuration")

    response = client.post(
        "/api/v1/management/mgmt_1/buckets",
        json={
            "name": "test-partial",
            "region": "us-east-1",
            "object_lock_enabled": True,
            "set_default_retention": True,
            "object_lock_mode": "GOVERNANCE",
            "object_lock_duration": 7,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert operations[-1]["confirmed"] is False
    assert operations[-1]["path"] == "/s3/buckets/test-partial/advanced-create"
    assert s3.buckets.issuperset({"test-partial"})
    assert [name for name, kwargs in s3.calls if name == "create_bucket" and kwargs["Bucket"] == "test-partial"] == ["create_bucket"]
    assert not any(name == "delete_bucket" and kwargs.get("Bucket") == "test-partial" for name, kwargs in s3.calls)


def test_real_management_ops_accepts_custom_s3_invokes_and_records_ledger(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    db.initialize(settings)
    permissions = {"bucket.manage": True, "iam.manage": False, "bucket_write_prefixes": ["photos"]}
    with db.connect(settings) as conn:
        management.initialize(conn)
        management_ops.initialize(conn)
        conn.execute(
            """
            INSERT INTO management_connections(
              id,name,admin_url,admin_secret_ref,s3_connection_id,protocol_baseline,
              endpoints_json,management_write_enabled,permissions_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mgmt_real",
                "Admin",
                "http://admin.local:23646",
                "server-admin",
                None,
                "4.48",
                "{}",
                1,
                json.dumps(permissions, sort_keys=True),
                "2026-10-04T00:00:00Z",
                "2026-10-04T00:00:00Z",
            ),
        )
        conn.commit()

    admin = FakeAdmin()
    s3 = FakeS3()
    mgmt = {
        "id": "mgmt_real",
        "management_write_enabled": True,
        "permissions": permissions,
    }

    monkeypatch.setattr(management, "require_management", lambda _request, management_id, write=False: dict(mgmt))
    monkeypatch.setattr(management, "admin_client", lambda _settings, _conn, _mgmt: admin)
    monkeypatch.setattr(management, "associated_s3_client", lambda _settings, _mgmt: s3, raising=False)
    monkeypatch.setattr(management_resources, "mutation", lambda _request: SimpleNamespace(id="admin"))

    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management_resources.router)

    with TestClient(app) as client:
        s3.buckets.discard("photos-new")
        created = client.post(
            "/api/v1/management/mgmt_real/buckets",
            json={"name": "photos-new", "region": "us-east-1"},
            headers={"Idempotency-Key": "create-1"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "confirmed"

        versioning = client.put(
            "/api/v1/management/mgmt_real/buckets/photos/versioning",
            json={"status": "Enabled"},
            headers={"Idempotency-Key": "versioning-1"},
        )
        assert versioning.status_code == 200, versioning.text
        assert versioning.json()["status"] == "confirmed"

        s3.buckets.discard("photos-advanced")
        advanced = client.post(
            "/api/v1/management/mgmt_real/buckets",
            json={
                "name": "photos-advanced",
                "region": "us-east-1",
                "quota_size": 1,
                "quota_unit": "GB",
                "quota_enabled": True,
                "owner": "alice",
                "object_lock_enabled": True,
                "set_default_retention": True,
                "object_lock_mode": "GOVERNANCE",
                "object_lock_duration": 7,
            },
            headers={"Idempotency-Key": "advanced-1"},
        )
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["status"] == "confirmed"

        s3.buckets.discard("photos-partial")
        s3.fail_methods.add("put_object_lock_configuration")
        partial = client.post(
            "/api/v1/management/mgmt_real/buckets",
            json={
                "name": "photos-partial",
                "region": "us-east-1",
                "object_lock_enabled": True,
                "set_default_retention": True,
                "object_lock_mode": "GOVERNANCE",
                "object_lock_duration": 7,
            },
            headers={"Idempotency-Key": "advanced-partial-1"},
        )
        assert partial.status_code == 200, partial.text
        assert partial.json()["status"] == "needs_review"
        s3.fail_methods.clear()

        deletion = client.delete(
            "/api/v1/management/mgmt_real/buckets/photos",
            headers={"Idempotency-Key": "delete-1"},
        )
        assert deletion.status_code == 200, deletion.text
        assert deletion.json()["status"] == "confirmed"

    assert ("create_bucket", {"Bucket": "photos-new"}) in s3.calls
    assert ("put_bucket_versioning", {"Bucket": "photos", "VersioningConfiguration": {"Status": "Enabled"}}) in s3.calls
    assert ("create_bucket", {"Bucket": "photos-advanced", "ObjectLockEnabledForBucket": True}) in s3.calls
    assert ("put_bucket_versioning", {"Bucket": "photos-advanced", "VersioningConfiguration": {"Status": "Enabled"}}) in s3.calls
    assert any(name == "create_bucket" and kwargs.get("Bucket") == "photos-partial" for name, kwargs in s3.calls)
    assert not any(name == "delete_bucket" and kwargs.get("Bucket") == "photos-partial" for name, kwargs in s3.calls)
    assert ("delete_bucket", {"Bucket": "photos"}) in s3.calls
    with db.connect(settings) as conn:
        rows = conn.execute("SELECT method,path,state FROM management_operations ORDER BY created_at").fetchall()
    assert [(row["method"], row["path"], row["state"]) for row in rows] == [
        ("POST", "/s3/buckets/photos-new", "confirmed"),
        ("PUT", "/s3/buckets/photos/versioning", "confirmed"),
        ("POST", "/s3/buckets/photos-advanced/advanced-create", "confirmed"),
        ("POST", "/s3/buckets/photos-partial/advanced-create", "needs_review"),
        ("DELETE", "/s3/buckets/photos", "confirmed"),
    ]


def test_real_management_ops_marks_ignored_admin_write_needs_review(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    db.initialize(settings)
    permissions = {"bucket.manage": True, "iam.manage": True, "bucket_write_prefixes": ["photos"]}
    with db.connect(settings) as conn:
        management.initialize(conn)
        management_ops.initialize(conn)
        conn.execute(
            """
            INSERT INTO management_connections(
              id,name,admin_url,admin_secret_ref,s3_connection_id,protocol_baseline,
              endpoints_json,management_write_enabled,permissions_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mgmt_real",
                "Admin",
                "http://admin.local:23646",
                "server-admin",
                None,
                "4.48",
                "{}",
                1,
                json.dumps(permissions, sort_keys=True),
                "2026-10-04T00:00:00Z",
                "2026-10-04T00:00:00Z",
            ),
        )
        conn.commit()

    admin = FakeAdmin()
    admin.ignore_writes = True
    mgmt = {
        "id": "mgmt_real",
        "management_write_enabled": True,
        "permissions": permissions,
    }

    monkeypatch.setattr(management, "require_management", lambda _request, management_id, write=False: dict(mgmt))
    monkeypatch.setattr(management, "admin_client", lambda _settings, _conn, _mgmt: admin)
    monkeypatch.setattr(management_resources, "mutation", lambda _request: SimpleNamespace(id="admin"))

    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management_resources.router)

    with TestClient(app) as client:
        quota = client.put(
            "/api/v1/management/mgmt_real/buckets/photos/quota",
            json={"quota_size": 10, "quota_unit": "GB", "quota_enabled": True},
            headers={"Idempotency-Key": "quota-ignored"},
        )
        assert quota.status_code == 200, quota.text
        assert quota.json()["status"] == "needs_review"
        assert quota.json()["result"]["confirmed"] is False

        lifecycle = client.put(
            "/api/v1/management/mgmt_real/buckets/photos/lifecycle",
            json={"lifecycle": {"rules": [{"id": "expire", "status": "Enabled", "expiration_days": 30}]}},
            headers={"Idempotency-Key": "lifecycle-ignored"},
        )
        assert lifecycle.status_code == 200, lifecycle.text
        assert lifecycle.json()["status"] == "needs_review"

        key_status = client.put(
            "/api/v1/management/mgmt_real/iam/users/alice/access-keys/AKIA/status",
            json={"status": "Inactive"},
            headers={"Idempotency-Key": "key-status-ignored"},
        )
        assert key_status.status_code == 200, key_status.text
        assert key_status.json()["status"] == "needs_review"

        user_policies = client.put(
            "/api/v1/management/mgmt_real/iam/users/alice/policies",
            json={"actions": []},
            headers={"Idempotency-Key": "user-policies-ignored"},
        )
        assert user_policies.status_code == 200, user_policies.text
        assert user_policies.json()["status"] == "needs_review"

        group_status = client.put(
            "/api/v1/management/mgmt_real/iam/groups/team/status",
            json={"status": "disabled"},
            headers={"Idempotency-Key": "group-status-ignored"},
        )
        assert group_status.status_code == 200, group_status.text
        assert group_status.json()["status"] == "needs_review"

        group_member = client.post(
            "/api/v1/management/mgmt_real/iam/groups/team/members",
            json={"username": "bob"},
            headers={"Idempotency-Key": "group-member-ignored"},
        )
        assert group_member.status_code == 200, group_member.text
        assert group_member.json()["status"] == "needs_review"

    with db.connect(settings) as conn:
        rows = conn.execute("SELECT path,state FROM management_operations ORDER BY created_at").fetchall()
    assert [(row["path"], row["state"]) for row in rows] == [
        ("/api/s3/buckets/photos/quota", "needs_review"),
        ("/api/s3/buckets/photos/lifecycle", "needs_review"),
        ("/api/users/alice/access-keys/AKIA/status", "needs_review"),
        ("/api/users/alice/policies", "needs_review"),
        ("/api/groups/team/status", "needs_review"),
        ("/api/groups/team/members", "needs_review"),
    ]


def test_http_admin_iam_crud_lifecycle_uses_official_wire_and_real_ledger(tmp_path: Path, monkeypatch):
    admin = HttpIamAdmin()
    settings, client = make_http_management_app(tmp_path, monkeypatch, admin)

    with client:
        principals = client.get("/api/v1/management/mgmt_http/iam/principals")
        assert principals.status_code == 200, principals.text
        assert principals.json()["principals"] == ["arn:aws:iam::user/alice", "arn:aws:iam::role/admin"]
        assert "secret_access_key" not in principals.text

        created = client.post(
            "/api/v1/management/mgmt_http/iam/users",
            json={"username": "bob", "email": "b@example.com", "actions": ["Read"], "generate_key": True, "policy_names": ["reader"]},
            headers={"Idempotency-Key": "user-create"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "confirmed"
        assert created.json()["created_credentials"] == {"access_key": "AK1", "secret_key": "secret-1"}

        replay = client.post(
            "/api/v1/management/mgmt_http/iam/users",
            json={"username": "bob", "email": "b@example.com", "actions": ["Read"], "generate_key": True, "policy_names": ["reader"]},
            headers={"Idempotency-Key": "user-create"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["secret_receipt_available"] is False
        assert "created_credentials" not in replay.json()

        updated = client.put("/api/v1/management/mgmt_http/iam/users/bob", json={"email": "new@example.com", "actions": ["Read", "Write"], "policy_names": []}, headers={"Idempotency-Key": "user-update"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "confirmed"

        key = client.post("/api/v1/management/mgmt_http/iam/users/bob/access-keys", json={}, headers={"Idempotency-Key": "key-create"})
        assert key.status_code == 200, key.text
        assert key.json()["created_credentials"] == {"access_key": "AK2", "secret_key": "secret-2"}
        access_key = key.json()["created_credentials"]["access_key"]

        key_status = client.put(f"/api/v1/management/mgmt_http/iam/users/bob/access-keys/{access_key}/status", json={"status": "Inactive"}, headers={"Idempotency-Key": "key-status"})
        assert key_status.status_code == 200, key_status.text
        assert key_status.json()["status"] == "confirmed"

        key_delete = client.delete(f"/api/v1/management/mgmt_http/iam/users/bob/access-keys/{access_key}", headers={"Idempotency-Key": "key-delete"})
        assert key_delete.status_code == 200, key_delete.text
        assert key_delete.json()["status"] == "confirmed"

        user_policies = client.put("/api/v1/management/mgmt_http/iam/users/bob/policies", json={"actions": []}, headers={"Idempotency-Key": "user-policies"})
        assert user_policies.status_code == 200, user_policies.text
        assert user_policies.json()["status"] == "confirmed"

        policy_doc = {"Version": "2012-10-17", "Statement": [{"Action": ["s3:GetObject"], "Effect": "Allow", "Resource": ["*"]}]}
        policy_create = client.post("/api/v1/management/mgmt_http/iam/policies", json={"name": "reader", "document": policy_doc}, headers={"Idempotency-Key": "policy-create"})
        assert policy_create.status_code == 200, policy_create.text
        assert policy_create.json()["status"] == "confirmed"

        policy_update_doc = {"Version": "2012-10-17", "Statement": [{"Action": ["s3:*"], "Effect": "Allow", "Resource": ["*"]}]}
        policy_update = client.put("/api/v1/management/mgmt_http/iam/policies/reader", json={"document": policy_update_doc}, headers={"Idempotency-Key": "policy-update"})
        assert policy_update.status_code == 200, policy_update.text
        assert policy_update.json()["status"] == "confirmed"

        group_create = client.post("/api/v1/management/mgmt_http/iam/groups", json={"name": "qa"}, headers={"Idempotency-Key": "group-create"})
        assert group_create.status_code == 200, group_create.text
        assert group_create.json()["status"] == "confirmed"

        group_status = client.put("/api/v1/management/mgmt_http/iam/groups/qa/status", json={"status": "disabled"}, headers={"Idempotency-Key": "group-status"})
        assert group_status.status_code == 200, group_status.text
        assert group_status.json()["status"] == "confirmed"

        member_add = client.post("/api/v1/management/mgmt_http/iam/groups/qa/members", json={"username": "bob"}, headers={"Idempotency-Key": "member-add"})
        assert member_add.status_code == 200, member_add.text
        assert member_add.json()["status"] == "confirmed"

        group_policy = client.post("/api/v1/management/mgmt_http/iam/groups/qa/policies", json={"policy_name": "reader"}, headers={"Idempotency-Key": "group-policy-add"})
        assert group_policy.status_code == 200, group_policy.text
        assert group_policy.json()["status"] == "confirmed"

        member_remove = client.delete("/api/v1/management/mgmt_http/iam/groups/qa/members/bob", headers={"Idempotency-Key": "member-remove"})
        assert member_remove.status_code == 200, member_remove.text
        assert member_remove.json()["status"] == "confirmed"

        group_policy_remove = client.delete("/api/v1/management/mgmt_http/iam/groups/qa/policies/reader", headers={"Idempotency-Key": "group-policy-remove"})
        assert group_policy_remove.status_code == 200, group_policy_remove.text
        assert group_policy_remove.json()["status"] == "confirmed"

        service = client.post("/api/v1/management/mgmt_http/iam/service-accounts", json={"parent_user": "bob", "description": "temp", "expiration": "2027-01-01T00:00:00Z"}, headers={"Idempotency-Key": "service-create"})
        assert service.status_code == 200, service.text
        assert service.json()["status"] == "confirmed"
        assert service.json()["created_credentials"] == {"access_key": "SAK1", "secret_key": "sa-secret-1"}

        service_update = client.put("/api/v1/management/mgmt_http/iam/service-accounts/sa-1", json={"status": "Inactive", "description": "", "expiration": ""}, headers={"Idempotency-Key": "service-update"})
        assert service_update.status_code == 200, service_update.text
        assert service_update.json()["status"] == "confirmed"

        service_delete = client.delete("/api/v1/management/mgmt_http/iam/service-accounts/sa-1", headers={"Idempotency-Key": "service-delete"})
        assert service_delete.status_code == 200, service_delete.text
        assert service_delete.json()["status"] == "confirmed"

        group_delete = client.delete("/api/v1/management/mgmt_http/iam/groups/qa", headers={"Idempotency-Key": "group-delete"})
        assert group_delete.status_code == 200, group_delete.text
        assert group_delete.json()["status"] == "confirmed"

        policy_delete = client.delete("/api/v1/management/mgmt_http/iam/policies/reader", headers={"Idempotency-Key": "policy-delete"})
        assert policy_delete.status_code == 200, policy_delete.text
        assert policy_delete.json()["status"] == "confirmed"

        user_delete = client.delete("/api/v1/management/mgmt_http/iam/users/bob", headers={"Idempotency-Key": "user-delete"})
        assert user_delete.status_code == 200, user_delete.text
        assert user_delete.json()["status"] == "confirmed"

    assert ("POST", "/api/users", {"username": "bob", "email": "b@example.com", "actions": ["Read"], "generate_key": True, "policy_names": ["reader"]}) in admin.requests
    assert ("GET", "/api/principals", None) in admin.requests
    assert ("PUT", "/api/groups/qa/status", {"enabled": False}) in admin.requests
    assert ("POST", "/api/groups/qa/policies", {"policy_name": "reader"}) in admin.requests
    assert ("PUT", "/api/service-accounts/sa-1", {"status": "Inactive", "description": "", "expiration": ""}) in admin.requests
    with db.connect(settings) as conn:
        states = [row["state"] for row in conn.execute("SELECT state FROM management_operations ORDER BY created_at").fetchall()]
    assert states == ["confirmed"] * 20


def test_http_admin_iam_ignored_write_and_scope_revocation_are_not_confirmed(tmp_path: Path, monkeypatch):
    admin = HttpIamAdmin()
    settings, client = make_http_management_app(tmp_path, monkeypatch, admin)

    with client:
        admin.ignore_writes = True
        ignored = client.put("/api/v1/management/mgmt_http/iam/groups/team/status", json={"status": "disabled"}, headers={"Idempotency-Key": "ignored-group-status"})
        assert ignored.status_code == 200, ignored.text
        assert ignored.json()["status"] == "needs_review"
        admin.ignore_writes = False

        with db.connect(settings) as conn:
            conn.execute(
                "UPDATE management_connections SET permissions_json=? WHERE id=?",
                (json.dumps({"bucket.manage": False, "iam.manage": False, "bucket_write_prefixes": ["swc-management-"]}, sort_keys=True), "mgmt_http"),
            )
            conn.commit()
        revoked = client.post("/api/v1/management/mgmt_http/iam/groups", json={"name": "blocked"}, headers={"Idempotency-Key": "revoked"})
        assert revoked.status_code == 403
        assert revoked.json()["error"]["code"] == "MANAGEMENT_WRITE_DISABLED"

    with db.connect(settings) as conn:
        rows = conn.execute("SELECT path,state FROM management_operations ORDER BY created_at").fetchall()
    assert [(row["path"], row["state"]) for row in rows] == [("/api/groups/team/status", "needs_review")]
