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


def test_query_numeric_filter_invalid_returns_422(foundation):
    conn, settings, scope = foundation
    with pytest.raises(AppError) as exc:
        catalog.query_catalog(conn, settings, scope, {"min_width": "wide"})
    assert exc.value.status == 422
