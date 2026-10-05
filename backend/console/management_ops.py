"""Durable control-plane write intents. Ambiguous writes are never resent."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Callable

from . import db
from .security import AppError, utc_now

SENSITIVE = {"password", "secretkey", "secretaccesskey", "sessiontoken", "accesstoken",
             "csrftoken", "authorization", "cookie", "setcookie", "privatekey",
             "secret", "token", "auth", "signature", "credential", "credentials", "awssecretaccesskey"}


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.sub(r"[_-]", "", key).lower() in SENSITIVE else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and any(marker in value.lower() for marker in ("x-amz-signature=", "x-amz-credential=", "signature=")):
        return "[REDACTED SIGNED URL]"
    return value


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _has_secret_input(value):
    if isinstance(value, dict):
        return any(re.sub(r"[_-]", "", key).lower() in SENSITIVE or _has_secret_input(item) for key, item in value.items())
    return isinstance(value, list) and any(_has_secret_input(item) for item in value)


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS management_operations (
      id TEXT PRIMARY KEY,management_id TEXT NOT NULL REFERENCES management_connections(id),
      actor_id TEXT NOT NULL,action TEXT NOT NULL,method TEXT NOT NULL,path TEXT NOT NULL,
      request_json TEXT NOT NULL,request_hash TEXT NOT NULL,namespace TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,state TEXT NOT NULL,
      before_json TEXT,result_json TEXT,error_code TEXT,
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      UNIQUE(namespace,idempotency_key));
    CREATE INDEX IF NOT EXISTS management_operation_time ON management_operations(management_id,created_at);
    """)


def _permission(action):
    prefix = action.split(".", 1)[0]
    return {"bucket": "bucket.manage", "iam": "iam.manage", "volume": "volume.manage",
            "maintenance": "maintenance.execute", "file": "file.manage",
            "object": "object.manage",
            "mq": "mq.manage", "table": "table.manage"}.get(prefix)


def _write_guard(connection, action, payload):
    permissions = connection.get("permissions", {})
    if isinstance(permissions, str):
        permissions = json.loads(permissions)
    permission = _permission(action)
    if not connection.get("management_write_enabled") or permission is None or permissions.get(permission) is not True:
        raise AppError("MANAGEMENT_WRITE_DISABLED", "此管理连接未授予该操作的写入权限", 403)
    if action.startswith("bucket."):
        # Live acceptance writes are scoped independently of cluster read access.
        bucket = payload.get("name") or payload.get("bucket")
        prefixes = permissions.get("bucket_write_prefixes", [])
        if not isinstance(bucket, str) or not isinstance(prefixes, list) or not any(isinstance(prefix, str) and prefix and bucket.startswith(prefix) for prefix in prefixes):
            raise AppError("MANAGEMENT_BUCKET_FORBIDDEN", "桶不在此管理连接批准的写入范围内", 403)


def perform_operation(settings, management, actor_id, action, method, path, payload,
                      readback_path=None, readback_params=None, idempotency_key=None,
                      expected_before_hash=None, verify: Callable | None = None,
                      return_created_secret=False, invoke: Callable | None = None,
                      readback: Callable | None = None, snapshot_filter: Callable | None = None,
                      params=None):
    """Record intent, dispatch once, verify readback, publish a redacted receipt.

    ``verify(before, after, response)`` is mandatory for confirmed success.
    A generated credential may be returned once but is never persisted.
    """
    _write_guard(management, action, payload)
    from .management import _public_connection
    with db.connect(settings) as conn:
        latest = conn.execute("SELECT * FROM management_connections WHERE id=?", (management["id"],)).fetchone()
        if latest is None:
            raise AppError("MANAGEMENT_CONNECTION_NOT_FOUND", "管理连接已不存在", 404)
        _write_guard(_public_connection(dict(latest)), action, payload)
    safe_payload = redact(payload)
    if _has_secret_input(payload) or safe_payload != payload:
        raise AppError("MANAGEMENT_SECRET_INPUT_FORBIDDEN", "不能把手动输入的密钥或密码保存为管理请求", 422)
    if method.upper() not in ("POST", "PUT", "DELETE", "PATCH"):
        raise AppError("INVALID_MANAGEMENT_METHOD", "该接口只接受显式管理写入", 422)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for management writes.", 422)
    key = idempotency_key.strip()
    identifier, stamp = "mgop_" + uuid.uuid4().hex, utc_now()
    namespace = f"{actor_id}:{management['id']}:{action}"
    digest = canonical_hash({"method": method, "path": path, "payload": payload,
                             "params": params, "expected_before_hash": expected_before_hash})
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT * FROM management_operations WHERE namespace=? AND idempotency_key=?", (namespace, key)).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                raise AppError("IDEMPOTENCY_CONFLICT", "同一管理请求的目标或参数已变化", 409)
            return {"operation_id": prior["id"], "status": prior["state"],
                    "result": json.loads(prior["result_json"] or "{}"),
                    "secret_receipt_available": False, "replayed": True}
        # If persistence fails, no network mutation can have started.
        conn.execute("""INSERT INTO management_operations(id,management_id,actor_id,action,method,path,
          request_json,request_hash,namespace,idempotency_key,state,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,'planned',?,?)""",
                     (identifier, management["id"], actor_id, action, method, path,
                      json.dumps(safe_payload), digest, namespace, key, stamp, stamp))

    client = None
    before = None
    dispatched = False
    write_returned = False
    try:
        if invoke is None or (readback is None and readback_path):
            from .management import admin_client
            with db.connect(settings) as conn:
                client = admin_client(settings, conn, management)

        def read():
            if readback:
                return readback()
            if readback_path:
                return client.get_json(readback_path, params=readback_params)
            return None

        if readback is not None or readback_path:
            try:
                before = read()
            except AppError as exc:
                if exc.status != 404:
                    raise
        semantic = snapshot_filter(before) if snapshot_filter else before
        if expected_before_hash and canonical_hash(semantic) != expected_before_hash:
            raise AppError("MANAGEMENT_RESOURCE_CHANGED", "管理资源已被修改，请重新读取差异", 409)
        with db.connect(settings) as conn:
            # Authorization may have been revoked while the remote snapshot was read.
            conn.execute("BEGIN IMMEDIATE")
            from .management import _public_connection
            latest = conn.execute("SELECT * FROM management_connections WHERE id=?", (management["id"],)).fetchone()
            if latest is None:
                raise AppError("MANAGEMENT_CONNECTION_NOT_FOUND", "管理连接已不存在", 404)
            _write_guard(_public_connection(dict(latest)), action, payload)
            result = conn.execute("UPDATE management_operations SET state='dispatching',before_json=?,updated_at=? WHERE id=? AND state='planned'",
                                  (json.dumps(redact(semantic)), utc_now(), identifier))
            if result.rowcount != 1:
                raise AppError("MANAGEMENT_INTENT_CHANGED", "管理意图已失效", 409)
        dispatched = True
        response = invoke() if invoke else client.request_json(method, path, json_body=payload, params=params, raw=True)
        write_returned = True
        try:
            after = read()
        except AppError as exc:
            if exc.status == 404 and method.upper() == "DELETE":
                after = None
            else:
                raise
        confirmed = verify is not None and verify(before, after, response) is True
        state = "confirmed" if confirmed else "needs_review"
        public_result = {"response": redact(response), "readback": redact(after),
                         "confirmed": confirmed, "remote_cas_supported": False}
        with db.connect(settings) as conn:
            conn.execute("UPDATE management_operations SET state=?,result_json=?,updated_at=? WHERE id=? AND state='dispatching'",
                         (state, json.dumps(public_result), utc_now(), identifier))
        receipt = {"operation_id": identifier, "status": state, "result": public_result, "replayed": False}
        if return_created_secret:
            credentials = _credential_receipt(response)
            receipt["secret_receipt_available"] = bool(confirmed and credentials)
            if confirmed and credentials:
                receipt["created_credentials"] = credentials
        return receipt
    except Exception as exc:
        code = getattr(exc, "code", "MANAGEMENT_OPERATION_FAILED")
        known_rejection = isinstance(exc, AppError) and exc.status in (400, 401, 403, 404, 405, 409, 412, 422, 501)
        state = "failed" if not dispatched or (known_rejection and not write_returned) else "needs_review"
        with db.connect(settings) as conn:
            conn.execute("UPDATE management_operations SET state=?,error_code=?,updated_at=? WHERE id=?", (state, code, utc_now(), identifier))
        if isinstance(exc, AppError):
            raise
        raise AppError("MANAGEMENT_OUTCOME_UNKNOWN" if dispatched else "MANAGEMENT_OPERATION_FAILED",
                       "远端结果需核查，禁止自动重复写入" if dispatched else "管理请求未能执行", 502) from None
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()


def list_operations(settings, management_id, limit=100):
    limit = min(200, max(1, limit))
    with db.connect(settings) as conn:
        rows = conn.execute("SELECT id,actor_id,action,state,error_code,created_at,updated_at FROM management_operations WHERE management_id=? ORDER BY created_at DESC LIMIT ?",
                            (management_id, limit)).fetchall()
        items = [dict(row) for row in rows]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_condition_probes'").fetchone():
            probes = conn.execute("SELECT id,actor_id,'object.condition.probe' AS action,state,error_code,created_at,updated_at FROM management_condition_probes WHERE management_id=? ORDER BY created_at DESC LIMIT ?",
                                  (management_id, limit)).fetchall()
            items.extend(dict(row) for row in probes)
    return {"items": sorted(items, key=lambda item: (item["created_at"], item["id"]), reverse=True)[:limit]}


def _credential_receipt(value):
    """Return only an actual generated public id + usable secret pair."""
    if not isinstance(value, dict):
        return None
    normalized = {re.sub(r"[_-]", "", key).lower(): item for key, item in value.items()}
    public = normalized.get("accesskey") or normalized.get("accesskeyid")
    secret = normalized.get("secretkey") or normalized.get("secretaccesskey")
    if isinstance(public, str) and isinstance(secret, str) and secret and not secret.startswith("[REDACTED"):
        return {"access_key": public, "secret_key": secret}
    for item in value.values():
        if isinstance(item, dict):
            found = _credential_receipt(item)
            if found:
                return found
        elif isinstance(item, list):
            for child in item:
                found = _credential_receipt(child)
                if found:
                    return found
    return None
