from __future__ import annotations

import io
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.environ["PYTHONPATH"] = str(BACKEND)

from console import catalog, core, db, jobs
from console.config import Settings


class Body(io.BytesIO):
    pass


class FakePagedS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.list_calls = 0

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Prefix"] == "raw/"
        self.list_calls += 1
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken") or 0)
        max_keys = min(1, int(kwargs["MaxKeys"]))
        page = keys[start:start + max_keys]
        next_index = start + len(page)
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[key]),
                    "ETag": f'"{key}"',
                    "ContentLength": len(self.objects[key]),
                    "ContentType": _content_type(key),
                }
                for key in page
            ],
            "IsTruncated": next_index < len(keys),
            "NextContinuationToken": str(next_index) if next_index < len(keys) else None,
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        return {
            "ETag": f'"{key}"',
            "ContentLength": len(self.objects[key]),
            "ContentType": _content_type(key),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        return {
            "Body": Body(self.objects[key]),
            "ContentLength": len(self.objects[key]),
            "ContentType": _content_type(key),
        }


def _content_type(key: str) -> str:
    if key.endswith(".png"):
        return "image/png"
    if key.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _image_bytes(fmt: str = "JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (5, 4), (10, 20, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture()
def connection(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "console.db",
        admin_password="admin123",
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
        scope = _seed_scope(conn)
        conn.commit()
        yield conn, settings, scope


def _seed_scope(conn: sqlite3.Connection) -> dict[str, Any]:
    storage_connection = core.create_connection(
        conn,
        {
            "display_name": "Fake SeaweedFS",
            "endpoint_url": "http://127.0.0.1:8333",
            "region": "us-east-1",
            "secret_ref": "server-test",
            "addressing_style": "path",
            "verify_tls": False,
        },
    )
    project = core.create_project(conn, {"project_key": "jobs", "display_name": "Jobs"})
    return core.create_scope(
        conn,
        project["id"],
        {
            "connection_id": storage_connection["id"],
            "display_name": "Raw",
            "bucket": "swc-integration-test",
            "prefix": "raw/",
            "scope_policy": {"allow_preview": True, "allow_original_download": True},
        },
    )


def test_checkpoint_rejects_stale_lease_commit(connection) -> None:
    conn, _settings, scope = connection
    submitted = jobs.submit_job(conn, scope["id"], "scan", {"max_objects": 10})
    conn.commit()
    claimed = jobs.claim(conn)
    assert claimed is not None

    conn.execute(
        "UPDATE jobs SET lease_token=?, fencing_version=fencing_version+1 WHERE id=?",
        ("newer-token", submitted["id"]),
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="LEASE_LOST"):
        jobs.checkpoint(conn, claimed, state="succeeded", processed=1)
    row = conn.execute("SELECT state, processed FROM jobs WHERE id=?", (submitted["id"],)).fetchone()
    assert row["state"] == "running"
    assert row["processed"] == 0


def test_reap_cancelled_running_job_clears_lease_and_fences_executor(connection) -> None:
    conn, _settings, scope = connection
    submitted = jobs.submit_job(conn, scope["id"], "scan", {"max_objects": 10})
    conn.commit()
    claimed = jobs.claim(conn)
    assert claimed is not None

    conn.execute("UPDATE jobs SET cancel_requested_at=?, lease_expires_at=? WHERE id=?", (jobs.now(), "2000-01-01T00:00:00.000000Z", submitted["id"]))
    jobs.reap(conn)
    conn.commit()

    row = conn.execute("SELECT state, lease_token, fencing_version FROM jobs WHERE id=?", (submitted["id"],)).fetchone()
    assert row["state"] == "cancelled"
    assert row["lease_token"] is None
    assert row["fencing_version"] > claimed["fencing_version"]


def test_paused_queued_job_is_not_claimed_until_resumed(connection) -> None:
    conn, _settings, scope = connection
    submitted = jobs.submit_job(conn, scope["id"], "scan", {"max_objects": 10})
    conn.execute("UPDATE jobs SET pause_requested_at=? WHERE id=?", (jobs.now(), submitted["id"]))
    conn.commit()

    assert jobs.claim(conn) is None

    conn.execute("UPDATE jobs SET pause_requested_at=NULL WHERE id=?", (submitted["id"],))
    conn.commit()
    assert jobs.claim(conn)["id"] == submitted["id"]


def test_scan_processes_more_than_five_pages_and_preserves_raw_prefixes(connection, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, settings, scope = connection
    image = _image_bytes("JPEG")
    objects = {
        "raw/%literal.jpg": image,
        "raw/_literal.png": _image_bytes("PNG"),
        "raw/../literal.webp": _image_bytes("WEBP"),
        "raw/page-03.jpg": image,
        "raw/page-04.jpg": image,
        "raw/page-05.jpg": image,
        "raw/page-06.jpg": image,
    }
    fake_s3 = FakePagedS3(objects)
    monkeypatch.setattr(catalog, "s3_for", lambda *_args: fake_s3)
    submitted = jobs.submit_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 20, "objects_per_second": 100})
    conn.commit()
    claimed = jobs.claim(conn)
    assert claimed is not None

    catalog.execute_job(claimed, settings)

    conn.commit()
    row = conn.execute("SELECT state, processed, errors, error_code FROM jobs WHERE id=?", (submitted["id"],)).fetchone()
    assert row["state"] == "succeeded"
    assert row["processed"] == 7
    assert row["errors"] == 0
    assert row["error_code"] is None
    assert fake_s3.list_calls > 5

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = core.list_objects(conn, settings, scope["id"], limit=1, cursor=cursor)
        seen.extend(item["key"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(objects)
    assert "raw/%literal.jpg" in seen
    assert "raw/_literal.png" in seen
    assert "raw/../literal.webp" in seen
