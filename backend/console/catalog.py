"""Scope-bound catalog queries, scans, previews, diagnostics and reporting."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from . import core, db, imaging, jobs, storage
from .security import AppError, current_user, mutation, sign_json, unsign_json, utc_add

router = APIRouter(prefix="/api/v1")
MAX_READ = 32 * 1024 * 1024
MAX_PAGE = 200
FILTER_NAMES = {"prefix", "query", "format", "decode_status", "tag", "min_width", "max_width", "min_height", "max_height", "min_size", "max_size", "min_ratio", "max_ratio", "after", "before", "presence"}
MAX_TAGS = 50
MAX_TAG_LENGTH = 128


def _int_param(value, name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_FILTER", f"{name} must be an integer", 422, field=name) from exc
    if not minimum <= number <= maximum:
        raise AppError("RESOURCE_LIMIT_EXCEEDED", f"{name} is outside the allowed range", 429, field=name)
    return number


def _float_param(value, name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_FILTER", f"{name} must be numeric", 422, field=name) from exc
    if not math.isfinite(number) or not minimum < number <= maximum:
        raise AppError("RESOURCE_LIMIT_EXCEEDED", f"{name} is outside the allowed range", 429, field=name)
    return number


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS preview_cache (
      cache_key TEXT PRIMARY KEY,object_id TEXT NOT NULL REFERENCES objects(id),
      revision TEXT NOT NULL,bytes INTEGER NOT NULL,accessed_at TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS operation_snapshots (
      id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(id),actor TEXT NOT NULL,
      authz_epoch INTEGER NOT NULL,filter_json TEXT NOT NULL,items_json TEXT NOT NULL,
      created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS diagnostic_results (
      id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(id),object_id TEXT,
      kind TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS capacity_samples (
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
      result_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS object_width ON objects(scope_id,json_extract(properties_json,'$.width'));
    CREATE INDEX IF NOT EXISTS object_decode ON objects(scope_id,json_extract(properties_json,'$.decode_status'));
    """)


def observation(key, head) -> dict:
    modified = head.get("LastModified")
    if hasattr(modified, "strftime"):
        modified = modified.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"key": key, "size": head.get("ContentLength", head.get("Size", 0)),
            "etag": head.get("ETag"), "last_modified": modified,
            "version_id": head.get("VersionId"), "content_type": head.get("ContentType")}


def _checkpoint_listing(entry: dict) -> dict:
    return observation(entry["Key"], entry)


def _scope_for_job(conn, job: dict, params: dict) -> dict:
    scope = core.get_scope(conn, job["scope_id"])
    if params.get("authz_epoch", scope["authz_epoch"]) != scope["authz_epoch"]:
        raise AppError("AUTHZ_EPOCH_CHANGED", "范围授权已改变", 409)
    return scope


def s3_for(conn, scope, settings):
    return storage.client(core._connection_private(conn, scope["connection_id"]), settings)


def read_bound(s3, scope, obj, max_bytes=MAX_READ) -> bytes:
    core.assert_key_in_scope(scope, obj["key"])
    kwargs = {"Bucket": scope["bucket"], "Key": obj["key"]}
    if obj.get("version_id") and obj["version_id"] != "null":
        kwargs["VersionId"] = obj["version_id"]
    try:
        before = s3.head_object(**kwargs)
        actual = core.make_revision(scope["bucket"], obj["key"], observation(obj["key"], before))
        if actual != obj["revision"]:
            raise AppError("OBJECT_REVISION_CHANGED", "源对象已改变，请重新扫描", 409)
        if before.get("ContentLength", 0) > max_bytes:
            raise AppError("RESOURCE_LIMIT_EXCEEDED", "对象超过读取预算", 429)
        response = s3.get_object(**kwargs, IfMatch=before["ETag"]) if before.get("ETag") else s3.get_object(**kwargs)
        body = response["Body"]
        try:
            parts, count = [], 0
            while True:
                chunk = body.read(min(1024 * 1024, max_bytes + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > max_bytes:
                    raise AppError("RESOURCE_LIMIT_EXCEEDED", "对象超过读取预算", 429)
                parts.append(chunk)
            data = b"".join(parts)
        finally:
            body.close()
        after = s3.head_object(**kwargs)
        if core.make_revision(scope["bucket"], obj["key"], observation(obj["key"], after)) != actual:
            raise AppError("OBJECT_REVISION_CHANGED", "读取过程中对象改变", 409)
        if len(data) != before.get("ContentLength", len(data)):
            raise AppError("INCOMPLETE_OBJECT_READ", "没有读到完整字节", 503)
        return data
    except (AppError, storage.StorageError):
        raise
    except Exception as exc:
        raise storage.sanitize_storage_exception(exc) from exc


def checksum_bound(s3, scope, obj, *, max_object_bytes: int, max_total_remaining: int) -> tuple[str, int]:
    if max_object_bytes < 1 or max_total_remaining < 1:
        raise AppError("RESOURCE_LIMIT_EXCEEDED", "checksum读取预算耗尽", 429)
    budget = min(max_object_bytes, max_total_remaining)
    core.assert_key_in_scope(scope, obj["key"])
    kwargs = {"Bucket": scope["bucket"], "Key": obj["key"]}
    if obj.get("version_id") and obj["version_id"] != "null":
        kwargs["VersionId"] = obj["version_id"]
    try:
        before = s3.head_object(**kwargs)
        actual = core.make_revision(scope["bucket"], obj["key"], observation(obj["key"], before))
        if actual != obj["revision"]:
            raise AppError("OBJECT_REVISION_CHANGED", "源对象已改变，请重新扫描", 409)
        if before.get("ContentLength") is not None and before["ContentLength"] > budget:
            raise AppError("RESOURCE_LIMIT_EXCEEDED", "checksum读取预算耗尽", 429)
        response = s3.get_object(**kwargs, IfMatch=before["ETag"]) if before.get("ETag") else s3.get_object(**kwargs)
        body = response["Body"]
        digest = hashlib.sha256()
        count = 0
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > budget:
                    raise AppError("RESOURCE_LIMIT_EXCEEDED", "checksum读取预算耗尽", 429)
                digest.update(chunk)
        finally:
            body.close()
        after = s3.head_object(**kwargs)
        if core.make_revision(scope["bucket"], obj["key"], observation(obj["key"], after)) != actual:
            raise AppError("OBJECT_REVISION_CHANGED", "读取过程中对象改变", 409)
        if before.get("ContentLength") is not None and count != before["ContentLength"]:
            raise AppError("INCOMPLETE_OBJECT_READ", "没有读到完整字节", 503)
        return digest.hexdigest(), count
    except (AppError, storage.StorageError):
        raise
    except Exception as exc:
        raise storage.sanitize_storage_exception(exc) from exc


def cache_key(scope, obj):
    binding = [scope["id"], scope["connection_id"], scope["bucket"], obj["key"], obj["revision"],
               "grid-v1", "640-webp", "safe-preview-v1", imaging.PIPELINE_VERSION]
    return hashlib.sha256(json.dumps(binding).encode()).hexdigest()


def cache_path(settings, digest):
    return settings.data_dir / "previews" / digest[:2] / (digest + ".webp")


def _validate_tags(value) -> list[str]:
    if not isinstance(value, list):
        raise AppError("INVALID_REQUEST", "tags must be a list", 422, field="tags")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AppError("INVALID_REQUEST", "tags must be strings", 422, field="tags")
        tag = item.strip()
        if not tag or len(tag) > MAX_TAG_LENGTH:
            raise AppError("INVALID_REQUEST", "tag length must be 1-128", 422, field="tags")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise AppError("RESOURCE_LIMIT_EXCEEDED", "too many tags", 429, field="tags")
    return tags


def _visible_asset(conn, scope: dict, asset_id: str) -> dict:
    row = conn.execute(
        """
        SELECT a.* FROM assets a
        WHERE a.id=? AND a.project_id=? AND EXISTS (
          SELECT 1 FROM objects o
          WHERE o.asset_id=a.id AND o.scope_id=? AND o.is_current=1 AND o.presence='present'
        )
        """,
        (asset_id, scope["project_id"], scope["id"]),
    ).fetchone()
    if not row:
        raise AppError("ASSET_NOT_FOUND", "当前范围内未找到资产", 404)
    return dict(row)


def _visible_objects_for_asset(conn, scope: dict, asset_id: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM objects
        WHERE asset_id=? AND scope_id=? AND is_current=1 AND presence='present'
        ORDER BY first_seen_at DESC,id DESC LIMIT ?
        """,
        (asset_id, scope["id"], limit),
    ).fetchall()
    return [core._object_public(dict(row), scope) for row in rows]


def _table_exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _asset_relationships(conn, scope: dict, asset_id: str) -> dict:
    if not _table_exists(conn, "object_relationships"):
        return {"known_sources": [], "derived": [], "reference_status": "unknown"}
    object_ids = [row["id"] for row in conn.execute(
        "SELECT id FROM objects WHERE asset_id=? AND scope_id=? AND is_current=1",
        (asset_id, scope["id"]),
    )]
    if not object_ids:
        return {"known_sources": [], "derived": [], "reference_status": "unknown"}
    placeholders = ",".join("?" for _ in object_ids)
    visible = []
    for row in conn.execute(
        f"""
        SELECT r.* FROM object_relationships r
        JOIN objects so ON so.id=r.source_object_id
        JOIN objects tobj ON tobj.id=r.target_object_id
        JOIN scopes ss ON ss.id=so.scope_id AND ss.is_active=1
        JOIN scopes ts ON ts.id=tobj.scope_id AND ts.is_active=1
        WHERE r.project_id=? AND (r.source_object_id IN ({placeholders}) OR r.target_object_id IN ({placeholders}))
          AND so.is_current=1 AND tobj.is_current=1
        LIMIT 200
        """,
        (scope["project_id"], *object_ids, *object_ids),
    ):
        visible.append(
            {
                "id": row["id"],
                "source_object_id": row["source_object_id"],
                "target_object_id": row["target_object_id"],
                "relation_type": row["relation_type"],
                "relation_source": row["relation_source"],
                "evidence": json.loads(row["evidence_json"] or "{}"),
            }
        )
    derived = []
    if _table_exists(conn, "derived_variants"):
        for row in conn.execute(
            f"""
            SELECT v.id,v.source_object_id,v.output_scope_id,v.output_bucket,v.output_key,
                   v.status,v.manifest_json
            FROM derived_variants v
            JOIN scopes os ON os.id=v.output_scope_id AND os.is_active=1
            WHERE v.project_id=? AND v.source_object_id IN ({placeholders})
            LIMIT 200
            """,
            (scope["project_id"], *object_ids),
        ):
            derived.append(
                {
                    "id": row["id"],
                    "source_object_id": row["source_object_id"],
                    "output_scope_id": row["output_scope_id"],
                    "output_bucket": row["output_bucket"],
                    "output_key": row["output_key"],
                    "status": row["status"],
                    "manifest": json.loads(row["manifest_json"] or "{}"),
                }
            )
    return {"known_sources": visible, "derived": derived, "reference_status": "unknown"}


def _approved_distribution_endpoint(connection: dict, endpoint_id: str | None) -> str | None:
    approved = connection.get("approved_endpoints") or {}
    candidates = []
    if isinstance(approved, dict):
        if endpoint_id:
            candidates.append(approved.get(endpoint_id))
        candidates.extend(approved.get(key) for key in ("distribution_url", "distribution", "cdn", "public"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("url") or candidate.get("endpoint")
        if isinstance(candidate, str) and candidate:
            return candidate.rstrip("/")
    return None


def _validate_probe_origin(origin: str) -> None:
    parts = urllib.parse.urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise AppError("INVALID_REQUEST", "需有效的业务origin", 400, field="origin")
    if "\n" in origin or "\r" in origin:
        raise AppError("INVALID_REQUEST", "origin contains invalid header characters", 400, field="origin")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _capacity_result_for_project(conn, project_id: str) -> dict:
    total = conn.execute("""
      WITH physical AS (
        SELECT s.connection_id,s.bucket,o.key,
               MAX(COALESCE(o.size,0)) AS size,
               COALESCE(MAX(json_extract(o.properties_json,'$.format')),'unknown') AS format
        FROM objects o JOIN scopes s ON s.id=o.scope_id
        WHERE s.project_id=? AND s.is_active=1 AND o.is_current=1 AND o.presence='present'
        GROUP BY s.connection_id,s.bucket,o.key
      )
      SELECT COUNT(*) AS object_count, COALESCE(SUM(size),0) AS bytes FROM physical
    """, (project_id,)).fetchone()
    by_format = {
        row["format"]: row["bytes"]
        for row in conn.execute("""
          WITH physical AS (
            SELECT s.connection_id,s.bucket,o.key,
                   MAX(COALESCE(o.size,0)) AS size,
                   COALESCE(MAX(json_extract(o.properties_json,'$.format')),'unknown') AS format
            FROM objects o JOIN scopes s ON s.id=o.scope_id
            WHERE s.project_id=? AND s.is_active=1 AND o.is_current=1 AND o.presence='present'
            GROUP BY s.connection_id,s.bucket,o.key
          )
          SELECT format, COALESCE(SUM(size),0) AS bytes FROM physical GROUP BY format
        """, (project_id,))
    }
    by_bucket = {
        row["bucket"]: row["bytes"]
        for row in conn.execute("""
          WITH physical AS (
            SELECT s.connection_id,s.bucket,o.key,MAX(COALESCE(o.size,0)) AS size
            FROM objects o JOIN scopes s ON s.id=o.scope_id
            WHERE s.project_id=? AND s.is_active=1 AND o.is_current=1 AND o.presence='present'
            GROUP BY s.connection_id,s.bucket,o.key
          )
          SELECT bucket, COALESCE(SUM(size),0) AS bytes FROM physical GROUP BY bucket
        """, (project_id,))
    }
    scanned = conn.execute("SELECT COUNT(*) FROM scan_runs r JOIN scopes s ON s.id=r.scope_id WHERE s.project_id=? AND r.enumeration='complete'", (project_id,)).fetchone()[0]
    result = {
        "current_logical_bytes": total["bytes"],
        "object_count": total["object_count"],
        "by_format": by_format,
        "by_bucket": by_bucket,
        "version_bytes": None,
        "physical_disk_bytes": None,
        "reference_state": "unknown",
        "coverage": "observed" if scanned else "unknown",
        "observed_at": jobs.now(),
    }
    result["private_cache_bytes"] = conn.execute(
        "SELECT COALESCE(SUM(c.bytes),0) FROM preview_cache c JOIN objects o ON o.id=c.object_id JOIN scopes s ON s.id=o.scope_id WHERE s.project_id=? AND s.is_active=1",
        (project_id,),
    ).fetchone()[0]
    return result


def _persist_scan_capacity_sample(conn, scope: dict, job: dict, state: str, *, processed: int, errors: int) -> None:
    if not _table_exists(conn, "capacity_samples") or state not in ("succeeded", "partially_failed"):
        return
    result = _capacity_result_for_project(conn, scope["project_id"])
    result.update(
        {
            "scope_id": scope["id"],
            "scan_id": job["id"],
            "scan_state": state,
            "processed": processed,
            "errors": errors,
            "coverage": "complete_enumeration",
        }
    )
    conn.execute(
        "INSERT OR REPLACE INTO capacity_samples(id,project_id,result_json,created_at) VALUES(?,?,?,?)",
        (job["id"], scope["project_id"], json.dumps(result, ensure_ascii=False, separators=(",", ":")), jobs.now()),
    )


def evict_cache(settings, max_bytes=None) -> dict:
    maximum = int(os.environ.get("CONSOLE_CACHE_MAX_BYTES", 512 * 1024 * 1024)) if max_bytes is None else max_bytes
    ttl = int(os.environ.get("CONSOLE_CACHE_TTL_SECONDS", 7 * 86400))
    deleted = 0
    with db.connect(settings) as conn:
        rows = conn.execute("SELECT * FROM preview_cache ORDER BY accessed_at DESC").fetchall()
        used = 0
        for row in rows:
            age = time.time() - __import__("datetime").datetime.fromisoformat(row["accessed_at"].replace("Z", "+00:00")).timestamp()
            path = cache_path(settings, row["cache_key"])
            if used + row["bytes"] > maximum or age > ttl or not path.is_file():
                path.unlink(missing_ok=True)
                conn.execute("DELETE FROM preview_cache WHERE cache_key=?", (row["cache_key"],))
                deleted += 1
            else:
                used += row["bytes"]
    return {"bytes": used, "max_bytes": maximum, "removed_entries": deleted, "storage_objects_deleted": 0}


def execute_job(job, settings):
    params, progress = json.loads(job["params_json"]), json.loads(job["checkpoint_json"])
    with db.connect(settings) as conn:
        scope = _scope_for_job(conn, job, params)
        s3 = s3_for(conn, scope, settings)
    if job["kind"] == "scan":
        _scan(job, settings, scope, s3, params, progress)
    else:
        _object_job(job, settings, scope, s3, params, progress)


def _scan(job, settings, scope, s3, params, progress):
    token = progress.get("token")
    page = progress.get("page")
    next_token = progress.get("next_token")
    position = int(progress.get("position", 0))
    processed, errors = job["processed"], job["errors"]
    maximum = int(params.get("max_objects", 100000))
    page_size = max(1, min(1000, int(params.get("page_size", params.get("list_max_keys", 1)))))
    rate = float(params.get("objects_per_second", 20))
    while True:
        with db.connect(settings) as conn:
            current = jobs.fenced(conn, job)
            scope = _scope_for_job(conn, job, params)
            if current["pause_requested_at"] or current["cancel_requested_at"]:
                jobs.checkpoint(conn, job, state="running", processed=processed, errors=errors, data={"token": token, "next_token": next_token, "page": page, "position": position})
                return
        if page is None or position >= len(page):
            kwargs = {"Bucket": scope["bucket"], "Prefix": scope["prefix"], "MaxKeys": min(page_size, max(1, maximum - processed))}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                listed_page = s3.list_objects_v2(**kwargs)
            except Exception as exc:
                raise storage.sanitize_storage_exception(exc) from exc
            page = [_checkpoint_listing(item) for item in listed_page.get("Contents", [])]
            next_token = listed_page.get("NextContinuationToken") if listed_page.get("IsTruncated") else None
            position = 0
            with db.connect(settings) as conn:
                jobs.fenced(conn, job)
                _scope_for_job(conn, job, params)
                jobs.checkpoint(conn, job, state="running", processed=processed, errors=errors, data={"token": token, "next_token": next_token, "page": page, "position": position})
            if not page and not next_token:
                break
        while position < len(page):
            listed = page[position]
            started = time.monotonic()
            key = listed["key"]
            core.assert_key_in_scope(scope, key)
            with db.connect(settings) as conn:
                current = jobs.fenced(conn, job)
                scope = _scope_for_job(conn, job, params)
                if current["pause_requested_at"] or current["cancel_requested_at"]:
                    jobs.checkpoint(conn, job, state="running", processed=processed, errors=errors, data={"token": token, "next_token": next_token, "page": page, "position": position})
                    return
                # Refresh the lease before bounded network/decode work.
                jobs.checkpoint(conn, job, state="running", processed=processed, errors=errors, data={"token": token, "next_token": next_token, "page": page, "position": position})
            props, err = {}, None
            try:
                head = s3.head_object(Bucket=scope["bucket"], Key=key)
                obs = observation(key, head)
                provisional = obs | {"revision": core.make_revision(scope["bucket"], key, obs)}
                data = read_bound(s3, scope, provisional)
                props, _ = imaging.decode(data)
                if props["decode_status"] in ("corrupt", "resource_limited"):
                    err = "IMAGE_" + props["decode_status"].upper()
            except Exception as exc:
                err = getattr(exc, "code", "STORAGE_UNAVAILABLE")
                obs = observation(key, listed)
                props = {"decode_status": "resource_limited" if err == "RESOURCE_LIMIT_EXCEEDED" else "read_failed", "error_code": err}
            with db.connect(settings) as conn:
                jobs.fenced(conn, job)
                scope = _scope_for_job(conn, job, params)
                obj = core.upsert_object(conn, scope["id"], obs | {"properties": props, "scan_id": job["id"]})
                processed += 1
                errors += int(err is not None)
                position += 1
                if err:
                    conn.execute("INSERT INTO job_items(job_id,object_id,state,error_code,created_at) VALUES(?,?,?,?,?)",
                                 (job["id"], obj["id"], "failed", err, jobs.now()))
                jobs.checkpoint(conn, job, state="running", processed=processed, errors=errors, data={"token": token, "next_token": next_token, "page": page, "position": position})
            time.sleep(max(0, 1 / rate - (time.monotonic() - started)))
        if processed >= maximum and next_token:
            with db.connect(settings) as conn:
                jobs.checkpoint(conn, job, state="failed", processed=processed, errors=errors, data={"token": next_token, "next_token": None, "page": None, "position": 0}, error_code="SCAN_BUDGET_EXHAUSTED")
            return
        if next_token:
            token = next_token
            next_token, page, position = None, None, 0
            with db.connect(settings) as conn:
                jobs.checkpoint(conn, job, state="running", processed=processed, errors=errors, data={"token": token, "next_token": None, "page": None, "position": 0})
            continue
        page, position = None, 0
        break
    # Confirm previously missing candidates only with an authoritative 404 and bounded pages.
    last_candidate = ""
    while True:
        with db.connect(settings) as conn:
            candidates = [dict(row) for row in conn.execute(
                "SELECT * FROM objects WHERE scope_id=? AND is_current=1 AND scan_id IS NOT ? AND presence='missing_candidate' AND id>? ORDER BY id LIMIT 50",
                (scope["id"], job["id"], last_candidate),
            )]
        if not candidates:
            break
        for obj in candidates:
            last_candidate = obj["id"]
            try:
                s3.head_object(Bucket=scope["bucket"], Key=obj["key"])
            except Exception as exc:
                if storage.sanitize_storage_exception(exc).status == 404:
                    with db.connect(settings) as conn:
                        jobs.fenced(conn, job)
                        conn.execute("UPDATE objects SET presence='missing_confirmed' WHERE id=?", (obj["id"],))
    with db.connect(settings) as conn:
        jobs.fenced(conn, job)
        scope = _scope_for_job(conn, job, params)
        conn.execute("UPDATE objects SET presence='missing_candidate' WHERE scope_id=? AND is_current=1 AND scan_id IS NOT ? AND presence='present'", (scope["id"], job["id"]))
        final_state = "partially_failed" if errors else "succeeded"
        _persist_scan_capacity_sample(conn, scope, job, final_state, processed=processed, errors=errors)
        jobs.checkpoint(conn, job, state=final_state, processed=processed, errors=errors, data={"enumeration_complete": True})
    return


def _object_job(job, settings, scope, s3, params, progress):
    identifiers = params["object_ids"]
    processed, errors = job["processed"], job["errors"]
    index, bytes_read = progress.get("index", 0), progress.get("bytes_read", 0)
    while index < len(identifiers):
        with db.connect(settings) as conn:
            row = jobs.fenced(conn, job)
            scope = _scope_for_job(conn, job, params)
            if row["pause_requested_at"] or row["cancel_requested_at"]:
                jobs.checkpoint(conn, job, state="running", data={"index": index, "bytes_read": bytes_read})
                return
            obj = core.get_object(conn, scope["id"], identifiers[index])
            jobs.checkpoint(conn, job, state="running")
        error, props, checksum, output = None, obj["properties"], obj.get("checksum"), None
        try:
            if job["kind"] == "checksum" and checksum:
                pass
            else:
                if job["kind"] == "checksum":
                    checksum, consumed = checksum_bound(
                        s3,
                        scope,
                        obj,
                        max_object_bytes=int(params.get("max_object_bytes", 1024 * 1024 * 1024)),
                        max_total_remaining=int(params.get("max_total_bytes", 256 * 1024 * 1024)) - bytes_read,
                    )
                    bytes_read += consumed
                else:
                    data = read_bound(s3, scope, obj, max_bytes=MAX_READ)
                    bytes_read += len(data)
                    props, output = imaging.decode(data, params={"long_edge": 640, "format": "webp", "color": "srgb"})
                    if output is None:
                        raise AppError("PREVIEW_DECODE_FAILED", "图片不能生成预览", 422)
        except Exception as exc:
            error = getattr(exc, "code", "STORAGE_UNAVAILABLE")
        with db.connect(settings) as conn:
            jobs.fenced(conn, job)
            _scope_for_job(conn, job, params)
            if output is not None:
                digest = cache_key(scope, obj)
                path = cache_path(settings, digest)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix("." + job["lease_token"] + ".tmp")
                tmp.write_bytes(output)
                tmp.replace(path)
                conn.execute("INSERT OR REPLACE INTO preview_cache VALUES(?,?,?,?,?,?)", (digest, obj["id"], obj["revision"], len(output), jobs.now(), jobs.now()))
                props["preview_state"] = "ready"
            conn.execute("UPDATE objects SET properties_json=?,checksum=? WHERE id=?", (json.dumps(props), checksum, obj["id"]))
            conn.execute("INSERT INTO job_items(job_id,object_id,state,error_code,created_at) VALUES(?,?,?,?,?)", (job["id"], obj["id"], "failed" if error else "succeeded", error, jobs.now()))
            index += 1
            processed += 1
            errors += int(error is not None)
            jobs.checkpoint(conn, job, state="running" if index < len(identifiers) else "partially_failed" if errors else "succeeded", processed=processed, errors=errors, data={"index": index, "bytes_read": bytes_read})
    evict_cache(settings)


def _filters(request):
    ignored = {"limit", "cursor"}
    values = {k: v for k, v in request.query_params.items() if k not in ignored and v != ""}
    if set(values) - FILTER_NAMES:
        raise AppError("INVALID_FILTER", "未知筛选条件", 400)
    return values


def query_catalog(conn, settings, scope, filters, limit=60, cursor=None):
    if not isinstance(filters, dict) or set(filters) - FILTER_NAMES:
        raise AppError("INVALID_FILTER", "未知筛选条件", 400)
    limit = min(MAX_PAGE, max(1, limit))
    where, values = ["o.scope_id=?", "o.is_current=1"], [scope["id"]]
    for name, value in filters.items():
        if name == "prefix":
            where.append("substr(o.key,1,?)=?")
            values.extend([len(value), value])
        elif name == "query":
            where.append("instr(o.key,?)>0")
            values.append(value)
        elif name == "tag":
            where.append("EXISTS(SELECT 1 FROM json_each(a.tags_json) WHERE value=?)")
            values.append(value)
        elif name in ("format", "decode_status"):
            where.append(f"COALESCE(json_extract(o.properties_json,'$.{name}'),'unknown')=?")
            values.append(value)
        elif name in ("after", "before"):
            where.append("o.last_modified" + (">=?" if name == "after" else "<=?"))
            try:
                instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if instant.tzinfo is None or instant.utcoffset() is None:
                    raise ValueError()
                values.append(instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
            except (ValueError, TypeError, OverflowError):
                raise AppError("INVALID_FILTER", "时间筛选需包含时区的 ISO 日期", 422)
        elif name == "presence":
            where.append("o.presence=?")
            values.append(value)
        else:
            operator, column = name.split("_", 1)
            expression = "o.size" if column == "size" else f"json_extract(o.properties_json,'$.{column}')"
            where.append(expression + (">=?" if operator == "min" else "<=?"))
            try:
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError()
                values.append(number)
            except (ValueError, TypeError):
                raise AppError("INVALID_FILTER", "数值筛选无效", 422)
    binding = {"scope": scope["id"], "filter": filters, "epoch": scope["authz_epoch"], "generation": scope["index_generation"]}
    if cursor:
        try:
            payload = unsign_json(cursor, settings.require_cursor_key(), "catalog:v1")
            if payload["binding"] != binding or payload["expires"] <= jobs.now():
                raise ValueError()
            last = payload["last"]
            where.append("(a.first_seen_at<? OR (a.first_seen_at=? AND a.id<?))")
            values.extend([last[0], last[0], last[1]])
        except (ValueError, KeyError, TypeError):
            raise AppError("INVALID_CURSOR", "翻页条件已经改变，请重新查询", 400)
    rows = conn.execute("SELECT o.*,a.first_seen_at AS asset_first_seen_at,a.tags_json FROM objects o JOIN assets a ON a.id=o.asset_id WHERE " + " AND ".join(where) + " ORDER BY a.first_seen_at DESC,a.id DESC LIMIT ?", (*values, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        obj = core._object_public(dict(row), scope)
        props = obj["properties"]
        obj.update({key: props.get(key) for key in ("format", "width", "height", "ratio", "decode_status", "has_alpha")})
        obj["primary_object_id"] = obj["id"]
        obj["tags"] = json.loads(row["tags_json"])
        digest = cache_key(scope, obj)
        if scope["allow_preview"] and cache_path(settings, digest).exists():
            obj["preview_state"] = "ready"
            obj["preview_url"] = f"/api/v1/scopes/{scope['id']}/objects/{obj['id']}/preview"
        else:
            obj["preview_state"] = "not_ready" if scope["allow_preview"] else "forbidden"
        items.append(obj)
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = sign_json({"binding": binding, "last": [last["asset_first_seen_at"], last["asset_id"]], "expires": utc_add(3600)}, settings.require_cursor_key(), "catalog:v1")
    return {"items": items, "next_cursor": next_cursor, "index_generation": scope["index_generation"], "warnings": []}


@router.get("/scopes/{scope_id}/assets")
def assets(request: Request, scope_id: str, limit: int = 60, cursor: str | None = None):
    scope = core.require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        return query_catalog(conn, request.app.state.settings, scope, _filters(request), limit, cursor)


@router.put("/scopes/{scope_id}/assets/{asset_id}/tags")
def update_asset_tags(request: Request, scope_id: str, asset_id: str, body: dict):
    mutation(request)
    scope = core.require_scope(request, scope_id)
    tags = _validate_tags(body.get("tags"))
    with db.connect(request.app.state.settings) as conn:
        _visible_asset(conn, scope, asset_id)
        conn.execute("UPDATE assets SET tags_json=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), asset_id))
        conn.commit()
        return {"asset_id": asset_id, "tags": tags}


@router.get("/scopes/{scope_id}/assets/{asset_id}")
def asset_detail(request: Request, scope_id: str, asset_id: str):
    scope = core.require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        asset = _visible_asset(conn, scope, asset_id)
        visible_objects = _visible_objects_for_asset(conn, scope, asset_id)
        relationships = _asset_relationships(conn, scope, asset_id)
        return {
            "asset_id": asset["id"],
            "project_id": asset["project_id"],
            "first_seen_at": asset["first_seen_at"],
            "tags": json.loads(asset["tags_json"] or "[]"),
            "visible_objects": visible_objects,
            **relationships,
        }


@router.get("/projects/{project_id}/groups/{group_id}/members")
def group_members(request: Request, project_id: str, group_id: str, scope: str | None = None, limit: int = 200):
    current_user(request)
    limit = min(200, max(1, limit))
    with db.connect(request.app.state.settings) as conn:
        core.get_project(conn, project_id)
        if not _table_exists(conn, "image_group_members"):
            return {"items": [], "next_cursor": None}
        group = conn.execute("SELECT * FROM image_groups WHERE id=? AND project_id=?", (group_id, project_id)).fetchone()
        if not group:
            raise AppError("GROUP_NOT_FOUND", "group not found", 404)
        values: list[Any] = [group_id, project_id]
        scope_clause = ""
        if scope:
            checked = core.get_scope(conn, scope)
            if checked["project_id"] != project_id:
                raise AppError("FORBIDDEN_SCOPE", "scope is not in project", 403)
            scope_clause = " AND o.scope_id=?"
            values.append(scope)
        rows = conn.execute(
            """
            SELECT m.group_id,m.relation_source,m.created_at,o.*
            FROM image_group_members m
            JOIN objects o ON o.id=m.object_id
            JOIN scopes s ON s.id=o.scope_id AND s.is_active=1
            WHERE m.group_id=? AND s.project_id=? AND o.is_current=1 AND o.presence='present'
            """ + scope_clause + " ORDER BY m.created_at DESC,o.id DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        items = []
        for row in rows:
            member_scope = core.get_scope(conn, row["scope_id"])
            obj = core._object_public(dict(row), member_scope)
            obj["relation_source"] = row["relation_source"]
            obj["member_created_at"] = row["created_at"]
            items.append(obj)
        return {"group_id": group_id, "items": items, "next_cursor": None}


@router.post("/scopes/{scope_id}/scans")
def scan(request: Request, scope_id: str, body: dict):
    user = mutation(request)
    scope = core.require_scope(request, scope_id)
    max_objects = _int_param(body.get("max_objects"), "max_objects", 100000, minimum=1, maximum=1000000)
    rate = _float_param(body.get("objects_per_second"), "objects_per_second", 20, minimum=0, maximum=100)
    params = {"authz_epoch": scope["authz_epoch"], "max_objects": max_objects, "objects_per_second": rate}
    with db.connect(request.app.state.settings) as conn:
        try:
            return jobs.submit_job(conn, scope_id, "scan", params, actor=user.id, idempotency_key=request.headers.get("Idempotency-Key"))
        except sqlite3.IntegrityError:
            raise AppError("SCAN_ALREADY_ACTIVE", "此范围已有活动扫描", 409)


@router.post("/scopes/{scope_id}/checksum-scans")
def checksum_scan(request: Request, scope_id: str, body: dict):
    user = mutation(request)
    scope = core.require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        identifiers = body.get("object_ids")
        if identifiers is None:
            identifiers = [row["id"] for row in conn.execute("SELECT id FROM objects WHERE scope_id=? AND is_current=1 AND presence='present' ORDER BY first_seen_at,id LIMIT 10001", (scope_id,))]
        if len(identifiers) > 10000 or not identifiers:
            raise AppError("RESOURCE_LIMIT_EXCEEDED", "checksum每次需1–10000个对象", 429)
        for identifier in identifiers:
            core.get_object(conn, scope_id, identifier)
        params = {
            "object_ids": identifiers,
            "authz_epoch": scope["authz_epoch"],
            "max_total_bytes": _int_param(body.get("max_total_bytes"), "max_total_bytes", 256 * 1024 * 1024, minimum=1, maximum=1024 * 1024 * 1024 * 1024),
            "max_object_bytes": _int_param(body.get("max_object_bytes"), "max_object_bytes", 1024 * 1024 * 1024, minimum=1, maximum=1024 * 1024 * 1024 * 1024),
        }
        return jobs.submit_job(conn, scope_id, "checksum", params, actor=user.id, idempotency_key=request.headers.get("Idempotency-Key"))


@router.post("/scopes/{scope_id}/preview-batches")
@router.post("/scopes/{scope_id}/previews/batch")
def previews(request: Request, scope_id: str, body: dict):
    user = mutation(request)
    scope = core.require_scope(request, scope_id)
    if not scope["allow_preview"]:
        raise AppError("FORBIDDEN_SCOPE", "此范围未授权预览", 403)
    identifiers = body.get("object_ids", [])
    if not 1 <= len(identifiers) <= 60:
        raise AppError("RESOURCE_LIMIT_EXCEEDED", "每批1–60个预览", 429)
    with db.connect(request.app.state.settings) as conn:
        for identifier in identifiers:
            core.get_object(conn, scope_id, identifier)
        return jobs.submit_job(conn, scope_id, "preview", {"object_ids": identifiers, "authz_epoch": scope["authz_epoch"]}, actor=user.id, idempotency_key=request.headers.get("Idempotency-Key"))


@router.api_route("/scopes/{scope_id}/objects/{object_id}/preview", methods=["GET", "HEAD"])
def preview(request: Request, scope_id: str, object_id: str):
    scope = core.require_scope(request, scope_id)
    if not scope["allow_preview"]:
        raise AppError("FORBIDDEN_SCOPE", "此范围未授权预览", 403)
    with db.connect(request.app.state.settings) as conn:
        obj = core.get_object(conn, scope_id, object_id)
        s3 = s3_for(conn, scope, request.app.state.settings)
    try:
        kwargs = {"Bucket": scope["bucket"], "Key": obj["key"]}
        if obj.get("version_id") and obj["version_id"] != "null":
            kwargs["VersionId"] = obj["version_id"]
        head = s3.head_object(**kwargs)
        if core.make_revision(scope["bucket"], obj["key"], observation(obj["key"], head)) != obj["revision"]:
            raise AppError("OBJECT_REVISION_CHANGED", "源对象已改变，请重新扫描", 409)
    except AppError:
        raise
    except Exception as exc:
        raise storage.sanitize_storage_exception(exc) from exc
    digest = cache_key(scope, obj)
    path = cache_path(request.app.state.settings, digest)
    if not path.exists():
        raise AppError("PREVIEW_NOT_READY", "预览尚未准备，请提交预览任务", 404)
    with db.connect(request.app.state.settings) as conn:
        conn.execute("UPDATE preview_cache SET accessed_at=? WHERE cache_key=?", (jobs.now(), digest))
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/scopes/{scope_id}/snapshots")
def snapshot(request: Request, scope_id: str, body: dict):
    user = mutation(request)
    scope = core.require_scope(request, scope_id)
    identifier = str(uuid.uuid4())
    with db.connect(request.app.state.settings) as conn:
        chosen = body.get("object_ids")
        if chosen is None:
            filters = body.get("filters", {})
            chosen, cursor = [], None
            while True:
                page = query_catalog(conn, request.app.state.settings, scope, filters, 200, cursor)
                chosen.extend(obj["id"] for obj in page["items"])
                if len(chosen) > 10000:
                    raise AppError("RESOURCE_LIMIT_EXCEEDED", "目标快照超过10000项，请缩小范围", 429)
                cursor = page["next_cursor"]
                if not cursor:
                    break
        if len(chosen) > 10000:
            raise AppError("RESOURCE_LIMIT_EXCEEDED", "目标快照超过10000项", 429)
        items = [{"object_id": obj["id"], "revision": obj["revision"]} for obj in [core.get_object(conn, scope_id, identifier) for identifier in dict.fromkeys(chosen)]]
        conn.execute("INSERT INTO operation_snapshots VALUES(?,?,?,?,?,?,?,?)", (identifier, scope_id, user.id, scope["authz_epoch"], json.dumps(body.get("filters", {})), json.dumps(items), jobs.now(), utc_add(3600)))
        return {"id": identifier, "count": len(items), "items": items, "expires_at": utc_add(3600)}


@router.get("/scopes/{scope_id}/reports/export")
def export(request: Request, scope_id: str):
    scope = core.require_scope(request, scope_id)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["object_id", "bucket", "key", "revision", "bytes", "format", "width", "height", "decode_status"])
    count, cursor = 0, None
    with db.connect(request.app.state.settings) as conn:
        while True:
            page = query_catalog(conn, request.app.state.settings, scope, _filters(request), 200, cursor)
            for obj in page["items"]:
                count += 1
                if count > 10000:
                    raise AppError("RESOURCE_LIMIT_EXCEEDED", "报告超过10000行，请缩小筛选", 429)
                cells = [obj.get(key, "") for key in ("id", "bucket", "key", "revision", "size_bytes", "format", "width", "height", "decode_status")]
                writer.writerow(["'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") else value for value in cells])
            cursor = page["next_cursor"]
            if not cursor:
                break
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="objects.csv"', "X-Report-Rows": str(count)})


@router.get("/scopes/{scope_id}/duplicates")
def duplicates(request: Request, scope_id: str):
    core.require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        total = conn.execute("SELECT COUNT(*) FROM objects WHERE scope_id=? AND is_current=1 AND presence='present'", (scope_id,)).fetchone()[0]
        covered = conn.execute("SELECT COUNT(*) FROM objects WHERE scope_id=? AND is_current=1 AND presence='present' AND checksum IS NOT NULL", (scope_id,)).fetchone()[0]
        groups = []
        for group in conn.execute("SELECT checksum,COUNT(*) AS count FROM objects WHERE scope_id=? AND is_current=1 AND presence='present' AND checksum IS NOT NULL GROUP BY checksum HAVING COUNT(*)>1 LIMIT 200", (scope_id,)):
            groups.append({"sha256": group["checksum"], "objects": [core.get_object(conn, scope_id, row["id"]) for row in conn.execute("SELECT id FROM objects WHERE scope_id=? AND is_current=1 AND presence='present' AND checksum=? LIMIT 200", (scope_id, group["checksum"]))]})
        return {"groups": groups, "algorithm": "full_object_sha256", "source": "complete_stream_get", "covered_count": covered, "eligible_count": total, "unscanned_count": total - covered, "reference_state": "unknown"}


@router.get("/projects/{project_id}/capacity")
def capacity(request: Request, project_id: str):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        core.get_project(conn, project_id)
        return _capacity_result_for_project(conn, project_id)


@router.post("/scopes/{scope_id}/diagnostics/access")
def access_diagnostic(request: Request, scope_id: str, body: dict):
    mutation(request)
    scope = core.require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        obj = core.get_object(conn, scope_id, body.get("object_id", ""))
        s3 = s3_for(conn, scope, request.app.state.settings)
    checks = []
    for operation in ("head", "get", "decode", "preview"):
        stamp = jobs.now()
        try:
            if operation == "head":
                s3.head_object(Bucket=scope["bucket"], Key=obj["key"])
                value = "supported"
            elif operation in ("get", "decode"):
                data = read_bound(s3, scope, obj)
                value = "supported" if operation == "get" else imaging.decode(data)[0]["decode_status"]
            else:
                value = "ready" if cache_path(request.app.state.settings, cache_key(scope, obj)).exists() else "not_ready"
            checks.append({"check": operation, "result": value, "position": "console_server", "checked_at": stamp})
        except Exception as exc:
            code = getattr(exc, "code", storage.sanitize_storage_exception(exc).code)
            checks.append({"check": operation, "result": code, "position": "console_server", "checked_at": stamp})
    checks.append({"check": "distribution", "result": "not_connected", "position": "configured_distribution", "checked_at": jobs.now()})
    identifier = str(uuid.uuid4())
    with db.connect(request.app.state.settings) as conn:
        conn.execute("INSERT INTO diagnostic_results VALUES(?,?,?,?,?,?)", (identifier, scope_id, obj["id"], "access", json.dumps(checks), jobs.now()))
    return {"id": identifier, "object_id": obj["id"], "checks": checks}


@router.post("/scopes/{scope_id}/diagnostics/cors-read")
def cors_diagnostic(request: Request, scope_id: str, body: dict):
    mutation(request)
    scope = core.require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        connection = core._connection_private(conn, scope["connection_id"])
        s3 = s3_for(conn, scope, request.app.state.settings)
    origin = body.get("origin", "")
    _validate_probe_origin(origin)
    try:
        config = s3.get_bucket_cors(Bucket=scope["bucket"])
        rules = config.get("CORSRules", [])
        result = "configured" if rules else "not_configured"
    except Exception as exc:
        rules = []
        result = storage.sanitize_storage_exception(exc).code
    preflight = {"attempted": False, "position": "server", "status": "not_requested"}
    object_id = body.get("object_id")
    endpoint = _approved_distribution_endpoint(connection, body.get("endpoint_id"))
    if object_id and endpoint:
        with db.connect(request.app.state.settings) as conn:
            obj = core.get_object(conn, scope_id, object_id)
        core.assert_key_in_scope(scope, obj["key"])
        url = endpoint.rstrip("/") + "/" + urllib.parse.quote(obj["key"], safe="/")
        req = urllib.request.Request(
            url,
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        handlers = [_NoRedirect]
        if not connection.get("verify_tls", True):
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=5) as response:
                preflight = {
                    "attempted": True,
                    "position": "server",
                    "status": response.status,
                    "access_control_allow_origin": response.headers.get("Access-Control-Allow-Origin"),
                    "access_control_allow_methods": response.headers.get("Access-Control-Allow-Methods"),
                }
        except urllib.error.HTTPError as exc:
            preflight = {
                "attempted": True,
                "position": "server",
                "status": exc.code,
                "access_control_allow_origin": exc.headers.get("Access-Control-Allow-Origin"),
                "access_control_allow_methods": exc.headers.get("Access-Control-Allow-Methods"),
            }
        except Exception as exc:
            preflight = {"attempted": True, "position": "server", "status": "failed", "error": exc.__class__.__name__}
    elif object_id and body.get("url"):
        raise AppError("INVALID_REQUEST", "diagnostic target must be an approved endpoint id", 400, field="url")
    return {"result": result, "rules": rules, "origin": origin, "position": "console_server",
            "checked_at": jobs.now(), "evidence_level": "storage_configuration", "business_browser_result": "unknown",
            "server_preflight": preflight,
            "message": "存储配置读取和服务端OPTIONS不能证明业务origin的真实浏览器访问；需目标浏览器trace"}
