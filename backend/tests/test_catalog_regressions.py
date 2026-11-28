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
    def __init__(self, objects: dict[str, bytes], *, page_limit: int | None = None):
        self.objects = objects
        self.page_limit = page_limit
        self.list_calls = 0
        self.list_max_keys: list[int] = []
        self.bodies: list[Body] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls += 1
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken") or 0)
        max_keys = int(kwargs["MaxKeys"])
        self.list_max_keys.append(max_keys)
        returned_size = min(max_keys, self.page_limit) if self.page_limit is not None else max_keys
        page = keys[start:start + returned_size]
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


@pytest.mark.parametrize("page_params, expected_size, expected_calls", [
    ({}, 1000, 3),
    ({"page_size": 10}, 10, 4),
    ({"list_max_keys": 12}, 12, 3),
    ({"page_size": 20, "list_max_keys": 1}, 20, 3),
])
def test_scan_pages_honor_requested_size_and_reduce_listing_calls(foundation, monkeypatch, page_params, expected_size, expected_calls):
    conn, settings, scope = foundation
    payload = image_bytes()
    # S3 may return fewer objects than MaxKeys while supplying a continuation token.
    fake = FakeS3({f"raw/{index:04d}.jpg": payload for index in range(33)}, page_limit=16)
    decoded_properties, _ = catalog.imaging.decode(payload)
    monkeypatch.setattr(catalog.imaging, "decode", lambda *_args, **_kwargs: (dict(decoded_properties), None))
    page_serializations, page_writes, checkpoint_sizes = [], [], []
    real_dumps, real_connect, real_checkpoint = json.dumps, db.connect, jobs.checkpoint

    def track_serialization(value, *args, **kwargs):
        if isinstance(value, list) and value and isinstance(value[0], dict) and "key" in value[0]:
            page_serializations.append(1)
        return real_dumps(value, *args, **kwargs)

    def track_connection(*args, **kwargs):
        tracked = real_connect(*args, **kwargs)
        tracked.set_trace_callback(lambda sql: page_writes.append(1) if sql.startswith("INSERT OR REPLACE INTO scan_listing_pages") else None)
        return tracked

    def track_checkpoint(*args, **kwargs):
        if kwargs.get("data") is not None:
            checkpoint_sizes.append(len(real_dumps(kwargs["data"])))
        return real_checkpoint(*args, **kwargs)

    monkeypatch.setattr(catalog.json, "dumps", track_serialization)
    monkeypatch.setattr(catalog.db, "connect", track_connection)
    monkeypatch.setattr(catalog.jobs, "checkpoint", track_checkpoint)
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog, "_rate_wait", lambda *_args: None)
    claimed = claim_job(conn, scope["id"], "scan", {
        "authz_epoch": scope["authz_epoch"], "max_objects": 2000, **page_params,
    })

    catalog.execute_job(claimed, settings)

    stored = conn.execute("SELECT state,processed,errors FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert dict(stored) == {"state": "succeeded", "processed": 33, "errors": 0}
    assert fake.list_calls == expected_calls
    assert fake.list_max_keys == [expected_size] * expected_calls
    assert len(page_serializations) == expected_calls
    assert len(page_writes) == expected_calls
    assert max(checkpoint_sizes) < 512, "per-object checkpoints must contain only small cursor/position metadata"
    assert len(fake.bodies) == 33
    assert conn.execute("SELECT count(*) FROM objects WHERE scan_id=?", (claimed["id"],)).fetchone()[0] == 33
    assert conn.execute("SELECT count(*) FROM scan_listing_pages WHERE job_id=?", (claimed["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("control", ["pause", "cancel"])
def test_scan_large_page_keeps_unprocessed_entries_at_control_checkpoint(foundation, monkeypatch, control):
    conn, settings, scope = foundation
    fake = FakeS3({f"raw/{letter}.jpg": image_bytes() for letter in "abc"})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})
    controls = []

    def request_control(*_args):
        if not controls:
            controls.append(control)
            conn.execute(f"UPDATE jobs SET {control}_requested_at=? WHERE id=?", (jobs.now(), claimed["id"]))
            conn.commit()

    monkeypatch.setattr(catalog, "_rate_wait", request_control)
    catalog.execute_job(claimed, settings)
    stored = conn.execute("SELECT state,processed,checkpoint_json FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    checkpoint = json.loads(stored["checkpoint_json"])
    assert stored["state"] == ("paused" if control == "pause" else "cancelled")
    assert stored["processed"] == 1
    assert checkpoint["position"] == 1
    assert "page" not in checkpoint
    saved = conn.execute("SELECT page_json FROM scan_listing_pages WHERE job_id=?", (claimed["id"],)).fetchone()
    if control == "pause":
        assert checkpoint["page_stored"] is True
        assert [item["key"] for item in json.loads(saved["page_json"])] == ["raw/a.jpg", "raw/b.jpg", "raw/c.jpg"]
    else:
        assert saved is None, "terminal cancelled scans release the saved listing"
    assert fake.list_calls == 1
    assert len(fake.bodies) == 1
    if control == "pause":
        conn.execute("UPDATE jobs SET state='queued',pause_requested_at=NULL WHERE id=?", (claimed["id"],))
        conn.commit()
        resumed = jobs.claim(conn)
        catalog.execute_job(resumed, settings)
        result = conn.execute("SELECT state,processed,errors FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
        assert dict(result) == {"state": "succeeded", "processed": 3, "errors": 0}
        assert fake.list_calls == 1
        assert len(fake.bodies) == 3
        assert [row["key"] for row in conn.execute("SELECT key FROM objects ORDER BY key")] == ["raw/a.jpg", "raw/b.jpg", "raw/c.jpg"]


def test_scan_stops_before_oversized_page_exceeds_budget(foundation, monkeypatch):
    conn, settings, scope = foundation

    class OversizedPageS3(FakeS3):
        def list_objects_v2(self, **kwargs):
            return super().list_objects_v2(**{**kwargs, "MaxKeys": 3})

    fake = OversizedPageS3({f"raw/{letter}.jpg": image_bytes() for letter in "abc"})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog, "_rate_wait", lambda *_args: None)
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 2})

    catalog.execute_job(claimed, settings)

    stored = conn.execute("SELECT state,processed,error_code,checkpoint_json FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    checkpoint = json.loads(stored["checkpoint_json"])
    assert stored["state"] == "failed"
    assert stored["error_code"] == "SCAN_BUDGET_EXHAUSTED"
    assert stored["processed"] == 2
    assert checkpoint["position"] == 2
    assert "page" not in checkpoint
    assert conn.execute("SELECT count(*) FROM scan_listing_pages WHERE job_id=?", (claimed["id"],)).fetchone()[0] == 0
    assert len(fake.bodies) == 2
    assert conn.execute("SELECT count(*) FROM capacity_samples WHERE id=?", (claimed["id"],)).fetchone()[0] == 0


def test_scan_resume_does_not_repeat_failed_entries_in_saved_page(foundation, monkeypatch):
    conn, settings, scope = foundation

    class FailedHeadS3(FakeS3):
        def head_object(self, **kwargs):
            if kwargs["Key"] == "raw/a.jpg":
                raise Unavailable()
            return super().head_object(**kwargs)

    fake = FailedHeadS3({f"raw/{letter}.jpg": image_bytes() for letter in "abc"})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})
    waits = []

    def pause_after_failed_entry(*_args):
        if not waits:
            conn.execute("UPDATE jobs SET pause_requested_at=? WHERE id=?", (jobs.now(), claimed["id"]))
            conn.commit()
        waits.append(True)

    monkeypatch.setattr(catalog, "_rate_wait", pause_after_failed_entry)
    catalog.execute_job(claimed, settings)
    paused = conn.execute("SELECT state,processed,errors FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert dict(paused) == {"state": "paused", "processed": 1, "errors": 1}
    conn.execute("UPDATE jobs SET state='queued',pause_requested_at=NULL WHERE id=?", (claimed["id"],))
    conn.commit()

    catalog.execute_job(jobs.claim(conn), settings)

    stored = conn.execute("SELECT state,processed,errors FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert dict(stored) == {"state": "partially_failed", "processed": 3, "errors": 1}
    assert fake.list_calls == 1
    assert conn.execute("SELECT count(*) FROM job_items WHERE job_id=?", (claimed["id"],)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM objects WHERE scan_id=?", (claimed["id"],)).fetchone()[0] == 3


@pytest.mark.parametrize("control", ["pause", "cancel"])
def test_scan_control_during_listing_keeps_safe_checkpoint_before_indexing(foundation, monkeypatch, control):
    conn, settings, scope = foundation
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})

    class ControlledListingS3(FakeS3):
        def list_objects_v2(self, **kwargs):
            response = super().list_objects_v2(**kwargs)
            conn.execute(f"UPDATE jobs SET {control}_requested_at=? WHERE id=?", (jobs.now(), claimed["id"]))
            conn.commit()
            return response

    fake = ControlledListingS3({f"raw/{letter}.jpg": image_bytes() for letter in "abc"})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog, "_rate_wait", lambda *_args: None)

    catalog.execute_job(claimed, settings)

    stored = conn.execute("SELECT state,processed,checkpoint_json FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert stored["state"] == ("paused" if control == "pause" else "cancelled")
    assert stored["processed"] == 0
    assert json.loads(stored["checkpoint_json"])["position"] == 0
    assert len(fake.bodies) == 0
    saved = conn.execute("SELECT page_json FROM scan_listing_pages WHERE job_id=?", (claimed["id"],)).fetchone()
    if control == "pause":
        assert len(json.loads(saved["page_json"])) == 3
        conn.execute("UPDATE jobs SET state='queued',pause_requested_at=NULL WHERE id=?", (claimed["id"],))
        conn.commit()
        catalog.execute_job(jobs.claim(conn), settings)
        assert fake.list_calls == 1
        assert len(fake.bodies) == 3
    else:
        assert saved is None


def test_scan_missing_stored_checkpoint_fails_without_relisting(foundation, monkeypatch):
    conn, settings, scope = foundation
    fake = FakeS3({"raw/a.jpg": image_bytes()})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})
    conn.execute("UPDATE jobs SET processed=1,checkpoint_json=? WHERE id=?", (json.dumps({
        "token": None, "next_token": None, "page_stored": True, "position": 1,
    }), claimed["id"]))
    conn.commit()
    recovered = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (claimed["id"],)).fetchone())

    with pytest.raises(AppError) as error:
        catalog.execute_job(recovered, settings)

    assert error.value.code == "SCAN_CHECKPOINT_INVALID"
    assert fake.list_calls == 0
    assert len(fake.bodies) == 0


@pytest.mark.parametrize("invalid_token", [None, "", "repeat"])
def test_scan_truncated_listing_requires_an_advancing_token(foundation, monkeypatch, invalid_token):
    conn, settings, scope = foundation

    class InvalidPaginationS3(FakeS3):
        def list_objects_v2(self, **_kwargs):
            self.list_calls += 1
            response = {"Contents": [self._listed("raw/a.jpg")], "IsTruncated": True}
            if invalid_token == "repeat":
                response["NextContinuationToken"] = "page-2"
            elif invalid_token is not None:
                response["NextContinuationToken"] = invalid_token
            return response

    fake = InvalidPaginationS3({"raw/a.jpg": image_bytes()})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog, "_rate_wait", lambda *_args: None)
    submitted = jobs.submit_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})
    conn.commit()

    assert jobs.run_once(settings) is True

    stored = conn.execute("SELECT state,processed,error_code FROM jobs WHERE id=?", (submitted["id"],)).fetchone()
    assert dict(stored) == {
        "state": "failed", "processed": 1 if invalid_token == "repeat" else 0,
        "error_code": "SCAN_PAGINATION_INVALID",
    }
    assert fake.list_calls == (2 if invalid_token == "repeat" else 1), "malformed tokens must fail within a bounded number of list requests"
    assert len(fake.bodies) == (1 if invalid_token == "repeat" else 0), "the repeated page must not index the same object again"
    assert conn.execute("SELECT enumeration FROM scan_runs WHERE id=?", (submitted["id"],)).fetchone()[0] == "partial"
    assert conn.execute("SELECT count(*) FROM capacity_samples WHERE id=?", (submitted["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM scan_listing_pages WHERE job_id=?", (submitted["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("has_next_page", [False, True])
def test_scan_resume_after_last_checkpointed_entry_does_not_relist_old_page(foundation, monkeypatch, has_next_page):
    conn, settings, scope = foundation
    fake = FakeS3({f"raw/{letter}.jpg": image_bytes() for letter in ("abc" if has_next_page else "ab")})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    monkeypatch.setattr(catalog, "_rate_wait", lambda *_args: None)
    claimed = claim_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 10})
    for key in ("raw/a.jpg", "raw/b.jpg"):
        core.upsert_object(conn, scope["id"], catalog.observation(key, fake.head_object(Key=key)) | {"scan_id": claimed["id"]})
    saved_page = [catalog._checkpoint_listing(fake._listed(key)) for key in ("raw/a.jpg", "raw/b.jpg")]
    conn.execute("UPDATE jobs SET processed=2,checkpoint_json=? WHERE id=?", (json.dumps({
        "token": None, "next_token": "2" if has_next_page else None, "page": saved_page, "position": 2,
    }), claimed["id"]))
    conn.commit()
    recovered = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (claimed["id"],)).fetchone())

    catalog.execute_job(recovered, settings)

    stored = conn.execute("SELECT state,processed FROM jobs WHERE id=?", (claimed["id"],)).fetchone()
    assert dict(stored) == {"state": "succeeded", "processed": 3 if has_next_page else 2}
    assert fake.list_calls == (1 if has_next_page else 0)
    assert len(fake.bodies) == (1 if has_next_page else 0)
    assert [row["key"] for row in conn.execute("SELECT key FROM objects ORDER BY key")] == sorted(fake.objects)


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

    # The phase transition, every HEAD, and each batch read (3 batches + the empty final read).
    assert len(phases) == 120 + 5
    assert phases[0]["last_candidate"] == "" and all(phase["last_candidate"] for phase in phases[3:])
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
    assert len(fake.heads) == 1  # controls are checked before every HEAD
    assert checkpoint["last_candidate"]
    assert conn.execute("SELECT COUNT(*) FROM objects WHERE presence='missing_candidate'").fetchone()[0] == 59

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


def test_low_scan_rate_never_waits_past_the_job_lease(foundation, monkeypatch: pytest.MonkeyPatch):
    conn, settings, scope = foundation
    seed_missing_candidates(conn, scope, 3)
    fake = CandidateS3({"raw/a.jpg": image_bytes()})
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake)
    sleeps: list[float] = []
    monkeypatch.setattr(catalog.time, "sleep", sleeps.append)
    claimed = claim_job(conn, scope["id"], "scan", scan_params(scope, objects_per_second=0.001))

    catalog.execute_job(claimed, settings)

    assert len(sleeps) == 4  # one listed object, three candidates
    assert max(sleeps) <= catalog.MAX_RATE_WAIT_SECONDS < 120
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
