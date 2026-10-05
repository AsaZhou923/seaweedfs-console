from __future__ import annotations

import io
import hashlib
from pathlib import Path
from typing import Any

import pytest

from console import batch_operations, catalog, core, db, jobs, storage
from console.config import Settings


LAST_MODIFIED = "2026-10-04T00:00:00.000000Z"


class ClientError(Exception):
    def __init__(self, status: int, code: str):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    def __init__(self, name: str):
        self.name = name
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def seed(self, bucket: str, key: str, data: bytes, *, content_type: str = "image/jpeg", metadata: dict[str, str] | None = None) -> None:
        self.objects[(bucket, key)] = {
            "Body": data,
            "ContentType": content_type,
            "Metadata": dict(metadata or {}),
            "ETag": f'"{self.name}-{key}-{len(data)}"',
            "LastModified": LAST_MODIFIED,
        }

    def mutate(self, bucket: str, key: str, data: bytes) -> None:
        current = self.objects[(bucket, key)]
        current["Body"] = data
        current["ETag"] = f'"{self.name}-{key}-{len(data)}-changed"'

    def head_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise ClientError(404, "NoSuchKey")
        obj = self.objects[key]
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(obj["Body"]),
            "ContentType": obj["ContentType"],
            "ETag": obj["ETag"],
            "LastModified": obj["LastModified"],
            "Metadata": dict(obj["Metadata"]),
        }

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        head = self.head_object(**kwargs)
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != head["ETag"]:
            raise ClientError(412, "PreconditionFailed")
        return {
            **head,
            "Body": io.BytesIO(self.objects[(kwargs["Bucket"], kwargs["Key"])]["Body"]),
        }

    def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        key = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError(412, "PreconditionFailed")
        body = kwargs["Body"]
        data = body if isinstance(body, bytes) else body.read()
        self.seed(kwargs["Bucket"], kwargs["Key"], data, content_type=kwargs.get("ContentType") or "application/octet-stream", metadata=kwargs.get("Metadata") or {})
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def delete_object(self, **kwargs):  # pragma: no cover - asserted unused.
        self.delete_calls.append(dict(kwargs))
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


@pytest.fixture()
def foundation(tmp_path: Path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_password="admin-pass",
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        dev_insecure_cookie=True,
        cookie_secure=False,
    )
    db.initialize(settings)
    source_s3 = FakeS3("source")
    target_s3 = FakeS3("target")
    clients: dict[str, FakeS3] = {}

    def fake_client(connection, _settings):
        return clients[connection["endpoint_url"]]

    monkeypatch.setattr(storage, "client", fake_client)
    with db.connect(settings) as conn:
        jobs.initialize(conn)
        catalog.initialize(conn)
        batch_operations.initialize(conn)
        core.bootstrap_admin(conn, settings)
        source_conn = core.create_connection(conn, {"display_name": "source", "endpoint_url": "mock://source", "secret_ref": "source-secret"})
        target_conn = core.create_connection(conn, {"display_name": "target", "endpoint_url": "mock://target", "secret_ref": "target-secret"})
        clients[source_conn["endpoint_url"]] = source_s3
        clients[target_conn["endpoint_url"]] = target_s3
        project = core.create_project(conn, {"project_key": "demo", "display_name": "Demo"})
        source_scope = core.create_scope(
            conn,
            project["id"],
            {"connection_id": source_conn["id"], "display_name": "Source", "bucket": "src", "prefix": "raw/", "scope_policy": {"allow_original_download": True}},
        )
        target_scope = core.create_scope(
            conn,
            project["id"],
            {"connection_id": target_conn["id"], "display_name": "Target", "bucket": "dst", "prefix": "copied/", "writable": True, "overlap_ack": True},
        )
    return settings, source_s3, target_s3, source_scope, target_scope


def seed_source(settings: Settings, source_s3: FakeS3, scope: dict[str, Any], key: str, data: bytes):
    source_s3.seed(scope["bucket"], key, data)
    with db.connect(settings) as conn:
        return core.upsert_object(
            conn,
            scope["id"],
            {
                "key": key,
                "size": len(data),
                "etag": source_s3.head_object(Bucket=scope["bucket"], Key=key)["ETag"],
                "last_modified": LAST_MODIFIED,
                "content_type": "image/jpeg",
            },
        )


def submit(settings: Settings, source_scope: dict[str, Any], items: list[dict[str, Any]], idem: str = "copy-1"):
    with db.connect(settings) as conn:
        batch = batch_operations.submit_copy_batch(conn, source_scope["id"], {"items": items}, actor="admin", idempotency_key=idem)
        return batch


def run_next(settings: Settings):
    with db.connect(settings) as conn:
        job = jobs.claim(conn)
    assert job is not None
    batch_operations.execute_copy_batch(job, settings)
    with db.connect(settings) as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())


def test_idempotency_reuses_job_and_rejects_changed_targets(foundation):
    settings, source_s3, _target_s3, source_scope, target_scope = foundation
    obj = seed_source(settings, source_s3, source_scope, "raw/a.jpg", b"alpha")
    item = {"object_id": obj["id"], "target_scope_id": target_scope["id"], "target_key": "copied/a.jpg"}

    first = submit(settings, source_scope, [item], idem="same")
    second = submit(settings, source_scope, [item], idem="same")
    assert second["job"]["id"] == first["job"]["id"]
    with db.connect(settings) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM copy_batch_items").fetchone()["c"] == 1

    changed = {"object_id": obj["id"], "target_scope_id": target_scope["id"], "target_key": "copied/b.jpg"}
    with db.connect(settings) as conn:
        with pytest.raises(core.AppError) as exc:
            batch_operations.submit_copy_batch(conn, source_scope["id"], {"items": [changed]}, actor="admin", idempotency_key="same")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_same_key_target_race_uses_conditional_put_and_does_not_overwrite(foundation):
    settings, source_s3, target_s3, source_scope, target_scope = foundation
    obj = seed_source(settings, source_s3, source_scope, "raw/a.jpg", b"alpha")
    target_s3.seed(target_scope["bucket"], "copied/a.jpg", b"existing", metadata={"owner": "someone-else"})
    submit(settings, source_scope, [{"object_id": obj["id"], "target_scope_id": target_scope["id"], "target_key": "copied/a.jpg"}])

    job = run_next(settings)

    assert job["state"] == "needs_review"
    assert target_s3.put_calls[0]["IfNoneMatch"] == "*"
    assert target_s3.objects[(target_scope["bucket"], "copied/a.jpg")]["Body"] == b"existing"
    with db.connect(settings) as conn:
        item = conn.execute("SELECT phase,error_code FROM copy_batch_items").fetchone()
    assert item["phase"] == "needs_review"
    assert item["error_code"] == "COPY_TARGET_NEEDS_REVIEW"


def test_source_changed_before_copy_fails_without_target_call(foundation):
    settings, source_s3, target_s3, source_scope, target_scope = foundation
    obj = seed_source(settings, source_s3, source_scope, "raw/a.jpg", b"alpha")
    submit(settings, source_scope, [{"object_id": obj["id"], "target_scope_id": target_scope["id"], "target_key": "copied/a.jpg"}])
    source_s3.mutate(source_scope["bucket"], "raw/a.jpg", b"changed")

    job = run_next(settings)

    assert job["state"] == "needs_review"
    assert target_s3.put_calls == []
    with db.connect(settings) as conn:
        item = conn.execute("SELECT phase,error_code FROM copy_batch_items").fetchone()
    assert item["phase"] == "failed"
    assert item["error_code"] == "OBJECT_REVISION_CHANGED"


def test_archived_output_scope_blocks_before_storage_calls(foundation):
    settings, source_s3, target_s3, source_scope, target_scope = foundation
    obj = seed_source(settings, source_s3, source_scope, "raw/a.jpg", b"alpha")
    submit(settings, source_scope, [{"object_id": obj["id"], "target_scope_id": target_scope["id"], "target_key": "copied/a.jpg"}])
    with db.connect(settings) as conn:
        core.archive_scope(conn, target_scope["id"])

    job = run_next(settings)

    assert job["state"] == "needs_review"
    assert source_s3.get_calls == []
    assert target_s3.put_calls == []
    with db.connect(settings) as conn:
        item = conn.execute("SELECT phase,error_code FROM copy_batch_items").fetchone()
    assert item["phase"] == "failed"
    assert item["error_code"] == "FORBIDDEN_SCOPE"


def test_batch_two_copies_mixed_results_indexes_confirmed_target_and_never_deletes_source(foundation):
    settings, source_s3, target_s3, source_scope, target_scope = foundation
    first = seed_source(settings, source_s3, source_scope, "raw/a.jpg", b"alpha")
    second = seed_source(settings, source_s3, source_scope, "raw/b.jpg", b"bravo")
    target_s3.seed(target_scope["bucket"], "copied/b.jpg", b"preexisting", metadata={"owner": "someone-else"})
    submit(
        settings,
        source_scope,
        [
            {"object_id": first["id"], "target_scope_id": target_scope["id"], "target_key": "copied/a.jpg"},
            {"object_id": second["id"], "target_scope_id": target_scope["id"], "target_key": "copied/b.jpg"},
        ],
    )

    job = run_next(settings)

    assert job["state"] == "needs_review"
    assert source_s3.delete_calls == []
    assert target_s3.objects[(target_scope["bucket"], "copied/a.jpg")]["Body"] == b"alpha"
    assert target_s3.objects[(target_scope["bucket"], "copied/b.jpg")]["Body"] == b"preexisting"
    with db.connect(settings) as conn:
        phases = [row["phase"] for row in conn.execute("SELECT phase FROM copy_batch_items ORDER BY ordinal")]
        indexed = conn.execute("SELECT * FROM objects WHERE scope_id=? AND key=?", (target_scope["id"], "copied/a.jpg")).fetchone()
    assert phases == ["confirmed", "needs_review"]
    assert indexed is not None
    assert indexed["checksum"] == hashlib.sha256(b"alpha").hexdigest()
