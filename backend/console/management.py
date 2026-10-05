from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Request

from . import db, storage
from .config import Settings, load_secret_registry
from .security import AppError, current_user, mutation, new_id, sign_json, unsign_json, utc_add, utc_now


router = APIRouter(prefix="/api/v1")

PROTOCOL_BASELINE = "4.48"
SOURCE_SHA = "530be3e37337488ecc34d58441e0bc476e121c93"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_VOLUME_PAGE = 200
_TEST_TRANSPORT: httpx.BaseTransport | None = None

READ_PATHS = {
    "/api/admin",
    "/api/config",
    "/api/cluster/topology",
    "/api/cluster/masters",
    "/api/cluster/volumes",
    "/api/volumes/export",
    "/api/s3/buckets",
    "/api/users",
    "/api/service-accounts",
    "/api/groups",
    "/api/object-store/policies",
    "/api/principals",
    "/api/plugin/status",
    "/api/plugin/workers",
    "/api/plugin/jobs",
    "/api/plugin/lanes",
    "/api/plugin/job-types",
    "/api/plugin/activities",
    "/api/plugin/schemas",
    "/api/plugin/config",
    "/api/plugin/runs",
    "/api/plugin/observations",
    "/api/plugin/capabilities",
    "/api/plugin/scheduler-states",
    "/api/plugin/scheduler-status",
    "/api/s3tables/buckets",
    "/api/s3tables/namespaces",
    "/api/s3tables/tables",
    "/api/s3tables/bucket-policy",
    "/api/s3tables/table-policy",
    "/api/s3tables/tags",
}

READ_PATTERNS = [
    re.compile(r"^/api/s3/buckets/[^/]+$"),
    re.compile(r"^/api/s3/buckets/[^/]+/(lifecycle|policy)$"),
    re.compile(r"^/api/users/[^/]+$"),
    re.compile(r"^/api/users/[^/]+/policies$"),
    re.compile(r"^/api/service-accounts/[^/]+$"),
    re.compile(r"^/api/groups/[^/]+$"),
    re.compile(r"^/api/groups/[^/]+/members$"),
    re.compile(r"^/api/groups/[^/]+/policies$"),
    re.compile(r"^/api/object-store/policies/[^/]+$"),
    re.compile(r"^/api/plugin/jobs/[^/]+/detail$"),
    re.compile(r"^/api/plugin/job-types/[^/]+/(config|runs|observations|capabilities|schemas)$"),
    re.compile(r"^/api/plugin/(schemas|config|runs|observations|capabilities)/[^/]+$"),
    re.compile(r"^/api/mq/topics/[^/]+/[^/]+$"),
]

WRITE_PATTERNS = [
    (re.compile(r"^/api/s3/buckets$"), {"POST"}),
    (re.compile(r"^/api/s3/buckets/[^/]+/lifecycle$"), {"PUT", "DELETE"}),
    (re.compile(r"^/api/s3/buckets/[^/]+/policy$"), {"PUT", "DELETE"}),
    (re.compile(r"^/api/s3/buckets/[^/]+/(quota|owner)$"), {"PUT"}),
    (re.compile(r"^/api/users$"), {"POST"}),
    (re.compile(r"^/api/users/[^/]+$"), {"PUT", "DELETE"}),
    (re.compile(r"^/api/users/[^/]+/access-keys$"), {"POST"}),
    (re.compile(r"^/api/users/[^/]+/access-keys/[^/]+$"), {"DELETE"}),
    (re.compile(r"^/api/users/[^/]+/access-keys/[^/]+/status$"), {"PUT"}),
    (re.compile(r"^/api/users/[^/]+/policies$"), {"PUT"}),
    (re.compile(r"^/api/service-accounts$"), {"POST"}),
    (re.compile(r"^/api/service-accounts/[^/]+$"), {"PUT", "DELETE"}),
    (re.compile(r"^/api/groups$"), {"POST"}),
    (re.compile(r"^/api/groups/[^/]+$"), {"DELETE"}),
    (re.compile(r"^/api/groups/[^/]+/status$"), {"PUT"}),
    (re.compile(r"^/api/groups/[^/]+/members$"), {"POST"}),
    (re.compile(r"^/api/groups/[^/]+/members/[^/]+$"), {"DELETE"}),
    (re.compile(r"^/api/groups/[^/]+/policies$"), {"POST"}),
    (re.compile(r"^/api/groups/[^/]+/policies/[^/]+$"), {"DELETE"}),
    (re.compile(r"^/api/object-store/policies$"), {"POST"}),
    (re.compile(r"^/api/object-store/policies/validate$"), {"POST"}),
    (re.compile(r"^/api/object-store/policies/[^/]+$"), {"PUT", "DELETE"}),
    (re.compile(r"^/api/mq/topics/create$"), {"POST"}),
    (re.compile(r"^/api/mq/topics/retention/update$"), {"POST"}),
    (re.compile(r"^/api/mq/retention/purge$"), {"POST"}),
    (re.compile(r"^/api/s3tables/(buckets|namespaces|tables)$"), {"POST", "DELETE"}),
    (re.compile(r"^/api/s3tables/(bucket-policy|table-policy|tags)$"), {"PUT", "DELETE"}),
    (re.compile(r"^/api/volumes/[^/]+/[^/]+/(vacuum|read-only)$"), {"POST"}),
    (re.compile(r"^/api/plugin/(jobs/execute|jobs/[^/]+/expire)$"), {"POST"}),
    (re.compile(r"^/api/plugin/job-types/[^/]+/(config|detect|run)$"), {"PUT", "POST"}),
    (re.compile(r"^/api/plugin/job-types/[^/]+/schema$"), {"POST"}),
]

SENSITIVE_KEYS = {
    "password",
    "secret",
    "secret_key",
    "secretaccesskey",
    "secretkey",
    "aws_secret_access_key",
    "session_token",
    "sessiontoken",
    "token",
    "cookie",
    "set-cookie",
    "authorization",
    "auth",
    "signature",
    "x-amz-signature",
    "credential",
}
SIGNED_QUERY_KEYS = {"x-amz-signature", "x-amz-credential", "signature", "awsaccesskeyid"}

DEFAULT_PERMISSIONS = {
    "bucket.manage": False,
    "bucket_write_prefixes": ["swc-management-"],
    "iam.manage": False,
    "maintenance.execute": False,
    "volume.manage": False,
    "file.manage": False,
    "object.manage": False,
    "file.read_roots": ["/"],
    "file.write_roots": [],
    "mq.manage": False,
    "table.manage": False,
}

IDENTITY_FIELDS = {"id", "name", "admin_url", "admin_secret_ref", "s3_connection_id", "protocol_baseline", "endpoints", "endpoints_json"}


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS management_connections (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          admin_url TEXT NOT NULL,
          admin_secret_ref TEXT NOT NULL,
          s3_connection_id TEXT REFERENCES storage_connections(id),
          protocol_baseline TEXT NOT NULL DEFAULT '4.48',
          endpoints_json TEXT NOT NULL DEFAULT '{}',
          management_write_enabled INTEGER NOT NULL DEFAULT 0,
          permissions_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS management_access_events (
          id TEXT PRIMARY KEY,
          management_id TEXT NOT NULL REFERENCES management_connections(id),
          actor_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          before_json TEXT NOT NULL,
          after_json TEXT NOT NULL,
          reason TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS management_access_events_connection_time
          ON management_access_events(management_id, created_at);
        """
    )


@router.get("/management/connections")
def list_management_connections(request: Request):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        rows = conn.execute("SELECT * FROM management_connections ORDER BY created_at DESC").fetchall()
        return {"items": [_public_connection(dict(row)) for row in rows]}


@router.post("/management/connections")
def create_management_connection(request: Request, body: dict[str, Any]):
    mutation(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        result = create_connection(conn, request.app.state.settings, body)
        return result


@router.put("/management/connections/{management_id}/access-policy")
def update_management_access_policy(request: Request, management_id: str, body: dict[str, Any]):
    actor = mutation(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        _reject_identity_updates(body)
        row = require_management_row(conn, management_id)
        before = _public_connection(row)
        if "management_write_enabled" not in body:
            raise AppError("INVALID_REQUEST", "management_write_enabled is required.", 422, field="management_write_enabled")
        write_enabled = _strict_bool(body["management_write_enabled"], "management_write_enabled")
        if write_enabled and body.get("acknowledge_management_write") is not True:
            raise AppError("MANAGEMENT_WRITE_ACK_REQUIRED", "Enabling management writes requires acknowledge_management_write=true.", 422, field="acknowledge_management_write")
        permissions = _validate_permissions(body.get("permissions") or before["permissions"])
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise AppError("INVALID_REQUEST", "reason must be a string.", 422, field="reason")
        stamp = utc_now()
        conn.execute(
            """
            UPDATE management_connections
            SET management_write_enabled=?, permissions_json=?, updated_at=?
            WHERE id=?
            """,
            (
                1 if write_enabled else 0,
                json.dumps(permissions, sort_keys=True, separators=(",", ":")),
                stamp,
                management_id,
            ),
        )
        after = _public_connection(require_management_row(conn, management_id))
        _record_access_event(conn, management_id, actor.id, before, after, reason)
        return after


@router.get("/management/{management_id}/operations")
def list_management_operations(request: Request, management_id: str, limit: int = 100):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        _validate_stored_endpoints(request.app.state.settings, require_management_row(conn, management_id))
        try:
            from . import management_ops

            management_ops.initialize(conn)
        except Exception as exc:
            raise AppError("MANAGEMENT_OPERATIONS_UNAVAILABLE", "Management operation history is unavailable.", 501) from exc
    from . import management_ops

    return management_ops.list_operations(request.app.state.settings, management_id, limit=limit)


@router.get("/management/{management_id}/overview")
def overview(request: Request, management_id: str):
    return _read_projection(request, management_id, _overview)


@router.get("/management/{management_id}/topology")
def topology(request: Request, management_id: str):
    return _read_projection(request, management_id, _topology)


@router.get("/management/{management_id}/services")
def services(request: Request, management_id: str):
    return _read_projection(request, management_id, _services)


@router.get("/management/{management_id}/services/health")
def services_health(request: Request, management_id: str):
    current_user(request)
    settings = request.app.state.settings
    with db.connect(settings) as conn:
        initialize(conn)
        mgmt = require_management_row(conn, management_id)
        _validate_stored_endpoints(settings, mgmt)
    endpoints = _json(mgmt["endpoints_json"], {})
    return {
        "connection_id": management_id,
        "source": "configured_endpoints_only",
        "checked_at": utc_now(),
        "health_scope": "configured_instance_health",
        "services": {
            "master": _probe_http_service(endpoints, "master", ("/dir/status", "/cluster/status")),
            "filer": _probe_http_service(endpoints, "filer", ("/",), params={"limit": 1, "pretty": "y"}),
            "volume": _probe_http_service(endpoints, "volume", ("/status",)),
            "s3": _probe_s3_service(settings, mgmt),
        },
    }


@router.get("/management/{management_id}/volumes")
def volumes(request: Request, management_id: str, limit: int = 50, cursor: str | None = None, collection: str | None = None, readonly: bool | None = None, disk_type: str | None = None):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        mgmt = require_management_row(conn, management_id)
        _validate_stored_endpoints(request.app.state.settings, mgmt)
        with admin_client(request.app.state.settings, conn, mgmt) as client:
            return _volumes(client, request.app.state.settings, mgmt, limit=limit, cursor=cursor, collection=collection, readonly=readonly, disk_type=disk_type)


@router.get("/management/{management_id}/collections")
def collections(request: Request, management_id: str):
    return _read_projection(request, management_id, _collections)


@router.get("/management/{management_id}/ec")
def ec(request: Request, management_id: str):
    return _read_projection(request, management_id, _ec)


def _read_projection(request: Request, management_id: str, projector):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        mgmt = require_management_row(conn, management_id)
        _validate_stored_endpoints(request.app.state.settings, mgmt)
        with admin_client(request.app.state.settings, conn, mgmt) as client:
            return projector(client, mgmt)


def create_connection(conn: sqlite3.Connection, settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    name = _require(body, "name")
    admin_url = _normalize_endpoint(_require(body, "admin_url"))
    secret_ref = _require(body, "admin_secret_ref")
    secret = _admin_secret(settings, secret_ref)
    allowed = _normalize_endpoint(str(secret.get("allowed_endpoint_url") or ""))
    if allowed != admin_url:
        raise AppError("ADMIN_ENDPOINT_DENIED", "Admin endpoint does not match the server-side secret binding.", 403)
    s3_connection_id = body.get("s3_connection_id")
    if s3_connection_id:
        if not conn.execute("SELECT 1 FROM storage_connections WHERE id=?", (s3_connection_id,)).fetchone():
            raise AppError("CONNECTION_NOT_FOUND", "Linked S3 connection was not found.", 404)
    endpoints = _validate_endpoints(body.get("endpoints") or {}, secret)
    permissions = _validate_permissions(body.get("permissions") or {})
    write_enabled = False
    if "management_write_enabled" in body:
        write_enabled = _strict_bool(body["management_write_enabled"], "management_write_enabled")
        if write_enabled and body.get("acknowledge_management_write") is not True:
            raise AppError("MANAGEMENT_WRITE_ACK_REQUIRED", "Enabling management writes requires acknowledge_management_write=true.", 422, field="acknowledge_management_write")
    stamp = utc_now()
    identifier = new_id("mgmt_")
    conn.execute(
        """
        INSERT INTO management_connections(
          id,name,admin_url,admin_secret_ref,s3_connection_id,protocol_baseline,
          endpoints_json,management_write_enabled,permissions_json,created_at,updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier,
            name,
            admin_url,
            secret_ref,
            s3_connection_id,
            PROTOCOL_BASELINE,
            json.dumps(endpoints, sort_keys=True, separators=(",", ":")),
            1 if write_enabled else 0,
            json.dumps(permissions, sort_keys=True, separators=(",", ":")),
            stamp,
            stamp,
        ),
    )
    return _public_connection(dict(conn.execute("SELECT * FROM management_connections WHERE id=?", (identifier,)).fetchone()))


def require_management(request: Request, id: str, write: bool = False) -> dict[str, Any]:
    if write:
        mutation(request)
    else:
        current_user(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        row = require_management_row(conn, id)
        _validate_stored_endpoints(request.app.state.settings, row)
        if write and not row["management_write_enabled"]:
            raise AppError("MANAGEMENT_WRITE_DISABLED", "Management writes are disabled for this connection.", 403)
        return _public_connection(row)


def require_management_row(conn: sqlite3.Connection, id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM management_connections WHERE id=?", (id,)).fetchone()
    if not row:
        raise AppError("MANAGEMENT_NOT_FOUND", "Management connection was not found.", 404)
    return dict(row)


def get_management(conn: sqlite3.Connection, id: str) -> dict[str, Any]:
    return require_management_row(conn, id)


def admin_client(settings: Settings, conn: sqlite3.Connection, management: dict[str, Any] | str) -> "AdminClient":
    if isinstance(management, str):
        row = require_management_row(conn, management)
    else:
        candidate = dict(management)
        management_id = candidate.get("id")
        if isinstance(management_id, str):
            row = require_management_row(conn, management_id)
        else:
            row = candidate
    _validate_stored_endpoints(settings, row)
    secret = _admin_secret(settings, row["admin_secret_ref"])
    return AdminClient(settings, row, secret)


def associated_s3_client(settings: Settings, management: dict[str, Any] | str):
    with db.connect(settings) as conn:
        initialize(conn)
        if isinstance(management, str):
            row = require_management_row(conn, management)
        else:
            management_id = dict(management).get("id")
            if not isinstance(management_id, str):
                raise AppError("MANAGEMENT_NOT_FOUND", "Management connection was not found.", 404)
            row = require_management_row(conn, management_id)
        connection_id = row.get("s3_connection_id")
        if not connection_id:
            raise AppError("ASSOCIATED_S3_REQUIRED", "Management connection does not have an associated S3 connection.", 409)
        connection_row = conn.execute("SELECT * FROM storage_connections WHERE id=?", (connection_id,)).fetchone()
        if not connection_row:
            raise AppError("CONNECTION_NOT_FOUND", "Associated S3 connection was not found.", 404)
        connection = dict(connection_row)
        connection["verify_tls"] = bool(connection.get("verify_tls", True))
    try:
        return storage.client(connection, settings)
    except storage.StorageError as exc:
        raise AppError(exc.code, str(exc), exc.status) from exc


class AdminClient:
    def __init__(self, settings: Settings, management: dict[str, Any], secret: dict[str, Any]):
        self.settings = settings
        self.management = management
        self.endpoint_url = _normalize_endpoint(management["admin_url"])
        parsed = urlsplit(self.endpoint_url)
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.path_prefix = parsed.path.rstrip("/")
        self.secret = secret
        self.checked_at = utc_now()
        self._csrf: str | None = None
        self._client = httpx.Client(
            base_url=self.base_url,
            trust_env=False,
            follow_redirects=False,
            timeout=8.0,
            transport=_TEST_TRANSPORT,
        )

    def __enter__(self) -> "AdminClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request_json("GET", path, params=params)

    def request_json(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        method = method.upper()
        _assert_allowed(method, path)
        if self._csrf is None:
            self._login()
        headers = {"Accept": "application/json"}
        if method != "GET" and self._csrf:
            headers["X-CSRF-Token"] = self._csrf
            headers["Origin"] = self.base_url
        response = self._request(method, path, headers=headers, json=json_body, params=params)
        data = self._json_response(response)
        return data if raw else redact(data)

    def _login(self) -> None:
        login_page = self._request("GET", "/login", headers={"Accept": "text/html"})
        parser = _LoginParser()
        parser.feed(login_page.text)
        csrf = parser.csrf_token
        if not csrf:
            raise AppError("ADMIN_LOGIN_FAILED", "Admin login page did not include a CSRF token.", 502)
        response = self._request(
            "POST",
            "/login",
            data={"username": self.secret["username"], "password": self.secret["password"], "csrf_token": csrf},
            headers={"Origin": self.base_url, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code not in {302, 303}:
            raise AppError("ADMIN_LOGIN_FAILED", "Admin login was not accepted.", 502)
        admin_page = self._request("GET", "/admin", headers={"Accept": "text/html"})
        meta = _MetaParser()
        meta.feed(admin_page.text)
        self._csrf = meta.csrf_token or csrf

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        _validate_path(path)
        request_path = self._join_path(path)
        body_limit = min(MAX_BODY_BYTES, kwargs.pop("_max_body_bytes", MAX_BODY_BYTES))
        deadline = kwargs.pop("_deadline_monotonic", getattr(self, "_deadline_monotonic", None))
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppError("ADMIN_TIMEOUT", "Admin request budget expired.", 504)
            timeout = kwargs.get("timeout", 8.0)
            kwargs["timeout"] = min(timeout, remaining) if isinstance(timeout, (float, int)) else remaining
        try:
            with self._client.stream(method, request_path, **kwargs) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    if deadline is not None and time.monotonic() > deadline:
                        raise AppError("ADMIN_TIMEOUT", "Admin request budget expired.", 504)
                    if len(content) + len(chunk) > body_limit:
                        raise AppError("ADMIN_RESPONSE_TOO_LARGE", "Admin upstream response exceeded the size limit.", 502)
                    content.extend(chunk)
                response = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    request=response.request,
                    extensions=response.extensions,
                )
        except httpx.TimeoutException as exc:
            raise AppError("ADMIN_TIMEOUT", "Admin upstream timed out.", 504, retryable=True) from exc
        except httpx.TransportError as exc:
            raise AppError("ADMIN_UNREACHABLE", "Admin upstream is unreachable.", 503, retryable=True) from exc
        if response.status_code == 401:
            raise AppError("ADMIN_UNAUTHENTICATED", "Admin upstream rejected authentication.", 502)
        if response.status_code == 403:
            raise AppError("ADMIN_FORBIDDEN", "Admin upstream denied the request.", 502)
        if response.status_code == 404:
            raise AppError("ADMIN_ENDPOINT_NOT_FOUND", "Admin upstream endpoint was not found.", 502)
        if response.status_code == 501:
            raise AppError("ADMIN_ENDPOINT_UNSUPPORTED", "Admin upstream endpoint is unsupported.", 502)
        if not 200 <= response.status_code < 400:
            raise AppError("ADMIN_UPSTREAM_ERROR", "Admin upstream returned an error.", 502, detail={"status": response.status_code})
        return response

    def _join_path(self, path: str) -> str:
        if not self.path_prefix:
            return path
        return f"{self.path_prefix}{path}"

    def _json_response(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise AppError("ADMIN_BAD_RESPONSE", "Admin upstream did not return JSON.", 502)
        try:
            return response.json()
        except ValueError as exc:
            raise AppError("ADMIN_BAD_RESPONSE", "Admin upstream returned malformed JSON.", 502) from exc


class NativeHttpClient:
    """Cookie-free HTTP client for approved private Master/Filer endpoints."""

    def __init__(self, base_url: str):
        self.base_url = _normalize_endpoint(base_url)
        self._client = httpx.Client(base_url=self.base_url, trust_env=False, follow_redirects=False, timeout=8.0, transport=_TEST_TRANSPORT)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise AppError("NATIVE_BAD_RESPONSE", "Native endpoint did not return JSON.", 502)
        try:
            return response.json()
        except ValueError as exc:
            raise AppError("NATIVE_BAD_RESPONSE", "Native endpoint returned malformed JSON.", 502) from exc

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        _validate_path(path)
        try:
            with self._client.stream(method, path, headers={"Accept": "application/json"}, **kwargs) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BODY_BYTES:
                        raise AppError("NATIVE_RESPONSE_TOO_LARGE", "Native endpoint response exceeded the size limit.", 502)
                response = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    request=response.request,
                    extensions=response.extensions,
                )
        except httpx.TimeoutException as exc:
            raise AppError("NATIVE_TIMEOUT", "Native endpoint timed out.", 504, retryable=True) from exc
        except httpx.TransportError as exc:
            raise AppError("NATIVE_UNREACHABLE", "Native endpoint is unreachable.", 503, retryable=True) from exc
        if response.status_code in {401, 403}:
            raise AppError("NATIVE_FORBIDDEN", "Native endpoint denied the request.", 502)
        if response.status_code == 404:
            raise AppError("NATIVE_NOT_FOUND", "Native endpoint was not found.", 404)
        if not 200 <= response.status_code < 400:
            raise AppError("NATIVE_UPSTREAM_ERROR", "Native endpoint returned an error.", 502, detail={"status": response.status_code})
        return response

    def close(self) -> None:
        self._client.close()


class _LoginParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf_token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "input" and data.get("type") == "hidden" and data.get("name") in {"csrf_token", "csrf"}:
            self.csrf_token = data.get("value")


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf_token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "meta" and data.get("name") == "csrf-token":
            self.csrf_token = data.get("content")


def _overview(client: AdminClient, mgmt: dict[str, Any]) -> dict[str, Any]:
    admin = _fetch_optional(client, "/api/admin")
    config = _fetch_optional(client, "/api/config")
    return _envelope(
        mgmt,
        client,
        {
            "admin": admin["data"],
            "config": config["data"],
            "modules": {"admin": admin["status"], "config": config["status"]},
        },
    )


def _topology(client: AdminClient, mgmt: dict[str, Any]) -> dict[str, Any]:
    result = _fetch_optional(client, "/api/cluster/topology")
    return _envelope(mgmt, client, {"topology": result["data"], "module_status": result["status"], "evidence": result["evidence"]})


def _services(client: AdminClient, mgmt: dict[str, Any]) -> dict[str, Any]:
    admin = _fetch_optional(client, "/api/admin")
    topology = _fetch_optional(client, "/api/cluster/topology")
    plugin = _fetch_optional(client, "/api/plugin/status")
    data = admin["data"] if isinstance(admin["data"], dict) else {}
    topo = topology["data"] if isinstance(topology["data"], dict) else {}
    plugin_data = plugin["data"] if isinstance(plugin["data"], dict) else {}
    payload = {
        "master_nodes": data.get("master_nodes") if data.get("master_nodes") is not None else topo.get("masters"),
        "volume_servers": data.get("volume_servers") or _topology_nodes(topo),
        "filer_nodes": data.get("filer_nodes"),
        "s3_nodes": data.get("s3_nodes"),
        "mq": _reported_count(data.get("message_brokers")),
        "mount_clients": _reported_count(data.get("mount_clients") if data.get("mount_clients") is not None else data.get("total_mount_clients")),
        "plugin": {"state": plugin["status"], "data": plugin_data},
        "health": "reported" if admin["status"] == "supported" or topology["status"] == "supported" else "not_checked",
        "module_status": {"admin": admin["status"], "topology": topology["status"], "plugin": plugin["status"]},
    }
    return _envelope(mgmt, client, payload)


def _volumes(
    client: AdminClient,
    settings: Settings,
    mgmt: dict[str, Any],
    *,
    limit: int,
    cursor: str | None,
    collection: str | None,
    readonly: bool | None,
    disk_type: str | None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), MAX_VOLUME_PAGE))
    offset = 0
    filter_payload = {"collection": collection, "readonly": readonly, "disk_type": disk_type}
    cursor_payload = None
    if cursor:
        try:
            cursor_payload = unsign_json(cursor, settings.require_cursor_key(), "mgmt-volumes:v1")
        except ValueError as exc:
            raise AppError("INVALID_CURSOR", "Cursor is malformed or has an invalid signature.", 400) from exc
        if not isinstance(cursor_payload, dict):
            raise AppError("INVALID_CURSOR", "Cursor payload is invalid.", 400)
        if cursor_payload.get("management_id") != mgmt["id"] or cursor_payload.get("filter") != filter_payload or cursor_payload.get("limit") != limit:
            raise AppError("INVALID_CURSOR", "Cursor no longer matches this query.", 400)
        _validate_cursor_expiry(cursor_payload.get("expires_at"))
        try:
            offset = int(cursor_payload.get("offset", 0))
        except (TypeError, ValueError) as exc:
            raise AppError("INVALID_CURSOR", "Cursor offset is invalid.", 400) from exc
        if offset < 0:
            raise AppError("INVALID_CURSOR", "Cursor offset is invalid.", 400)
    result = _fetch_optional(client, "/api/volumes/export")
    all_rows = _flatten_volumes(result["data"])
    source_signature = _source_signature(all_rows)
    if cursor_payload and cursor_payload.get("source_signature") != source_signature:
        raise AppError("INVALID_CURSOR", "Volume export changed; reload the list.", 400)
    rows = [
        row
        for row in all_rows
        if (collection is None or row.get("collection") == collection)
        and (readonly is None or bool(row.get("read_only")) == readonly)
        and (disk_type is None or row.get("disk_type") == disk_type)
    ]
    page = rows[offset : offset + limit]
    next_cursor = None
    if offset + limit < len(rows):
        next_cursor = sign_json(
            {
                "management_id": mgmt["id"],
                "filter": filter_payload,
                "limit": limit,
                "offset": offset + limit,
                "source_signature": source_signature,
                "expires_at": utc_add(30 * 60),
            },
            settings.require_cursor_key(),
            "mgmt-volumes:v1",
        )
    logical = {(row.get("collection") or "default", row.get("id")) for row in rows if row.get("id") is not None}
    return _envelope(
        mgmt,
        client,
        {
            "items": page,
            "next_cursor": next_cursor,
            "counts": {
                "physical_replicas": len(rows) if result["status"] == "supported" else None,
                "logical_volumes": len(logical) if result["status"] == "supported" else None,
            },
            "module_status": result["status"],
            "evidence": result["evidence"],
        },
    )


def _collections(client: AdminClient, mgmt: dict[str, Any]) -> dict[str, Any]:
    result = _fetch_optional(client, "/api/volumes/export")
    rows = _flatten_volumes(result["data"])
    collections: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("collection") or "default"
        item = collections.setdefault(name, {"name": name, "physical_replicas": 0, "logical_ids": set(), "ec_volumes": None, "size_bytes": 0, "file_count": None, "state": "reported"})
        item["physical_replicas"] += 1
        if row.get("id") is not None:
            item["logical_ids"].add(row.get("id"))
        if isinstance(row.get("size"), int):
            item["size_bytes"] += row["size"]
    public = []
    for item in collections.values():
        public.append({**{k: v for k, v in item.items() if k != "logical_ids"}, "logical_volumes": len(item["logical_ids"])})
    return _envelope(mgmt, client, {"items": sorted(public, key=lambda x: x["name"]), "module_status": result["status"], "evidence": result["evidence"]})


def _ec(client: AdminClient, mgmt: dict[str, Any]) -> dict[str, Any]:
    cluster = _fetch_optional(client, "/api/cluster/volumes")
    export = _fetch_optional(client, "/api/volumes/export")
    items = _flatten_ec(cluster["data"]) or _flatten_ec(export["data"])
    return _envelope(
        mgmt,
        client,
        {
            "items": items,
            "counts": {
                "ec_shards": len(items) if cluster["status"] == "supported" or export["status"] == "supported" else None,
                "ec_volumes": len({item.get("volume_id") for item in items if item.get("volume_id") is not None}) if items else None,
                "missing_shards": None,
            },
            "module_status": cluster["status"] if cluster["status"] == "supported" else export["status"],
        },
    )


def _probe_http_service(endpoints: dict[str, Any], name: str, paths: tuple[str, ...], params: dict[str, Any] | None = None) -> dict[str, Any]:
    endpoint = endpoints.get(name)
    if not isinstance(endpoint, str) or not endpoint.strip():
        return _health_status("not_configured", source="server_registry")
    client = NativeHttpClient(endpoint)
    try:
        observations = []
        status = "healthy"
        for path in paths:
            try:
                data = client.get_json(path, params=params if path == paths[0] else None)
                observations.append({"path": path, "status": "healthy", "version": _extract_version(data), "fields": _health_field_summary(data)})
            except AppError as exc:
                mapped = _health_status_from_error(exc)
                observations.append({"path": path, "status": mapped, "error_code": exc.code})
                if status == "healthy":
                    status = mapped
        is_leader = next((obs.get("fields", {}).get("IsLeader") for obs in observations if obs.get("path") == "/cluster/status" and isinstance(obs.get("fields", {}).get("IsLeader"), bool)), None)
        result = {"status": status, "endpoint_configured": True, "endpoint": endpoint, "source": "server_registry_endpoint", "checked_at": utc_now(), "observations": observations}
        if is_leader is not None:
            result["is_leader"] = is_leader
        return result
    finally:
        client.close()


def _probe_s3_service(settings: Settings, mgmt: dict[str, Any]) -> dict[str, Any]:
    if not mgmt.get("s3_connection_id"):
        return _health_status("not_configured", source="associated_s3_connection")
    try:
        with db.connect(settings) as conn:
            row = conn.execute("SELECT endpoint_url FROM storage_connections WHERE id=?", (mgmt["s3_connection_id"],)).fetchone()
            endpoint = row["endpoint_url"] if row else None
        s3 = associated_s3_client(settings, mgmt)
        response = s3.list_buckets()
        version = _extract_s3_version(response)
        return {
            "status": "healthy",
            "endpoint_configured": True,
            "endpoint": endpoint,
            "source": "associated_s3_client",
            "checked_at": utc_now(),
            "operation": "ListBuckets",
            "version": version,
            "version_source": "s3_response_header" if version else None,
        }
    except AppError as exc:
        return _health_status(_health_status_from_error(exc), source="associated_s3_client", error_code=exc.code)
    except Exception as exc:
        status = _s3_exception_status(exc)
        return _health_status(status, source="associated_s3_client", error_code=_s3_exception_code(exc))


def _health_status(status: str, *, source: str, error_code: str | None = None) -> dict[str, Any]:
    result = {"status": status, "endpoint_configured": status != "not_configured", "source": source, "checked_at": utc_now()}
    if error_code:
        result["error_code"] = error_code
    return result


def _health_status_from_error(exc: AppError) -> str:
    return {
        "NATIVE_FORBIDDEN": "permission_denied",
        "NATIVE_TIMEOUT": "unreachable",
        "NATIVE_UNREACHABLE": "unreachable",
        "NATIVE_NOT_FOUND": "not_configured",
        "NATIVE_BAD_RESPONSE": "bad_response",
        "NATIVE_RESPONSE_TOO_LARGE": "bad_response",
        "SECRET_REF_NOT_FOUND": "permission_denied",
        "SECRET_REF_FORBIDDEN": "permission_denied",
        "SECRET_REF_INCOMPLETE": "permission_denied",
    }.get(exc.code, "unknown")


def _health_field_summary(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        allowed = {"IsLeader", "Leader", "TopologyId", "Version"}
        return {key: data[key] for key in allowed if key in data and isinstance(data[key], (str, bool, int, float))}
    if isinstance(data, list):
        return {"list_length": len(data)}
    return {"type": type(data).__name__}


def _extract_version(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    for key in ("Version", "version", "SeaweedVersion", "seaweed_version"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return {"value": value, "source_field": key}
    return None


def _extract_s3_version(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    server = headers.get("server") if isinstance(headers, dict) else None
    match = re.search(r"SeaweedFS\s+.*?(\d+\.\d+)", server or "")
    return match.group(1) if match else None


def _s3_exception_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code") or type(exc).__name__)


def _s3_exception_status(exc: Exception) -> str:
    status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = _s3_exception_code(exc)
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "not_configured"
    if status in {501, 405} or code in {"NotImplemented", "NotSupported"}:
        return "unsupported"
    return "unreachable" if code in {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"} else "unknown"


def _fetch_optional(client: AdminClient, path: str) -> dict[str, Any]:
    checked_at = utc_now()
    try:
        return {"status": "supported", "data": client.get_json(path), "evidence": {"path": path, "checked_at": checked_at}}
    except AppError as exc:
        status = {
            "ADMIN_FORBIDDEN": "permission_denied",
            "ADMIN_ENDPOINT_NOT_FOUND": "not_configured",
            "ADMIN_ENDPOINT_UNSUPPORTED": "unsupported",
            "ADMIN_TIMEOUT": "unreachable",
            "ADMIN_UNREACHABLE": "unreachable",
            "ADMIN_BAD_RESPONSE": "stub",
            "ADMIN_UPSTREAM_ERROR": "unknown",
        }.get(exc.code, "unknown")
        return {"status": status, "data": None, "evidence": {"path": path, "checked_at": checked_at, "error_code": exc.code}}


def _validate_cursor_expiry(value: Any) -> None:
    try:
        expires = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_CURSOR", "Cursor expiry is invalid.", 400) from exc
    if expires <= datetime.now(timezone.utc):
        raise AppError("INVALID_CURSOR", "Cursor has expired.", 400)


def _source_signature(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stable_fields = (
        "id",
        "server",
        "collection",
        "datacenter",
        "rack",
        "disk_type",
        "read_only",
        "size",
        "file_count",
        "delete_count",
        "deleted_bytes",
        "replica_placement",
        "version",
    )
    canonical = []
    for row in rows:
        canonical.append({field: row.get(field) for field in stable_fields if row.get(field) is not None})
    canonical.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"row_count": len(canonical), "sha256": digest}


def _envelope(mgmt: dict[str, Any], client: AdminClient, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_id": mgmt["id"],
        "protocol_baseline": mgmt["protocol_baseline"],
        "module_source_pin": {"upstream_type": "official_admin_http", "protocol_baseline": PROTOCOL_BASELINE, "source_sha": SOURCE_SHA},
        "source": "official_admin_http",
        "checked_at": client.checked_at,
        "source_updated_at": payload.pop("source_updated_at", None),
        "metadata": {"redacted": True, "schema_source": "seaweedfs-4.48"},
        **redact(payload),
    }


def _flatten_volumes(data: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return rows
    for dc in data.get("data_centers") or data.get("datacenters") or []:
        for rack in dc.get("racks") or []:
            for node in rack.get("nodes") or rack.get("data_nodes") or []:
                server = node.get("id") or node.get("address")
                for disk in node.get("disks") or node.get("disk_infos") or []:
                    disk_type = disk.get("type") or disk.get("disk_type")
                    volumes = disk.get("volumes") or disk.get("volume_infos") or []
                    for volume in volumes:
                        row = dict(volume)
                        row.update({"server": server, "datacenter": dc.get("id"), "rack": rack.get("id"), "disk_type": disk_type})
                        row.setdefault("collection", row.get("Collection") or row.get("collection") or "default")
                        row.setdefault("id", row.get("Id") or row.get("volume_id") or row.get("VolumeID"))
                        row.setdefault("size", row.get("Size") or row.get("size"))
                        row.setdefault("read_only", row.get("ReadOnly") if "ReadOnly" in row else row.get("read_only"))
                        rows.append(redact(row))
                for volume in node.get("volumes") or []:
                    row = dict(volume)
                    row.update({"server": server, "datacenter": dc.get("id"), "rack": rack.get("id")})
                    row.setdefault("collection", row.get("collection") or "default")
                    rows.append(redact(row))
    return rows


def _flatten_ec(data: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return items
    for server in data.get("volume_servers") or []:
        for shard in server.get("ec_shard_details") or []:
            item = dict(shard)
            item.setdefault("server", server.get("id") or server.get("address"))
            item.setdefault("volume_id", item.get("volume_id") or item.get("id") or item.get("VolumeID"))
            items.append(redact(item))
    for row in _flatten_volumes(data):
        for shard in row.get("ec_shard_details") or [] if isinstance(row.get("ec_shard_details"), list) else []:
            items.append(redact(shard))
    return items


def _topology_nodes(topo: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for dc in topo.get("datacenters") or []:
        for rack in dc.get("racks") or []:
            for node in rack.get("nodes") or []:
                nodes.append(node)
    return nodes


def _reported_count(value: Any) -> dict[str, Any]:
    if value is None:
        return {"state": "unknown", "count": None}
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return {"state": "reported", "count": value}
    if isinstance(value, list):
        return {"state": "reported", "count": len(value)}
    return {"state": "reported", "count": None, "data": value}


def redact(data: Any) -> Any:
    return _redact(copy.deepcopy(data))


def _redact(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            lowered = str(key).replace("-", "_").lower()
            if lowered in SENSITIVE_KEYS or lowered.endswith("_secret") or lowered.endswith("_password"):
                out[key] = "[REDACTED]"
            elif lowered == "credentials" and isinstance(value, list):
                out[key] = [_redact_credential(item) for item in value]
            else:
                out[key] = _redact(value)
        return out
    if isinstance(data, list):
        return [_redact(item) for item in data]
    if isinstance(data, str):
        return _redact_url(data)
    return data


def _redact_credential(item: Any) -> Any:
    if not isinstance(item, dict):
        return _redact(item)
    out = {}
    for key, value in item.items():
        lowered = str(key).replace("-", "_").lower()
        if lowered in {"secret_key", "secretaccesskey", "secretkey", "password", "session_token", "token"}:
            out[key] = "[REDACTED]"
        else:
            out[key] = _redact(value)
    return out


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc or not parsed.query:
        return value
    query = parse_qsl(parsed.query, keep_blank_values=True)
    changed = False
    clean = []
    for key, val in query:
        if key.lower() in SIGNED_QUERY_KEYS or key.lower().startswith("x-amz-"):
            clean.append((key, "[REDACTED]"))
            changed = True
        else:
            clean.append((key, val))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(clean), parsed.fragment)) if changed else value


def _admin_secret(settings: Settings, secret_ref: str) -> dict[str, Any]:
    entry = load_secret_registry(settings).get(secret_ref)
    if not isinstance(entry, dict) or entry.get("kind") != "seaweed_admin":
        raise AppError("ADMIN_SECRET_NOT_FOUND", "Admin secret reference is not registered.", 403)
    if not entry.get("username") or not entry.get("password") or not entry.get("allowed_endpoint_url"):
        raise AppError("ADMIN_SECRET_INCOMPLETE", "Admin secret reference is incomplete.", 403)
    return entry


def _public_connection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "admin_url": row["admin_url"],
        "s3_connection_id": row["s3_connection_id"],
        "protocol_baseline": row["protocol_baseline"],
        "endpoints": _json(row["endpoints_json"], {}),
        "management_write_enabled": bool(row["management_write_enabled"]),
        "permissions": {**DEFAULT_PERMISSIONS, **_json(row["permissions_json"], {})},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _record_access_event(conn: sqlite3.Connection, management_id: str, actor_id: str, before: dict[str, Any], after: dict[str, Any], reason: str | None) -> None:
    conn.execute(
        """
        INSERT INTO management_access_events(id,management_id,actor_id,event_type,before_json,after_json,reason,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            new_id("mgae_"),
            management_id,
            actor_id,
            "access_policy_update",
            json.dumps(redact(before), sort_keys=True, separators=(",", ":")),
            json.dumps(redact(after), sort_keys=True, separators=(",", ":")),
            reason,
            utc_now(),
        ),
    )


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise AppError("INVALID_REQUEST", "field must be boolean.", 422, field=field)
    return value


def _reject_identity_updates(body: dict[str, Any]) -> None:
    for key in IDENTITY_FIELDS:
        if key in body:
            raise AppError("MANAGEMENT_IDENTITY_IMMUTABLE", "Management connection identity fields cannot be changed through access policy.", 422, field=key)


def _validate_permissions(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppError("INVALID_REQUEST", "permissions must be an object.", 422, field="permissions")
    result = dict(DEFAULT_PERMISSIONS)
    allowed = set(DEFAULT_PERMISSIONS)
    for key, item in value.items():
        if key not in allowed:
            raise AppError("INVALID_REQUEST", "unknown management permission.", 422, field=f"permissions.{key}")
        if isinstance(DEFAULT_PERMISSIONS[key], bool) and not isinstance(item, bool):
            raise AppError("INVALID_REQUEST", "permission must be boolean.", 422, field=f"permissions.{key}")
        if isinstance(DEFAULT_PERMISSIONS[key], list) and not (isinstance(item, list) and all(isinstance(x, str) for x in item)):
            raise AppError("INVALID_REQUEST", "permission list must contain strings.", 422, field=f"permissions.{key}")
        if key in {"file.read_roots", "file.write_roots"}:
            result[key] = [_validate_file_root(root, f"permissions.{key}") for root in item]
        elif key == "bucket_write_prefixes":
            result[key] = [_validate_bucket_prefix(prefix, f"permissions.{key}") for prefix in item]
        else:
            result[key] = item
    return result


def _validate_file_root(value: str, field: str) -> str:
    root = value.strip()
    decoded = unquote(root)
    if not root.startswith("/") or "\\" in decoded or ".." in decoded.split("/") or "?" in root or "#" in root:
        raise AppError("INVALID_REQUEST", "file root must be an absolute safe path.", 422, field=field)
    return root.rstrip("/") or "/"


def _validate_bucket_prefix(value: str, field: str) -> str:
    prefix = value.strip()
    decoded = unquote(prefix)
    if not prefix or "\\" in decoded or ".." in decoded.split("/") or "?" in prefix or "#" in prefix:
        raise AppError("INVALID_REQUEST", "bucket write prefix is invalid.", 422, field=field)
    return prefix


def _validate_endpoints(value: dict[str, Any], secret: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppError("INVALID_REQUEST", "endpoints must be an object.", 422, field="endpoints")
    allowed = {"master", "filer", "volume", "iceberg", "lance", "s3", "mq"}
    approved = (secret or {}).get("allowed_endpoints") or {}
    if not isinstance(approved, dict):
        raise AppError("ADMIN_ENDPOINT_DENIED", "Admin secret endpoint approvals are invalid.", 403)
    result = {}
    for key, endpoint in value.items():
        if key not in allowed:
            raise AppError("INVALID_REQUEST", "unknown management endpoint.", 422, field=f"endpoints.{key}")
        normalized = _normalize_endpoint(str(endpoint))
        approved_value = approved.get(key)
        if not isinstance(approved_value, str) or not approved_value.strip():
            raise AppError("ADMIN_ENDPOINT_DENIED", "Management endpoint is not approved by the server-side secret binding.", 403, field=f"endpoints.{key}")
        if _normalize_endpoint(approved_value) != normalized:
            raise AppError("ADMIN_ENDPOINT_DENIED", "Management endpoint does not match the server-side secret binding.", 403, field=f"endpoints.{key}")
        result[key] = normalized
    return result


def _validate_stored_endpoints(settings: Settings, row: dict[str, Any]) -> None:
    endpoints = _json(row.get("endpoints_json"), {})
    if endpoints:
        secret = _admin_secret(settings, row["admin_secret_ref"])
        _validate_endpoints(endpoints, secret)


def _assert_allowed(method: str, path: str) -> None:
    _validate_path(path)
    if method == "GET" and path in READ_PATHS:
        return
    if method == "GET" and any(pattern.match(path) for pattern in READ_PATTERNS):
        return
    for pattern, methods in WRITE_PATTERNS:
        if method in methods and pattern.match(path):
            return
    raise AppError("ADMIN_ENDPOINT_DENIED", "Admin endpoint is not allowlisted for this SeaweedFS baseline.", 403)


def _validate_path(path: str) -> None:
    parsed = urlsplit(path)
    decoded = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or "\\" in decoded or ".." in decoded.split("/"):
        raise AppError("ADMIN_ENDPOINT_DENIED", "Admin request path is invalid.", 403)
    if parsed.query:
        raise AppError("ADMIN_ENDPOINT_DENIED", "Admin request query must be passed as params.", 403)


def _normalize_endpoint(value: str) -> str:
    parsed = urlsplit(value.strip())
    decoded = unquote(parsed.path or "")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in decoded
        or ".." in decoded.split("/")
    ):
        raise AppError("INVALID_ENDPOINT", "Management endpoint must be a credential-free HTTP(S) origin.", 422)
    netloc = parsed.hostname.lower()
    if parsed.port:
        netloc += f":{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _require(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AppError("INVALID_REQUEST", f"{key} is required.", 422, field=key)
    return value.strip()

