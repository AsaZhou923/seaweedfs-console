from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from console import catalog, core, db, jobs
from console.config import Settings


class Body(io.BytesIO):
    pass


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken") or 0)
        max_keys = int(kwargs["MaxKeys"])
        page = keys[start:start + max_keys]
        next_index = start + len(page)
        return {
            "Contents": [{"Key": key, "Size": len(self.objects[key]), "ETag": f'"{key}"'} for key in page],
            "IsTruncated": next_index < len(keys),
            "NextContinuationToken": str(next_index) if next_index < len(keys) else None,
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        return {"ContentLength": len(self.objects[key]), "ContentType": "image/jpeg", "ETag": f'"{key}"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        return {"Body": Body(self.objects[key]), "ContentType": "image/jpeg", "ContentLength": len(self.objects[key])}


class FailingGetS3(FakeS3):
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("read failed")


def image_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (5, 4), "red").save(out, format="JPEG")
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
    return settings, scope


def seed_scope(conn) -> dict[str, Any]:
    connection = core.create_connection(
        conn,
        {
            "display_name": "Fake",
            "endpoint_url": "http://127.0.0.1:8333",
            "region": "us-east-1",
            "secret_ref": "server-test",
            "addressing_style": "path",
            "verify_tls": False,
        },
    )
    project = core.create_project(conn, {"project_key": "capacity", "display_name": "Capacity"})
    return core.create_scope(
        conn,
        project["id"],
        {
            "connection_id": connection["id"],
            "display_name": "Raw",
            "bucket": "images",
            "prefix": "raw/",
            "scope_policy": {"allow_preview": True, "allow_original_download": True},
        },
    )


def run_scan(settings: Settings, scope: dict[str, Any], fake: FakeS3, params: dict[str, Any]) -> str:
    with db.connect(settings) as conn:
        submitted = jobs.submit_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], **params})
        conn.commit()
        claimed = jobs.claim(conn)
    assert claimed is not None
    original = catalog.s3_for
    catalog.s3_for = lambda *_args: fake
    try:
        catalog.execute_job(claimed, settings)
    finally:
        catalog.s3_for = original
    return submitted["id"]


def sample_for(settings: Settings, job_id: str) -> dict[str, Any] | None:
    with db.connect(settings) as conn:
        row = conn.execute("SELECT * FROM capacity_samples WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["result_json"])


def test_successful_scan_persists_capacity_sample_and_survives_restart(foundation):
    settings, scope = foundation
    fake = FakeS3({"raw/a.jpg": image_bytes(), "raw/b.jpg": image_bytes()})
    job_id = run_scan(settings, scope, fake, {"max_objects": 10, "objects_per_second": 100, "page_size": 2})

    first = sample_for(settings, job_id)
    assert first is not None
    assert first["scope_id"] == scope["id"]
    assert first["scan_id"] == job_id
    assert first["coverage"] == "complete_enumeration"
    assert first["object_count"] == 2
    assert first["current_logical_bytes"] > 0
    assert first["version_bytes"] is None
    assert first["physical_disk_bytes"] is None

    # Reopen the database to prove the sample is persisted and not process-local state.
    reopened = sample_for(settings, job_id)
    assert reopened == first


def test_partial_property_scan_records_nonzero_observed_sample(foundation):
    settings, scope = foundation
    fake = FailingGetS3({"raw/a.jpg": image_bytes()})
    job_id = run_scan(settings, scope, fake, {"max_objects": 10, "objects_per_second": 100})
    sample = sample_for(settings, job_id)
    assert sample is not None
    assert sample["scan_state"] == "partially_failed"
    assert sample["errors"] == 1
    assert sample["object_count"] == 1


def test_budget_exhausted_scan_does_not_write_fake_zero_sample(foundation):
    settings, scope = foundation
    fake = FakeS3({"raw/a.jpg": image_bytes(), "raw/b.jpg": image_bytes()})
    job_id = run_scan(settings, scope, fake, {"max_objects": 1, "objects_per_second": 100, "page_size": 1})
    with db.connect(settings) as conn:
        state = conn.execute("SELECT state,error_code FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert state["state"] == "failed"
    assert state["error_code"] == "SCAN_BUDGET_EXHAUSTED"
    assert sample_for(settings, job_id) is None
