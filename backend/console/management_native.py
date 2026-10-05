from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response

from . import db, storage
from .security import AppError, current_user, mutation, sign_json, unsign_json, utc_add


router = APIRouter(prefix="/api/v1/management")

MAX_FILE_LIST_LIMIT = 200
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_NATIVE_BODY_BYTES = 12 * 1024 * 1024
MAX_DELETE_PREVIEW_ENTRIES = 1000
CURSOR_MAX_AGE_MINUTES = 30
MAX_JS_SAFE_INTEGER = 9_007_199_254_740_991
PROTECTED_PREFIXES = ("/.etc", "/.system", "/etc", "/topics", "/filer-store")
S3_BUCKETS_PREFIX = "/buckets"
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
S3_TABLES_NAMESPACE_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")
MODE_DIR_BIT = 1 << 31
SAFE_HEAD_HEADERS = {
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-range",
    "content-type",
    "etag",
    "expires",
    "last-modified",
    "x-seaweedfs-fid",
}
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

WORKER_READ_PATHS = {
    "status": "/api/plugin/status",
    "workers": "/api/plugin/workers",
    "jobs": "/api/plugin/jobs",
    "lanes": "/api/plugin/lanes",
    "job_types": "/api/plugin/job-types",
    "activities": "/api/plugin/activities",
    "scheduler_states": "/api/plugin/scheduler-states",
    "scheduler_status": "/api/plugin/scheduler-status",
}
MAINTENANCE_ACTIONS = {
    "config_update",
    "detect",
    "run",
    "job_execute",
    "job_expire",
}
MODULE_PATHS = {
    "s3_tables_buckets": "/api/s3tables/buckets",
    "s3_tables_namespaces": "/api/s3tables/namespaces",
    "s3_tables_tables": "/api/s3tables/tables",
}


def initialize(conn: Any) -> None:
    from . import management, management_ops

    management.initialize(conn)
    management_ops.initialize(conn)


def _management_module():
    from . import management

    return management


def _management_ops_module():
    from . import management_ops

    return management_ops


def _require_management(request: Request, management_id: str, *, write: bool = False) -> dict[str, Any]:
    return dict(_management_module().require_management(request, management_id, write=write))


def _full_management(settings: Any, management_id: str) -> dict[str, Any]:
    with db.connect(settings) as conn:
        return dict(_management_module().get_management(conn, management_id))


def _endpoint(management: Mapping[str, Any], name: str) -> str:
    endpoints = management.get("endpoints") or {}
    if isinstance(endpoints, str):
        endpoints = json.loads(endpoints or "{}")
    endpoint = endpoints.get(name)
    if not endpoint:
        raise AppError("MANAGEMENT_ENDPOINT_NOT_CONFIGURED", f"{name} endpoint is not configured.", 404)
    return str(endpoint)


def _permissions(management: Mapping[str, Any]) -> dict[str, Any]:
    permissions = management.get("permissions") or {}
    if isinstance(permissions, str):
        permissions = json.loads(permissions or "{}")
    return dict(permissions)


def _canonical_path(raw: str | None, *, allow_root: bool = True) -> str:
    if raw is None or raw == "":
        if allow_root:
            return "/"
        raise AppError("INVALID_PATH", "path is required.", 422)
    if "\x00" in raw or "\\" in raw:
        raise AppError("INVALID_PATH", "path must be a canonical POSIX Filer path.", 422)
    value = raw.strip()
    if not value.startswith("/"):
        raise AppError("INVALID_PATH", "path must start with /.", 422)
    parts = [part for part in value.split("/") if part not in ("", ".")]
    for part in parts:
        decoded_once = unquote(part)
        decoded_twice = unquote(decoded_once)
        if part == ".." or decoded_once in {".", ".."} or decoded_twice in {".", ".."} or "/" in decoded_twice or "\\" in decoded_twice:
            raise AppError("INVALID_PATH", "path traversal is not allowed.", 422)
    clean = "/" + "/".join(parts)
    return "/" if clean == "/" else clean.rstrip("/")


def _is_under(path: str, root: str) -> bool:
    root = _canonical_path(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _protected(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


def _s3_bytes_path(path: str) -> bool:
    return path == S3_BUCKETS_PREFIX or path.startswith(S3_BUCKETS_PREFIX + "/")


def _require_root(management: Mapping[str, Any], path: str, key: str) -> None:
    roots = _permissions(management).get(key, [])
    if not _path_allowed(management, path, key):
        raise AppError("MANAGEMENT_PATH_FORBIDDEN", "path is outside the approved Filer roots.", 403)
    if _protected(path):
        raise AppError("MANAGEMENT_PATH_FORBIDDEN", "protected Filer system paths are not exposed.", 403)


def _path_allowed(management: Mapping[str, Any], path: str, key: str) -> bool:
    roots = _permissions(management).get(key, [])
    return isinstance(roots, list) and any(isinstance(root, str) and _is_under(path, root) for root in roots)


def _require_write_root(management: Mapping[str, Any], path: str) -> None:
    _require_root(management, path, "file.write_roots")
    if _s3_bytes_path(path):
        raise AppError("MANAGEMENT_S3_BYTES_FORBIDDEN", "raw byte writes under /buckets are blocked; use S3-scoped APIs.", 403)


def _file_cursor(settings: Any, management_id: str, path: str, cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        payload = unsign_json(cursor, settings.require_cursor_key(), "mgmt-files:v1")
    except ValueError as exc:
        raise AppError("INVALID_CURSOR", "cursor is malformed or has an invalid signature.", 400) from exc
    if payload.get("management_id") != management_id or payload.get("path") != path:
        raise AppError("INVALID_CURSOR", "cursor does not match this directory.", 400)
    expires_at = payload.get("expires_at")
    try:
        expires = datetime.strptime(str(expires_at), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_CURSOR", "cursor expiry is invalid.", 400) from exc
    if expires <= datetime.now(timezone.utc):
        raise AppError("INVALID_CURSOR", "cursor has expired.", 400)
    return payload.get("lastFileName")


def _sign_file_cursor(settings: Any, management_id: str, path: str, last_name: str | None) -> str | None:
    if not last_name:
        return None
    return sign_json(
        {"management_id": management_id, "path": path, "lastFileName": last_name, "expires_at": utc_add(CURSOR_MAX_AGE_MINUTES * 60)},
        settings.require_cursor_key(),
        "mgmt-files:v1",
    )


def _native_request(
    base_url: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    max_bytes: int = MAX_NATIVE_BODY_BYTES,
) -> httpx.Response:
    if "\x00" in path or "\r" in path or "\n" in path or "\\" in path or not path.startswith("/") or any(part == ".." for part in path.split("/")):
        raise AppError("NATIVE_ENDPOINT_DENIED", "native endpoint path is invalid.", 403)
    request_path = quote(path, safe="/")
    transport = getattr(_management_module(), "_TEST_TRANSPORT", None)
    try:
        with httpx.Client(base_url=base_url, trust_env=False, follow_redirects=False, timeout=8.0, transport=transport) as client:
            with client.stream(method, request_path, params=params, headers=headers or {}, content=content) as response:
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise AppError("NATIVE_RESPONSE_TOO_LARGE", "native endpoint response exceeded the size limit.", 413)
                    chunks.append(chunk)
                response = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=b"".join(chunks),
                    request=response.request,
                    extensions=response.extensions,
                )
    except AppError:
        raise
    except httpx.TimeoutException as exc:
        raise AppError("NATIVE_TIMEOUT", "native endpoint timed out.", 504, retryable=True) from exc
    except httpx.TransportError as exc:
        raise AppError("NATIVE_UNREACHABLE", "native endpoint is unreachable.", 503, retryable=True) from exc
    if response.status_code == 404:
        raise AppError("NATIVE_NOT_FOUND", "native resource was not found.", 404)
    if response.status_code in (401, 403):
        raise AppError("NATIVE_FORBIDDEN", "native endpoint denied the request.", 502)
    if not 200 <= response.status_code < 400:
        raise AppError("NATIVE_UPSTREAM_ERROR", "native endpoint returned an error.", 502, detail={"status": response.status_code})
    return response


def _native_exists(base_url: str, path: str) -> bool:
    try:
        _native_request(base_url, "HEAD", path)
        return True
    except AppError as exc:
        if exc.code == "NATIVE_NOT_FOUND":
            return False
        if exc.code == "NATIVE_UPSTREAM_ERROR":
            data = _native_directory(base_url, path)
            return data is not None
        raise


def _native_directory(base_url: str, path: str) -> dict[str, Any] | None:
    try:
        data = _native_json(base_url, path, {"limit": 1, "pretty": "y"})
    except AppError as exc:
        if exc.code in {"NATIVE_NOT_FOUND", "NATIVE_BAD_RESPONSE"}:
            return None
        raise
    return data if isinstance(data, dict) and ("Entries" in data or "Path" in data) else None


def _native_json(base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
    response = _native_request(base_url, "GET", path, params=params, headers={"Accept": "application/json"}, max_bytes=8 * 1024 * 1024)
    if len(response.content) > 8 * 1024 * 1024:
        raise AppError("NATIVE_RESPONSE_TOO_LARGE", "native JSON response exceeded the size limit.", 502)
    if "json" not in response.headers.get("content-type", "").lower():
        raise AppError("NATIVE_BAD_RESPONSE", "native endpoint did not return JSON.", 502)
    try:
        return response.json()
    except ValueError as exc:
        raise AppError("NATIVE_BAD_RESPONSE", "native endpoint returned malformed JSON.", 502) from exc


def _optional(label: str, fetch: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"status": "supported", "data": fetch()}
    except AppError as exc:
        return {"status": _status_from_error(exc), "data": None, "error_code": exc.code, "label": label}


def _status_from_error(exc: AppError) -> str:
    return {
        "ADMIN_ENDPOINT_NOT_FOUND": "not_configured",
        "ADMIN_ENDPOINT_UNSUPPORTED": "unsupported",
        "ADMIN_BAD_RESPONSE": "stub",
        "ADMIN_TIMEOUT": "unreachable",
        "ADMIN_UNREACHABLE": "unreachable",
        "NATIVE_NOT_FOUND": "not_configured",
        "NATIVE_BAD_RESPONSE": "stub",
        "NATIVE_TIMEOUT": "unreachable",
        "NATIVE_UNREACHABLE": "unreachable",
    }.get(exc.code, "unknown")


def _admin_request_json(settings: Any, management: Mapping[str, Any], method: str, path: str, payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
    mgmt = _full_management(settings, str(management["id"]))
    module = _management_module()
    with db.connect(settings) as conn:
        client = module.admin_client(settings, conn, mgmt)
    try:
        if getattr(client, "_csrf", None) is None:
            client._login()
        headers = {"Accept": "application/json"}
        if method.upper() != "GET":
            headers["X-CSRF-Token"] = client._csrf
            headers["Origin"] = client.base_url
        response = client._request(method.upper(), path, headers=headers, json=payload, params=params)
        return module.redact(client._json_response(response))
    finally:
        client.close()


def _admin_upload_file(settings: Any, management: Mapping[str, Any], parent: str, filename: str, data: bytes, content_type: str | None) -> Any:
    mgmt = _full_management(settings, str(management["id"]))
    module = _management_module()
    with db.connect(settings) as conn:
        client = module.admin_client(settings, conn, mgmt)
    try:
        if getattr(client, "_csrf", None) is None:
            client._login()
        headers = {"Accept": "application/json", "X-CSRF-Token": client._csrf, "Origin": client.base_url}
        response = client._request(
            "POST",
            "/api/files/upload",
            headers=headers,
            data={"path": parent},
            files={"files": (filename, data, content_type or "application/octet-stream")},
        )
        return module.redact(client._json_response(response))
    finally:
        client.close()


def _segment(value: str, field: str) -> str:
    if not SEGMENT_RE.match(value):
        raise AppError("INVALID_REQUEST", f"{field} contains unsupported characters.", 422, field=field)
    return value


def _current_job_types(settings: Any, management: Mapping[str, Any]) -> set[str]:
    data = _admin_request_json(settings, management, "GET", "/api/plugin/job-types")
    if isinstance(data, dict):
        rows = data.get("job_types")
        if rows is None:
            rows = data.get("items")
        if not isinstance(rows, list):
            raise AppError("MANAGEMENT_JOB_TYPE_REGISTRY_UNKNOWN", "upstream job type registry did not return a job_types list.", 502)
    elif isinstance(data, list):
        rows = data
    else:
        raise AppError("MANAGEMENT_JOB_TYPE_REGISTRY_UNKNOWN", "upstream job type registry returned an unsupported shape.", 502)
    result = set()
    for row in rows:
        if isinstance(row, str):
            result.add(row)
        elif isinstance(row, dict):
            value = row.get("job_type") or row.get("type") or row.get("name")
            if value:
                result.add(str(value))
    if not result:
        raise AppError("MANAGEMENT_JOB_TYPE_REGISTRY_EMPTY", "upstream job type registry did not report usable job types.", 502)
    return result


def _ensure_plugin_enabled(settings: Any, management: Mapping[str, Any]) -> None:
    data = _admin_request_json(settings, management, "GET", "/api/plugin/status")
    if not isinstance(data, dict) or data.get("enabled") is not True:
        raise AppError("MANAGEMENT_PLUGIN_DISABLED", "maintenance plugin is not enabled upstream.", 409)


def _require_optional_dict(body: Mapping[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    if key not in body:
        if required:
            raise AppError("INVALID_REQUEST", f"{key} is required.", 422, field=key)
        return {}
    value = body.get(key)
    if not isinstance(value, dict):
        raise AppError("INVALID_REQUEST", f"{key} must be an object.", 422, field=key)
    return dict(value)


def _volume_rows(data: Any) -> list[dict[str, Any]]:
    return _management_module()._flatten_volumes(data)


def _find_volume(rows: list[dict[str, Any]], volume_id: str, server_id: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("id")) == str(volume_id) and str(row.get("server")) == str(server_id):
            return row
    return None


def _delete_preview_confirmed(body: Mapping[str, Any], path: str, preview: Mapping[str, Any] | None) -> bool:
    if preview is None:
        return True
    preview_path = body.get("recursive_preview_path")
    preview_hash = body.get("recursive_preview_hash")
    return body.get("confirm_recursive") is True and preview_path == path and preview_hash == preview.get("preview_hash") and preview.get("complete") is True


def _directory_preview_hash(directory: Mapping[str, Any]) -> str:
    path = _canonical_path(str(directory.get("Path") or directory.get("path") or "/"))
    entries = directory.get("Entries") if isinstance(directory.get("Entries"), list) else []
    summary = []
    for entry in entries:
        item = _safe_entry(entry, path)
        if item is not None:
            summary.append(
                {
                    "name": item["name"],
                    "full_path": item["full_path"],
                    "is_directory": item["is_directory"],
                    "size": item["size"],
                    "mtime": item["mtime"],
                }
            )
    summary.sort(key=lambda item: (item["full_path"], item["name"]))
    return hashlib.sha256(json.dumps({"path": path, "entries": summary}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _delete_preview_hash(path: str, items: list[Mapping[str, Any]], *, complete: bool) -> str:
    stable_items = [
        {
            "full_path": item.get("full_path"),
            "is_directory": item.get("is_directory"),
            "name": item.get("name"),
            "size": item.get("size"),
            "mtime": item.get("mtime"),
        }
        for item in items
    ]
    stable_items.sort(key=lambda item: (str(item.get("full_path") or ""), str(item.get("name") or "")))
    return hashlib.sha256(json.dumps({"path": path, "complete": complete, "items": stable_items}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _delete_preview(base_url: str, path: str) -> dict[str, Any] | None:
    directory = _native_directory(base_url, path)
    if directory is None:
        return None
    stack = [path]
    items: list[dict[str, Any]] = []
    truncated = False
    seen_dirs: set[str] = set()
    while stack and not truncated:
        current = stack.pop()
        if current in seen_dirs:
            continue
        seen_dirs.add(current)
        last = ""
        while True:
            payload = _native_json(base_url, current, {"limit": MAX_FILE_LIST_LIMIT, "pretty": "y", "lastFileName": last})
            entries = payload.get("Entries") if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                entries = []
            for entry in entries:
                item = _safe_entry(entry, current)
                if item is None:
                    continue
                items.append(item)
                if len(items) > MAX_DELETE_PREVIEW_ENTRIES:
                    truncated = True
                    break
                if item["is_directory"]:
                    full_path = str(item["full_path"])
                    if _protected(full_path) or _s3_bytes_path(full_path):
                        truncated = True
                        break
                    stack.append(full_path)
            if truncated:
                break
            if not isinstance(payload, dict) or not payload.get("ShouldDisplayLoadMore"):
                break
            next_name = payload.get("LastFileName") or payload.get("lastFileName")
            if not next_name or str(next_name) == last:
                truncated = True
                break
            last = str(next_name)
    complete = not truncated
    return {
        "path": path,
        "items": items,
        "entry_count": len(items),
        "truncated": truncated,
        "complete": complete,
        "preview_hash": _delete_preview_hash(path, items, complete=complete),
        "cas_atomic": False,
    }


def _safe_head_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items() if key.lower() in SAFE_HEAD_HEADERS}


def _file_sha256_if_readable(base_url: str, management: Mapping[str, Any], path: str) -> str | None:
    if not _path_allowed(management, path, "file.read_roots"):
        return None
    data = _native_request(base_url, "GET", path, max_bytes=MAX_DOWNLOAD_BYTES).content
    return hashlib.sha256(data).hexdigest()


def _upload_readback(base_url: str, path: str, *, expected_size: int, can_read_bytes: bool) -> dict[str, Any]:
    try:
        response = _native_request(base_url, "HEAD", path)
    except AppError as exc:
        if exc.code == "NATIVE_NOT_FOUND":
            return {"exists": False, "headers": {}}
        raise
    headers = {key.lower(): value for key, value in response.headers.items()}
    result = {"exists": True, "headers": headers, "sha256": None, "content_verified": False}
    if not can_read_bytes:
        result["reason"] = "file.read_roots does not include the upload target"
        return result
    length = headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_DOWNLOAD_BYTES:
                result["reason"] = "readback exceeded the management byte limit"
                return result
        except ValueError:
            result["reason"] = "readback content-length is invalid"
            return result
    if expected_size > MAX_DOWNLOAD_BYTES:
        result["reason"] = "expected upload size exceeds readback byte limit"
        return result
    data = _native_request(base_url, "GET", path, max_bytes=MAX_DOWNLOAD_BYTES).content
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise AppError("NATIVE_RESPONSE_TOO_LARGE", "upload readback exceeded the management byte limit.", 413)
    result["sha256"] = hashlib.sha256(data).hexdigest()
    result["content_verified"] = True
    return result


def _contains_subset(container: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(container, dict):
            return False
        return all(key in container and _contains_subset(container[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(container, list) or len(container) < len(expected):
            return False
        return all(_contains_subset(container[index], value) for index, value in enumerate(expected))
    return container == expected


def _json_contains(value: Any, needle: str) -> bool:
    return needle in json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _changed(before: Any, after: Any) -> bool:
    return json.dumps(before, sort_keys=True, separators=(",", ":"), ensure_ascii=False) != json.dumps(after, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


SUCCESS_STATES = {"accepted", "queued", "running", "started", "completed", "complete", "success", "succeeded"}
EXPIRE_STATES = {"expired", "cancelled", "canceled"}
FAIL_STATES = {"failed", "failure", "error", "errored", "cancelled", "canceled"}


def _state_of(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("status") or value.get("state") or value.get("result") or "").lower()


def _job_identifier(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = value.get("job_id") or value.get("jobId") or value.get("id")
    if direct:
        return str(direct)
    nested = value.get("job")
    if isinstance(nested, dict):
        return str(nested.get("job_id") or nested.get("jobId") or nested.get("id") or "")
    return ""


def _job_type_of(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = value.get("job_type") or value.get("jobType") or value.get("type")
    if direct:
        return str(direct)
    nested = value.get("job")
    if isinstance(nested, dict):
        return str(nested.get("job_type") or nested.get("jobType") or nested.get("type") or "")
    return ""


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _job_with_state(data: Any, *, job_id: str = "", job_type: str = "", states: set[str] | None = None) -> bool:
    states = states or SUCCESS_STATES
    for item in _walk_dicts(data):
        if job_id and _job_identifier(item) != job_id:
            continue
        if job_type and _job_type_of(item) != job_type:
            continue
        state = _state_of(item)
        if state in states:
            return True
    return False


def _job_ids_with_state(data: Any, *, job_type: str = "", states: set[str] | None = None) -> set[str]:
    states = states or SUCCESS_STATES
    result: set[str] = set()
    for item in _walk_dicts(data):
        if job_type and _job_type_of(item) != job_type:
            continue
        if _state_of(item) not in states:
            continue
        identifier = _job_identifier(item)
        if identifier:
            result.add(identifier)
    return result


def _activity_with_request_state(data: Any, request_id: str, states: set[str] | None = None) -> bool:
    states = states or SUCCESS_STATES
    for item in _walk_dicts(data):
        if str(item.get("request_id") or item.get("requestId") or "") == request_id and _state_of(item) in states:
            return True
    return False


def _response_success_state(response: Mapping[str, Any]) -> bool:
    state = _state_of(response)
    return bool(state and state in SUCCESS_STATES and state not in FAIL_STATES)


def _verify_maintenance(action: str, payload: Mapping[str, Any], before: Any, after: Any, response: Any, *, job_type: str | None = None, job_id: str | None = None) -> bool:
    if not isinstance(response, dict):
        return False
    if action == "config_update":
        return _contains_subset(after, payload)
    if action == "detect":
        request_id = str(response.get("request_id") or "")
        return bool(request_id and _activity_with_request_state(after, request_id, states=SUCCESS_STATES))
    if action == "run":
        executed = int(response.get("executed_count") or 0)
        success = int(response.get("success_count") or 0)
        errors = int(response.get("error_count") or 0)
        canceled = int(response.get("canceled_count") or 0)
        if not (job_type and executed > 0 and success > 0 and errors == 0 and canceled == 0):
            return False
        response_job_id = _job_identifier(response)
        if response_job_id:
            return _job_with_state(after, job_id=response_job_id, job_type=job_type, states=SUCCESS_STATES)
        before_ids = _job_ids_with_state(before, job_type=job_type, states=SUCCESS_STATES)
        after_ids = _job_ids_with_state(after, job_type=job_type, states=SUCCESS_STATES)
        return bool(after_ids - before_ids)
    if action == "job_execute":
        response_job_id = _job_identifier(response) or _job_identifier(payload) or (job_id or "")
        return bool(response_job_id and _response_success_state(response) and _job_with_state(after, job_id=response_job_id, states=SUCCESS_STATES))
    if action == "job_expire":
        response_job_id = str(response.get("job_id") or response.get("jobId") or job_id or "")
        return bool(response.get("expired") is True and response_job_id and _job_with_state(after, job_id=response_job_id, states=EXPIRE_STATES | {"failed"}))
    return False


def _required_text(body: Mapping[str, Any], key: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise AppError("INVALID_REQUEST", f"{key} is required.", 422, field=key)
    return value


def _topic_matches(data: Any, namespace: str, topic: str) -> bool:
    if isinstance(data, dict) and "data" in data and "status" in data:
        data = data.get("data")
    if not isinstance(data, dict):
        return False
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return namespace in text and topic in text


def _topic_retention_matches(data: Any, retention: Mapping[str, Any]) -> bool:
    if isinstance(data, dict) and "data" in data and "status" in data:
        data = data.get("data")
    if not isinstance(data, dict):
        return False
    enabled = bool(retention.get("enabled"))
    seconds = int(retention.get("retention_seconds") or 0)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).lower()
    if enabled and str(seconds) not in text:
        return False
    return ("retention" in text) or ("retention_seconds" in text)


def _list_field(data: Any, *names: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for name in names:
        value = data.get(name)
        if isinstance(value, list):
            return value
    return []


def _s3_tables_bucket_exists(data: Any, bucket_arn: str | None = None, name: str | None = None) -> bool:
    needle = bucket_arn or name or ""
    if not needle:
        return False
    return any(needle in json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for item in _list_field(data, "buckets", "items", "table_buckets"))


def _s3_tables_namespace_exists(data: Any, namespace: str) -> bool:
    return any(namespace in json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for item in _list_field(data, "namespaces", "items"))


def _s3_tables_table(data: Any, name: str) -> Any | None:
    for item in _list_field(data, "tables", "items"):
        if name in json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False):
            return item
    return None


def _s3_table_empty(table: Any) -> bool:
    if table is None:
        return False
    if not isinstance(table, dict):
        return False
    for key in ("row_count", "rows", "observed_row_count", "record_count", "records"):
        value = table.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value) == 0
        except (TypeError, ValueError):
            return str(value).lower() in {"0", "zero", "empty"}
    return False


def _policy_matches(data: Any, policy: str) -> bool:
    return isinstance(data, dict) and data.get("policy") == policy


def _tags_include(data: Any, tags: Mapping[str, str]) -> bool:
    if not isinstance(data, dict):
        return False
    observed = data.get("tags") or data.get("Tags") or data
    return isinstance(observed, dict) and all(observed.get(key) == value for key, value in tags.items())


def _s3_tables_namespace_parts(namespace: str) -> list[str]:
    value = namespace.strip()
    if not value:
        raise AppError("INVALID_REQUEST", "namespace is required.", 422, field="namespace")
    parts = value.split(".")
    for part in parts:
        lowered = part.lower()
        if (
            not S3_TABLES_NAMESPACE_PART_RE.match(part)
            or lowered in {".", ".."}
            or lowered.startswith("aws")
            or part.endswith(("-", "_"))
        ):
            raise AppError("INVALID_REQUEST", "namespace is invalid for S3 Tables.", 422, field="namespace")
    return parts


def _s3_tables_connection(settings: Any, management: Mapping[str, Any]) -> dict[str, Any]:
    management_id = str(management["id"])
    s3_connection_id = management.get("s3_connection_id")
    if not s3_connection_id:
        raise AppError("S3_TABLES_NOT_CONFIGURED", "management connection has no associated S3 connection.", 404)
    endpoints = management.get("endpoints") or {}
    if isinstance(endpoints, str):
        endpoints = json.loads(endpoints or "{}")
    registry_endpoint = endpoints.get("s3")
    if not registry_endpoint:
        raise AppError("S3_TABLES_NOT_CONFIGURED", "approved S3 endpoint is not configured for this management connection.", 404)
    with db.connect(settings) as conn:
        row = conn.execute("SELECT * FROM storage_connections WHERE id=?", (s3_connection_id,)).fetchone()
    if not row:
        raise AppError("S3_TABLES_NOT_CONFIGURED", "associated S3 connection was not found.", 404)
    connection = dict(row)
    connection["verify_tls"] = bool(connection.get("verify_tls", True))
    normalize = _management_module()._normalize_endpoint
    if normalize(str(registry_endpoint)) != normalize(str(connection.get("endpoint_url") or "")):
        raise AppError("S3_TABLES_NOT_CONFIGURED", "approved S3 endpoint does not match the associated S3 connection.", 404)
    try:
        connection["_secret"] = storage.secret_for_connection(settings, connection)
    except storage.StorageError as exc:
        raise AppError(exc.code, str(exc), exc.status) from exc
    return connection


def _s3_tables_status(exc: AppError) -> str:
    if exc.status == 404 or exc.code in {"S3_TABLES_NOT_CONFIGURED"}:
        return "not_configured" if exc.code == "S3_TABLES_NOT_CONFIGURED" else "not_found"
    if exc.status in {401, 403}:
        return "permission_denied"
    if exc.status in {405, 501} or exc.code.endswith("UNSUPPORTED"):
        return "unsupported"
    if exc.code in {"S3_TABLES_TIMEOUT", "S3_TABLES_UNREACHABLE"}:
        return "unknown"
    return "unknown"


def _s3_tables_error_response(management_id: str, exc: AppError, *, status_code: int | None = None) -> dict[str, Any]:
    return {
        "connection_id": management_id,
        "source": "native_s3tables_get_table",
        "status": _s3_tables_status(exc),
        "details": None,
        "error_code": exc.code,
        "upstream_status": status_code,
    }


def _s3_tables_target_request(settings: Any, management: Mapping[str, Any], operation: str, payload: Mapping[str, Any]) -> Any:
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
    except ImportError as exc:
        raise AppError("S3_TABLES_UNSUPPORTED", "botocore S3 Tables signing support is unavailable.", 503) from exc

    connection = _s3_tables_connection(settings, management)
    secret = connection["_secret"]
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    endpoint = str(connection["endpoint_url"]).rstrip("/")
    url = endpoint + "/"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": f"S3Tables.{operation}",
    }
    credentials = Credentials(
        str(secret["aws_access_key_id"]),
        str(secret["aws_secret_access_key"]),
        str(secret.get("aws_session_token") or "") or None,
    )
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "s3tables", connection.get("region") or "us-east-1").add_auth(aws_request)
    signed_headers = dict(aws_request.headers.items())
    transport = getattr(_management_module(), "_TEST_TRANSPORT", None)
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=8.0, transport=transport, verify=connection.get("verify_tls", True)) as client:
            with client.stream("POST", url, headers=signed_headers, content=body) as response:
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_NATIVE_BODY_BYTES:
                        raise AppError("S3_TABLES_RESPONSE_TOO_LARGE", "S3 Tables response exceeded the size limit.", 413)
                    chunks.append(chunk)
                response = httpx.Response(response.status_code, headers=response.headers, content=b"".join(chunks), request=response.request, extensions=response.extensions)
    except AppError:
        raise
    except httpx.TimeoutException as exc:
        raise AppError("S3_TABLES_TIMEOUT", "S3 Tables endpoint timed out.", 504, retryable=True) from exc
    except httpx.TransportError as exc:
        raise AppError("S3_TABLES_UNREACHABLE", "S3 Tables endpoint is unreachable.", 503, retryable=True) from exc
    if response.status_code in {401, 403}:
        raise AppError("S3_TABLES_PERMISSION_DENIED", "S3 Tables endpoint denied the request.", response.status_code)
    if response.status_code == 404:
        raise AppError("S3_TABLES_TABLE_NOT_FOUND", "S3 Tables table was not found.", 404)
    if response.status_code in {405, 501}:
        raise AppError("S3_TABLES_UNSUPPORTED", "S3 Tables operation is unsupported by this endpoint.", response.status_code)
    if not 200 <= response.status_code < 300:
        raise AppError("S3_TABLES_UPSTREAM_ERROR", "S3 Tables endpoint returned an error.", 502, detail={"status": response.status_code})
    if "json" not in response.headers.get("content-type", "").lower():
        raise AppError("S3_TABLES_BAD_RESPONSE", "S3 Tables endpoint did not return JSON.", 502)
    try:
        return response.json()
    except ValueError as exc:
        raise AppError("S3_TABLES_BAD_RESPONSE", "S3 Tables endpoint returned malformed JSON.", 502) from exc


def _s3_tables_redact(data: Any) -> Any:
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            lowered = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            parts = set(filter(None, lowered.split("_")))
            if lowered in SENSITIVE_KEYS or {"secret", "password", "token", "credential", "signature", "auth"} & parts or "secret" in lowered:
                out[key] = "[REDACTED]"
            else:
                out[key] = _s3_tables_redact(value)
        return out
    if isinstance(data, list):
        return [_s3_tables_redact(item) for item in data]
    if isinstance(data, str):
        return _redact_metadata_url(data)
    return data


def _redact_metadata_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"[REDACTED]@{host}"
    query = []
    changed = "@" in parsed.netloc
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SIGNED_QUERY_KEYS or lowered.startswith("x-amz-") or "secret" in lowered or "token" in lowered:
            query.append((key, "[REDACTED]"))
            changed = True
        else:
            query.append((key, val))
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), "")) if changed else value


def _parse_full_metadata(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _json_safe_ints(data: Any) -> Any:
    if isinstance(data, bool):
        return data
    if isinstance(data, int):
        return str(data) if abs(data) > MAX_JS_SAFE_INTEGER else data
    if isinstance(data, dict):
        return {key: _json_safe_ints(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_json_safe_ints(item) for item in data]
    return data


def _pick_current_schema(full: Mapping[str, Any]) -> Any:
    schemas = full.get("schemas")
    if isinstance(schemas, list):
        current = full.get("current-schema-id")
        for schema in schemas:
            if isinstance(schema, dict) and schema.get("schema-id") == current:
                return schema.get("fields")
        for schema in schemas:
            if isinstance(schema, dict) and isinstance(schema.get("fields"), list):
                return schema.get("fields")
    schema = full.get("schema")
    if isinstance(schema, dict):
        return schema.get("fields")
    return None


def _pick_current_snapshot_summary(full: Mapping[str, Any]) -> Any:
    current = full.get("current-snapshot-id")
    snapshots = full.get("snapshots")
    if not isinstance(snapshots, list):
        return None
    for snapshot in snapshots:
        if isinstance(snapshot, dict) and snapshot.get("snapshot-id") == current:
            return snapshot.get("summary")
    return None


def _s3_tables_metadata_summary(response: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = response.get("metadata")
    if not isinstance(metadata, dict):
        return None
    iceberg = metadata.get("iceberg")
    full = _parse_full_metadata(metadata.get("fullMetadata"))
    result: dict[str, Any] = {}
    if isinstance(iceberg, dict):
        schema = iceberg.get("schema")
        if isinstance(schema, dict) and isinstance(schema.get("fields"), list):
            result["schema"] = _s3_tables_redact(schema.get("fields"))
        if iceberg.get("tableUuid"):
            result["table_uuid"] = iceberg.get("tableUuid")
    if full:
        fields = _pick_current_schema(full)
        if fields is not None:
            result["schema"] = _s3_tables_redact(fields)
        result["snapshots"] = _s3_tables_redact(full.get("snapshots") if isinstance(full.get("snapshots"), list) else [])
        result["history"] = _s3_tables_redact(full.get("snapshot-log") if isinstance(full.get("snapshot-log"), list) else [])
        result["snapshot_summary"] = _s3_tables_redact(_pick_current_snapshot_summary(full))
        result["partition_specs"] = _s3_tables_redact(full.get("partition-specs") if isinstance(full.get("partition-specs"), list) else [])
        result["properties"] = _s3_tables_redact(full.get("properties") if isinstance(full.get("properties"), dict) else {})
    return _json_safe_ints(result) if result else None


def _s3_tables_detail_payload(bucket_arn: str, namespace: str, name: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AppError("S3_TABLES_BAD_RESPONSE", "S3 Tables GetTable response was not an object.", 502)
    table_format = str(response.get("format") or "").upper()
    catalog = {
        "bucket_arn": bucket_arn,
        "namespace": namespace,
        "name": response.get("name") or name,
        "tableARN": response.get("tableARN"),
        "versionToken": response.get("versionToken"),
        "metadataLocation": _s3_tables_redact(response.get("metadataLocation")),
        "metadataVersion": response.get("metadataVersion"),
        "format": response.get("format"),
        "createdAt": response.get("createdAt"),
        "modifiedAt": response.get("modifiedAt"),
        "ownerAccountId": response.get("ownerAccountId"),
        "warehouseLocation": _s3_tables_redact(response.get("warehouseLocation")),
    }
    details = {**catalog, "catalog": catalog, "metadata": None, "metadata_supported": table_format != "LANCE", "needs_scope_preview": bool(response.get("metadataLocation"))}
    if table_format == "LANCE":
        details["metadata_note"] = "LANCE is catalog-only here; data and schema parsing require a format-aware worker."
    else:
        details["metadata"] = _s3_tables_metadata_summary(response)
    return _json_safe_ints(details)


def _safe_entry(entry: Any, parent: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    raw_path = entry.get("FullPath") or entry.get("full_path")
    raw_name = entry.get("Name") or entry.get("name")
    if raw_path:
        try:
            full_path = _canonical_path(str(raw_path))
        except AppError:
            return None
    elif raw_name:
        try:
            full_path = _canonical_path((parent.rstrip("/") + "/" + str(raw_name)) if parent != "/" else "/" + str(raw_name))
        except AppError:
            return None
    else:
        return None
    name = raw_name or full_path.rstrip("/").rsplit("/", 1)[-1] or "/"
    mode = entry.get("Mode") if "Mode" in entry else entry.get("mode")
    try:
        mode_int = int(mode) if mode is not None else 0
    except (TypeError, ValueError):
        mode_int = 0
    attributes = entry.get("Attributes") if isinstance(entry.get("Attributes"), dict) else entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
    is_directory = bool(entry.get("IsDirectory") or entry.get("is_directory") or (mode_int & MODE_DIR_BIT))
    size = entry.get("FileSize", entry.get("file_size", attributes.get("FileSize", attributes.get("file_size"))))
    mtime = entry.get("Mtime", entry.get("mtime", attributes.get("Mtime", attributes.get("mtime"))))
    safe_name = str(name)
    return {
        "name": safe_name,
        "Name": safe_name,
        "full_path": full_path,
        "FullPath": full_path,
        "is_directory": is_directory,
        "IsDirectory": is_directory,
        "mode": mode_int if mode is not None else None,
        "size": size,
        "FileSize": size,
        "mtime": mtime,
        "Mtime": mtime,
        "protected": _protected(full_path),
    }


@router.get("/{management_id}/files")
def list_files(request: Request, management_id: str, path: str = "/", limit: int = 50, cursor: str | None = None):
    mgmt = _require_management(request, management_id)
    clean = _canonical_path(path)
    _require_root(mgmt, clean, "file.read_roots")
    limit = max(1, min(int(limit or 50), MAX_FILE_LIST_LIMIT))
    last = _file_cursor(request.app.state.settings, management_id, clean, cursor)
    payload = _native_json(_endpoint(mgmt, "filer"), clean, {"limit": limit, "pretty": "y", "lastFileName": last or ""})
    entries = payload.get("Entries") if isinstance(payload, dict) else []
    safe_entries = []
    if isinstance(entries, list):
        for entry in entries:
            item = _safe_entry(entry, clean)
            if item is not None:
                item.setdefault("Name", item["name"])
                item.setdefault("FullPath", item["full_path"])
                item.setdefault("IsDirectory", item["is_directory"])
                safe_entries.append(item)
    next_name = None
    if isinstance(payload, dict):
        should_display_more = payload.get("ShouldDisplayLoadMore") is True or payload.get("shouldDisplayLoadMore") is True
        if should_display_more:
            upstream_last_name = payload.get("LastFileName") or payload.get("lastFileName")
            next_name = str(upstream_last_name or (safe_entries[-1].get("name") if safe_entries else "") or "")
    return {
        "connection_id": management_id,
        "path": clean,
        "limit": limit,
        "source": "native_filer_http_json",
        "items": safe_entries,
        "page_hash": _directory_preview_hash(payload) if isinstance(payload, dict) else None,
        "raw": {k: v for k, v in payload.items() if k != "Entries"} if isinstance(payload, dict) else None,
        "next_cursor": _sign_file_cursor(request.app.state.settings, management_id, clean, next_name),
    }


@router.get("/{management_id}/files/delete-preview")
def file_delete_preview(request: Request, management_id: str, path: str):
    mgmt = _require_management(request, management_id)
    clean = _canonical_path(path, allow_root=False)
    _require_root(mgmt, clean, "file.read_roots")
    if _s3_bytes_path(clean):
        raise AppError("MANAGEMENT_S3_BYTES_FORBIDDEN", "raw Filer deletes under /buckets are blocked; use S3-scoped APIs.", 403)
    preview = _delete_preview(_endpoint(mgmt, "filer"), clean)
    if preview is None:
        raise AppError("MANAGEMENT_NOT_DIRECTORY", "delete preview is only available for Filer directories.", 422, field="path")
    return preview


@router.get("/{management_id}/files/properties")
def file_properties(request: Request, management_id: str, path: str):
    mgmt = _require_management(request, management_id)
    clean = _canonical_path(path, allow_root=False)
    _require_root(mgmt, clean, "file.read_roots")
    if _s3_bytes_path(clean):
        return {"path": clean, "source": "filer_metadata_only", "byte_download_supported": False, "reason": "use S3-scoped APIs for /buckets byte content"}
    response = _native_request(_endpoint(mgmt, "filer"), "HEAD", clean)
    return {"path": clean, "headers": _safe_head_headers(response.headers), "source": "native_filer_http_head"}


@router.get("/{management_id}/files/download")
def download_file(request: Request, management_id: str, path: str):
    mgmt = _require_management(request, management_id)
    clean = _canonical_path(path, allow_root=False)
    _require_root(mgmt, clean, "file.read_roots")
    if _s3_bytes_path(clean):
        raise AppError("MANAGEMENT_S3_BYTES_FORBIDDEN", "raw byte download under /buckets is blocked; use S3-scoped APIs.", 403)
    response = _native_request(_endpoint(mgmt, "filer"), "GET", clean, max_bytes=MAX_DOWNLOAD_BYTES)
    if len(response.content) > MAX_DOWNLOAD_BYTES:
        raise AppError("NATIVE_RESPONSE_TOO_LARGE", "download exceeded the management byte limit.", 413)
    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "no-store", "Content-Disposition": "attachment; filename*=UTF-8''" + quote(clean.rsplit("/", 1)[-1])},
    )


@router.post("/{management_id}/files/mkdir")
def mkdir_file(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    parent = _canonical_path(str(body.get("path") or "/"))
    name = str(body.get("folder_name") or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise AppError("INVALID_REQUEST", "folder_name is invalid.", 422, field="folder_name")
    target = _canonical_path(parent.rstrip("/") + "/" + name)
    _require_write_root(mgmt, target)
    filer = _endpoint(mgmt, "filer")

    def invoke() -> Any:
        if _native_exists(filer, target):
            raise AppError("MANAGEMENT_FILE_EXISTS", "target already exists; SeaweedFS Admin does not expose an atomic no-overwrite mkdir contract.", 409)
        return _admin_request_json(request.app.state.settings, mgmt, "POST", "/api/files/create-folder", {"path": parent, "folder_name": name})

    return _management_ops_module().perform_operation(
        request.app.state.settings,
        mgmt,
        user.id,
        "file.mkdir",
        "POST",
        "/api/files/create-folder",
        {"path": parent, "folder_name": name},
        idempotency_key=body.get("idempotency_key"),
        invoke=invoke,
        readback=lambda: _native_exists(filer, target),
        verify=lambda before, after, response: bool(response) and before is False and after is True,
    )


@router.post("/{management_id}/files/delete")
def delete_file(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    clean = _canonical_path(str(body.get("path") or ""), allow_root=False)
    _require_write_root(mgmt, clean)
    filer = _endpoint(mgmt, "filer")
    directory_preview = _delete_preview(filer, clean)
    if (
        directory_preview is not None
        and body.get("confirm_recursive") is True
        and body.get("recursive_preview_path") == clean
        and body.get("recursive_preview_hash") == directory_preview.get("preview_hash")
        and directory_preview.get("complete") is not True
    ):
        raise AppError("MANAGEMENT_DELETE_PREVIEW_INCOMPLETE", "directory delete preview is truncated; recursive delete is not allowed.", 409)
    if not _delete_preview_confirmed(body, clean, directory_preview):
        raise AppError("MANAGEMENT_RECURSIVE_DELETE_REQUIRES_CONFIRMATION", "directory delete requires an explicit recursive preview confirmation.", 409)
    expected_source_sha256 = str(body.get("expected_source_sha256") or "").strip().lower()

    def invoke() -> Any:
        if not _native_exists(filer, clean):
            raise AppError("NATIVE_NOT_FOUND", "native resource was not found.", 404)
        current_preview = _delete_preview(filer, clean)
        if current_preview is not None:
            if current_preview.get("complete") is not True:
                raise AppError("MANAGEMENT_DELETE_PREVIEW_INCOMPLETE", "directory delete preview is truncated; recursive delete is not allowed.", 409)
            if not _delete_preview_confirmed(body, clean, current_preview):
                raise AppError("MANAGEMENT_DELETE_PREVIEW_CHANGED", "directory delete preview no longer matches the confirmed hash.", 409)
        if expected_source_sha256:
            observed = _file_sha256_if_readable(filer, mgmt, clean)
            if observed != expected_source_sha256:
                raise AppError("MANAGEMENT_SOURCE_CHANGED", "source content hash does not match the delete precondition.", 409)
        return _admin_request_json(request.app.state.settings, mgmt, "DELETE", "/api/files/delete", {"path": clean})

    return _management_ops_module().perform_operation(
        request.app.state.settings,
        mgmt,
        user.id,
        "file.delete",
        "DELETE",
        "/api/files/delete",
        {
            "path": clean,
            "expected_source_sha256": expected_source_sha256 or None,
            "recursive_preview_path": body.get("recursive_preview_path"),
            "recursive_preview_hash": body.get("recursive_preview_hash"),
            "source_hash_cas_atomic": False,
            "recursive_preview_cas_atomic": False,
        },
        idempotency_key=body.get("idempotency_key"),
        invoke=invoke,
        readback=lambda: {"exists": _native_exists(filer, clean), "source_sha256": None if not _native_exists(filer, clean) or not expected_source_sha256 else _file_sha256_if_readable(filer, mgmt, clean)},
        verify=lambda before, after, response: bool(response)
        and before.get("exists") is True
        and after.get("exists") is False
        and (not expected_source_sha256 or before.get("source_sha256") == expected_source_sha256),
    )


@router.post("/{management_id}/files/rename")
def rename_file(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    source = _canonical_path(str(body.get("source_path") or ""), allow_root=False)
    target = _canonical_path(str(body.get("target_path") or ""), allow_root=False)
    _require_write_root(mgmt, source)
    _require_write_root(mgmt, target)
    filer = _endpoint(mgmt, "filer")
    expected_source_sha256 = str(body.get("expected_source_sha256") or "").strip().lower()

    def invoke() -> dict[str, Any]:
        if not _native_exists(filer, source):
            raise AppError("NATIVE_NOT_FOUND", "native source was not found.", 404)
        if _native_exists(filer, target):
            raise AppError("MANAGEMENT_FILE_EXISTS", "target already exists; SeaweedFS Filer mv.from has no atomic no-overwrite contract.", 409)
        if expected_source_sha256:
            observed = _file_sha256_if_readable(filer, mgmt, source)
            if observed != expected_source_sha256:
                raise AppError("MANAGEMENT_SOURCE_CHANGED", "source content hash does not match the rename precondition.", 409)
        _native_request(filer, "POST", target, params={"mv.from": source})
        return {"moved": True, "no_overwrite_atomic": False}

    def readback() -> dict[str, Any]:
        source_exists = _native_exists(filer, source)
        target_exists = _native_exists(filer, target)
        target_sha256 = _file_sha256_if_readable(filer, mgmt, target) if target_exists and expected_source_sha256 else None
        return {"source_exists": source_exists, "target_exists": target_exists, "target_sha256": target_sha256}

    return _management_ops_module().perform_operation(
        request.app.state.settings,
        mgmt,
        user.id,
        "file.rename",
        "POST",
        target,
        {"source_path": source, "target_path": target, "expected_source_sha256": expected_source_sha256 or None, "no_overwrite_atomic": False, "source_hash_cas_atomic": False},
        idempotency_key=body.get("idempotency_key"),
        invoke=invoke,
        readback=readback,
        verify=lambda before, after, response: bool(response)
        and before.get("source_exists") is True
        and before.get("target_exists") is False
        and after.get("source_exists") is False
        and after.get("target_exists") is True
        and (not expected_source_sha256 or after.get("target_sha256") == expected_source_sha256),
    )


@router.post("/{management_id}/files/upload")
async def upload_file(request: Request, management_id: str, path: str = "/", idempotency_key: str | None = None, file: UploadFile = File(...)):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    parent = _canonical_path(path)
    filename = file.filename or ""
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise AppError("INVALID_REQUEST", "filename is invalid.", 422, field="file")
    target = _canonical_path(parent.rstrip("/") + "/" + filename)
    _require_write_root(mgmt, target)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AppError("UPLOAD_TOO_LARGE", "upload exceeded the management byte limit.", 413)
    content_type = file.content_type or "application/octet-stream"
    digest = hashlib.sha256(data).hexdigest()
    filer = _endpoint(mgmt, "filer")
    can_read_bytes = _path_allowed(mgmt, target, "file.read_roots")

    def invoke() -> Any:
        if _native_exists(filer, target):
            raise AppError("MANAGEMENT_FILE_EXISTS", "target already exists; SeaweedFS Admin upload does not expose an atomic no-overwrite contract.", 409)
        return _admin_upload_file(request.app.state.settings, mgmt, parent, filename, data, content_type)

    return _management_ops_module().perform_operation(
        request.app.state.settings,
        mgmt,
        user.id,
        "file.upload",
        "POST",
        "/api/files/upload",
        {"path": parent, "filename": filename, "size": len(data), "content_type": content_type, "sha256": digest, "no_overwrite_atomic": False},
        idempotency_key=idempotency_key,
        invoke=invoke,
        readback=lambda: _upload_readback(filer, target, expected_size=len(data), can_read_bytes=can_read_bytes),
        verify=lambda before, after, response: bool(response) and before.get("exists") is False and after.get("exists") is True and after.get("content_verified") is True and after.get("sha256") == digest,
    )


@router.get("/{management_id}/workers")
def workers(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    settings = request.app.state.settings
    data = {key: _optional(key, lambda p=path: _admin_request_json(settings, mgmt, "GET", p)) for key, path in WORKER_READ_PATHS.items()}
    return {"connection_id": management_id, "source": "official_admin_plugin_api", **data}


@router.get("/{management_id}/maintenance")
def maintenance(request: Request, management_id: str):
    return workers(request, management_id)


@router.get("/{management_id}/maintenance/job-types/{job_type}")
def maintenance_job_type(request: Request, management_id: str, job_type: str, force_refresh: bool = False):
    mgmt = _require_management(request, management_id)
    settings = request.app.state.settings
    clean_job_type = _segment(job_type, "job_type")
    base = f"/api/plugin/job-types/{quote(clean_job_type, safe='')}"
    schema_params = {"force_refresh": "true"} if force_refresh else None
    return {
        "connection_id": management_id,
        "job_type": clean_job_type,
        "source": "official_admin_plugin_api",
        "config": _optional("job_type_config", lambda: _admin_request_json(settings, mgmt, "GET", f"{base}/config")),
        "schema": _optional("job_type_schema", lambda: _admin_request_json(settings, mgmt, "POST", f"{base}/schema", {}, params=schema_params)),
        "descriptor": _optional("job_type_descriptor", lambda: _admin_request_json(settings, mgmt, "GET", f"{base}/descriptor")),
        "runs": _optional("job_type_runs", lambda: _admin_request_json(settings, mgmt, "GET", f"{base}/runs")),
    }


@router.get("/{management_id}/maintenance/jobs/{job_id}")
def maintenance_job_detail(request: Request, management_id: str, job_id: str):
    mgmt = _require_management(request, management_id)
    settings = request.app.state.settings
    clean_job_id = _segment(job_id, "job_id")
    return {
        "connection_id": management_id,
        "job_id": clean_job_id,
        "source": "official_admin_plugin_api",
        "job": _optional("job", lambda: _maintenance_job(settings, mgmt, clean_job_id)),
        "detail": _optional("job_detail", lambda: _admin_request_json(settings, mgmt, "GET", f"/api/plugin/jobs/{quote(clean_job_id, safe='')}/detail")),
    }


def _maintenance_job(settings: Any, mgmt: Mapping[str, Any], job_id: str) -> Any:
    path = f"/api/plugin/jobs/{quote(job_id, safe='')}"
    try:
        return _admin_request_json(settings, mgmt, "GET", path)
    except AppError as exc:
        if exc.code not in {"ADMIN_ENDPOINT_DENIED", "ADMIN_ENDPOINT_NOT_FOUND", "ADMIN_ENDPOINT_UNSUPPORTED"}:
            raise
    data = _admin_request_json(settings, mgmt, "GET", "/api/plugin/jobs")
    rows = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("job_id") or row.get("id") or "") == job_id:
                return row
    raise AppError("NATIVE_NOT_FOUND", "maintenance job was not reported by upstream.", 404)


@router.post("/{management_id}/maintenance/actions")
def maintenance_action(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    action = str(body.get("action") or "")
    if action not in MAINTENANCE_ACTIONS:
        raise AppError("INVALID_REQUEST", "maintenance action is not supported.", 422, field="action")
    _ensure_plugin_enabled(settings, mgmt)
    job_type = body.get("job_type")
    if action in {"config_update", "detect", "run"}:
        job_type = _segment(str(job_type or ""), "job_type")
        known = _current_job_types(settings, mgmt)
        if job_type not in known:
            raise AppError("MANAGEMENT_JOB_TYPE_UNSUPPORTED", "job type is not reported by the upstream registry.", 422)
    if action == "config_update":
        _require_optional_dict(body, "config", required=True)
    if action in {"detect", "run"}:
        _require_optional_dict(body, "params")
    job_id = body.get("job_id")
    if action == "job_expire":
        job_id = _segment(str(job_id or ""), "job_id")
    if action == "job_execute" and not isinstance(body.get("job"), dict):
        raise AppError("INVALID_REQUEST", "job_execute requires a job object.", 422, field="job")
    path, method, payload, readback_path = _maintenance_target(action, job_type, job_id, body)
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "maintenance." + action,
        method,
        path,
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, method, path, payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", readback_path),
        verify=lambda before, after, response: _verify_maintenance(action, payload, before, after, response, job_type=job_type, job_id=job_id),
    )


def _maintenance_target(action: str, job_type: str | None, job_id: str | None, body: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], str]:
    if action == "config_update":
        return f"/api/plugin/job-types/{job_type}/config", "PUT", _require_optional_dict(body, "config", required=True), f"/api/plugin/job-types/{job_type}/config"
    if action == "detect":
        return f"/api/plugin/job-types/{job_type}/detect", "POST", _require_optional_dict(body, "params"), "/api/plugin/activities"
    if action == "run":
        return f"/api/plugin/job-types/{job_type}/run", "POST", _require_optional_dict(body, "params"), "/api/plugin/jobs"
    if action == "job_execute":
        return "/api/plugin/jobs/execute", "POST", dict(body.get("job") or {}), "/api/plugin/jobs"
    return f"/api/plugin/jobs/{job_id}/expire", "POST", dict(body.get("params") or {}), f"/api/plugin/jobs/{job_id}/detail"


@router.post("/{management_id}/volumes/{volume_id}/actions")
def volume_action(request: Request, management_id: str, volume_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    server_id = str(body.get("server_id") or "")
    action = str(body.get("action") or "")
    if action not in {"read_only", "vacuum"}:
        raise AppError("INVALID_REQUEST", "volume action is not supported.", 422, field="action")
    if not server_id:
        raise AppError("INVALID_REQUEST", "server_id is required.", 422, field="server_id")
    settings = request.app.state.settings
    before = _admin_request_json(settings, mgmt, "GET", "/api/volumes/export")
    rows = _volume_rows(before)
    if not _find_volume(rows, volume_id, server_id):
        raise AppError("MANAGEMENT_VOLUME_NOT_FOUND", "volume replica was not found in the current export.", 404)
    if action == "read_only":
        read_only = body.get("read_only")
        if not isinstance(read_only, bool):
            raise AppError("INVALID_REQUEST", "read_only must be boolean.", 422, field="read_only")
        path = f"/api/volumes/{quote(volume_id, safe='')}/{quote(server_id, safe='')}/read-only"
        payload = {"read_only": read_only}
        def verify(_before: Any, after: Any, _response: Any) -> bool:
            row = _find_volume(_volume_rows(after), volume_id, server_id)
            observed = row.get("read_only") if row else None
            return isinstance(observed, bool) and observed is read_only
    else:
        path = f"/api/volumes/{quote(volume_id, safe='')}/{quote(server_id, safe='')}/vacuum"
        payload = {}
        verify = lambda _before, _after, _response: False
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "volume." + action,
        "POST",
        path,
        payload,
        idempotency_key=body.get("idempotency_key"),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/volumes/export"),
        invoke=lambda: _admin_request_json(settings, mgmt, "POST", path, payload),
        verify=verify,
    )


@router.get("/{management_id}/modules")
def modules(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    settings = request.app.state.settings
    admin = _optional("admin", lambda: _admin_request_json(settings, mgmt, "GET", "/api/admin"))
    plugin = _optional("plugin", lambda: _admin_request_json(settings, mgmt, "GET", "/api/plugin/status"))
    s3tables = {key: _optional(key, lambda p=path: _admin_request_json(settings, mgmt, "GET", p)) for key, path in MODULE_PATHS.items()}
    endpoints = mgmt.get("endpoints") or {}
    optional = {
        "filer": "configured" if endpoints.get("filer") else "not_configured",
        "master": "configured" if endpoints.get("master") else "not_configured",
        "iceberg": "unknown",
        "lance": "unknown",
        "mount_clients": "unknown",
    }
    return {
        "connection_id": management_id,
        "source": "official_admin_modules",
        "admin": admin,
        "plugin": plugin,
        "s3_tables": s3tables,
        "optional_services": optional,
    }


@router.get("/{management_id}/modules/mq/topics/{namespace}/{topic}")
def mq_topic(request: Request, management_id: str, namespace: str, topic: str):
    mgmt = _require_management(request, management_id)
    namespace = _segment(namespace, "namespace")
    topic = _segment(topic, "topic")
    path = f"/api/mq/topics/{quote(namespace, safe='')}/{quote(topic, safe='')}"
    data = _admin_request_json(request.app.state.settings, mgmt, "GET", path)
    if isinstance(data, dict):
        data.pop("subscribers", None)
        data.pop("epoch", None)
    return {"connection_id": management_id, "source": "official_admin_mq_api", "namespace": namespace, "topic": topic, "details": data}


@router.post("/{management_id}/modules/mq/topics")
def mq_create_topic(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    namespace = _segment(_required_text(body, "namespace"), "namespace")
    topic = _segment(_required_text(body, "name"), "name")
    partition_count = int(body.get("partition_count") or 0)
    retention = dict(body.get("retention") or {})
    if partition_count < 1 or partition_count > 100:
        raise AppError("INVALID_REQUEST", "partition_count must be between 1 and 100.", 422, field="partition_count")
    if retention.get("enabled") and int(retention.get("retention_seconds") or 0) <= 0:
        raise AppError("INVALID_REQUEST", "retention_seconds must be positive when retention is enabled.", 422, field="retention.retention_seconds")
    payload = {"namespace": namespace, "name": topic, "partition_count": partition_count, "retention": retention}
    detail_path = f"/api/mq/topics/{quote(namespace, safe='')}/{quote(topic, safe='')}"
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "mq.create_topic",
        "POST",
        "/api/mq/topics/create",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "POST", "/api/mq/topics/create", payload),
        readback=lambda: _optional("mq_topic", lambda: _admin_request_json(settings, mgmt, "GET", detail_path)),
        verify=lambda _before, after, response: bool(response) and _topic_matches(after, namespace, topic),
    )


@router.post("/{management_id}/modules/mq/topics/retention")
def mq_update_retention(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    namespace = _segment(_required_text(body, "namespace"), "namespace")
    topic = _segment(_required_text(body, "name"), "name")
    retention = dict(body.get("retention") or {})
    if retention.get("enabled") and int(retention.get("retention_seconds") or 0) <= 0:
        raise AppError("INVALID_REQUEST", "retention_seconds must be positive when retention is enabled.", 422, field="retention.retention_seconds")
    payload = {"namespace": namespace, "name": topic, "retention": retention}
    detail_path = f"/api/mq/topics/{quote(namespace, safe='')}/{quote(topic, safe='')}"
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "mq.update_retention",
        "POST",
        "/api/mq/topics/retention/update",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "POST", "/api/mq/topics/retention/update", payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", detail_path),
        verify=lambda _before, after, response: bool(response) and _topic_retention_matches(after, retention),
    )


@router.post("/{management_id}/modules/mq/retention/purge")
def mq_purge_retention(request: Request, management_id: str, body: dict[str, Any] | None = None):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    body = body or {}
    return _management_ops_module().perform_operation(
        request.app.state.settings,
        mgmt,
        user.id,
        "mq.purge_retention",
        "POST",
        "/api/mq/retention/purge",
        {},
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(request.app.state.settings, mgmt, "POST", "/api/mq/retention/purge", {}),
        readback=lambda: None,
        verify=lambda _before, _after, _response: False,
    )


@router.get("/{management_id}/modules/s3-tables")
def s3_tables(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    settings = request.app.state.settings
    return {key: _optional(key, lambda p=path: _admin_request_json(settings, mgmt, "GET", p)) for key, path in MODULE_PATHS.items()}


@router.get("/{management_id}/modules/s3-tables/buckets")
def s3_table_buckets(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    return _admin_request_json(request.app.state.settings, mgmt, "GET", "/api/s3tables/buckets")


@router.post("/{management_id}/modules/s3-tables/buckets")
def s3_table_create_bucket(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    payload = {key: body[key] for key in ("name", "tags", "owner", "format") if key in body}
    name = _segment(_required_text(payload, "name"), "name")
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.create_bucket",
        "POST",
        "/api/s3tables/buckets",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "POST", "/api/s3tables/buckets", payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/buckets"),
        verify=lambda _before, after, response: bool(response.get("arn")) and _s3_tables_bucket_exists(after, response.get("arn"), name),
    )


@router.delete("/{management_id}/modules/s3-tables/buckets")
def s3_table_delete_bucket(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    bucket_arn = _required_text(body, "bucket_arn")
    if body.get("confirm_empty") is not True:
        raise AppError("MANAGEMENT_TABLE_BUCKET_DELETE_REQUIRES_EMPTY_CONFIRMATION", "table bucket delete requires confirm_empty=true.", 409)
    payload = {"bucket_arn": bucket_arn}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.delete_bucket",
        "DELETE",
        "/api/s3tables/buckets",
        payload,
        params={"bucket": bucket_arn},
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "DELETE", "/api/s3tables/buckets", params={"bucket": bucket_arn}),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/buckets"),
        verify=lambda before, after, response: bool(response) and _s3_tables_bucket_exists(before, bucket_arn) and not _s3_tables_bucket_exists(after, bucket_arn),
    )


@router.get("/{management_id}/modules/s3-tables/namespaces")
def s3_table_namespaces(request: Request, management_id: str, bucket_arn: str):
    mgmt = _require_management(request, management_id)
    return _admin_request_json(request.app.state.settings, mgmt, "GET", "/api/s3tables/namespaces", params={"bucket": bucket_arn})


@router.post("/{management_id}/modules/s3-tables/namespaces")
def s3_table_create_namespace(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    bucket_arn = _required_text(body, "bucket_arn")
    name = _required_text(body, "name")
    payload = {"bucket_arn": bucket_arn, "name": name}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.create_namespace",
        "POST",
        "/api/s3tables/namespaces",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "POST", "/api/s3tables/namespaces", payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/namespaces", params={"bucket": bucket_arn}),
        verify=lambda _before, after, response: bool(response) and _s3_tables_namespace_exists(after, name),
    )


@router.delete("/{management_id}/modules/s3-tables/namespaces")
def s3_table_delete_namespace(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    bucket_arn = _required_text(body, "bucket_arn")
    name = _required_text(body, "name")
    payload = {"bucket_arn": bucket_arn, "name": name}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.delete_namespace",
        "DELETE",
        "/api/s3tables/namespaces",
        payload,
        params={"bucket": bucket_arn, "name": name},
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "DELETE", "/api/s3tables/namespaces", params={"bucket": bucket_arn, "name": name}),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/namespaces", params={"bucket": bucket_arn}),
        verify=lambda before, after, response: bool(response) and _s3_tables_namespace_exists(before, name) and not _s3_tables_namespace_exists(after, name),
    )


@router.get("/{management_id}/modules/s3-tables/tables")
def s3_table_tables(request: Request, management_id: str, bucket_arn: str, namespace: str = ""):
    mgmt = _require_management(request, management_id)
    return _admin_request_json(request.app.state.settings, mgmt, "GET", "/api/s3tables/tables", params={"bucket": bucket_arn, "namespace": namespace})


@router.get("/{management_id}/modules/s3-tables/table-details")
def s3_table_details(request: Request, management_id: str, bucket_arn: str, namespace: str, name: str):
    return fetch_table_details(request, management_id, bucket_arn, namespace, name)


def fetch_table_details(request: Request, management_id: str, bucket_arn: str, namespace: str, name: str) -> dict[str, Any]:
    current_user(request)
    mgmt = _require_management(request, management_id)
    namespace_parts = _s3_tables_namespace_parts(namespace)
    table_name = _segment(name.strip(), "name")
    payload = {"tableBucketARN": bucket_arn, "namespace": namespace_parts, "name": table_name}
    try:
        response = _s3_tables_target_request(request.app.state.settings, mgmt, "GetTable", payload)
        details = _s3_tables_detail_payload(bucket_arn, namespace, table_name, response)
    except AppError as exc:
        status_code = None
        if isinstance(getattr(exc, "detail", None), dict):
            status_code = exc.detail.get("status")
        return _s3_tables_error_response(management_id, exc, status_code=status_code)
    return {
        "connection_id": management_id,
        "source": "native_s3tables_get_table",
        "status": "supported",
        "details": details,
    }


@router.post("/{management_id}/modules/s3-tables/tables")
def s3_table_create_table(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    bucket_arn = _required_text(body, "bucket_arn")
    namespace = _required_text(body, "namespace")
    name = _segment(_required_text(body, "name"), "name")
    payload = {key: body[key] for key in ("bucket_arn", "namespace", "name", "format", "tags", "metadata") if key in body}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.create_table",
        "POST",
        "/api/s3tables/tables",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "POST", "/api/s3tables/tables", payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/tables", params={"bucket": bucket_arn, "namespace": namespace}),
        verify=lambda _before, after, response: bool(response.get("table_arn")) and _s3_tables_table(after, name) is not None,
    )


@router.delete("/{management_id}/modules/s3-tables/tables")
def s3_table_delete_table(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    bucket_arn = _required_text(body, "bucket_arn")
    namespace = _required_text(body, "namespace")
    name = _segment(_required_text(body, "name"), "name")
    version = str(body.get("version") or "")
    before = _admin_request_json(settings, mgmt, "GET", "/api/s3tables/tables", params={"bucket": bucket_arn, "namespace": namespace})
    table = _s3_tables_table(before, name)
    if body.get("confirm_empty") is not True or not _s3_table_empty(table):
        raise AppError("MANAGEMENT_TABLE_DELETE_REQUIRES_EMPTY_CONFIRMATION", "table delete requires confirm_empty=true and an empty observed table.", 409)
    params = {"bucket": bucket_arn, "namespace": namespace, "name": name}
    if version:
        params["version"] = version
    payload = {"bucket_arn": bucket_arn, "namespace": namespace, "name": name, "version": version}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.delete_table",
        "DELETE",
        "/api/s3tables/tables",
        payload,
        params=params,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "DELETE", "/api/s3tables/tables", params=params),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/tables", params={"bucket": bucket_arn, "namespace": namespace}),
        verify=lambda _before, after, response: bool(response) and _s3_tables_table(after, name) is None,
    )


@router.get("/{management_id}/modules/s3-tables/bucket-policy")
def s3_table_bucket_policy(request: Request, management_id: str, bucket_arn: str):
    mgmt = _require_management(request, management_id)
    return _admin_request_json(request.app.state.settings, mgmt, "GET", "/api/s3tables/bucket-policy", params={"bucket": bucket_arn})


@router.put("/{management_id}/modules/s3-tables/bucket-policy")
def s3_table_put_bucket_policy(request: Request, management_id: str, body: dict[str, Any]):
    return _s3_table_policy_write(request, management_id, body, table_policy=False, delete=False)


@router.delete("/{management_id}/modules/s3-tables/bucket-policy")
def s3_table_delete_bucket_policy(request: Request, management_id: str, body: dict[str, Any]):
    return _s3_table_policy_write(request, management_id, body, table_policy=False, delete=True)


@router.get("/{management_id}/modules/s3-tables/table-policy")
def s3_table_policy(request: Request, management_id: str, bucket_arn: str, namespace: str, name: str):
    mgmt = _require_management(request, management_id)
    return _admin_request_json(request.app.state.settings, mgmt, "GET", "/api/s3tables/table-policy", params={"bucket": bucket_arn, "namespace": namespace, "name": name})


@router.put("/{management_id}/modules/s3-tables/table-policy")
def s3_table_put_table_policy(request: Request, management_id: str, body: dict[str, Any]):
    return _s3_table_policy_write(request, management_id, body, table_policy=True, delete=False)


@router.delete("/{management_id}/modules/s3-tables/table-policy")
def s3_table_delete_table_policy(request: Request, management_id: str, body: dict[str, Any]):
    return _s3_table_policy_write(request, management_id, body, table_policy=True, delete=True)


def _s3_table_policy_write(request: Request, management_id: str, body: dict[str, Any], *, table_policy: bool, delete: bool):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    bucket_arn = _required_text(body, "bucket_arn")
    policy = "" if delete else _required_text(body, "policy")
    payload = {"bucket_arn": bucket_arn}
    params = {"bucket": bucket_arn}
    path = "/api/s3tables/table-policy" if table_policy else "/api/s3tables/bucket-policy"
    action = "table.delete_table_policy" if delete and table_policy else "table.put_table_policy" if table_policy else "table.delete_bucket_policy" if delete else "table.put_bucket_policy"
    if table_policy:
        namespace = _required_text(body, "namespace")
        name = _segment(_required_text(body, "name"), "name")
        payload.update({"namespace": namespace, "name": name})
        params.update({"namespace": namespace, "name": name})
    if not delete:
        payload["policy"] = policy
    method = "DELETE" if delete else "PUT"
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        action,
        method,
        path,
        payload,
        params=params if delete else None,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, method, path, payload if not delete else None, params=params if delete else None),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", path, params=params),
        verify=lambda _before, after, response: bool(response) and ((after.get("policy") in {"", None}) if delete else _policy_matches(after, policy)),
    )


@router.get("/{management_id}/modules/s3-tables/tags")
def s3_table_tags(request: Request, management_id: str, resource_arn: str):
    mgmt = _require_management(request, management_id)
    return _admin_request_json(request.app.state.settings, mgmt, "GET", "/api/s3tables/tags", params={"arn": resource_arn})


@router.put("/{management_id}/modules/s3-tables/tags")
def s3_table_put_tags(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    resource_arn = _required_text(body, "resource_arn")
    tags = body.get("tags")
    if not isinstance(tags, dict) or not tags:
        raise AppError("INVALID_REQUEST", "tags must be a non-empty object.", 422, field="tags")
    payload = {"resource_arn": resource_arn, "tags": {str(k): str(v) for k, v in tags.items()}}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.tag_resource",
        "PUT",
        "/api/s3tables/tags",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "PUT", "/api/s3tables/tags", payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/tags", params={"arn": resource_arn}),
        verify=lambda _before, after, response: bool(response) and _tags_include(after, payload["tags"]),
    )


@router.delete("/{management_id}/modules/s3-tables/tags")
def s3_table_delete_tags(request: Request, management_id: str, body: dict[str, Any]):
    user = mutation(request)
    mgmt = _require_management(request, management_id, write=True)
    settings = request.app.state.settings
    resource_arn = _required_text(body, "resource_arn")
    tag_keys = body.get("tag_keys")
    if not isinstance(tag_keys, list) or not all(isinstance(key, str) and key for key in tag_keys):
        raise AppError("INVALID_REQUEST", "tag_keys must be a non-empty string list.", 422, field="tag_keys")
    payload = {"resource_arn": resource_arn, "tag_keys": tag_keys}
    return _management_ops_module().perform_operation(
        settings,
        mgmt,
        user.id,
        "table.untag_resource",
        "DELETE",
        "/api/s3tables/tags",
        payload,
        idempotency_key=body.get("idempotency_key"),
        invoke=lambda: _admin_request_json(settings, mgmt, "DELETE", "/api/s3tables/tags", payload),
        readback=lambda: _admin_request_json(settings, mgmt, "GET", "/api/s3tables/tags", params={"arn": resource_arn}),
        verify=lambda _before, after, response: bool(response) and not any(key in (after.get("tags") or {}) for key in tag_keys),
    )
