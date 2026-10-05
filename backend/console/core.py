from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from . import storage
from .config import Settings
from .security import (
    AppError,
    Principal,
    hash_password,
    hash_token,
    make_csrf_token,
    new_id,
    sign_json,
    unsign_json,
    utc_add,
    utc_now,
    verify_password,
)


@dataclass(frozen=True)
class Download:
    filename: str
    content_type: str
    content_length: int | None
    chunks: Iterator[bytes]


def bootstrap_admin(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (settings.admin_username,)).fetchone()
    if row:
        return dict(row)

    existing = conn.execute("SELECT username FROM users ORDER BY created_at, id LIMIT 1").fetchone()
    if existing:
        raise RuntimeError(
            "Configured admin user is missing while another admin already exists; "
            "set CONSOLE_ADMIN_USERNAME to the existing admin or reset the database intentionally."
        )

    password = settings.require_admin_password()
    user_id = new_id("usr_")
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO users(id, username, password_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, settings.admin_username, hash_password(password), now),
    )
    row = conn.execute("SELECT * FROM users WHERE username = ?", (settings.admin_username,)).fetchone()
    if not row:
        raise RuntimeError("Configured admin user could not be bootstrapped")
    return dict(row)


def login(conn: sqlite3.Connection, settings: Settings, username: str, password: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        raise AppError("UNAUTHENTICATED", "Invalid username or password.", 401)
    now = utc_now()
    session_id = new_id("ses_")
    token = new_id()
    csrf_nonce = new_id("csrf_")
    conn.execute(
        """
        INSERT INTO sessions(id, user_id, token_hash, csrf_nonce, created_at, expires_at,
                             absolute_expires_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            row["id"],
            hash_token(token),
            csrf_nonce,
            now,
            utc_add(settings.session_ttl_seconds),
            utc_add(settings.session_absolute_ttl_seconds),
            now,
        ),
    )
    csrf = make_csrf_token(session_id, csrf_nonce, settings)
    return {
        "session_id": session_id,
        "session_token": token,
        "csrf_token": csrf,
        "user": {"id": row["id"], "username": row["username"]},
    }


def current_user_from_token(conn: sqlite3.Connection, token: str) -> tuple[Principal, dict[str, Any]]:
    now = utc_now()
    row = conn.execute(
        """
        SELECT sessions.*, users.username
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ?
          AND sessions.revoked_at IS NULL
          AND sessions.expires_at > ?
          AND sessions.absolute_expires_at > ?
        """,
        (hash_token(token), now, now),
    ).fetchone()
    if not row:
        raise AppError("UNAUTHENTICATED", "Session is missing or expired.", 401)
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
    principal = Principal(id=row["user_id"], username=row["username"])
    return principal, dict(row)


def logout(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (utc_now(), session_id))


def me(conn: sqlite3.Connection, principal: Principal, session: dict[str, Any], settings: Settings) -> dict[str, Any]:
    projects = []
    for project in conn.execute(
        """
        SELECT p.id AS project_id, p.display_name, s.id AS scope_id, s.display_name AS scope_name,
               s.allow_preview, s.allow_original_download
        FROM projects p
        LEFT JOIN scopes s ON s.project_id = p.id AND s.is_active = 1
        WHERE p.archived_at IS NULL
        ORDER BY p.display_name, s.display_name
        """
    ):
        _append_project_scope(projects, dict(project))
    return {
        "user": {"id": principal.id, "username": principal.username},
        "projects": projects,
        "csrf_token": make_csrf_token(session["id"], session["csrf_nonce"], settings),
    }


def _append_project_scope(projects: list[dict[str, Any]], row: dict[str, Any]) -> None:
    project = next((item for item in projects if item["project_id"] == row["project_id"]), None)
    if project is None:
        project = {"project_id": row["project_id"], "display_name": row["display_name"], "scopes": []}
        projects.append(project)
    if row.get("scope_id"):
        grants = ["asset:read", "object:read"]
        if row["allow_preview"]:
            grants.append("object:preview")
        if row["allow_original_download"]:
            grants.append("object:download_original")
        project["scopes"].append(
            {
                "scope_id": row["scope_id"],
                "display_name": row["scope_name"],
                "roles": ["admin"],
                "explicit_grants": grants,
            }
        )


def create_connection(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    connection_id = new_id("con_")
    conn.execute(
        """
        INSERT INTO storage_connections(
          id, display_name, endpoint_url, region, secret_ref, addressing_style, verify_tls,
          approved_endpoints_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            connection_id,
            _require(data, "display_name"),
            _require(data, "endpoint_url"),
            data.get("region"),
            _require(data, "secret_ref"),
            data.get("addressing_style", "path"),
            1 if data.get("verify_tls", True) else 0,
            json.dumps(data.get("approved_endpoints", {}), separators=(",", ":"), sort_keys=True),
            now,
            now,
        ),
    )
    return get_connection(conn, connection_id)


def list_connections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_public_connection(dict(row)) for row in conn.execute("SELECT * FROM storage_connections ORDER BY created_at DESC")]


def get_connection(conn: sqlite3.Connection, connection_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM storage_connections WHERE id = ?", (connection_id,)).fetchone()
    if not row:
        raise AppError("CONNECTION_NOT_FOUND", "Connection was not found.", 404)
    return _public_connection(dict(row))


def _connection_private(conn: sqlite3.Connection, connection_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM storage_connections WHERE id = ?", (connection_id,)).fetchone()
    if not row:
        raise AppError("CONNECTION_NOT_FOUND", "Connection was not found.", 404)
    data = dict(row)
    data["verify_tls"] = bool(data["verify_tls"])
    return data


def _public_connection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "endpoint_url": row["endpoint_url"],
        "region": row["region"],
        "secret_ref": row["secret_ref"],
        "addressing_style": row["addressing_style"],
        "verify_tls": bool(row["verify_tls"]),
        "approved_endpoints": _json(row["approved_endpoints_json"], {}),
        "capabilities": _json(row["capabilities_json"], {}),
        "server_version": row["server_version"],
        "version_source": row["version_source"],
        "version_observed_at": row["version_observed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def probe_connection(
    conn: sqlite3.Connection,
    settings: Settings,
    connection_id: str,
    bucket: str | None = None,
    prefix: str = "",
    s3_client: Any | None = None,
) -> dict[str, Any]:
    connection = _connection_private(conn, connection_id)
    s3_client = s3_client or storage.client(connection, settings)
    checked_at = utc_now()
    result = storage.probe_read_capabilities(s3_client, bucket=bucket, prefix=prefix)
    response = {"connection_id": connection_id, "checked_at": checked_at, **result}
    conn.execute(
        """
        UPDATE storage_connections
        SET capabilities_json = ?, server_version = COALESCE(?, server_version),
            version_source = COALESCE(?, version_source),
            version_observed_at = CASE WHEN ? IS NOT NULL THEN ? ELSE version_observed_at END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(response, separators=(",", ":"), sort_keys=True),
            result.get("server_version"),
            result.get("version_source"),
            result.get("server_version"),
            checked_at,
            checked_at,
            connection_id,
        ),
    )
    return response


def create_project(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    project_id = new_id("prj_")
    conn.execute(
        """
        INSERT INTO projects(id, project_key, display_name, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, _require(data, "project_key"), _require(data, "display_name"), data.get("description"), now, now),
    )
    return get_project(conn, project_id)


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM projects WHERE id = ? AND archived_at IS NULL", (project_id,)).fetchone()
    if not row:
        raise AppError("PROJECT_NOT_FOUND", "Project was not found.", 404)
    return dict(row)


def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM projects WHERE archived_at IS NULL ORDER BY display_name")]


def create_scope(conn: sqlite3.Connection, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    get_project(conn, project_id)
    _connection_private(conn, _require(data, "connection_id"))
    bucket = _require(data, "bucket")
    prefix = data.get("prefix", "")
    overlaps = _scope_overlaps(conn, data["connection_id"], bucket, prefix)
    if overlaps and not data.get("overlap_ack"):
        raise AppError(
            "SCOPE_OVERLAP_ACK_REQUIRED",
            "Scope overlaps an existing active scope.",
            409,
            detail={"overlaps": overlaps},
        )
    now = utc_now()
    scope_id = new_id("scp_")
    policy = data.get("scope_policy", {})
    conn.execute(
        """
        INSERT INTO scopes(
          id, project_id, connection_id, display_name, bucket, prefix, scope_kind,
          allow_preview, allow_original_download, writable, manage_bucket, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scope_id,
            project_id,
            data["connection_id"],
            _require(data, "display_name"),
            bucket,
            prefix,
            data.get("scope_kind", "source"),
            1 if policy.get("allow_preview", False) else 0,
            1 if policy.get("allow_original_download", False) else 0,
            1 if data.get("writable", False) else 0,
            1 if data.get("manage_bucket", False) else 0,
            now,
            now,
        ),
    )
    return get_scope(conn, scope_id, include_archived=False)


def _scope_overlaps(conn: sqlite3.Connection, connection_id: str, bucket: str, prefix: str) -> list[dict[str, Any]]:
    overlaps = []
    rows = conn.execute(
        """
        SELECT id, display_name, prefix, authz_epoch
        FROM scopes
        WHERE connection_id = ? AND bucket = ? AND is_active = 1
        """,
        (connection_id, bucket),
    )
    for row in rows:
        other = row["prefix"]
        if prefix.startswith(other) or other.startswith(prefix):
            overlaps.append(
                {
                    "scope_id": row["id"],
                    "display_name": row["display_name"],
                    "prefix": other,
                    "authz_epoch": row["authz_epoch"],
                }
            )
    return overlaps


def list_scopes(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    get_project(conn, project_id)
    return [
        _scope_public(dict(row))
        for row in conn.execute(
            "SELECT * FROM scopes WHERE project_id = ? AND is_active = 1 ORDER BY display_name", (project_id,)
        )
    ]


def get_scope(conn: sqlite3.Connection, scope_id: str, include_archived: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM scopes WHERE id = ?"
    params: tuple[Any, ...] = (scope_id,)
    if not include_archived:
        sql += " AND is_active = 1"
    row = conn.execute(sql, params).fetchone()
    if not row:
        raise AppError("FORBIDDEN_SCOPE", "The current session cannot access this project scope.", 403)
    return _scope_public(dict(row))


def archive_scope(conn: sqlite3.Connection, scope_id: str) -> dict[str, Any]:
    scope = get_scope(conn, scope_id, include_archived=False)
    now = utc_now()
    conn.execute(
        """
        UPDATE scopes
        SET is_active = 0, archived_at = ?, updated_at = ?, authz_epoch = authz_epoch + 1
        WHERE id = ?
        """,
        (now, now, scope_id),
    )
    scope["is_active"] = False
    scope["archived_at"] = now
    return scope


def _scope_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "connection_id": row["connection_id"],
        "display_name": row["display_name"],
        "bucket": row["bucket"],
        "prefix": row["prefix"],
        "scope_kind": row["scope_kind"],
        "allow_preview": bool(row["allow_preview"]),
        "allow_original_download": bool(row["allow_original_download"]),
        "writable": bool(row["writable"]),
        "manage_bucket": bool(row["manage_bucket"]),
        "authz_epoch": row["authz_epoch"],
        "index_generation": row["index_generation"],
        "is_active": bool(row["is_active"]),
        "archived_at": row["archived_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ensure_scope_permission(conn: sqlite3.Connection, scope_id: str, permission: str) -> dict[str, Any]:
    scope = get_scope(conn, scope_id, include_archived=False)
    if permission == "object:preview" and not scope["allow_preview"]:
        raise AppError("FORBIDDEN_SCOPE", "Preview is not allowed for this scope.", 403)
    if permission == "object:download_original" and not scope["allow_original_download"]:
        raise AppError("FORBIDDEN_SCOPE", "Download is not allowed for this scope.", 403)
    if permission == "object:write" and not scope["writable"]:
        raise AppError("FORBIDDEN_SCOPE", "Scope is not writable.", 403)
    if permission == "config:write" and not scope["manage_bucket"]:
        raise AppError("FORBIDDEN_SCOPE", "Scope cannot manage bucket configuration.", 403)
    return scope


def assert_key_in_scope(scope: dict[str, Any], key: str) -> None:
    if not key.startswith(scope["prefix"]):
        raise AppError("FORBIDDEN_SCOPE", "Object key is outside this scope prefix.", 403, field="key")


def upsert_object(conn: sqlite3.Connection, scope_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope = ensure_scope_permission(conn, scope_id, "object:read")
    key = _require(data, "key")
    assert_key_in_scope(scope, key)
    now = utc_now()
    revision = data.get("revision") or make_revision(scope["bucket"], key, data)
    object_id = data.get("id") or new_id("obj_")
    existing = conn.execute(
        "SELECT id, asset_id FROM objects WHERE scope_id = ? AND key = ? AND revision = ?",
        (scope_id, key, revision),
    ).fetchone()
    current = conn.execute(
        "SELECT asset_id FROM objects WHERE scope_id = ? AND key = ? AND is_current = 1",
        (scope_id, key),
    ).fetchone()
    asset_id = data.get("asset_id") or (existing["asset_id"] if existing else None) or (current["asset_id"] if current else None)
    if asset_id is None:
        asset_id = new_id("ast_")
        conn.execute(
            "INSERT INTO assets(id, project_id, first_seen_at, tags_json) VALUES (?, ?, ?, ?)",
            (asset_id, scope["project_id"], now, json.dumps(data.get("tags", []), separators=(",", ":"))),
        )
    conn.execute("UPDATE objects SET is_current = 0 WHERE scope_id = ? AND key = ? AND revision != ?", (scope_id, key, revision))
    properties_json = json.dumps(data.get("properties", {}), separators=(",", ":"), sort_keys=True)
    if existing:
        object_id = existing["id"]
        conn.execute(
            """
            UPDATE objects
            SET asset_id = ?, version_id = ?, size = ?, etag = ?, last_modified = ?,
                observed_at = ?, is_current = 1, presence = ?, properties_json = ?,
                checksum = ?, scan_id = ?, content_type = ?, effective_identity_strength = ?
            WHERE id = ?
            """,
            (
                asset_id,
                data.get("version_id"),
                int(data.get("size", 0)),
                data.get("etag"),
                data.get("last_modified"),
                now,
                data.get("presence", "present"),
                properties_json,
                data.get("checksum"),
                data.get("scan_id"),
                data.get("content_type"),
                identity_strength(data),
                object_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO objects(
              id, asset_id, scope_id, key, revision, version_id, size, etag, last_modified,
              first_seen_at, observed_at, is_current, presence, properties_json, checksum,
              scan_id, content_type, effective_identity_strength
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                asset_id,
                scope_id,
                key,
                revision,
                data.get("version_id"),
                int(data.get("size", 0)),
                data.get("etag"),
                data.get("last_modified"),
                now,
                now,
                data.get("presence", "present"),
                properties_json,
                data.get("checksum"),
                data.get("scan_id"),
                data.get("content_type"),
                identity_strength(data),
            ),
        )
    return get_object(conn, scope_id, object_id)


def identity_strength(data: dict[str, Any]) -> str:
    version_id = data.get("version_id")
    if version_id and version_id != "null":
        return "strong_version"
    if version_id == "null":
        return "literal_null_version"
    return "weak_observation"


def make_revision(bucket: str, key: str, data: dict[str, Any]) -> str:
    version_id = data.get("version_id")
    if version_id and version_id != "null":
        return f"version:{version_id}"
    import hashlib

    observed = json.dumps(
        {
            "bucket": bucket,
            "key": key,
            "version_id_marker": "literal_null" if version_id == "null" else None,
            "size": data.get("size"),
            "etag": data.get("etag"),
            "last_modified": data.get("last_modified"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "observed:" + hashlib.sha256(observed.encode("utf-8")).hexdigest()


def list_objects(
    conn: sqlite3.Connection,
    settings: Settings,
    scope_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    scope = ensure_scope_permission(conn, scope_id, "object:read")
    limit = max(1, min(limit, 200))
    last_seen_at: str | None = None
    last_id: str | None = None
    if cursor:
        payload = _decode_cursor(settings, cursor, scope)
        last_seen_at, last_id = payload["last"]
    params: list[Any] = [scope_id]
    where = "WHERE o.scope_id = ? AND o.is_current = 1"
    if last_seen_at and last_id:
        where += " AND (a.first_seen_at < ? OR (a.first_seen_at = ? AND a.id < ?))"
        params.extend([last_seen_at, last_seen_at, last_id])
    rows = conn.execute(
        f"""
        SELECT o.*, a.first_seen_at AS asset_first_seen_at
        FROM objects o
        JOIN assets a ON a.id = o.asset_id
        {where}
        ORDER BY a.first_seen_at DESC, a.id DESC
        LIMIT ?
        """,
        (*params, limit + 1),
    ).fetchall()
    items = [_object_public(dict(row), scope) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(settings, scope, [last["asset_first_seen_at"], last["asset_id"]])
    return {"items": items, "next_cursor": next_cursor, "index_generation": scope["index_generation"], "warnings": []}


def _encode_cursor(settings: Settings, scope: dict[str, Any], last: list[str]) -> str:
    payload = {
        "v": 1,
        "principal_id": "admin",
        "project_scope_id": scope["id"],
        "filter_hash": "sha256:default",
        "sort": ["first_seen_at", "id"],
        "last": last,
        "authz_epoch": scope["authz_epoch"],
        "index_generation": scope["index_generation"],
        "expires_at": utc_add(60 * 60),
    }
    return sign_json(payload, settings.require_cursor_key(), "cursor:v1")


def _decode_cursor(settings: Settings, token: str, scope: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = unsign_json(token, settings.require_cursor_key(), "cursor:v1")
    except ValueError as exc:
        raise AppError("INVALID_CURSOR", "Cursor is invalid.", 400) from exc
    now = utc_now()
    if (
        payload.get("project_scope_id") != scope["id"]
        or payload.get("authz_epoch") != scope["authz_epoch"]
        or payload.get("index_generation") != scope["index_generation"]
        or payload.get("expires_at", "") <= now
    ):
        raise AppError("INVALID_CURSOR", "Cursor is expired or no longer matches this scope.", 400)
    return payload


def get_object(conn: sqlite3.Connection, scope_id: str, object_id: str) -> dict[str, Any]:
    scope = ensure_scope_permission(conn, scope_id, "object:read")
    row = conn.execute("SELECT * FROM objects WHERE id = ? AND scope_id = ?", (object_id, scope_id)).fetchone()
    if not row:
        raise AppError("OBJECT_NOT_FOUND", "Object was not found in this scope.", 404)
    return _object_public(dict(row), scope)


def download_object(
    conn: sqlite3.Connection,
    settings: Settings,
    scope_id: str,
    object_id: str,
    s3_client: Any | None = None,
    requested_version_id: str | None = None,
) -> Download:
    scope = ensure_scope_permission(conn, scope_id, "object:download_original")
    obj = get_object(conn, scope_id, object_id)
    assert_key_in_scope(scope, obj["key"])
    connection = _connection_private(conn, scope["connection_id"])
    s3_client = s3_client or storage.client(connection, settings)
    get_kwargs: dict[str, Any] = {"Bucket": scope["bucket"], "Key": obj["key"]}
    if requested_version_id is not None:
        head_kwargs = {"Bucket": scope["bucket"], "Key": obj["key"], "VersionId": requested_version_id}
        try:
            head = s3_client.head_object(**head_kwargs)
        except Exception as exc:
            if _is_delete_marker_response(exc):
                raise AppError("VERSION_DELETE_MARKER", "Requested object version is a delete marker.", 409) from exc
            raise storage.sanitize_storage_exception(exc) from exc
        if head.get("DeleteMarker"):
            raise AppError("VERSION_DELETE_MARKER", "Requested object version is a delete marker.", 409)
        get_kwargs["VersionId"] = requested_version_id
        if requested_version_id == "null" and head.get("ETag"):
            get_kwargs["IfMatch"] = head["ETag"]
    elif obj.get("version_id"):
        get_kwargs["VersionId"] = obj["version_id"]
    if requested_version_id is None and obj["effective_identity_strength"] != "strong_version" and obj.get("etag"):
        get_kwargs["IfMatch"] = obj["etag"]
    try:
        response = s3_client.get_object(**get_kwargs)
    except Exception as exc:
        raise storage.sanitize_storage_exception(exc) from exc
    body = response["Body"]
    return Download(
        filename=obj["key"].rsplit("/", 1)[-1] or obj["id"],
        content_type=response.get("ContentType") or obj.get("content_type") or "application/octet-stream",
        content_length=response.get("ContentLength"),
        chunks=storage.stream_object_body(body),
    )


def _is_delete_marker_response(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(response.get("Error", {}).get("Code", ""))
    return status == 405 or code in {"MethodNotAllowed", "InvalidObjectState", "DeleteMarker"}


def require_scope(request, scope_id: str, write: bool = False) -> dict[str, Any]:
    from . import db
    from .security import current_user

    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        permission = "object:write" if write else "object:read"
        return ensure_scope_permission(conn, scope_id, permission)


def require_object(request, scope_id: str, object_id: str) -> dict[str, Any]:
    from . import db
    from .security import current_user

    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        return get_object(conn, scope_id, object_id)


def get_client(request, scope: dict[str, Any]):
    from . import db

    with db.connect(request.app.state.settings) as conn:
        connection = _connection_private(conn, scope["connection_id"])
    return storage.client(connection, request.app.state.settings)


def upsert_object_metadata(
    conn: sqlite3.Connection,
    scope: dict[str, Any],
    key: str,
    head: dict[str, Any],
    scan_id: str | None = None,
) -> dict[str, Any]:
    return upsert_object(
        conn,
        scope["id"],
        {
            "key": key,
            "version_id": head.get("VersionId") or head.get("version_id"),
            "size": head.get("ContentLength") or head.get("size") or 0,
            "etag": head.get("ETag") or head.get("etag"),
            "last_modified": _head_last_modified(head),
            "content_type": head.get("ContentType") or head.get("content_type"),
            "checksum": head.get("ChecksumSHA256") or head.get("checksum"),
            "scan_id": scan_id,
        },
    )


def _head_last_modified(head: dict[str, Any]) -> str | None:
    value = head.get("LastModified") or head.get("last_modified")
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value


def _object_public(row: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    props = _json(row["properties_json"], {})
    return {
        "id": row["id"],
        "asset_id": row["asset_id"],
        "scope_id": row["scope_id"],
        "bucket": scope["bucket"],
        "key": row["key"],
        "key_display": row["key"],
        "revision": row["revision"],
        "object_revision": row["revision"],
        "version_id": row["version_id"],
        "size_bytes": row["size"],
        "etag": row["etag"],
        "last_modified": row["last_modified"],
        "first_seen_at": row["first_seen_at"],
        "observed_at": row["observed_at"],
        "is_current": bool(row["is_current"]),
        "presence": row["presence"],
        "properties": props,
        "checksum": row["checksum"],
        "scan_id": row["scan_id"],
        "content_type": row["content_type"],
        "effective_identity_strength": row["effective_identity_strength"],
        "preview_state": props.get("preview_state", "not_ready"),
        "reference_status": props.get("reference_status", "unknown"),
    }


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _require(data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise AppError("INVALID_REQUEST", f"{key} is required.", 400, field=key)
    return value

