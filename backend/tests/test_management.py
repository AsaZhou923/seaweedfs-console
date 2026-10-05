from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from console import core, db, management, management_resources
from console.config import Settings
from console.security import AppError


def make_settings(tmp_path: Path) -> Settings:
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(
        json.dumps(
            {
                "server-admin": {
                    "kind": "seaweed_admin",
                    "username": "admin",
                    "password": "admin-pass",
                    "allowed_endpoint_url": "http://admin.local:23646",
                    "allowed_endpoints": {
                        "master": "http://master.local:9333",
                        "filer": "http://filer.local:8888",
                        "volume": "http://volume.local:9340",
                    },
                },
                "other-admin": {
                    "kind": "seaweed_admin",
                    "username": "admin",
                    "password": "admin-pass",
                    "allowed_endpoint_url": "http://other.local:23646",
                    "allowed_endpoints": {},
                },
                "prefixed-admin": {
                    "kind": "seaweed_admin",
                    "username": "admin",
                    "password": "admin-pass",
                    "allowed_endpoint_url": "http://admin.local:23646/proxy",
                    "allowed_endpoints": {"volume": "http://volume.local:8080/api"},
                },
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


def app_with_management(settings: Settings) -> FastAPI:
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)
        management.initialize(conn)
    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management.router)
    app.include_router(management_resources.router)
    return app


def login_cookie(settings: Settings) -> tuple[str, str]:
    with db.connect(settings) as conn:
        result = core.login(conn, settings, "admin", "console-pass")
    return result["session_token"], result["csrf_token"]


class AdminMock:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.mode = "ok"
        self.native_mode = "ok"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in {"master.local", "filer.local", "volume.local"}:
            return self._native(request)
        path = request.url.path
        if path.startswith("/proxy/"):
            path = path[len("/proxy") :]
        if path == "/login" and request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "set-cookie": "sid=login; Path=/"},
                text='<form><input type="hidden" name="csrf_token" value="login-csrf"></form>',
            )
        if path == "/login" and request.method == "POST":
            form = parse_qs(request.content.decode())
            assert form["username"] == ["admin"]
            assert form["password"] == ["admin-pass"]
            assert form["csrf_token"] == ["login-csrf"]
            assert request.headers["origin"] == "http://admin.local:23646"
            return httpx.Response(303, headers={"location": "/admin", "set-cookie": "sid=admin; Path=/"})
        if path == "/admin" and request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<meta name="csrf-token" content="rotated-csrf">')
        if self.mode == "timeout":
            raise httpx.TimeoutException("slow", request=request)
        if self.mode == "large":
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"data":"' + b"x" * (8 * 1024 * 1024 + 1) + b'"}')
        if self.mode == "forbidden":
            return httpx.Response(403, headers={"content-type": "application/json"}, json={"error": "no"})
        if self.mode == "html":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>stub</html>")
        if self.mode == "unsupported":
            return httpx.Response(501, headers={"content-type": "application/json"}, json={"error": "unsupported"})
        if path == "/api/admin":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "username": "admin",
                    "total_volumes": 3,
                    "master_nodes": [{"address": "127.0.0.1:9333", "is_leader": True}],
                    "volume_servers": [{"id": "node-a", "address": "127.0.0.1:9340", "volumes": 3}],
                    "filer_nodes": [{"address": "127.0.0.1:8888"}],
                    "s3_nodes": [{"address": "127.0.0.1:18333"}],
                    "message_brokers": None,
                    "total_mount_clients": 0,
                    "signed": "http://admin.local/file?X-Amz-Signature=abc&AWSAccessKeyId=public-id",
                    "credentials": [{"accessKey": "keep-id", "secretKey": "hide-me"}],
                },
            )
        if path == "/api/config":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"protocol": "4.48", "password": "hide"})
        if path == "/api/cluster/topology":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"datacenters": [{"id": "dc1", "racks": [{"id": "rack1", "nodes": [{"id": "node-a", "volumes": 3}]}]}]},
            )
        if path == "/api/cluster/masters":
            return httpx.Response(200, headers={"content-type": "application/json"}, json=[{"address": "127.0.0.1:9333"}])
        if path == "/api/cluster/volumes":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"volume_servers": [{"id": "node-a", "ec_shard_details": [{"volume_id": 7, "shard_id": 1}]}]})
        if path == "/api/plugin/status":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"enabled": True, "worker_count": 1})
        if path == "/api/s3/buckets":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"buckets": []})
        if path == "/api/object-store/policies/validate" and request.method == "POST":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"valid": True})
        if path in {"/api/s3/buckets/photos/lifecycle", "/api/s3/buckets/photos/policy"}:
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"bucket": "photos"})
        if request.method == "GET" and (path.startswith("/api/plugin/") or path.startswith("/api/mq/") or path.startswith("/api/s3tables/")):
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"path": path})
        if path in {
            "/api/plugin/job-types/example/schema",
            "/api/mq/topics/create",
            "/api/mq/topics/retention/update",
            "/api/mq/retention/purge",
            "/api/s3tables/buckets",
            "/api/s3tables/namespaces",
            "/api/s3tables/tables",
            "/api/s3tables/bucket-policy",
            "/api/s3tables/table-policy",
            "/api/s3tables/tags",
        } and request.method in {"POST", "PUT", "DELETE"}:
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"path": path, "method": request.method})
        if path == "/api/volumes/export":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "generated_at": "2026-10-04T00:00:00Z",
                    "totals": {"volume_count": 3},
                    "data_centers": [
                        {
                            "id": "dc1",
                            "racks": [
                                {
                                    "id": "rack1",
                                    "nodes": [
                                        {
                                            "id": "node-a",
                                            "disks": [
                                                {
                                                    "type": "hdd",
                                                    "volumes": [
                                                        {"id": 1, "collection": "photos", "size": 10, "read_only": False},
                                                        {"id": 1, "collection": "photos", "size": 10, "read_only": False},
                                                        {"id": 2, "collection": "default", "size": 20, "read_only": True},
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": "missing"})

    def _native(self, request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers
        if self.native_mode == "timeout":
            raise httpx.TimeoutException("slow", request=request)
        if self.native_mode == "html":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>not-json</html>")
        if request.url.host == "master.local" and request.url.path == "/dir/status":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"Version": "4.48", "Leader": "m1"})
        if request.url.host == "master.local" and request.url.path == "/cluster/status":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"IsLeader": True, "Peers": []})
        if request.url.host == "filer.local" and request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"Path": "/", "Entries": []})
        if request.url.host == "volume.local" and request.url.path == "/status":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"Version": "4.48", "Volumes": 0})
        return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": request.url.path})


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    admin = AdminMock()
    monkeypatch.setattr(management, "_TEST_TRANSPORT", admin.transport())
    app = app_with_management(settings)
    token, csrf = login_cookie(settings)
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, token)
        yield client, settings, csrf, admin
    monkeypatch.setattr(management, "_TEST_TRANSPORT", None)


def create_connection(client: TestClient, csrf: str, secret_ref: str = "server-admin") -> dict:
    response = client.post(
        "/api/v1/management/connections",
        json={"name": "Admin", "admin_url": "http://admin.local:23646", "admin_secret_ref": secret_ref},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_connection_body(client: TestClient, csrf: str, body: dict) -> TestClient:
    return client.post(
        "/api/v1/management/connections",
        json=body,
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )


def test_login_csrf_rotates_and_public_redaction(api):
    client, _settings, csrf, admin = api
    conn = create_connection(client, csrf)

    response = client.get(f"/api/v1/management/{conn['id']}/overview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admin"]["credentials"][0]["accessKey"] == "keep-id"
    assert body["admin"]["credentials"][0]["secretKey"] == "[REDACTED]"
    assert "abc" not in json.dumps(body)
    assert body["config"]["password"] == "[REDACTED]"
    paths = [(req.method, req.url.path) for req in admin.requests]
    assert paths[:4] == [("GET", "/login"), ("POST", "/login"), ("GET", "/admin"), ("GET", "/api/admin")]


def test_bound_endpoint_mismatch_is_denied(api):
    client, _settings, csrf, _admin = api

    response = client.post(
        "/api/v1/management/connections",
        json={"name": "Admin", "admin_url": "http://admin.local:23646", "admin_secret_ref": "other-admin"},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_ENDPOINT_DENIED"


def test_services_volumes_collections_and_ec_projection(api):
    client, settings, csrf, _admin = api
    conn = create_connection(client, csrf)

    services = client.get(f"/api/v1/management/{conn['id']}/services").json()
    assert services["master_nodes"][0]["address"] == "127.0.0.1:9333"
    assert services["mq"]["state"] == "unknown"
    assert services["mount_clients"] == {"state": "reported", "count": 0}
    assert services["plugin"]["data"]["worker_count"] == 1

    volumes = client.get(f"/api/v1/management/{conn['id']}/volumes", params={"limit": 2}).json()
    assert len(volumes["items"]) == 2
    assert volumes["counts"] == {"physical_replicas": 3, "logical_volumes": 2}
    assert volumes["next_cursor"]
    next_page = client.get(f"/api/v1/management/{conn['id']}/volumes", params={"cursor": volumes["next_cursor"], "limit": 2}).json()
    assert [item["id"] for item in next_page["items"]] == [2]

    bad_cursor = client.get(f"/api/v1/management/{conn['id']}/volumes", params={"cursor": "not-a-cursor"})
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["error"]["code"] == "INVALID_CURSOR"

    mismatched_limit_cursor = management.sign_json(
        {
            "management_id": conn["id"],
            "filter": {"collection": None, "readonly": None, "disk_type": None},
            "limit": 1,
            "offset": 1,
            "source_signature": volumes["next_cursor"] and management.unsign_json(volumes["next_cursor"], settings.require_cursor_key(), "mgmt-volumes:v1")["source_signature"],
            "expires_at": management.utc_add(30 * 60),
        },
        settings.require_cursor_key(),
        "mgmt-volumes:v1",
    )
    mismatched_limit = client.get(f"/api/v1/management/{conn['id']}/volumes", params={"cursor": mismatched_limit_cursor, "limit": 2})
    assert mismatched_limit.status_code == 400
    assert mismatched_limit.json()["error"]["code"] == "INVALID_CURSOR"

    changed_source_cursor = management.sign_json(
        {
            "management_id": conn["id"],
            "filter": {"collection": None, "readonly": None, "disk_type": None},
            "limit": 2,
            "offset": 2,
            "source_signature": {"row_count": 0, "sha256": "changed"},
            "expires_at": management.utc_add(30 * 60),
        },
        settings.require_cursor_key(),
        "mgmt-volumes:v1",
    )
    changed_source = client.get(f"/api/v1/management/{conn['id']}/volumes", params={"cursor": changed_source_cursor, "limit": 2})
    assert changed_source.status_code == 400
    assert changed_source.json()["error"]["code"] == "INVALID_CURSOR"

    collections = client.get(f"/api/v1/management/{conn['id']}/collections").json()
    photos = next(item for item in collections["items"] if item["name"] == "photos")
    assert photos["physical_replicas"] == 2
    assert photos["logical_volumes"] == 1

    ec = client.get(f"/api/v1/management/{conn['id']}/ec").json()
    assert ec["items"][0]["volume_id"] == 7
    assert ec["counts"]["ec_shards"] == 1


def test_volume_source_signature_ignores_export_timestamp():
    export_a = {
        "generated_at": "2026-10-04T00:00:00Z",
        "data_centers": [{"id": "dc1", "racks": [{"id": "rack1", "nodes": [{"id": "node-a", "disks": [{"type": "hdd", "volumes": [{"id": 1, "collection": "photos", "size": 10}]}]}]}]}],
    }
    export_b = {
        "generated_at": "2026-10-04T00:00:01Z",
        "data_centers": [{"id": "dc1", "racks": [{"id": "rack1", "nodes": [{"id": "node-a", "disks": [{"type": "hdd", "volumes": [{"id": 1, "collection": "photos", "size": 10}]}]}]}]}],
    }
    export_c = {
        "generated_at": "2026-10-04T00:00:02Z",
        "data_centers": [{"id": "dc1", "racks": [{"id": "rack1", "nodes": [{"id": "node-a", "disks": [{"type": "hdd", "volumes": [{"id": 2, "collection": "photos", "size": 10}]}]}]}]}],
    }

    assert management._source_signature(management._flatten_volumes(export_a)) == management._source_signature(management._flatten_volumes(export_b))
    assert management._source_signature(management._flatten_volumes(export_a)) != management._source_signature(management._flatten_volumes(export_c))


def test_unknown_or_stub_schema_reports_status_without_fake_zero(api):
    client, _settings, csrf, admin = api
    conn = create_connection(client, csrf)
    admin.mode = "html"

    response = client.get(f"/api/v1/management/{conn['id']}/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["modules"]["admin"] == "stub"
    assert body["admin"] is None


def test_admin_error_mapping(api):
    client, settings, csrf, admin = api
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        row = management.get_management(raw_conn, conn["id"])
        admin.mode = "forbidden"
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "/api/admin")
        assert exc.value.code == "ADMIN_FORBIDDEN"
        admin.mode = "unsupported"
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "/api/admin")
        assert exc.value.code == "ADMIN_ENDPOINT_UNSUPPORTED"


def test_timeout_and_endpoint_allowlist(api):
    _client, settings, csrf, admin = api
    client = _client
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        row = management.get_management(raw_conn, conn["id"])
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "http://evil.local/api/admin")
        assert exc.value.code == "ADMIN_ENDPOINT_DENIED"
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "/api/service-accounts?parent_user=admin")
        assert exc.value.code == "ADMIN_ENDPOINT_DENIED"
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "/api/not-allowed")
        assert exc.value.code == "ADMIN_ENDPOINT_DENIED"
        admin.mode = "timeout"
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "/api/admin")
        assert exc.value.code == "ADMIN_TIMEOUT"
        admin.mode = "large"
        with management.admin_client(settings, raw_conn, row) as upstream:
            with pytest.raises(AppError) as exc:
                upstream.request_json("GET", "/api/admin")
        assert exc.value.code == "ADMIN_RESPONSE_TOO_LARGE"


def test_allowlisted_resource_paths_and_redaction(api):
    client, settings, csrf, _admin = api
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        row = management.get_management(raw_conn, conn["id"])
        with management.admin_client(settings, raw_conn, row) as upstream:
            buckets = upstream.request_json("GET", "/api/s3/buckets")
            assert buckets == {"buckets": []}
            assert upstream.request_json("GET", "/api/s3/buckets/photos/lifecycle") == {"bucket": "photos"}
            assert upstream.request_json("GET", "/api/s3/buckets/photos/policy") == {"bucket": "photos"}
            validation = upstream.request_json("POST", "/api/object-store/policies/validate", {"document": {}})
            assert validation == {"valid": True}

    redacted = management.redact(
        {
            "credentials": [{"access_key_id": "keep-id", "secretKey": "hide"}],
            "condition": {"s3:signatureAge": 600},
        }
    )
    assert redacted["credentials"][0]["access_key_id"] == "keep-id"
    assert redacted["credentials"][0]["secretKey"] == "[REDACTED]"
    assert redacted["condition"]["s3:signatureAge"] == 600


def test_native_read_allowlist_and_count_semantics(api):
    client, settings, csrf, _admin = api
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        row = management.get_management(raw_conn, conn["id"])
        with management.admin_client(settings, raw_conn, row) as upstream:
            allowed = [
                "/api/plugin/schemas",
                "/api/plugin/job-types/example/config",
                "/api/plugin/config/example",
                "/api/plugin/runs/example",
                "/api/plugin/observations/example",
                "/api/plugin/capabilities/example",
                "/api/mq/topics/default/events",
                "/api/s3tables/buckets",
                "/api/s3tables/namespaces",
                "/api/s3tables/tables",
                "/api/s3tables/bucket-policy",
                "/api/s3tables/table-policy",
                "/api/s3tables/tags",
            ]
            for path in allowed:
                assert upstream.request_json("GET", path)["path"] == path

            with pytest.raises(AppError) as denied:
                upstream.request_json("GET", "/api/plugin/arbitrary/proxy/path")
            assert denied.value.code == "ADMIN_ENDPOINT_DENIED"

    assert management._reported_count(0) == {"state": "reported", "count": 0}
    assert management._reported_count(False) == {"state": "reported", "count": None, "data": False}


def test_native_write_allowlist_for_mq_and_s3tables(api):
    client, settings, csrf, _admin = api
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        row = management.get_management(raw_conn, conn["id"])
        with management.admin_client(settings, raw_conn, row) as upstream:
            cases = [
                ("POST", "/api/plugin/job-types/example/schema", {"force_refresh": False}),
                ("POST", "/api/mq/topics/create", {"namespace": "default", "name": "events"}),
                ("POST", "/api/mq/topics/retention/update", {"namespace": "default", "name": "events"}),
                ("POST", "/api/mq/retention/purge", {}),
                ("POST", "/api/s3tables/buckets", {"name": "bucket"}),
                ("DELETE", "/api/s3tables/buckets", None),
                ("POST", "/api/s3tables/namespaces", {"name": "ns"}),
                ("DELETE", "/api/s3tables/namespaces", None),
                ("POST", "/api/s3tables/tables", {"name": "tbl"}),
                ("DELETE", "/api/s3tables/tables", None),
                ("PUT", "/api/s3tables/bucket-policy", {"policy": "{}"}),
                ("DELETE", "/api/s3tables/bucket-policy", None),
                ("PUT", "/api/s3tables/table-policy", {"policy": "{}"}),
                ("DELETE", "/api/s3tables/table-policy", None),
                ("PUT", "/api/s3tables/tags", {"tags": {"k": "v"}}),
                ("DELETE", "/api/s3tables/tags", {"tag_keys": ["k"]}),
            ]
            for method, path, payload in cases:
                response = upstream.request_json(method, path, payload, params={"bucket": "arn"} if method == "DELETE" and path.startswith("/api/s3tables/") else None)
                assert response == {"path": path, "method": method}

            denied = [
                ("POST", "/api/mq/topics/default/events"),
                ("PUT", "/api/mq/topics/create"),
                ("PATCH", "/api/s3tables/buckets"),
                ("POST", "/api/s3tables/arbitrary"),
                ("DELETE", "/api/plugin/arbitrary"),
            ]
            for method, path in denied:
                with pytest.raises(AppError) as exc:
                    upstream.request_json(method, path, {})
                assert exc.value.code == "ADMIN_ENDPOINT_DENIED"


def test_resource_router_uses_public_management_dto_without_secret_ref(api):
    client, _settings, csrf, admin = api
    conn = create_connection(client, csrf)

    response = client.get(f"/api/v1/management/{conn['id']}/buckets")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0}
    assert [(req.method, req.url.path) for req in admin.requests[:4]] == [
        ("GET", "/login"),
        ("POST", "/login"),
        ("GET", "/admin"),
        ("GET", "/api/s3/buckets"),
    ]


def test_associated_s3_client_uses_private_management_row_and_storage_factory(api, monkeypatch):
    client, settings, csrf, _admin = api
    storage_client = object()
    captured = {}

    def fake_storage_client(connection, supplied_settings):
        captured["connection"] = connection
        captured["settings"] = supplied_settings
        return storage_client

    monkeypatch.setattr(management.storage, "client", fake_storage_client)
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        raw_conn.execute(
            """
            INSERT INTO storage_connections(id,display_name,endpoint_url,region,secret_ref,addressing_style,verify_tls,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "con_s3",
                "S3",
                "http://s3.local:8333",
                "us-east-1",
                "server-s3",
                "path",
                0,
                "2026-10-04T00:00:00Z",
                "2026-10-04T00:00:00Z",
            ),
        )
        raw_conn.execute("UPDATE management_connections SET s3_connection_id=? WHERE id=?", ("con_s3", conn["id"]))

    public_dto = client.get("/api/v1/management/connections").json()["items"][0]
    assert "admin_secret_ref" not in public_dto

    result = management.associated_s3_client(settings, public_dto)

    assert result is storage_client
    assert captured["settings"] is settings
    assert captured["connection"]["id"] == "con_s3"
    assert captured["connection"]["secret_ref"] == "server-s3"
    assert captured["connection"]["verify_tls"] is False


def test_associated_s3_client_requires_explicit_link(api):
    client, settings, csrf, _admin = api
    conn = create_connection(client, csrf)
    public_dto = client.get("/api/v1/management/connections").json()["items"][0]

    with pytest.raises(AppError) as exc:
        management.associated_s3_client(settings, public_dto)

    assert exc.value.code == "ASSOCIATED_S3_REQUIRED"
    assert conn["s3_connection_id"] is None


def test_services_health_probes_only_configured_registry_endpoints(api, monkeypatch):
    client, settings, csrf, admin = api
    conn = create_connection_body(
        client,
        csrf,
        {
            "name": "Admin",
            "admin_url": "http://admin.local:23646",
            "admin_secret_ref": "server-admin",
            "endpoints": {"master": "http://master.local:9333", "filer": "http://filer.local:8888", "volume": "http://volume.local:9340"},
        },
    ).json()

    response = client.get(f"/api/v1/management/{conn['id']}/services/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health_scope"] == "configured_instance_health"
    assert body["services"]["master"]["status"] == "healthy"
    assert body["services"]["master"]["endpoint"] == "http://master.local:9333"
    assert body["services"]["master"]["is_leader"] is True
    assert body["services"]["master"]["observations"][0]["version"] == {"value": "4.48", "source_field": "Version"}
    assert body["services"]["master"]["observations"][0]["fields"] == {"Leader": "m1", "Version": "4.48"}
    assert body["services"]["master"]["observations"][1]["fields"] == {"IsLeader": True}
    assert body["services"]["filer"]["endpoint"] == "http://filer.local:8888"
    assert body["services"]["filer"]["status"] == "healthy"
    assert body["services"]["volume"]["endpoint"] == "http://volume.local:9340"
    assert body["services"]["volume"]["status"] == "healthy"
    assert body["services"]["s3"]["status"] == "not_configured"
    native_hosts = [req.url.host for req in admin.requests if req.url.host in {"master.local", "filer.local", "volume.local"}]
    assert native_hosts == ["master.local", "master.local", "filer.local", "volume.local"]
    assert all("cookie" not in req.headers for req in admin.requests if req.url.host in {"master.local", "filer.local", "volume.local"})

    class FakeS3:
        def list_buckets(self):
            return {"ResponseMetadata": {"HTTPHeaders": {"server": "SeaweedFS 30GB 4.48"}}}

    monkeypatch.setattr(management.storage, "client", lambda connection, supplied_settings: FakeS3())
    with db.connect(settings) as raw_conn:
        raw_conn.execute(
            """
            INSERT INTO storage_connections(id,display_name,endpoint_url,region,secret_ref,addressing_style,verify_tls,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            ("con_health", "S3", "http://s3.local:8333", "us-east-1", "server-s3", "path", 1, "2026-10-04T00:00:00Z", "2026-10-04T00:00:00Z"),
        )
        raw_conn.execute("UPDATE management_connections SET s3_connection_id=? WHERE id=?", ("con_health", conn["id"]))
    s3_response = client.get(f"/api/v1/management/{conn['id']}/services/health")
    assert s3_response.status_code == 200
    assert s3_response.json()["services"]["s3"]["endpoint"] == "http://s3.local:8333"
    assert s3_response.json()["services"]["s3"]["version"] == "4.48"


def test_services_health_denies_unapproved_stored_endpoint(api):
    client, settings, csrf, _admin = api
    conn = create_connection_body(
        client,
        csrf,
        {"name": "Admin", "admin_url": "http://admin.local:23646", "admin_secret_ref": "server-admin", "endpoints": {"master": "http://master.local:9333"}},
    ).json()
    with db.connect(settings) as raw_conn:
        raw_conn.execute("UPDATE management_connections SET endpoints_json=? WHERE id=?", (json.dumps({"master": "http://attacker.local:9333"}), conn["id"]))

    response = client.get(f"/api/v1/management/{conn['id']}/services/health")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_ENDPOINT_DENIED"


def test_services_health_maps_bad_response_and_timeout(api):
    client, _settings, csrf, admin = api
    conn = create_connection_body(
        client,
        csrf,
        {"name": "Admin", "admin_url": "http://admin.local:23646", "admin_secret_ref": "server-admin", "endpoints": {"master": "http://master.local:9333"}},
    ).json()

    admin.native_mode = "html"
    html_response = client.get(f"/api/v1/management/{conn['id']}/services/health")
    assert html_response.status_code == 200
    assert html_response.json()["services"]["master"]["status"] == "bad_response"

    admin.native_mode = "timeout"
    timeout_response = client.get(f"/api/v1/management/{conn['id']}/services/health")
    assert timeout_response.status_code == 200
    assert timeout_response.json()["services"]["master"]["status"] == "unreachable"


def test_operation_history_route_initializes_and_lists(api):
    client, _settings, csrf, _admin = api
    conn = create_connection(client, csrf)

    response = client.get(f"/api/v1/management/{conn['id']}/operations")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}


def test_access_policy_update_is_explicit_and_audited(api):
    client, settings, csrf, _admin = api
    conn = create_connection(client, csrf)

    string_bool = client.put(
        f"/api/v1/management/connections/{conn['id']}/access-policy",
        json={"management_write_enabled": "false", "permissions": {}},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert string_bool.status_code == 422

    missing_ack = client.put(
        f"/api/v1/management/connections/{conn['id']}/access-policy",
        json={"management_write_enabled": True, "permissions": {"bucket.manage": True}},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert missing_ack.status_code == 422

    updated = client.put(
        f"/api/v1/management/connections/{conn['id']}/access-policy",
        json={
            "management_write_enabled": True,
            "acknowledge_management_write": True,
            "permissions": {"bucket.manage": True, "bucket_write_prefixes": ["swc-management-"]},
            "reason": "operator enabled bucket bootstrap",
        },
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["admin_url"] == "http://admin.local:23646"
    assert body["management_write_enabled"] is True
    assert body["permissions"]["bucket.manage"] is True
    assert body["permissions"]["iam.manage"] is False
    assert body["permissions"]["file.manage"] is False
    assert body["permissions"]["mq.manage"] is False
    assert body["permissions"]["table.manage"] is False

    listed = client.get("/api/v1/management/connections").json()["items"][0]
    assert listed["management_write_enabled"] is True
    with db.connect(settings) as raw_conn:
        events = raw_conn.execute("SELECT * FROM management_access_events WHERE management_id=?", (conn["id"],)).fetchall()
    assert len(events) == 1
    assert json.loads(events[0]["before_json"])["management_write_enabled"] is False
    assert json.loads(events[0]["after_json"])["management_write_enabled"] is True

    identity_change = client.put(
        f"/api/v1/management/connections/{conn['id']}/access-policy",
        json={
            "management_write_enabled": False,
            "admin_url": "http://evil.local:23646",
            "permissions": {},
        },
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert identity_change.status_code == 422
    assert identity_change.json()["error"]["code"] == "MANAGEMENT_IDENTITY_IMMUTABLE"

    bad_root = client.put(
        f"/api/v1/management/connections/{conn['id']}/access-policy",
        json={
            "management_write_enabled": False,
            "permissions": {"file.write_roots": ["/safe/../escape"]},
        },
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert bad_root.status_code == 422


def test_create_write_enabled_requires_literal_bool_and_ack(api):
    client, _settings, csrf, _admin = api
    response = create_connection_body(
        client,
        csrf,
        {
            "name": "Admin",
            "admin_url": "http://admin.local:23646",
            "admin_secret_ref": "server-admin",
            "management_write_enabled": "true",
            "acknowledge_management_write": True,
        },
    )
    assert response.status_code == 422

    response = create_connection_body(
        client,
        csrf,
        {
            "name": "Admin",
            "admin_url": "http://admin.local:23646",
            "admin_secret_ref": "server-admin",
            "management_write_enabled": True,
        },
    )
    assert response.status_code == 422

    response = create_connection_body(
        client,
        csrf,
        {
            "name": "Admin",
            "admin_url": "http://admin.local:23646",
            "admin_secret_ref": "server-admin",
            "management_write_enabled": True,
            "acknowledge_management_write": True,
            "permissions": {"maintenance.execute": True},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["management_write_enabled"] is True
    assert response.json()["permissions"]["maintenance.execute"] is True


def test_endpoint_prefix_binding_is_exact_and_prefix_is_used(api):
    client, settings, csrf, admin = api
    mismatch = create_connection_body(
        client,
        csrf,
        {"name": "Admin", "admin_url": "http://admin.local:23646/proxy", "admin_secret_ref": "server-admin"},
    )
    assert mismatch.status_code == 403
    assert admin.requests == []

    prefixed = create_connection_body(
        client,
        csrf,
        {"name": "Admin", "admin_url": "http://admin.local:23646/proxy", "admin_secret_ref": "prefixed-admin", "endpoints": {"volume": "http://volume.local:8080/api"}},
    )
    assert prefixed.status_code == 200, prefixed.text
    body = prefixed.json()
    assert body["admin_url"] == "http://admin.local:23646/proxy"
    assert body["endpoints"]["volume"] == "http://volume.local:8080/api"

    response = client.get(f"/api/v1/management/{body['id']}/overview")
    assert response.status_code == 200, response.text
    assert [(req.method, req.url.path) for req in admin.requests[:4]] == [
        ("GET", "/proxy/login"),
        ("POST", "/proxy/login"),
        ("GET", "/proxy/admin"),
        ("GET", "/proxy/api/admin"),
    ]

    attacker_endpoint = create_connection_body(
        client,
        csrf,
        {"name": "Admin", "admin_url": "http://admin.local:23646/proxy", "admin_secret_ref": "prefixed-admin", "endpoints": {"filer": "http://attacker.local:8888"}},
    )
    assert attacker_endpoint.status_code == 403
    assert attacker_endpoint.json()["error"]["code"] == "ADMIN_ENDPOINT_DENIED"

    with db.connect(settings) as raw_conn:
        raw_conn.execute("UPDATE management_connections SET endpoints_json=? WHERE id=?", (json.dumps({"volume": "http://attacker.local:8080/api"}), body["id"]))
    guarded = client.get(f"/api/v1/management/{body['id']}/services")
    assert guarded.status_code == 403
    assert guarded.json()["error"]["code"] == "ADMIN_ENDPOINT_DENIED"


def test_endpoint_rejects_encoded_traversal(api):
    client, _settings, csrf, _admin = api
    response = create_connection_body(
        client,
        csrf,
        {"name": "Admin", "admin_url": "http://admin.local:23646/%2e%2e/admin", "admin_secret_ref": "server-admin"},
    )
    assert response.status_code == 422

