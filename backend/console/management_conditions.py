from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, Request

from . import core, db, management, storage
from .security import AppError, current_user, mutation, utc_now

router = APIRouter(prefix="/api/v1/management")

PROOF_TTL = timedelta(hours=24)
PROBE_PREFIX = ".console-probe/"
SUPPORTED_CAPABILITY = "conditional_delete_if_match"
_TEST_CLIENT: Any | None = None


def initialize(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS management_condition_probes (
          id TEXT PRIMARY KEY,
          management_id TEXT NOT NULL REFERENCES management_connections(id),
          scope_id TEXT NOT NULL REFERENCES scopes(id),
          actor_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          probe_key TEXT NOT NULL,
          version_id TEXT,
          etag TEXT,
          state TEXT NOT NULL,
          result_json TEXT NOT NULL DEFAULT '{}',
          error_code TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(management_id, actor_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS management_condition_probe_scope_time
          ON management_condition_probes(scope_id, created_at);
        """
    )


@router.post("/{management_id}/objects/check-conditional-delete")
def check_conditional_delete(request: Request, management_id: str, body: dict[str, Any]):
    actor = mutation(request)
    idempotency_key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Conditional delete probe requires Idempotency-Key.", 422)
    scope_id = body.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise AppError("INVALID_REQUEST", "scope_id is required.", 422, field="scope_id")
    manager, scope = _bound(request, management_id, scope_id.strip())
    request_hash = _hash_request({"scope_id": scope["id"], "action": "conditional_delete_if_match:v1"})
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM management_condition_probes WHERE management_id=? AND actor_id=? AND idempotency_key=?",
            (manager["id"], actor.id, idempotency_key.strip()),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise AppError("IDEMPOTENCY_CONFLICT", "Conditional delete probe idempotency key was reused for a different scope.", 409)
            return _stored_result(dict(existing), replayed=True)
        probe_id = "cdp_" + uuid.uuid4().hex
        probe_key = _probe_key(scope)
        stamp = utc_now()
        conn.execute(
            """
            INSERT INTO management_condition_probes(
              id,management_id,scope_id,actor_id,idempotency_key,request_hash,probe_key,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'planned',?,?)
            """,
            (probe_id, manager["id"], scope["id"], actor.id, idempotency_key.strip(), request_hash, probe_key, stamp, stamp),
        )
    return _execute_probe(request.app.state.settings, manager, scope, probe_id)


@router.get("/{management_id}/objects/conditional-delete-probes")
def list_conditional_delete_probes(request: Request, management_id: str, scope_id: str, limit: int = 100):
    current_user(request)
    if not 1 <= limit <= 100:
        raise AppError("INVALID_REQUEST", "limit must be between 1 and 100.", 422, field="limit")
    manager = management.require_management(request, management_id, write=False)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        scope = core.ensure_scope_permission(conn, scope_id, "object:read")
        if not manager.get("s3_connection_id") or scope["connection_id"] != manager["s3_connection_id"]:
            raise AppError("MANAGEMENT_SCOPE_MISMATCH", "Storage scope is not linked to this management connection.", 403)
        rows = conn.execute(
            """
            SELECT * FROM management_condition_probes
            WHERE management_id=? AND scope_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (manager["id"], scope["id"], limit),
        ).fetchall()
    return {"items": [_probe_summary(dict(row)) for row in rows], "total": None, "limit": limit}


def supported(settings, scope: dict[str, Any], head: dict[str, Any] | None = None) -> bool:
    try:
        with db.connect(settings) as conn:
            connection = core._connection_private(conn, scope["connection_id"])
        capabilities = _json(connection.get("capabilities_json"), {})
        if capabilities.get(SUPPORTED_CAPABILITY) != "supported":
            return False
        evidence = capabilities.get("evidence", {}).get(SUPPORTED_CAPABILITY, {})
        if evidence.get("scopeId") != scope.get("id"):
            return False
        checked_at = _parse_time(evidence.get("checkedAt"))
        now = datetime.now(timezone.utc)
        if checked_at is None or checked_at > now or now - checked_at > PROOF_TTL:
            return False
        if evidence.get("endpoint") != connection.get("endpoint_url") or evidence.get("verifyTLS") != bool(connection.get("verify_tls", True)):
            return False
        server_header = _server_header(head or {})
        if not server_header or not evidence.get("serverHeader") or evidence.get("serverHeader") != server_header:
            return False
        probe_key = evidence.get("probeKey")
        if not isinstance(probe_key, str) or not probe_key.startswith(f"{scope['prefix']}{PROBE_PREFIX}"):
            return False
        return True
    except Exception:
        return False


def _bound(request: Request, management_id: str, scope_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manager = management.require_management(request, management_id, write=True)
    permissions = manager.get("permissions", {})
    if permissions.get("object.manage") is not True:
        raise AppError("MANAGEMENT_WRITE_DISABLED", "Management connection is not allowed to manage objects.", 403)
    with db.connect(request.app.state.settings) as conn:
        scope = core.ensure_scope_permission(conn, scope_id, "object:write")
    if not manager.get("s3_connection_id") or scope["connection_id"] != manager["s3_connection_id"]:
        raise AppError("MANAGEMENT_SCOPE_MISMATCH", "Storage scope is not linked to this management connection.", 403)
    return manager, scope


def _execute_probe(settings, manager: dict[str, Any], scope: dict[str, Any], probe_id: str) -> dict[str, Any]:
    try:
        _recheck_dispatch_authorization(settings, manager, scope)
    except AppError as exc:
        _mark_probe_failed(settings, probe_id, exc, scope)
        raise
    s3 = _client(settings, scope)
    try:
        try:
            _ensure_bucket_is_probe_safe(s3, scope["bucket"])
        except AppError as exc:
            _mark_probe_failed(settings, probe_id, exc, scope)
            raise
        with db.connect(settings) as conn:
            row = conn.execute("SELECT * FROM management_condition_probes WHERE id=?", (probe_id,)).fetchone()
            if not row:
                raise AppError("PROBE_NOT_FOUND", "Conditional delete probe was not found.", 404)
            if row["state"] != "planned":
                return _stored_result(dict(row), replayed=True)
            conn.execute("UPDATE management_condition_probes SET state='dispatching',updated_at=? WHERE id=? AND state='planned'", (utc_now(), probe_id))
        try:
            _recheck_dispatch_authorization(settings, manager, scope)
        except AppError as exc:
            _mark_probe_failed(settings, probe_id, exc, scope)
            raise
        result = _probe_remote(s3, settings, manager, scope, probe_id)
        return result
    finally:
        close = getattr(s3, "close", None)
        if callable(close):
            close()


def _recheck_dispatch_authorization(settings, manager: dict[str, Any], scope: dict[str, Any]) -> None:
    with db.connect(settings) as conn:
        latest_manager = management.require_management_row(conn, manager["id"])
        public = management._public_connection(latest_manager)
        if not public.get("management_write_enabled") or public.get("permissions", {}).get("object.manage") is not True:
            raise AppError("MANAGEMENT_WRITE_DISABLED", "Management connection is not allowed to manage objects.", 403)
        current_scope = core.ensure_scope_permission(conn, scope["id"], "object:write")
        if current_scope["authz_epoch"] != scope["authz_epoch"]:
            raise AppError("FORBIDDEN_SCOPE", "Storage scope authorization changed before dispatch.", 403)
        if public.get("s3_connection_id") != current_scope["connection_id"]:
            raise AppError("MANAGEMENT_SCOPE_MISMATCH", "Storage scope is not linked to this management connection.", 403)


def _probe_remote(s3, settings, manager: dict[str, Any], scope: dict[str, Any], probe_id: str) -> dict[str, Any]:
    with db.connect(settings) as conn:
        row = dict(conn.execute("SELECT * FROM management_condition_probes WHERE id=?", (probe_id,)).fetchone())
    key = row["probe_key"]
    content = ("seaweedfs-console conditional delete probe " + probe_id).encode()
    base = {"Bucket": scope["bucket"], "Key": key}
    try:
        put = s3.put_object(**base, Body=content, IfNoneMatch="*")
        version_id = put.get("VersionId")
        etag = put.get("ETag") or '"' + hashlib.md5(content, usedforsecurity=False).hexdigest() + '"'
        row["version_id"] = version_id
        row["etag"] = etag
        with db.connect(settings) as conn:
            conn.execute("UPDATE management_condition_probes SET version_id=?,etag=?,updated_at=? WHERE id=? AND state='dispatching'", (version_id, etag, utc_now(), probe_id))
        delete_kwargs = dict(base, IfMatch='"swc-never-match"')
        if version_id is not None:
            delete_kwargs["VersionId"] = version_id
        try:
            s3.delete_object(**delete_kwargs)
            outcome = _unsupported_result("CONDITIONAL_DELETE_IGNORED", "DeleteObject ignored an incorrect IfMatch condition.")
        except Exception as exc:
            if _status(exc) == 412:
                outcome = _verify_unchanged(s3, base, version_id, etag, content)
            else:
                outcome = _unknown_result(_error_code(exc), "Conditional delete probe could not verify storage behavior.")
        cleanup = _cleanup_probe(s3, base, version_id, etag)
        if cleanup["status"] != "cleaned":
            outcome = _needs_review_result("PROBE_CLEANUP_UNKNOWN", cleanup)
        outcome = _with_probe_context(outcome, scope, row)
        if outcome["capability_status"] == "supported":
            _record_capability(settings, scope, manager, outcome, put, key)
        else:
            _clear_capability(settings, scope)
        return _finish_probe(settings, probe_id, outcome)
    except AppError:
        _cleanup_probe_after_partial_failure(s3, base, row)
        raise
    except Exception as exc:
        cleanup = _cleanup_probe_after_partial_failure(s3, base, row)
        outcome = _unknown_result(_error_code(exc), "Conditional delete probe could not complete.")
        if cleanup.get("status") == "needs_review":
            outcome = _needs_review_result("PROBE_CLEANUP_UNKNOWN", cleanup)
        outcome = _with_probe_context(outcome, scope, row)
        _clear_capability(settings, scope)
        return _finish_probe(settings, probe_id, outcome)


def _ensure_bucket_is_probe_safe(s3, bucket: str) -> None:
    try:
        config = s3.get_object_lock_configuration(Bucket=bucket)
    except AttributeError as exc:
        raise AppError("OBJECT_LOCK_UNKNOWN", "Cannot verify bucket object lock configuration.", 409) from exc
    except Exception as exc:
        if storage._code_from_exception(exc) in {"ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration", "NoSuchBucketObjectLockConfiguration"}:
            return
        raise AppError("OBJECT_LOCK_UNKNOWN", "Cannot verify bucket object lock configuration.", 409) from None
    if config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled":
        raise AppError("OBJECT_LOCK_ENABLED", "Bucket object lock is enabled; refusing conditional delete probe.", 409)
    raise AppError("OBJECT_LOCK_UNKNOWN", "Cannot verify bucket object lock configuration.", 409)


def _verify_unchanged(s3, base: dict[str, Any], version_id: str | None, etag: str, content: bytes) -> dict[str, Any]:
    kwargs = dict(base)
    if version_id is not None:
        kwargs["VersionId"] = version_id
    head = s3.head_object(**kwargs)
    if head.get("ETag") != etag:
        return _unknown_result("PROBE_CHANGED", "Probe object identity changed after conditional delete.")
    get_kwargs = dict(kwargs)
    if etag:
        get_kwargs["IfMatch"] = etag
    result = s3.get_object(**get_kwargs)
    body = result.get("Body")
    data = body.read() if hasattr(body, "read") else body
    close = getattr(body, "close", None)
    if callable(close):
        close()
    if data != content:
        return _unknown_result("PROBE_CHANGED", "Probe object content changed after conditional delete.")
    return {"capability_status": "supported", "capability": SUPPORTED_CAPABILITY, "response": "precondition_failed", "etag": etag, "version_id": version_id}


def _cleanup_probe(s3, base: dict[str, Any], version_id: str | None, etag: str | None = None) -> dict[str, Any]:
    mutable_identity = version_id in (None, "", "null")
    kwargs = dict(base)
    if version_id is not None:
        kwargs["VersionId"] = version_id
    try:
        if mutable_identity:
            head_kwargs = dict(base)
            delete_kwargs = dict(base)
            verify_kwargs = dict(base)
            if version_id == "null":
                head_kwargs["VersionId"] = "null"
                delete_kwargs["VersionId"] = "null"
                verify_kwargs["VersionId"] = "null"
            try:
                head = s3.head_object(**head_kwargs)
            except Exception as exc:
                if _status(exc) == 404:
                    return {"status": "cleaned", "version_id": version_id, "identity_check": "already_missing"}
                return {"status": "needs_review", "error_code": _error_code(exc), "version_id": version_id}
            if not etag or head.get("ETag") != etag:
                return {"status": "needs_review", "error_code": "PROBE_IDENTITY_CHANGED", "version_id": version_id}
            delete_kwargs["IfMatch"] = etag
            s3.delete_object(**delete_kwargs)
            try:
                s3.head_object(**verify_kwargs)
            except Exception as exc:
                if _status(exc) == 404:
                    return {"status": "cleaned", "version_id": version_id, "identity_check": "etag", "cas_atomic": False}
                return {"status": "needs_review", "error_code": _error_code(exc), "version_id": version_id}
            return {"status": "needs_review", "error_code": "PROBE_STILL_EXISTS", "version_id": version_id}
        s3.delete_object(**kwargs)
        try:
            s3.head_object(**kwargs)
        except Exception as exc:
            if _status(exc) == 404:
                return {"status": "cleaned", "version_id": version_id}
            return {"status": "needs_review", "error_code": _error_code(exc), "version_id": version_id}
        return {"status": "needs_review", "error_code": "PROBE_STILL_EXISTS", "version_id": version_id}
    except Exception as exc:
        return {"status": "needs_review", "error_code": _error_code(exc), "version_id": version_id}


def _cleanup_probe_after_partial_failure(s3, base: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    version_id = row.get("version_id")
    etag = row.get("etag")
    if not version_id and not etag:
        return {"status": "not_created"}
    return _cleanup_probe(s3, base, version_id, etag)


def _record_capability(settings, scope: dict[str, Any], manager: dict[str, Any], outcome: dict[str, Any], put: dict[str, Any], probe_key: str) -> None:
    checked = utc_now()
    with db.connect(settings) as conn:
        connection = core._connection_private(conn, scope["connection_id"])
        capabilities = _json(connection.get("capabilities_json"), {})
        capabilities[SUPPORTED_CAPABILITY] = "supported"
        evidence = capabilities.setdefault("evidence", {})
        evidence[SUPPORTED_CAPABILITY] = {
            "managementId": manager["id"],
            "scopeId": scope["id"],
            "endpoint": connection["endpoint_url"],
            "verifyTLS": bool(connection.get("verify_tls", True)),
            "serverHeader": _server_header(put),
            "checkedAt": checked,
            "probeKey": probe_key,
        }
        conn.execute(
            "UPDATE storage_connections SET capabilities_json=?,updated_at=? WHERE id=?",
            (json.dumps(capabilities, sort_keys=True, separators=(",", ":")), checked, scope["connection_id"]),
        )


def _clear_capability(settings, scope: dict[str, Any]) -> None:
    with db.connect(settings) as conn:
        connection = core._connection_private(conn, scope["connection_id"])
        capabilities = _json(connection.get("capabilities_json"), {})
        evidence = capabilities.get("evidence") if isinstance(capabilities.get("evidence"), dict) else {}
        current = evidence.get(SUPPORTED_CAPABILITY) if isinstance(evidence, dict) else None
        if current is None or current.get("scopeId") == scope.get("id"):
            capabilities.pop(SUPPORTED_CAPABILITY, None)
            if isinstance(evidence, dict):
                evidence.pop(SUPPORTED_CAPABILITY, None)
                if evidence:
                    capabilities["evidence"] = evidence
                else:
                    capabilities.pop("evidence", None)
            conn.execute(
                "UPDATE storage_connections SET capabilities_json=?,updated_at=? WHERE id=?",
                (json.dumps(capabilities, sort_keys=True, separators=(",", ":")), utc_now(), scope["connection_id"]),
            )


def _with_probe_context(outcome: dict[str, Any], scope: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    result = dict(outcome)
    result.setdefault("bucket", scope["bucket"])
    result.setdefault("probe_key", row.get("probe_key"))
    if row.get("version_id") is not None:
        result.setdefault("version_id", row.get("version_id"))
    return result


def _finish_probe(settings, probe_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
    capability_status = outcome.get("capability_status") or outcome.get("status") or "unknown"
    state = "confirmed" if capability_status in {"supported", "unsupported"} else "needs_review"
    public_outcome = dict(outcome)
    public_outcome["capability_status"] = capability_status
    public_outcome.pop("status", None)
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE management_condition_probes SET state=?,result_json=?,error_code=?,updated_at=? WHERE id=? AND state='dispatching'",
            (state, json.dumps(_redact(public_outcome), sort_keys=True, separators=(",", ":")), public_outcome.get("error_code"), utc_now(), probe_id),
        )
    result = dict(public_outcome)
    result.update({"operation_id": probe_id, "journal_status": state, "status": state, "replayed": False})
    return result


def _stored_result(row: dict[str, Any], replayed: bool) -> dict[str, Any]:
    result = _json(row.get("result_json"), {})
    result.update({"operation_id": row["id"], "journal_status": row["state"], "status": row["state"], "replayed": replayed})
    result.setdefault("probe_key", row.get("probe_key"))
    result.setdefault("version_id", row.get("version_id"))
    return result


def _mark_probe_failed(settings, probe_id: str, exc: AppError, scope: dict[str, Any]) -> None:
    with db.connect(settings) as conn:
        row = conn.execute("SELECT * FROM management_condition_probes WHERE id=?", (probe_id,)).fetchone()
        if row is None or row["state"] not in {"planned", "dispatching"}:
            return
        result = {
            "capability_status": "failed",
            "capability": SUPPORTED_CAPABILITY,
            "error_code": exc.code,
            "bucket": scope["bucket"],
            "probe_key": row["probe_key"],
            "version_id": row["version_id"],
        }
        conn.execute(
            "UPDATE management_condition_probes SET state='failed',result_json=?,error_code=?,updated_at=? WHERE id=?",
            (json.dumps(_redact(result), sort_keys=True, separators=(",", ":")), exc.code, utc_now(), probe_id),
        )
    _clear_capability(settings, scope)


def _probe_summary(row: dict[str, Any]) -> dict[str, Any]:
    result = _json(row.get("result_json"), {})
    return {
        "operation_id": row["id"],
        "management_id": row["management_id"],
        "scope_id": row["scope_id"],
        "journal_status": row["state"],
        "capability_status": result.get("capability_status"),
        "error_code": row.get("error_code") or result.get("error_code"),
        "bucket": result.get("bucket"),
        "probe_key": result.get("probe_key") or row.get("probe_key"),
        "version_id": result.get("version_id") if "version_id" in result else row.get("version_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _unsupported_result(code: str, message: str) -> dict[str, Any]:
    return {"capability_status": "unsupported", "capability": SUPPORTED_CAPABILITY, "error_code": code, "message": message}


def _unknown_result(code: str, message: str) -> dict[str, Any]:
    return {"capability_status": "unknown", "capability": SUPPORTED_CAPABILITY, "error_code": code, "message": message}


def _needs_review_result(code: str, cleanup: dict[str, Any]) -> dict[str, Any]:
    return {"capability_status": "needs_review", "capability": SUPPORTED_CAPABILITY, "error_code": code, "cleanup": cleanup}


def _client(settings, scope: dict[str, Any]):
    if _TEST_CLIENT is not None:
        return _TEST_CLIENT
    with db.connect(settings) as conn:
        connection = core._connection_private(conn, scope["connection_id"])
    return storage.client(connection, settings)


def _probe_key(scope: dict[str, Any]) -> str:
    return f"{scope['prefix']}{PROBE_PREFIX}{uuid.uuid4().hex}"


def _hash_request(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _status(exc: Exception) -> int | None:
    return storage._status_from_exception(exc)


def _error_code(exc: Exception) -> str:
    return storage._code_from_exception(exc) or exc.__class__.__name__


def _server_header(response: dict[str, Any]) -> str | None:
    try:
        value = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("server")
        return str(value) if value else None
    except AttributeError:
        return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if value is not None else fallback


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if "secret" in key.lower() or "token" in key.lower() else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
