from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from console import catalog, core, db, jobs
from console.config import Settings
from console.security import AppError


class Body(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.list_calls = 0
        self.bodies: list[Body] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls += 1
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken") or 0)
        max_keys = int(kwargs["MaxKeys"])
        page = keys[start:start + max_keys]
        next_index = start + len(page)
        return {
            "Contents": [self._listed(key) for key in page],
            "IsTruncated": next_index < len(keys),
            "NextContinuationToken": str(next_index) if next_index < len(keys) else None,
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return self._head(kwargs["Key"])

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        assert kwargs.get("IfMatch") == f'"{key}"'
        body = Body(self.objects[key])
        self.bodies.append(body)
        return {"Body": body, **self._head(key)}

    def _listed(self, key: str) -> dict[str, Any]:
        return {"Key": key, "Size": len(self.objects[key]), "ETag": f'"{key}"', "ContentType": content_type(key)}

    def _head(self, key: str) -> dict[str, Any]:
        return {"ContentLength": len(self.objects[key]), "ETag": f'"{key}"', "ContentType": content_type(key)}


def content_type(key: str) -> str:
    if key.endswith(".png"):
        return "image/png"
    return "image/jpeg"


def image_bytes(fmt: str = "JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (6, 5), (1, 2, 3)).save(out, format=fmt)
    return out.getvalue()


@pytest.fixture()
def foundation(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "console.db",
        admin_password="admin-pass",
        cursor_signing_key="cursor-key",
        csrf_signing_key="csrf-key",
        cookie_secure=False,
        dev_insecure_cookie=True,
    )
    db.initialize(settings)
    with db.connect(settings) as conn:
        jobs.initialize(conn)
        catalog.initialize(conn)
        core.bootstrap_admin(conn, settings)
        scope = seed_scope(conn)
        conn.commit()
        yield conn, settings, scope


def seed_scope(conn: sqlite3.Connection, *, prefix: str = "raw/") -> dict[str, Any]:
    connection = core.create_connection(
        conn,
        {
            "display_name": f"Fake {prefix}",
            "endpoint_url": "http://127.0.0.1:8333",
            "region": "us-east-1",
            "secret_ref": "server-test",
            "addressing_style": "path",
            "verify_tls": False,
        },
    )
    project = core.create_project(conn, {"project_key": f"p-{prefix.replace('/', '-')}", "display_name": f"P {prefix}"})
    return core.create_scope(
        conn,
        project["id"],
        {
            "connection_id": connection["id"],
            "display_name": f"Raw {prefix}",
            "bucket": "images",
            "prefix": prefix,
            "scope_policy": {"allow_preview": True, "allow_original_download": True},
        },
    )


def claim_job(conn: sqlite3.Connection, scope_id: str, kind: str, params: dict, **kwargs) -> dict[str, Any]:
    submitted = jobs.submit_job(conn, scope_id, kind, params, **kwargs)
    conn.commit()
    claimed = jobs.claim(conn)
    assert claimed is not None
    assert claimed["id"] == submitted["id"]
    return claimed


def test_scan_resume_midpage_uses_saved_page_without_relisting(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    fake = FakeS3({"raw/a.jpg": image_bytes(), "raw/b.jpg": image_bytes(), "raw/c.jpg": image_bytes()})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    claimed = claim_job(
        conn,
        scope["id"],
        "scan",
        {"authz_epoch": scope["authz_epoch"], "max_objects": 10, "objects_per_second": 100, "page_size": 3},
    )
    saved_page = [catalog._checkpoint_listing({"Key": "raw/a.jpg", "Size": len(fake.objects["raw/a.jpg"]), "ETag": '"raw/a.jpg"', "ContentType": "image/jpeg"}),
                  catalog._checkpoint_listing({"Key": "raw/b.jpg", "Size": len(fake.objects["raw/b.jpg"]), "ETag": '"raw/b.jpg"', "ContentType": "image/jpeg"})]
    conn.execute(
        "UPDATE jobs SET processed=1, checkpoint_json=? WHERE id=?",
        (json.dumps({"token": None, "next_token": None, "page": saved_page, "position": 1}), claimed["id"]),
    )
    conn.commit()
    resumed = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (claimed["id"],)).fetchone())

    catalog.execute_job(resumed, settings)

    row = conn.execute("SELECT state, processed, errors FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert row["state"] == "succeeded"
    assert row["processed"] == 2
    assert row["errors"] == 0
    assert fake.list_calls == 0
    keys = [row["key"] for row in conn.execute("SELECT key FROM objects ORDER BY key")]
    assert keys == ["raw/b.jpg"]


def test_checkpoint_cas_rejects_stale_publish(foundation):
    conn, _settings, scope = foundation
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})
    conn.execute("UPDATE jobs SET lease_token='stolen', fencing_version=fencing_version+1 WHERE id=?", (claimed["id"],))
    conn.commit()

    with pytest.raises(RuntimeError, match="LEASE_LOST"):
        jobs.checkpoint(conn, claimed, state="succeeded", processed=1)

    row = conn.execute("SELECT state, processed FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert row["state"] == "running"
    assert row["processed"] == 0


def test_remote_mutating_cancel_checkpoint_preserves_needs_review(foundation):
    conn, _settings, scope = foundation
    claimed = claim_job(
        conn,
        scope["id"],
        "copy",
        {"authz_epoch": scope["authz_epoch"]},
        effect_class="remote_mutating",
    )
    conn.execute("UPDATE jobs SET cancel_requested_at=? WHERE id=?", (jobs.now(), claimed["id"]))
    conn.commit()

    jobs.checkpoint(conn, claimed, state="running", error_code="REMOTE_UNKNOWN")
    row = conn.execute("SELECT state, error_code FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert row["state"] == "needs_review"
    assert row["error_code"] == "REMOTE_UNKNOWN"


def test_checksum_streams_large_object_in_chunks(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    payload = b"x" * (2 * 1024 * 1024 + 17)
    fake = FakeS3({"raw/big.jpg": payload})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    obj = core.upsert_object(
        conn,
        scope["id"],
        {"key": "raw/big.jpg", "size": len(payload), "etag": '"raw/big.jpg"', "content_type": "image/jpeg"},
    )
    claimed = claim_job(
        conn,
        scope["id"],
        "checksum",
        {"authz_epoch": scope["authz_epoch"], "object_ids": [obj["id"]], "max_object_bytes": len(payload) + 1, "max_total_bytes": len(payload) + 1},
    )

    catalog.execute_job(claimed, settings)

    stored = core.get_object(conn, scope["id"], obj["id"])
    assert stored["checksum"] == hashlib.sha256(payload).hexdigest()
    assert fake.bodies
    assert max(fake.bodies[0].read_sizes) <= 1024 * 1024


class NotFound(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 404}}


class Unavailable(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 503}}


class CandidateS3(FakeS3):
    """Listing hides ``unlisted`` keys; HEAD answers 404 for absent keys."""

    def __init__(self, objects: dict[str, bytes], *, unlisted: set[str] = frozenset(), head_errors: dict[str, Exception] | None = None):
        super().__init__(objects)
        self.unlisted = set(unlisted)
        self.head_errors = head_errors or {}
        self.heads: list[str] = []
        self.on_head = None

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        visible = {key: value for key, value in self.objects.items() if key not in self.unlisted}
        original, self.objects = self.objects, visible
        try:
            return super().list_objects_v2(**kwargs)
        finally:
            self.objects = original

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        self.heads.append(key)
        if self.on_head:
            self.on_head(key)
        if key in self.head_errors:
            raise self.head_errors[key]
        if key not in self.objects:
            raise NotFound(key)
        return super().head_object(**kwargs)


def scan_params(scope: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"authz_epoch": scope["authz_epoch"], "max_objects": 1000, "objects_per_second": 100, "page_size": 100} | extra


def seed_missing_candidates(conn: sqlite3.Connection, scope: dict[str, Any], count: int) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        key = f"raw/gone-{index:03d}.jpg"
        rows.append(core.upsert_object(conn, scope["id"], {"key": key, "size": 10, "etag": f'"{key}"', "presence": "missing_candidate"}))
    conn.commit()
    return rows


def object_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM objects WHERE key=? AND is_current=1", (key,)).fetchone()


def test_scan_keeps_listing_size_and_etag_for_object_over_read_budget(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    payload = image_bytes() + b"x" * 64
    fake = CandidateS3({"raw/big.jpg": payload})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog.read_bound, "__defaults__", (16,))
    claimed = claim_job(conn, scope["id"], "scan", scan_params(scope))

    catalog.execute_job(claimed, settings)

    row = object_row(conn, "raw/big.jpg")
    assert row["size"] == len(payload)
    assert row["etag"] == '"raw/big.jpg"'
    assert json.loads(row["properties_json"]) == {"decode_status": "resource_limited", "error_code": "RESOURCE_LIMIT_EXCEEDED"}
    assert row["revision"] == core.make_revision("images", "raw/big.jpg", {"size": len(payload), "etag": '"raw/big.jpg"', "last_modified": None})
    assert conn.execute("SELECT state FROM jobs WHERE id=?", (claimed["id"],)).fetchone()[0] == "partially_failed"


def test_scan_head_failure_indexes_listing_observation_not_zero(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    payload = image_bytes()
    fake = CandidateS3({"raw/a.jpg": payload}, head_errors={"raw/a.jpg": Unavailable("down")})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    claimed = claim_job(conn, scope["id"], "scan", scan_params(scope))

    catalog.execute_job(claimed, settings)

    row = object_row(conn, "raw/a.jpg")
    assert row["size"] == len(payload)
    assert row["etag"] == '"raw/a.jpg"'
    assert row["content_type"] == "image/jpeg"
    assert json.loads(row["properties_json"])["decode_status"] == "read_failed"


def test_missing_candidate_phase_checkpoints_each_batch_and_rate_limits(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    seed_missing_candidates(conn, scope, 120)
    fake = CandidateS3({})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    sleeps: list[float] = []
    monkeypatch.setattr(catalog.time, "sleep", sleeps.append)
    phases: list[dict[str, Any]] = []
    real_checkpoint = jobs.checkpoint

    def spy(conn, job, **kwargs):
        if (kwargs.get("data") or {}).get("phase") == "confirm_missing":
            phases.append(kwargs["data"])
        return real_checkpoint(conn, job, **kwargs)

    monkeypatch.setattr(catalog.jobs, "checkpoint", spy)
    claimed = claim_job(conn, scope["id"], "scan", scan_params(scope))

    catalog.execute_job(claimed, settings)

    # 120 candidates in batches of 50: three batches plus the empty final read.
    assert len(phases) == 4
    assert [phase["last_candidate"] != "" for phase in phases] == [False, True, True, True]
    assert len(fake.heads) == 120
    assert len(sleeps) == 120
    assert conn.execute("SELECT COUNT(*) FROM objects WHERE presence='missing_confirmed'").fetchone()[0] == 120
    assert conn.execute("SELECT state FROM jobs WHERE id=?", (claimed["id"],)).fetchone()[0] == "succeeded"


def test_missing_candidate_phase_honours_pause_and_resumes_without_relisting(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    seed_missing_candidates(conn, scope, 60)
    fake = CandidateS3({})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog.time, "sleep", lambda _seconds: None)
    claimed = claim_job(conn, scope["id"], "scan", scan_params(scope))

    def pause_after_first_head(_key):
        if len(fake.heads) == 1:
            with db.connect(settings) as other:
                other.execute("UPDATE jobs SET pause_requested_at=? WHERE id=?", (jobs.now(), claimed["id"]))
                other.commit()

    fake.on_head = pause_after_first_head
    catalog.execute_job(claimed, settings)

    paused = conn.execute("SELECT state, checkpoint_json FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    checkpoint = json.loads(paused["checkpoint_json"])
    assert paused["state"] == "paused"
    assert checkpoint["phase"] == "confirm_missing"
    assert len(fake.heads) == 50  # the in-flight batch finishes; the next one is not started
    assert conn.execute("SELECT COUNT(*) FROM objects WHERE presence='missing_candidate'").fetchone()[0] == 10

    fake.on_head = None
    list_calls = fake.list_calls
    conn.execute("UPDATE jobs SET state='queued', pause_requested_at=NULL WHERE id=?", (claimed["id"],))
    conn.commit()
    resumed = jobs.claim(conn)
    assert resumed["id"] == claimed["id"]

    catalog.execute_job(resumed, settings)

    assert fake.list_calls == list_calls
    assert len(fake.heads) == 60
    assert conn.execute("SELECT COUNT(*) FROM objects WHERE presence='missing_confirmed'").fetchone()[0] == 60
    assert conn.execute("SELECT state FROM jobs WHERE id=?", (claimed["id"],)).fetchone()[0] == "succeeded"


def test_missing_candidate_that_still_exists_is_restored_once(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    same = image_bytes()
    changed = image_bytes("PNG")
    core.upsert_object(conn, scope["id"], {"key": "raw/same.jpg", "size": len(same), "etag": '"raw/same.jpg"', "content_type": "image/jpeg", "presence": "missing_candidate"})
    old_changed = core.upsert_object(conn, scope["id"], {"key": "raw/changed.png", "size": 1, "etag": '"old"', "presence": "missing_candidate"})
    conn.commit()
    fake = CandidateS3({"raw/same.jpg": same, "raw/changed.png": changed}, unlisted={"raw/same.jpg", "raw/changed.png"})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog.time, "sleep", lambda _seconds: None)
    claimed = claim_job(conn, scope["id"], "scan", scan_params(scope))

    catalog.execute_job(claimed, settings)

    restored = object_row(conn, "raw/same.jpg")
    assert restored["presence"] == "present"
    assert restored["scan_id"] == claimed["id"]
    replaced = object_row(conn, "raw/changed.png")
    assert replaced["id"] != old_changed["id"]
    assert replaced["presence"] == "present"
    assert replaced["size"] == len(changed)
    assert json.loads(replaced["properties_json"]) == {"decode_status": "unknown"}

    # The next scan must not HEAD them again as missing candidates.
    fake.heads.clear()
    second = claim_job(conn, scope["id"], "scan", scan_params(scope))
    catalog.execute_job(second, settings)
    assert fake.heads == []


def test_query_numeric_filter_invalid_returns_422(foundation):
    conn, settings, scope = foundation
    with pytest.raises(AppError) as exc:
        catalog.query_catalog(conn, settings, scope, {"min_width": "wide"})
    assert exc.value.status == 422
