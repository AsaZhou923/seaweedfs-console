"""Persistent bounded jobs with leases, fencing and explicit control requests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import uuid

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1")
HANDLERS: dict = {}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(id),kind TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'queued',effect_class TEXT NOT NULL DEFAULT 'source_readonly',
      params_json TEXT NOT NULL,checkpoint_json TEXT NOT NULL DEFAULT '{}',actor TEXT NOT NULL,
      processed INTEGER NOT NULL DEFAULT 0,errors INTEGER NOT NULL DEFAULT 0,
      attempt_count INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL DEFAULT 5,
      lease_token TEXT,lease_expires_at TEXT,fencing_version INTEGER NOT NULL DEFAULT 0,
      pause_requested_at TEXT,cancel_requested_at TEXT,error_code TEXT,
      namespace TEXT,idempotency_key TEXT,request_hash TEXT,
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      UNIQUE(namespace,idempotency_key));
    CREATE UNIQUE INDEX IF NOT EXISTS active_scan ON jobs(scope_id)
      WHERE kind='scan' AND state IN ('queued','running','paused');
    CREATE TABLE IF NOT EXISTS job_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(id),
      object_id TEXT,state TEXT NOT NULL,error_code TEXT,result_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS scan_runs (
      id TEXT PRIMARY KEY REFERENCES jobs(id),scope_id TEXT NOT NULL REFERENCES scopes(id),
      enumeration TEXT NOT NULL DEFAULT 'unknown',properties TEXT NOT NULL DEFAULT 'unknown',
      reference_state TEXT NOT NULL DEFAULT 'unknown',started_at TEXT NOT NULL,completed_at TEXT);
    """)


def submit_job(conn, scope_id: str, kind: str, params: dict, actor: str = "admin",
               idempotency_key: str | None = None, effect_class: str = "source_readonly") -> dict:
    from .security import AppError
    request_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    namespace = f"{actor}:{scope_id}:{kind}"
    if idempotency_key:
        prior = conn.execute("SELECT * FROM jobs WHERE namespace=? AND idempotency_key=?",
                             (namespace, idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                raise AppError("IDEMPOTENCY_CONFLICT", "同一幂等键的参数已经改变", 409)
            return public(dict(prior))
    stamp, identifier = now(), str(uuid.uuid4())
    conn.execute("""INSERT INTO jobs(id,scope_id,kind,params_json,actor,effect_class,
      namespace,idempotency_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 (identifier, scope_id, kind, json.dumps(params), actor, effect_class,
                  namespace, idempotency_key, request_hash, stamp, stamp))
    if kind == "scan":
        conn.execute("INSERT INTO scan_runs(id,scope_id,started_at) VALUES(?,?,?)",
                     (identifier, scope_id, stamp))
    return public(dict(conn.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()))


def public(job: dict) -> dict:
    # Continuation tokens, credentials and decoder paths stay server-side.
    return {key: job[key] for key in ("id", "scope_id", "kind", "state", "processed", "errors",
            "attempt_count", "max_attempts", "pause_requested_at", "cancel_requested_at",
            "error_code", "created_at", "updated_at")} | {
        "control_request": "cancel" if job["cancel_requested_at"] else "pause" if job["pause_requested_at"] else "none",
        "total": None}


def reap(conn) -> None:
    stamp = now()
    conn.execute("""UPDATE jobs SET state=CASE WHEN effect_class='remote_mutating' THEN 'needs_review'
      WHEN cancel_requested_at IS NOT NULL THEN 'cancelled' WHEN pause_requested_at IS NOT NULL THEN 'paused'
      WHEN attempt_count>=max_attempts THEN 'failed' ELSE 'queued' END,
      lease_token=NULL,lease_expires_at=NULL,fencing_version=fencing_version+1,
      error_code='LEASE_EXPIRED',updated_at=? WHERE state='running' AND lease_expires_at<=?""", (stamp, stamp))
    conn.execute("UPDATE jobs SET state='failed',error_code='ATTEMPTS_EXHAUSTED',updated_at=? WHERE state='queued' AND attempt_count>=max_attempts", (stamp,))


def claim(conn) -> dict | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        reap(conn)
        row = conn.execute("""SELECT * FROM jobs WHERE state='queued' AND attempt_count<max_attempts
          AND pause_requested_at IS NULL AND cancel_requested_at IS NULL ORDER BY created_at LIMIT 1""").fetchone()
        if not row:
            conn.commit()
            return None
        token = uuid.uuid4().hex
        expires = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("""UPDATE jobs SET state='running',lease_token=?,lease_expires_at=?,
          fencing_version=fencing_version+1,attempt_count=attempt_count+1,updated_at=? WHERE id=? AND state='queued'""",
                     (token, expires, now(), row["id"]))
        result = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def fenced(conn, job: dict) -> dict:
    row = conn.execute("""SELECT * FROM jobs WHERE id=? AND state='running' AND lease_token=?
      AND fencing_version=? AND lease_expires_at>?""",
                       (job["id"], job["lease_token"], job["fencing_version"], now())).fetchone()
    if not row:
        raise RuntimeError("LEASE_LOST")
    return dict(row)


def checkpoint(conn, job: dict, *, state="queued", processed=None, errors=None, data=None, error_code=None):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    current = fenced(conn, job)
    if current["effect_class"] == "remote_mutating" and state in ("cancelled", "paused", "failed"):
        state = "needs_review"
    elif current["cancel_requested_at"]:
        state = "needs_review" if current["effect_class"] == "remote_mutating" else "cancelled"
    elif current["pause_requested_at"]:
        state = "needs_review" if current["effect_class"] == "remote_mutating" else "paused"
    expires = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if state == "running" else None
    token = job["lease_token"] if state == "running" else None
    fence = job["fencing_version"] if state == "running" else job["fencing_version"] + 1
    next_error = error_code if error_code is not None else current["error_code"]
    result = conn.execute("""UPDATE jobs SET state=?,processed=?,errors=?,checkpoint_json=?,error_code=?,
      lease_token=?,lease_expires_at=?,fencing_version=?,updated_at=?
      WHERE id=? AND state='running' AND lease_token=? AND fencing_version=? AND lease_expires_at>?""",
                 (state, current["processed"] if processed is None else processed,
                  current["errors"] if errors is None else errors,
                  current["checkpoint_json"] if data is None else json.dumps(data), next_error,
                  token, expires, fence, now(), job["id"], job["lease_token"], job["fencing_version"], now()))
    if result.rowcount != 1:
        raise RuntimeError("LEASE_LOST")
    if job["kind"] == "scan" and state in ("succeeded", "partially_failed", "failed", "cancelled"):
        conn.execute("UPDATE scan_runs SET enumeration=?,properties=?,completed_at=? WHERE id=?",
                     ("complete" if state in ("succeeded", "partially_failed") else "partial",
                      "partial" if errors else "complete" if state == "succeeded" else "unknown", now(), job["id"]))


def run_once(settings=None) -> bool:
    from . import db, catalog
    from .config import get_settings
    settings = settings or get_settings()
    with db.connect(settings) as conn:
        job = claim(conn)
    if job is None:
        return False
    try:
        if job["kind"] in ("scan", "checksum", "preview"):
            catalog.execute_job(job, settings)
        elif job["kind"] in HANDLERS:
            HANDLERS[job["kind"]](job, settings)
        else:
            raise RuntimeError("UNKNOWN_JOB_KIND")
    except Exception as exc:
        from .security import AppError
        code = exc.code if isinstance(exc, AppError) else "WORKER_FAILURE"
        with db.connect(settings) as conn:
            try:
                checkpoint(conn, job, state="needs_review" if job["effect_class"] == "remote_mutating" else "failed", error_code=code)
            except RuntimeError:
                pass  # A superseded executor cannot publish even an error.
    return True


@router.get("/jobs")
def list_jobs(request: Request, scope_id: str | None = None):
    from . import db, core
    from .security import current_user
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()
        visible = []
        for row in rows:
            if scope_id and scope_id != row["scope_id"]:
                continue
            try:
                core.require_scope(request, row["scope_id"])
            except Exception:
                continue
            visible.append(public(dict(row)))
        return {"items": visible}


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    from . import db, core
    from .security import AppError
    with db.connect(request.app.state.settings) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise AppError("JOB_NOT_FOUND", "任务不存在", 404)
        core.require_scope(request, row["scope_id"])
        result = public(dict(row))
        result["items"] = [dict(item) for item in conn.execute("SELECT object_id,state,error_code,result_json FROM job_items WHERE job_id=? ORDER BY id LIMIT 500", (job_id,))]
        if row["kind"] == "scan":
            result["coverage"] = dict(conn.execute("SELECT enumeration,properties,reference_state FROM scan_runs WHERE id=?", (job_id,)).fetchone())
        return result


@router.post("/jobs/{job_id}/{action}")
def control_job(request: Request, job_id: str, action: str):
    from . import db, core
    from .security import mutation, AppError
    mutation(request)
    with db.connect(request.app.state.settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise AppError("JOB_NOT_FOUND", "任务不存在", 404)
        core.require_scope(request, row["scope_id"])
        state, stamp = row["state"], now()
        if action in ("cancel", "pause") and state in ("queued", "running", "paused"):
            column = "cancel_requested_at" if action == "cancel" else "pause_requested_at"
            conn.execute(f"UPDATE jobs SET {column}=COALESCE({column},?),updated_at=? WHERE id=?", (stamp, stamp, job_id))
            if state != "running":
                conn.execute("UPDATE jobs SET state=?,fencing_version=fencing_version+1 WHERE id=?", ("cancelled" if action == "cancel" else "paused", job_id))
        elif action == "resume" and state == "paused":
            conn.execute("UPDATE jobs SET state=?,pause_requested_at=NULL,updated_at=? WHERE id=?",
                         ("failed" if row["attempt_count"] >= row["max_attempts"] else "queued", stamp, job_id))
        elif action == "retry" and state in ("failed", "partially_failed") and row["effect_class"] != "remote_mutating":
            return submit_job(conn, row["scope_id"], row["kind"], json.loads(row["params_json"]), row["actor"], effect_class=row["effect_class"])
        elif action in ("cancel", "pause") and state in ("cancelled", "paused"):
            pass
        else:
            raise AppError("INVALID_JOB_TRANSITION", "当前状态不接受此操作；不确定写入需先核查", 409)
        return public(dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))


def main():
    from .main import create_app
    app = create_app()
    while True:
        if not run_once(app.state.settings):
            time.sleep(1)


if __name__ == "__main__":
    main()
