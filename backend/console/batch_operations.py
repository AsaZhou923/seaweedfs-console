from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Request

from . import catalog, core, db, jobs, storage
from .security import AppError, current_user, mutation, utc_now


router = APIRouter(prefix="/api/v1")
MAX_COPY_ITEMS = 60
COPY_READ_LIMIT = 32 * 1024 * 1024


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS copy_batches (
          id TEXT PRIMARY KEY,
          job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
          source_scope_id TEXT NOT NULL REFERENCES scopes(id),
          actor TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS copy_batch_items (
          id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL REFERENCES copy_batches(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          source_scope_id TEXT NOT NULL REFERENCES scopes(id),
          source_object_id TEXT NOT NULL,
          source_revision TEXT NOT NULL,
          source_key TEXT NOT NULL,
          target_scope_id TEXT NOT NULL REFERENCES scopes(id),
          target_key TEXT NOT NULL,
          target_object_id TEXT,
          phase TEXT NOT NULL DEFAULT 'planned',
          expected_sha256 TEXT,
          content_type TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          result_json TEXT NOT NULL DEFAULT '{}',
          error_code TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(batch_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_copy_batch_items_batch ON copy_batch_items(batch_id, ordinal);
        """
    )


@router.post("/scopes/{source_scope_id}/objects/copy-batches")
def create_copy_batch(request: Request, source_scope_id: str, body: dict[str, Any]):
    principal = mutation(request)
    idempotency_key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    if not idempotency_key:
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for copy batches.", 400)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        return submit_copy_batch(conn, source_scope_id, body, actor=principal.id, idempotency_key=idempotency_key)


@router.get("/scopes/{scope_id}/objects/copy-batches/{batch_id}")
def get_copy_batch(request: Request, scope_id: str, batch_id: str):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        initialize(conn)
        source_scope = core.ensure_scope_permission(conn, scope_id, "object:read")
        batch = conn.execute("SELECT * FROM copy_batches WHERE id=? AND source_scope_id=?", (batch_id, scope_id)).fetchone()
        if not batch:
            raise AppError("COPY_BATCH_NOT_FOUND", "Copy batch was not found.", 404)
        items = []
        counts: dict[str, int] = {}
        for row in conn.execute("SELECT * FROM copy_batch_items WHERE batch_id=? ORDER BY ordinal", (batch_id,)):
            item = dict(row)
            try:
                target_scope = core.ensure_scope_permission(conn, item["target_scope_id"], "object:read")
            except AppError:
                continue
            if target_scope["project_id"] != source_scope["project_id"]:
                continue
            counts[item["phase"]] = counts.get(item["phase"], 0) + 1
            items.append(_public_item(item))
        if not items:
            raise AppError("COPY_BATCH_NOT_FOUND", "Copy batch was not found.", 404)
        return {"id": batch["id"], "job_id": batch["job_id"], "state": batch["status"], "counts": counts, "items": items}


def submit_copy_batch(
    conn: sqlite3.Connection,
    source_scope_id: str,
    body: dict[str, Any],
    *,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    initialize(conn)
    source_scope = core.ensure_scope_permission(conn, source_scope_id, "object:read")
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_COPY_ITEMS:
        raise AppError("INVALID_COPY_BATCH", "items must contain 1-60 copy targets.", 422, field="items")
    planned = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise AppError("INVALID_COPY_BATCH", "items must be objects.", 422, field=f"items.{index}")
        source_object_id = _required(raw, "object_id", index)
        target_scope_id = _required(raw, "target_scope_id", index)
        target_key = _required(raw, "target_key", index)
        source = core.get_object(conn, source_scope_id, source_object_id)
        if source["revision"] != raw.get("source_revision", source["revision"]):
            raise AppError("OBJECT_REVISION_CHANGED", "Source object revision changed.", 409, field=f"items.{index}.object_id")
        target_scope = core.ensure_scope_permission(conn, target_scope_id, "object:write")
        if target_scope["project_id"] != source_scope["project_id"]:
            raise AppError("FORBIDDEN_SCOPE", "Copy target scope must belong to the same project.", 403)
        core.assert_key_in_scope(target_scope, target_key)
        planned.append(
            {
                "ordinal": index,
                "source_scope_id": source_scope_id,
                "source_object_id": source["id"],
                "source_revision": source["revision"],
                "source_key": source["key"],
                "target_scope_id": target_scope_id,
                "target_key": target_key,
            }
        )

    params = {"source_scope_id": source_scope_id, "items": planned}
    job = jobs.submit_job(conn, source_scope_id, "copy_batch", params, actor=actor, idempotency_key=idempotency_key, effect_class="remote_mutating")
    existing = conn.execute("SELECT * FROM copy_batches WHERE job_id=?", (job["id"],)).fetchone()
    if existing:
        return _batch_public(conn, dict(existing), job)

    stamp = utc_now()
    batch_id = "cpb_" + uuid.uuid4().hex
    request_hash = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO copy_batches(id, job_id, source_scope_id, actor, idempotency_key, request_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, job["id"], source_scope_id, actor, idempotency_key, request_hash, stamp, stamp),
    )
    for item in planned:
        conn.execute(
            """
            INSERT INTO copy_batch_items(
              id,batch_id,ordinal,source_scope_id,source_object_id,source_revision,source_key,
              target_scope_id,target_key,created_at,updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cpi_" + uuid.uuid4().hex,
                batch_id,
                item["ordinal"],
                item["source_scope_id"],
                item["source_object_id"],
                item["source_revision"],
                item["source_key"],
                item["target_scope_id"],
                item["target_key"],
                stamp,
                stamp,
            ),
        )
    return _batch_public(conn, dict(conn.execute("SELECT * FROM copy_batches WHERE id=?", (batch_id,)).fetchone()), job)


def execute_copy_batch(job: dict[str, Any], settings: Any) -> None:
    with db.connect(settings) as conn:
        initialize(conn)
        batch = conn.execute("SELECT * FROM copy_batches WHERE job_id=?", (job["id"],)).fetchone()
        if not batch:
            raise AppError("COPY_BATCH_NOT_FOUND", "Copy batch metadata was not found.", 404)
        batch = dict(batch)
        rows = [dict(row) for row in conn.execute("SELECT * FROM copy_batch_items WHERE batch_id=? ORDER BY ordinal", (batch["id"],))]

    for item in rows:
        if item["phase"] != "planned":
            continue
        _process_item(settings, job, batch, item)

    with db.connect(settings) as conn:
        initialize(conn)
        counts = _counts(conn, batch["id"])
        if counts.get("needs_review"):
            state, error_code = "needs_review", "COPY_TARGET_NEEDS_REVIEW"
        elif counts.get("failed"):
            state, error_code = "partially_failed" if counts.get("confirmed") else "failed", "COPY_BATCH_PARTIAL_FAILURE"
        else:
            state, error_code = "succeeded", None
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, job)
        conn.execute("UPDATE copy_batches SET status=?, updated_at=? WHERE id=?", (state, utc_now(), batch["id"]))
        jobs.checkpoint(conn, job, state=state, processed=counts.get("confirmed", 0), errors=counts.get("failed", 0) + counts.get("needs_review", 0), data={"batch_id": batch["id"], "counts": counts}, error_code=error_code)
        conn.commit()


def _process_item(settings: Any, job: dict[str, Any], batch: dict[str, Any], item: dict[str, Any]) -> None:
    try:
        with db.connect(settings) as conn:
            source_scope = core.ensure_scope_permission(conn, item["source_scope_id"], "object:read")
            target_scope = core.ensure_scope_permission(conn, item["target_scope_id"], "object:write")
            if source_scope["project_id"] != target_scope["project_id"]:
                raise AppError("FORBIDDEN_SCOPE", "Copy source and target must be in the same project.", 403)
            source = core.get_object(conn, item["source_scope_id"], item["source_object_id"])
            if source["revision"] != item["source_revision"]:
                raise AppError("OBJECT_REVISION_CHANGED", "Source object revision changed.", 409)
            source_client = storage.client(core._connection_private(conn, source_scope["connection_id"]), settings)
            target_client = storage.client(core._connection_private(conn, target_scope["connection_id"]), settings)

        _assert_job_current(settings, job)
        source_bytes = catalog.read_bound(source_client, source_scope, source, max_bytes=COPY_READ_LIMIT)
        expected_sha = hashlib.sha256(source_bytes).hexdigest()
        content_type = source.get("content_type") or "application/octet-stream"
        metadata = {
            "swc-copy-batch-id": batch["id"],
            "swc-copy-item-id": item["id"],
            "swc-source-object-id": source["id"],
            "swc-source-revision": source["revision"],
            "swc-source-sha256": expected_sha,
        }

        with db.connect(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs.fenced(conn, job)
            conn.execute(
                """
                UPDATE copy_batch_items
                SET phase='dispatching', expected_sha256=?, content_type=?, metadata_json=?, updated_at=?
                WHERE id=? AND phase='planned'
                """,
                (expected_sha, content_type, json.dumps(metadata, sort_keys=True, separators=(",", ":")), utc_now(), item["id"]),
            )
            jobs.checkpoint(conn, job, state="running", data={"batch_id": batch["id"], "item_id": item["id"], "phase": "dispatching"})
            conn.commit()

        _assert_job_current(settings, job)
        try:
            target_client.put_object(
                Bucket=target_scope["bucket"],
                Key=item["target_key"],
                Body=source_bytes,
                ContentType=content_type,
                Metadata=metadata,
                IfNoneMatch="*",
            )
        except Exception as exc:
            if _status_from_exception(exc) != 412:
                raise storage.sanitize_storage_exception(exc) from exc

        result = _verify_target(target_client, target_scope, item["target_key"], expected_sha, metadata)
        if result["status"] != "confirmed":
            _finish_item(settings, job, item, result["status"], error_code="COPY_TARGET_NEEDS_REVIEW", result=result)
            return
        _finish_confirmed_item(settings, job, item, target_scope, result, expected_sha, content_type)
    except AppError as exc:
        _finish_item(settings, job, item, "failed", error_code=exc.code, result={"message": str(exc)})
    except storage.StorageError as exc:
        phase = "needs_review" if exc.status >= 500 else "failed"
        _finish_item(settings, job, item, phase, error_code=exc.code, result={"message": str(exc)})


def _verify_target(target_client: Any, target_scope: dict[str, Any], target_key: str, expected_sha: str, metadata: dict[str, str]) -> dict[str, Any]:
    head = _head_or_none(target_client, target_scope["bucket"], target_key)
    if not head:
        return {"status": "needs_review", "reason": "target write outcome is unknown"}
    observed_metadata = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or head.get("metadata") or {}).items()}
    for key, value in metadata.items():
        if observed_metadata.get(key) != value:
            return {"status": "needs_review", "reason": "target exists without matching copy metadata", "target": _head_public(head)}
    provisional = {
        "key": target_key,
        "revision": core.make_revision(target_scope["bucket"], target_key, catalog.observation(target_key, head)),
        "version_id": head.get("VersionId"),
        "etag": head.get("ETag"),
        "content_type": head.get("ContentType"),
    }
    data = catalog.read_bound(target_client, target_scope, provisional, max_bytes=COPY_READ_LIMIT)
    observed_sha = hashlib.sha256(data).hexdigest()
    if observed_sha != expected_sha:
        return {"status": "needs_review", "reason": "target checksum mismatch", "observed_sha256": observed_sha, "target": _head_public(head)}
    return {"status": "confirmed", "observed_sha256": observed_sha, "target": _head_public(head)}


def _finish_confirmed_item(settings: Any, job: dict[str, Any], item: dict[str, Any], target_scope: dict[str, Any], result: dict[str, Any], expected_sha: str, content_type: str) -> None:
    head = result["target"]
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, job)
        target = core.upsert_object(
            conn,
            item["target_scope_id"],
            {
                "key": item["target_key"],
                "version_id": head.get("VersionId"),
                "size": head.get("ContentLength", 0),
                "etag": head.get("ETag"),
                "last_modified": core._head_last_modified(head),
                "content_type": content_type,
                "checksum": expected_sha,
                "properties": {"copy_source_object_id": item["source_object_id"], "copy_batch_id": item["batch_id"]},
                "scan_id": job["id"],
            },
        )
        if _table_exists(conn, "object_relationships"):
            conn.execute(
                """
                INSERT OR IGNORE INTO object_relationships(
                  id,project_id,scope_id,source_object_id,target_object_id,relation_type,
                  relation_source,evidence_json,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rel_" + uuid.uuid4().hex,
                    target_scope["project_id"],
                    item["target_scope_id"],
                    item["source_object_id"],
                    target["id"],
                    "copy",
                    "copy_batch",
                    json.dumps({"batch_id": item["batch_id"], "item_id": item["id"], "sha256": expected_sha}, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                ),
            )
        _update_item(conn, item["id"], "confirmed", target_object_id=target["id"], expected_sha=expected_sha, content_type=content_type, result=result)
        jobs.checkpoint(conn, job, state="running", data={"batch_id": item["batch_id"], "item_id": item["id"], "phase": "confirmed"})
        conn.commit()


def _finish_item(settings: Any, job: dict[str, Any], item: dict[str, Any], phase: str, *, error_code: str, result: dict[str, Any]) -> None:
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, job)
        _update_item(conn, item["id"], phase, error_code=error_code, result=result)
        jobs.checkpoint(conn, job, state="running", data={"batch_id": item["batch_id"], "item_id": item["id"], "phase": phase}, error_code=error_code)
        conn.commit()


def _update_item(
    conn: sqlite3.Connection,
    item_id: str,
    phase: str,
    *,
    target_object_id: str | None = None,
    expected_sha: str | None = None,
    content_type: str | None = None,
    error_code: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE copy_batch_items
        SET phase=?, target_object_id=COALESCE(?, target_object_id),
            expected_sha256=COALESCE(?, expected_sha256),
            content_type=COALESCE(?, content_type),
            error_code=?, result_json=?, updated_at=?
        WHERE id=?
        """,
        (phase, target_object_id, expected_sha, content_type, error_code, json.dumps(result or {}, sort_keys=True, separators=(",", ":")), utc_now(), item_id),
    )


def _assert_job_current(settings: Any, job: dict[str, Any]) -> None:
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, job)
        conn.commit()


def _head_or_none(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _status_from_exception(exc) == 404:
            return None
        raise storage.sanitize_storage_exception(exc) from exc


def _status_from_exception(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return status
    return None


def _counts(conn: sqlite3.Connection, batch_id: str) -> dict[str, int]:
    return {
        row["phase"]: row["count"]
        for row in conn.execute(
            "SELECT phase, COUNT(*) AS count FROM copy_batch_items WHERE batch_id=? GROUP BY phase",
            (batch_id,),
        )
    }


def _batch_public(conn: sqlite3.Connection, batch: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": batch["id"],
        "job": job,
        "state": batch["status"],
        "counts": _counts(conn, batch["id"]),
        "items": [_public_item(dict(row)) for row in conn.execute("SELECT * FROM copy_batch_items WHERE batch_id=? ORDER BY ordinal", (batch["id"],))],
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "phase": item["phase"],
        "source_object_id": item["source_object_id"],
        "source_revision": item["source_revision"],
        "target_scope_id": item["target_scope_id"],
        "target_key": item["target_key"],
        "target_object_id": item["target_object_id"],
        "error_code": item["error_code"],
        "result": json.loads(item["result_json"] or "{}"),
    }


def _head_public(head: dict[str, Any]) -> dict[str, Any]:
    return {
        "ContentLength": head.get("ContentLength"),
        "ContentType": head.get("ContentType"),
        "ETag": head.get("ETag"),
        "LastModified": core._head_last_modified(head),
        "VersionId": head.get("VersionId"),
        "Metadata": head.get("Metadata") or head.get("metadata") or {},
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _required(data: dict[str, Any], key: str, index: int) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise AppError("INVALID_COPY_BATCH", f"{key} is required.", 422, field=f"items.{index}.{key}")
    return value


jobs.HANDLERS["copy_batch"] = execute_copy_batch
