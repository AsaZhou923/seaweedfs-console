from __future__ import annotations

import json
import sys
from concurrent import futures
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from console import core, db, management, management_grpc
from console.config import Settings
from console.security import AppError


def make_settings(tmp_path: Path, secret: dict | None = None, registry_extra: dict | None = None) -> Settings:
    secret_path = tmp_path / "secrets.json"
    registry = {
        "server-admin": secret
        if secret is not None
        else {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
            "allowed_grpc_endpoints": {"filer": {"target": "127.0.0.1:31888", "transport": "plaintext"}},
        }
    }
    if registry_extra:
        registry.update(registry_extra)
    secret_path.write_text(json.dumps(registry), encoding="utf-8")
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


def app_with_management_grpc(settings: Settings) -> FastAPI:
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
    app.include_router(management_grpc.router)
    return app


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    app = app_with_management_grpc(settings)
    with db.connect(settings) as conn:
        result = core.login(conn, settings, "admin", "console-pass")
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, result["session_token"])
        yield client, settings, result["csrf_token"]
    monkeypatch.setattr(management_grpc, "_CHANNEL_FACTORY", None)


def create_connection(client: TestClient, csrf: str) -> dict:
    response = client.post(
        "/api/v1/management/connections",
        json={"name": "Admin", "admin_url": "http://admin.local:23646", "admin_secret_ref": "server-admin"},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    return response.json()


class FakeRequest:
    def __init__(self, client_types):
        self.client_types = list(client_types)


class FakePb2:
    ListMetadataSubscribersRequest = FakeRequest


class FakeChannel:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def install_fake_mount(monkeypatch, subscribers=None, error: Exception | None = None):
    calls = {"configs": [], "requests": [], "channels": []}

    class FakeStub:
        def __init__(self, channel):
            self.channel = channel

        def ListMetadataSubscribers(self, request, timeout):
            calls["requests"].append((list(request.client_types), timeout))
            if error is not None:
                raise error
            return SimpleNamespace(subscribers=list(subscribers or []))

    class FakeGrpc:
        SeaweedFilerStub = FakeStub

    def channel_factory(config):
        channel = FakeChannel()
        calls["configs"].append(dict(config))
        calls["channels"].append(channel)
        return channel

    monkeypatch.setattr(management_grpc, "_mount_modules", lambda: (FakePb2, FakeGrpc))
    monkeypatch.setattr(management_grpc, "_CHANNEL_FACTORY", channel_factory)
    return calls


def test_mount_clients_uses_registry_target_and_filters_to_mount_types(api, monkeypatch):
    client, _settings, csrf = api
    conn = create_connection(client, csrf)
    calls = install_fake_mount(
        monkeypatch,
        [
            SimpleNamespace(client_id=1, client_name="m1", client_type="mount", address="10.0.0.2:7777", path="/mnt/a", connected_at_ns=1_000_000_000),
            SimpleNamespace(client_id=2, client_name="v1", client_type="sw-vfs", address="10.0.0.3:7777", path="/mnt/b", connected_at_ns=0),
            SimpleNamespace(client_id=3, client_name="other", client_type="backup", address="10.0.0.4:7777", path="/private", connected_at_ns=2_000_000_000),
        ],
    )

    response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health_scope"] == "configured_filer"
    assert body["source"] == "filer_grpc_ListMetadataSubscribers"
    assert body["status"] == "supported"
    assert body["checked_at"].endswith("Z")
    assert body["count"] == 2
    assert [item["id"] for item in body["items"]] == ["1", "2"]
    assert body["items"][0]["connected_at"] == "1970-01-01T00:00:01.000000Z"
    assert body["items"][0]["path_prefix"] == "/mnt/a"
    assert body["items"][1]["connected_at"] is None
    assert body["items"][1]["connected_at_ns"] is None
    assert calls["configs"] == [{"target": "127.0.0.1:31888", "transport": "plaintext", "root_cert_ref": None}]
    assert calls["requests"] == [(["mount", "sw-vfs"], 5.0)]
    assert calls["channels"][0].closed is True


def test_mount_clients_missing_registry_is_not_configured(tmp_path: Path):
    settings = make_settings(
        tmp_path,
        {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
        },
    )
    app = app_with_management_grpc(settings)
    with db.connect(settings) as conn:
        login = core.login(conn, settings, "admin", "console-pass")
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, login["session_token"])
        conn = create_connection(client, login["csrf_token"])
        response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "connection_id": conn["id"],
        "health_scope": "configured_filer",
        "source": "filer_grpc_ListMetadataSubscribers",
        "checked_at": response.json()["checked_at"],
        "status": "not_configured",
        "count": None,
        "items": [],
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        {"target": "http://evil.local:31888/path", "transport": "plaintext"},
        {"target": "user@127.0.0.1:31888", "transport": "plaintext"},
        {"target": "10.0.0.9:31888", "transport": "plaintext"},
        {"target": "127.0.0.1:31888", "transport": "tls"},
        {"target": "127.0.0.1:31888", "transport": "tls", "root_cert_ref": "missing-cert"},
    ],
)
def test_invalid_or_unapproved_targets_are_denied_before_channel(tmp_path: Path, monkeypatch, endpoint):
    settings = make_settings(
        tmp_path,
        {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
            "allowed_grpc_endpoints": {"filer": endpoint},
        },
    )
    app = app_with_management_grpc(settings)
    with db.connect(settings) as conn:
        login = core.login(conn, settings, "admin", "console-pass")
    called = False

    def forbidden_factory(_config):
        nonlocal called
        called = True
        raise AssertionError("channel factory must not be called for denied endpoints")

    monkeypatch.setattr(management_grpc, "_CHANNEL_FACTORY", forbidden_factory)
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, login["session_token"])
        conn = create_connection(client, login["csrf_token"])
        response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "GRPC_ENDPOINT_DENIED"
    assert called is False


def test_explicit_plaintext_non_loopback_approval_allows_channel(tmp_path: Path, monkeypatch):
    settings = make_settings(
        tmp_path,
        {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
            "allowed_grpc_endpoints": {
                "filer": {"target": "10.0.0.9:31888", "transport": "plaintext", "allow_plaintext_non_loopback": True}
            },
        },
    )
    app = app_with_management_grpc(settings)
    with db.connect(settings) as conn:
        login = core.login(conn, settings, "admin", "console-pass")
    calls = install_fake_mount(monkeypatch, [])
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, login["session_token"])
        conn = create_connection(client, login["csrf_token"])
        response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "supported"
    assert response.json()["count"] == 0
    assert calls["configs"][0]["target"] == "10.0.0.9:31888"


def test_missing_proto_reports_unsupported(api, monkeypatch):
    client, _settings, csrf = api
    conn = create_connection(client, csrf)

    def missing_proto():
        raise AppError("GRPC_PROTO_UNAVAILABLE", "SeaweedFS gRPC client stubs are not available.", 501) from ImportError("missing test proto")

    monkeypatch.setattr(management_grpc, "_mount_modules", missing_proto)
    response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unsupported"
    assert response.json()["count"] is None
    assert response.json()["items"] == []
    assert response.json()["error_code"] == "GRPC_PROTO_UNAVAILABLE"


def test_bad_subscriber_timestamp_reports_bad_response(api, monkeypatch):
    client, _settings, csrf = api
    conn = create_connection(client, csrf)
    install_fake_mount(monkeypatch, [SimpleNamespace(client_id="m1", client_type="mount", connected_at_ns="not-int")])

    response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "bad_response"
    assert response.json()["count"] is None
    assert response.json()["items"] == []
    assert response.json()["error_code"] == "GRPC_BAD_RESPONSE"



class FakeGrpcStatus:
    def __init__(self, name: str):
        self.name = name


class FakeRpcError(Exception):
    def __init__(self, status_name: str, detail: str = "private detail"):
        super().__init__(detail)
        self._status_name = status_name

    def code(self):
        return FakeGrpcStatus(self._status_name)


@pytest.mark.parametrize(
    ("status_name", "expected_status", "expected_error"),
    [
        ("PERMISSION_DENIED", "permission_denied", "GRPC_PERMISSION_DENIED"),
        ("UNAUTHENTICATED", "permission_denied", "GRPC_PERMISSION_DENIED"),
        ("RESOURCE_EXHAUSTED", "bad_response", "GRPC_BAD_RESPONSE"),
        ("DATA_LOSS", "bad_response", "GRPC_BAD_RESPONSE"),
        ("INTERNAL", "bad_response", "GRPC_BAD_RESPONSE"),
        ("UNAVAILABLE", "unreachable", "GRPC_UNREACHABLE"),
        ("DEADLINE_EXCEEDED", "unreachable", "GRPC_UNREACHABLE"),
        ("ABORTED", "unknown", "GRPC_UNKNOWN"),
    ],
)
def test_rpc_status_code_classification(api, monkeypatch, status_name, expected_status, expected_error):
    client, _settings, csrf = api
    conn = create_connection(client, csrf)
    install_fake_mount(monkeypatch, error=FakeRpcError(status_name, "secret upstream detail"))

    response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == expected_status
    assert body["count"] is None
    assert body["items"] == []
    assert body["error_code"] == expected_error
    assert "secret upstream detail" not in response.text


def test_rpc_failure_reports_unreachable(api, monkeypatch):
    client, _settings, csrf = api
    conn = create_connection(client, csrf)
    install_fake_mount(monkeypatch, error=RuntimeError("boom private detail"))

    response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unreachable"
    assert response.json()["count"] is None
    assert response.json()["items"] == []
    assert response.json()["error_code"] == "GRPC_UNREACHABLE"
    assert "boom" not in response.text


def test_runtime_endpoint_validation_runs_before_grpc_channel(api, monkeypatch):
    client, settings, csrf = api
    conn = create_connection(client, csrf)
    with db.connect(settings) as raw_conn:
        raw_conn.execute(
            "UPDATE management_connections SET endpoints_json=? WHERE id=?",
            (json.dumps({"filer": "http://attacker.local:8888"}), conn["id"]),
        )
    called = False

    def forbidden_factory(_config):
        nonlocal called
        called = True
        raise AssertionError("channel factory must not run when HTTP endpoint binding is invalid")

    monkeypatch.setattr(management_grpc, "_CHANNEL_FACTORY", forbidden_factory)

    response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_ENDPOINT_DENIED"
    assert called is False


def test_localhost_host_port_target_is_valid(tmp_path: Path, monkeypatch):
    settings = make_settings(
        tmp_path,
        {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
            "allowed_grpc_endpoints": {"filer": {"target": "localhost:31888", "transport": "plaintext"}},
        },
    )
    app = app_with_management_grpc(settings)
    with db.connect(settings) as conn:
        login = core.login(conn, settings, "admin", "console-pass")
    calls = install_fake_mount(monkeypatch, [])
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, login["session_token"])
        conn = create_connection(client, login["csrf_token"])
        response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "supported"
    assert calls["configs"][0]["target"] == "localhost:31888"



def test_tls_registry_certificate_uses_secure_channel_without_tcp(tmp_path: Path, monkeypatch):
    cert = tmp_path / "root.pem"
    cert.write_bytes(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    settings = make_settings(
        tmp_path,
        {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
            "allowed_grpc_endpoints": {"filer": {"target": "filer.internal:31888", "transport": "tls", "root_cert_ref": "filer-root"}},
        },
        {"filer-root": {"kind": "grpc_tls", "root_certificate_file": str(cert)}},
    )
    secret = management_grpc._admin_secret(settings, "server-admin")
    config = management_grpc._grpc_endpoint(settings, secret, "filer")
    calls = {}

    class FakeGrpc:
        @staticmethod
        def ssl_channel_credentials(root_certificates):
            calls["root_certificates"] = root_certificates
            return "creds"

        @staticmethod
        def secure_channel(target, credentials, options=()):
            calls["secure_channel"] = (target, credentials, list(options))
            return "secure-channel"

        @staticmethod
        def insecure_channel(*_args, **_kwargs):
            raise AssertionError("TLS must not fall back to plaintext")

    monkeypatch.setitem(sys.modules, "grpc", FakeGrpc)

    channel = management_grpc._default_channel_factory(config)

    assert channel == "secure-channel"
    assert calls["root_certificates"] == cert.read_bytes()
    assert calls["secure_channel"][0] == "filer.internal:31888"
    assert ("grpc.enable_http_proxy", 0) in calls["secure_channel"][2]


def test_tls_bad_certificate_reference_denied_before_channel(tmp_path: Path, monkeypatch):
    settings = make_settings(
        tmp_path,
        {
            "kind": "seaweed_admin",
            "username": "admin",
            "password": "admin-pass",
            "allowed_endpoint_url": "http://admin.local:23646",
            "allowed_grpc_endpoints": {"filer": {"target": "filer.internal:31888", "transport": "tls", "root_cert_ref": "wrong-kind"}},
        },
        {"wrong-kind": {"kind": "seaweed_admin"}},
    )
    secret = management_grpc._admin_secret(settings, "server-admin")

    with pytest.raises(AppError) as exc:
        management_grpc._grpc_endpoint(settings, secret, "filer")

    assert exc.value.code == "GRPC_ENDPOINT_DENIED"


def test_real_local_grpc_server_wire_path(tmp_path: Path):
    import grpc

    from console.vendor import seaweed_mount_pb2, seaweed_mount_pb2_grpc

    seen_requests = []

    class Servicer(seaweed_mount_pb2_grpc.SeaweedFilerServicer):
        def ListMetadataSubscribers(self, request, context):
            seen_requests.append(list(request.client_types))
            return seaweed_mount_pb2.ListMetadataSubscribersResponse(
                subscribers=[
                    seaweed_mount_pb2.MetadataSubscriber(
                        client_name="mount-a",
                        client_type="mount",
                        address="10.0.0.2:7777",
                        path_prefix="/mnt/photos",
                        client_id=41,
                        connected_at_ns=2_000_000_000,
                        filer_address="filer-a",
                    ),
                    seaweed_mount_pb2.MetadataSubscriber(
                        client_name="other-a",
                        client_type="backup",
                        address="10.0.0.3:7777",
                        path_prefix="/hidden",
                        client_id=42,
                        connected_at_ns=3_000_000_000,
                    ),
                ]
            )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    seaweed_mount_pb2_grpc.add_SeaweedFilerServicer_to_server(Servicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        settings = make_settings(
            tmp_path,
            {
                "kind": "seaweed_admin",
                "username": "admin",
                "password": "admin-pass",
                "allowed_endpoint_url": "http://admin.local:23646",
                "allowed_grpc_endpoints": {"filer": {"target": f"127.0.0.1:{port}", "transport": "plaintext"}},
            },
        )
        app = app_with_management_grpc(settings)
        with db.connect(settings) as conn:
            login = core.login(conn, settings, "admin", "console-pass")
        with TestClient(app) as client:
            client.cookies.set(settings.session_cookie_name, login["session_token"])
            conn = create_connection(client, login["csrf_token"])
            response = client.get(f"/api/v1/management/{conn['id']}/modules/mount-clients")
    finally:
        server.stop(0)

    assert response.status_code == 200, response.text
    body = response.json()
    assert seen_requests == [["mount", "sw-vfs"]]
    assert body["status"] == "supported"
    assert body["count"] == 1
    assert body["items"] == [
        {
            "id": "41",
            "client_name": "mount-a",
            "client_id": 41,
            "client_epoch": None,
            "client_type": "mount",
            "address": "10.0.0.2:7777",
            "path": "/mnt/photos",
            "path_prefix": "/mnt/photos",
            "filer_address": "filer-a",
            "connected_at": "1970-01-01T00:00:02.000000Z",
            "connected_at_ns": 2_000_000_000,
        }
    ]
