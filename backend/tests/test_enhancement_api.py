from __future__ import annotations

import asyncio
import io
import json
import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from backend.console import catalog, core, db, enhancement_api, enhancements, jobs
from backend.console.security import AppError


class Body(io.BytesIO):
    def close(self):  # catalog.read_bound closes the stream.
        super().close()


class FakeS3:
    def __init__(self):
        self.objects = {
            ("bucket", "in.jpg"): {
                "Body": b"source",
                "ETag": '"source-etag"',
                "ContentLength": 6,
                "ContentType": "image/jpeg",
                "ChecksumSHA256": hashlib.sha256(b"source").hexdigest(),
            }
        }
        self.puts = 0
        self.copies = 0
        self.deny_missing_status = None
        self.multipart_completed = []

    def head_object(self, Bucket, Key, **kwargs):
        if (Bucket, Key) not in self.objects:
            if self.deny_missing_status:
                raise FakeClientError(self.deny_missing_status)
            raise FakeClientError(404)
        item = self.objects[(Bucket, Key)]
        return {k: v for k, v in item.items() if k != "Body"}

    def get_object(self, Bucket, Key, **kwargs):
        if 'IfMatch' in kwargs and kwargs['IfMatch'] != self.objects[(Bucket, Key)]['ETag']:
            raise FakeClientError(412)
        return {"Body": Body(self.objects[(Bucket, Key)]["Body"])}

    def put_object(self, Bucket, Key, Body, Metadata, ContentType, IfNoneMatch=None):
        assert IfNoneMatch == "*"
        self.puts += 1
        self.objects[(Bucket, Key)] = {
            "Body": Body,
            "ETag": '"out-etag"',
            "ContentLength": len(Body),
            "ContentType": ContentType,
            "Metadata": dict(Metadata),
            "ChecksumSHA256": hashlib.sha256(Body).hexdigest(),
        }

    def copy_object(self, **kwargs):
        self.copies += 1

    def create_multipart_upload(self, Bucket, Key, Metadata):
        return {"UploadId": "upload-1"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch=None):
        assert IfNoneMatch == "*"
        if (Bucket, Key) in self.objects:
            raise FakeClientError(412)
        self.multipart_completed.append((Bucket, Key, UploadId, MultipartUpload))
        self.objects[(Bucket, Key)] = {
            "Body": b"".join(b"x" * int(part.get("Size", 0)) for part in MultipartUpload.get("Parts", [])),
            "ETag": '"multipart"',
            "ContentLength": 1,
            "Metadata": {},
        }

    def put_object_retention(self, Bucket, Key, Retention, VersionId=None):
        self.retention = (Bucket, Key, VersionId, dict(Retention))

    def put_object_legal_hold(self, Bucket, Key, LegalHold, VersionId=None):
        self.legal_hold = (Bucket, Key, VersionId, dict(LegalHold))

    def get_object_retention(self, Bucket, Key, VersionId=None):
        return {"Retention": getattr(self, "retention", (None, None, None, {}))[3]}

    def get_object_legal_hold(self, Bucket, Key, VersionId=None):
        return {"LegalHold": getattr(self, "legal_hold", (None, None, None, {}))[3]}

    def get_bucket_cors(self, Bucket):
        self.cors_request_id = getattr(self, "cors_request_id", 0) + 1
        config = getattr(self, "cors", {"CORSRules": [{"AllowedOrigins": ["https://example.test"], "AllowedMethods": ["GET"]}]})
        return dict(config) | {"ResponseMetadata": {"RequestId": f"request-{self.cors_request_id}", "HTTPStatusCode": 200}}

    def put_bucket_cors(self, Bucket, CORSConfiguration):
        self.cors = dict(CORSConfiguration)


class FakeClientError(Exception):
    def __init__(self, status):
        super().__init__("fake client error")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


class FakeImaging:
    PIPELINE_VERSION = "test-pipeline"

    @staticmethod
    def decode(data, params):
        assert params["fit"] == "fit"
        return {"output_mime": "image/webp", "width": params["width"], "height": params["height"]}, b"derived"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(enhancement_api.catalog, "imaging", FakeImaging)
    return SimpleNamespace(database_path=tmp_path / "db.sqlite", data_dir=tmp_path, admin_password="admin")


def setup_variant(settings):
    db.initialize(settings)
    with db.connect(settings) as conn:
        jobs.initialize(conn)
        enhancements.initialize(conn)
        now = jobs.now()
        conn.execute(
            "INSERT INTO storage_connections(id,display_name,endpoint_url,region,secret_ref,addressing_style,verify_tls,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("con", "c", "http://s3", "us-east-1", "ref", "path", 1, now, now),
        )
        conn.execute("INSERT INTO projects(id,project_key,display_name,created_at,updated_at) VALUES(?,?,?,?,?)", ("p", "p", "P", now, now))
        conn.execute(
            "INSERT INTO scopes(id,project_id,connection_id,display_name,bucket,prefix,writable,manage_bucket,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s", "p", "con", "source", "bucket", "", 1, 1, now, now),
        )
        conn.execute("INSERT INTO assets(id,project_id,first_seen_at,tags_json) VALUES(?,?,?,?)", ("a", "p", now, "[]"))
        source_checksum = hashlib.sha256(b"source").hexdigest()
        revision = core.make_revision("bucket", "in.jpg", {"etag": '"source-etag"', "size": 6, "last_modified": None, "checksum": source_checksum})
        conn.execute(
            """
            INSERT INTO objects(id,asset_id,scope_id,key,revision,size,etag,first_seen_at,observed_at,properties_json,content_type,checksum)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("o", "a", "s", "in.jpg", revision, 6, '"source-etag"', now, now, "{}", "image/jpeg", source_checksum),
        )
        preset = enhancements.create_preset(
            conn,
            project_id="p",
            name="thumb",
            params={
                "mode": "fit",
                "width": 10,
                "height": 10,
                "quality": 80,
                "format": "webp",
                "alpha_policy": "preserve",
                "orientation": "auto",
                "color_policy": "srgb",
                "metadata_policy": "strip",
            },
        )
        variant = enhancements.create_derived_variant_intent(
            conn,
            project_id="p",
            scope_id="s",
            source_object_id="o",
            source_revision=revision,
            preset_id=preset["id"],
            output_scope_id="s",
            output_bucket="bucket",
            output_key="out.webp",
        )
        job = jobs.submit_job(conn, "s", "derived_variant", {"variant_id": variant["id"]}, actor="admin", effect_class="remote_mutating")
        conn.commit()
        claimed = jobs.claim(conn)
        conn.commit()
        return variant, claimed


def test_execute_variant_job_writes_once_and_records_success(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))

    enhancement_api.execute_variant_job(job, settings)

    with db.connect(settings) as conn:
        row = conn.execute("SELECT state,processed,errors FROM jobs WHERE id=?", (job["id"],)).fetchone()
        stored = enhancements.get_derived_variant(conn, variant["id"])
    assert dict(row) == {"state": "succeeded", "processed": 1, "errors": 0}
    assert stored["status"] == "succeeded"
    assert stored["output_size"] == len(b"derived")
    assert fake.puts == 1
    assert fake.objects[("bucket", "out.webp")]["Metadata"]["swc-variant-id"] == variant["id"]


def test_execute_variant_job_reuses_owned_existing_target(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()
    fake.put_object(
        Bucket="bucket",
        Key="out.webp",
        Body=b"derived",
        Metadata={"swc-variant-id": variant["id"]},
        ContentType="image/webp",
        IfNoneMatch="*",
    )
    fake.puts = 0
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))

    enhancement_api.execute_variant_job(job, settings)

    with db.connect(settings) as conn:
        stored = enhancements.get_derived_variant(conn, variant["id"])
    assert stored["status"] == "succeeded"
    assert stored["output_size"] == len(b"derived")
    assert fake.puts == 0


def test_derived_health_checks_output_missing_corrupt_outdated_and_healthy(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:read"))

    enhancement_api.execute_variant_job(job, settings)
    healthy = enhancement_api.derived_health(request, "s")
    assert healthy["checks"][0]["state"] == "healthy"

    fake.objects.pop(("bucket", "out.webp"))
    missing = enhancement_api.derived_health(request, "s")
    assert missing["checks"][0]["state"] == "missing_output"

    fake.put_object(Bucket="bucket", Key="out.webp", Body=b"broken", Metadata={"swc-variant-id": variant["id"]}, ContentType="image/webp", IfNoneMatch="*")
    corrupt = enhancement_api.derived_health(request, "s")
    assert corrupt["checks"][0]["state"] == "corrupt_output"

    with db.connect(settings) as conn:
        enhancements.create_preset(
            conn,
            project_id="p",
            name="thumb",
            params={"mode": "fit", "width": 11, "height": 11, "quality": 80, "format": "webp", "alpha_policy": "preserve", "orientation": "auto", "color_policy": "srgb", "metadata_policy": "strip"},
        )
        conn.commit()
    outdated = enhancement_api.derived_health(request, "s")
    assert outdated["checks"][0]["state"] == "outdated_preset"


def test_derived_health_detects_rescanned_current_source_revision_without_mutating_old_row(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:read"))

    enhancement_api.execute_variant_job(job, settings)
    fake.objects[("bucket", "in.jpg")]["Body"] = b"source-overwritten"
    fake.objects[("bucket", "in.jpg")]["ETag"] = '"source-new-etag"'
    fake.objects[("bucket", "in.jpg")]["ContentLength"] = len(b"source-overwritten")
    fake.objects[("bucket", "in.jpg")]["ChecksumSHA256"] = hashlib.sha256(b"source-overwritten").hexdigest()
    with db.connect(settings) as conn:
        now = jobs.now()
        new_revision = core.make_revision("bucket", "in.jpg", {"etag": '"source-new-etag"', "size": len(b"source-overwritten"), "last_modified": None})
        conn.execute("UPDATE objects SET is_current=0 WHERE id='o'")
        conn.execute(
            "INSERT INTO objects(id,asset_id,scope_id,key,revision,size,etag,first_seen_at,observed_at,properties_json,content_type,checksum,is_current) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o-current", "a", "s", "in.jpg", new_revision, len(b"source-overwritten"), '"source-new-etag"', now, now, "{}", "image/jpeg", hashlib.sha256(b"source-overwritten").hexdigest(), 1),
        )
        old_revision = conn.execute("SELECT revision FROM objects WHERE id='o'").fetchone()["revision"]
        conn.commit()

    health = enhancement_api.derived_health(request, "s")
    check = health["checks"][0]
    assert check["state"] == "outdated_source"
    assert check["stored_input_revision"] == old_revision == variant["source_revision"]
    assert check["current_indexed_revision"] != check["stored_input_revision"]
    assert check["current_observed_revision"] != check["stored_input_revision"]


def test_execute_variant_job_rejects_changed_source(settings, monkeypatch):
    _variant, job = setup_variant(settings)
    fake = FakeS3()
    fake.objects[("bucket", "in.jpg")]["ETag"] = '"changed"'
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))

    with pytest.raises(Exception):
        enhancement_api.execute_variant_job(job, settings)


def _setup_batch(settings, monkeypatch, *, key="batch-key"):
    setup_variant(settings)
    with db.connect(settings) as conn:
        now = jobs.now()
        conn.execute("INSERT INTO assets(id,project_id,first_seen_at,tags_json) VALUES(?,?,?,?)", ("a2", "p", now, "[]"))
        checksum = hashlib.sha256(b"source2").hexdigest()
        revision = core.make_revision("bucket", "in2.jpg", {"etag": '"source2-etag"', "size": 7, "last_modified": None, "checksum": checksum})
        conn.execute(
            "INSERT INTO objects(id,asset_id,scope_id,key,revision,size,etag,first_seen_at,observed_at,properties_json,content_type,checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o2", "a2", "s", "in2.jpg", revision, 7, '"source2-etag"', now, now, "{}", "image/jpeg", checksum),
        )
        conn.execute(
            "INSERT INTO scopes(id,project_id,connection_id,display_name,bucket,prefix,writable,manage_bucket,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("out", "p", "con", "output", "bucket", "allowed/", 1, 0, now, now),
        )
        preset2 = enhancements.create_preset(
            conn,
            project_id="p",
            name="small",
            params={"mode": "fit", "width": 5, "height": 5, "quality": 80, "format": "webp", "alpha_policy": "preserve", "orientation": "auto", "color_policy": "srgb", "metadata_policy": "strip"},
        )
        preset_ids = [row["id"] for row in conn.execute("SELECT id FROM enhancement_presets ORDER BY created_at").fetchall()]
        assert preset2["id"] in preset_ids
        conn.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={"Idempotency-Key": key}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:read"))
    return request, preset_ids


def _count(settings, sql):
    with db.connect(settings) as conn:
        return conn.execute(sql).fetchone()[0]


def test_derived_variant_batch_plans_two_by_two_and_reuses_idempotency(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)

    body = {"object_ids": ["o", "o2"], "preset_ids": preset_ids, "output_scope_id": "s", "output_prefix": "batch/"}
    first = enhancement_api.submit_derived_variant_batch(request, "s", body)
    second = enhancement_api.submit_derived_variant_batch(request, "s", body)

    assert first["planned_count"] == 4
    assert len(first["items"]) == 4
    assert first["id"] == second["id"]
    assert first["status"] == second["status"] == "queued"
    assert {item["job"]["kind"] for item in first["items"]} == {"derived_variant"}
    assert all(item["source_revision"] for item in first["items"])
    assert all(item["output_key"].startswith("batch/") for item in first["items"])
    assert _count(settings, "SELECT COUNT(*) FROM jobs WHERE kind='derived_variant'") == 5  # setup fixture job + 4 batch jobs


def test_derived_variant_batch_default_key_uses_output_scope_prefix_literally(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)

    default = enhancement_api.submit_derived_variant_batch(request, "s", {"object_ids": ["o"], "preset_ids": preset_ids, "output_scope_id": "out"})
    request.headers = {"Idempotency-Key": "literal-prefix"}
    literal = enhancement_api.submit_derived_variant_batch(request, "s", {"object_ids": ["o"], "preset_ids": preset_ids[:1], "output_scope_id": "out", "output_prefix": "thumbs-"})

    assert all(item["output_key"].startswith("allowed/derived/") for item in default["items"])
    assert all(item["variant"]["output_key"] == item["output_key"] for item in default["items"])
    assert literal["items"][0]["output_key"].startswith("allowed/thumbs-")
    assert "/" not in literal["items"][0]["output_key"][len("allowed/thumbs-"):]


def _pin_snapshot(settings, snapshot_id, items, *, scope_id="s", actor="admin", epoch=1, expires_at=None):
    with db.connect(settings) as conn:
        catalog.initialize(conn)
        conn.execute(
            "INSERT INTO operation_snapshots VALUES(?,?,?,?,?,?,?,?)",
            (snapshot_id, scope_id, actor, epoch, "{}", json.dumps(items), jobs.now(), expires_at or "9999-12-31T00:00:00.000000Z"),
        )
        conn.commit()


def test_derived_variant_batch_resolves_pinned_snapshot(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)
    with db.connect(settings) as conn:
        revisions = {row["id"]: row["revision"] for row in conn.execute("SELECT id, revision FROM objects")}
    _pin_snapshot(settings, "snap", [{"object_id": "o", "revision": revisions["o"]}, {"object_id": "o2", "revision": revisions["o2"]}])

    batch = enhancement_api.submit_derived_variant_batch(request, "s", {"snapshot_id": "snap", "preset_ids": preset_ids[:1]})

    assert batch["planned_count"] == 2
    assert {item["object_id"] for item in batch["items"]} == {"o", "o2"}


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        ({"scope_id": "out"}, "SNAPSHOT_NOT_FOUND"),
        ({"actor": "someone-else"}, "SNAPSHOT_NOT_FOUND"),
        ({"epoch": 0}, "SNAPSHOT_STALE"),
        ({"expires_at": "2000-01-01T00:00:00.000000Z"}, "SNAPSHOT_EXPIRED"),
        ({"revision": "stale-revision"}, "OBJECT_REVISION_CHANGED"),
    ],
)
def test_derived_variant_batch_rejects_unusable_snapshot_without_side_effects(settings, monkeypatch, snapshot, code):
    request, preset_ids = _setup_batch(settings, monkeypatch)
    with db.connect(settings) as conn:
        revision = conn.execute("SELECT revision FROM objects WHERE id='o'").fetchone()[0]
    options = {key: value for key, value in snapshot.items() if key != "revision"}
    _pin_snapshot(settings, "snap", [{"object_id": "o", "revision": snapshot.get("revision", revision)}], **options)

    with pytest.raises(AppError) as error:
        enhancement_api.submit_derived_variant_batch(request, "s", {"snapshot_id": "snap", "preset_ids": preset_ids})

    assert error.value.code == code
    assert _count(settings, "SELECT COUNT(*) FROM derived_variant_batches") == 0
    assert _count(settings, "SELECT COUNT(*) FROM jobs") == 1


def test_derived_variant_batch_rolls_back_partial_planning_and_replay_succeeds(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)
    body = {"object_ids": ["o", "o2"], "preset_ids": preset_ids}
    real_submit = jobs.submit_job
    calls = {"count": 0}

    def flaky_submit(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(enhancement_api.jobs, "submit_job", flaky_submit)
    with pytest.raises(sqlite3.OperationalError):
        enhancement_api.submit_derived_variant_batch(request, "s", body)

    assert _count(settings, "SELECT COUNT(*) FROM derived_variant_batches") == 0
    assert _count(settings, "SELECT COUNT(*) FROM derived_variants") == 1  # setup fixture only
    assert _count(settings, "SELECT COUNT(*) FROM jobs") == 1

    monkeypatch.setattr(enhancement_api.jobs, "submit_job", real_submit)
    batch = enhancement_api.submit_derived_variant_batch(request, "s", body)
    assert batch["status"] == "queued"
    assert len({item["job_id"] for item in batch["items"]}) == 4


def test_derived_variant_batch_replay_resumes_legacy_planning_batch(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)
    body = {"object_ids": ["o"], "preset_ids": preset_ids}
    with db.connect(settings) as conn:
        now = jobs.now()
        conn.execute(
            "INSERT INTO derived_variant_batches(id,scope_id,project_id,request_hash,idempotency_key,status,planned_count,items_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("batch_legacy", "s", "p", enhancements.sha256_text(enhancements.canonical_json(body)), "batch-key", "planning", 2, "[]", now, now),
        )
        conn.commit()

    batch = enhancement_api.submit_derived_variant_batch(request, "s", body)

    assert batch["id"] == "batch_legacy"
    assert batch["status"] == "queued"
    assert len(batch["items"]) == 2
    assert all(item["job_id"] and item["variant_id"] for item in batch["items"])


def test_derived_variant_batch_resume_keeps_persisted_plan_and_queued_jobs(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)
    # Written by the old code: output_prefix "batch" was normalized to "batch/".
    body = {"object_ids": ["o"], "preset_ids": preset_ids[:1], "output_prefix": "batch"}
    with db.connect(settings) as conn:
        now = jobs.now()
        revision = conn.execute("SELECT revision FROM objects WHERE id='o'").fetchone()[0]
        stored = [{"object_id": "o", "source_revision": revision, "preset_id": preset_ids[0], "preset_version": 1, "output_scope_id": "s", "output_key": "batch/legacy.webp", "state": "planned"}]
        conn.execute(
            "INSERT INTO derived_variant_batches(id,scope_id,project_id,request_hash,idempotency_key,status,planned_count,items_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("batch_legacy", "s", "p", enhancements.sha256_text(enhancements.canonical_json(body)), "batch-key", "planning", 1, json.dumps(stored), now, now),
        )
        # The interrupted attempt had already committed this pair's variant and job.
        variant_id = enhancements.plan_derived_variant(
            conn, project_id="p", scope_id="s", source_object_id="o", source_revision=revision, preset_id=preset_ids[0],
            output_scope_id="s", output_bucket="bucket", output_key="batch/legacy.webp", actor_id="admin",
        )
        earlier_job = jobs.submit_job(conn, "s", "derived_variant", {"variant_id": variant_id, "source_revision": revision}, actor="admin", idempotency_key=f"batch_legacy:o:{preset_ids[0]}", effect_class="remote_mutating")
        conn.commit()

    batch = enhancement_api.submit_derived_variant_batch(request, "s", body)

    assert batch["status"] == "queued"
    assert [item["output_key"] for item in batch["items"]] == ["batch/legacy.webp"]
    assert batch["items"][0]["variant_id"] == variant_id
    assert batch["items"][0]["job_id"] == earlier_job["id"]


def test_derived_variant_batch_status_follows_items(settings, monkeypatch):
    request, preset_ids = _setup_batch(settings, monkeypatch)
    batch = enhancement_api.submit_derived_variant_batch(request, "s", {"object_ids": ["o", "o2"], "preset_ids": preset_ids[:1]})
    first, second = batch["items"]

    def read():
        with db.connect(settings) as conn:
            return enhancement_api._batch_public(conn, enhancements.get_derived_batch(conn, batch["id"]))

    def set_state(item, job_state, variant_state):
        with db.connect(settings) as conn:
            conn.execute("UPDATE jobs SET state=? WHERE id=?", (job_state, item["job_id"]))
            conn.execute("UPDATE derived_variants SET status=? WHERE id=?", (variant_state, item["variant_id"]))
            conn.commit()

    assert read()["status"] == "queued"
    set_state(first, "running", "planned")
    assert read()["status"] == "running"
    set_state(first, "succeeded", "succeeded")
    set_state(second, "succeeded", "succeeded")
    current = read()
    assert current["status"] == "succeeded"
    assert current["counts"] == {"succeeded": 2}

    # A terminal job whose variant row was never updated is reported by job state.
    set_state(second, "needs_review", "planned")
    current = read()
    assert current["status"] == "needs_review"
    assert current["counts"] == {"succeeded": 1, "needs_review": 1}

    set_state(second, "needs_review", "failed")
    assert read()["status"] == "partially_failed"
    set_state(first, "needs_review", "failed")
    assert read()["status"] == "failed"


def test_execute_variant_job_records_post_dispatch_storage_failure(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()

    def broken_put(**kwargs):
        raise FakeClientError(500)

    fake.put_object = broken_put
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))

    with pytest.raises(enhancement_api.storage.StorageError):
        enhancement_api.execute_variant_job(job, settings)

    with db.connect(settings) as conn:
        stored = enhancements.get_derived_variant(conn, variant["id"])
    assert stored["status"] == "needs_review"
    assert stored["last_error"] == "STORAGE_UNAVAILABLE"
    assert stored["manifest"]["status"] == "needs_review"


def test_execute_variant_job_marks_variant_failed_before_dispatch(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()
    fake.objects[("bucket", "in.jpg")]["ETag"] = '"changed"'
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))

    with pytest.raises(AppError):
        enhancement_api.execute_variant_job(job, settings)

    with db.connect(settings) as conn:
        stored = enhancements.get_derived_variant(conn, variant["id"])
    assert stored["status"] == "failed"
    assert stored["last_error"] == "OBJECT_REVISION_CHANGED"


def test_execute_variant_job_failure_does_not_write_after_lease_loss(settings, monkeypatch):
    variant, job = setup_variant(settings)
    fake = FakeS3()
    fake.objects[("bucket", "in.jpg")]["ETag"] = '"changed"'
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))
    real_assert = enhancement_api._assert_job_current

    def lose_lease(settings, current):
        real_assert(settings, current)
        with db.connect(settings) as conn:
            conn.execute("UPDATE jobs SET fencing_version=fencing_version+1 WHERE id=?", (current["id"],))
            conn.commit()

    monkeypatch.setattr(enhancement_api, "_assert_job_current", lose_lease)

    with pytest.raises(AppError):
        enhancement_api.execute_variant_job(job, settings)

    with db.connect(settings) as conn:
        stored = enhancements.get_derived_variant(conn, variant["id"])
    assert stored["status"] == "planned"
    assert stored["last_error"] is None


def test_run_once_preserves_storage_error_code(settings, monkeypatch):
    _variant, job = setup_variant(settings)
    with db.connect(settings) as conn:
        conn.execute("UPDATE jobs SET state='queued', lease_token=NULL, lease_expires_at=NULL WHERE id=?", (job["id"],))
        conn.commit()

    def storage_failure(job, settings):
        raise enhancement_api.storage.StorageError("STORAGE_FORBIDDEN", "Storage denied the request.", 403)

    monkeypatch.setitem(jobs.HANDLERS, "derived_variant", storage_failure)

    assert jobs.run_once(settings) is True
    with db.connect(settings) as conn:
        row = conn.execute("SELECT state, error_code FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert dict(row) == {"state": "needs_review", "error_code": "STORAGE_FORBIDDEN"}
    assert jobs.error_code(RuntimeError("boom")) == "WORKER_FAILURE"


def test_copy_object_only_treats_404_as_missing_target():
    fake = FakeS3()
    fake.deny_missing_status = 403
    adapter = enhancement_api.S3EnhancementAdapter(fake)

    with pytest.raises(Exception):
        adapter.copy_object("bucket", "in.jpg", "bucket", "blocked.webp")
    assert fake.copies == 0


def test_single_object_copy_rejects_index_revision_drift_before_target_write(settings, monkeypatch):
    setup_variant(settings)
    fake = FakeS3()
    fake.objects[("bucket", "in.jpg")].update(
        {
            "Body": b"new-current",
            "ETag": '"new-current"',
            "ContentLength": len(b"new-current"),
            "ChecksumSHA256": hashlib.sha256(b"new-current").hexdigest(),
        }
    )
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user={"id": "admin"}), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:write" if write else "object:read"))

    with pytest.raises(Exception):
        enhancement_api.copy_object(request, "s", {"object_id": "o", "target_key": "copy.jpg"})

    assert ("bucket", "copy.jpg") not in fake.objects
    assert fake.puts == 0


def test_adapter_preserves_literal_null_version_for_reads_tags_and_copy():
    class NullVersionS3:
        def __init__(self):
            self.calls = []
            self.objects = {}

        def _record(self, name, kwargs):
            self.calls.append((name, dict(kwargs)))

        def head_object(self, **kwargs):
            self._record("head_object", kwargs)
            if kwargs["Key"] == "target":
                raise FakeClientError(404)
            version = kwargs.get("VersionId", "current")
            body = b"null-version" if version == "null" else b"current-version"
            return {"ETag": f'"{version}"', "ContentLength": len(body), "ChecksumSHA256": hashlib.sha256(body).hexdigest(), "VersionId": version}

        def get_object(self, **kwargs):
            self._record("get_object", kwargs)
            version = kwargs.get("VersionId", "current")
            body = b"null-version" if version == "null" else b"current-version"
            return {"Body": Body(body)}

        def put_object(self, **kwargs):
            self._record("put_object", kwargs)
            self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs

        def get_object_tagging(self, **kwargs):
            self._record("get_object_tagging", kwargs)
            return {"TagSet": [{"Key": "version", "Value": kwargs.get("VersionId", "current")}]}

        def put_object_tagging(self, **kwargs):
            self._record("put_object_tagging", kwargs)

        def copy_object(self, **kwargs):
            self._record("copy_object", kwargs)

    s3 = NullVersionS3()
    adapter = enhancement_api.S3EnhancementAdapter(s3)

    assert adapter.head_object("bucket", "source", "null")["version_id"] == "null"
    assert adapter.get_object_bytes("bucket", "source", "null") == b"null-version"
    assert adapter.get_object_tags("bucket", "source", "null") == {"version": "null"}
    adapter.put_object_tags("bucket", "source", {"k": "v"}, "null")
    adapter.copy_object("bucket", "source", "bucket", "target", source_version_id="null")

    assert ("head_object", {"Bucket": "bucket", "Key": "source", "VersionId": "null"}) in s3.calls
    assert ("get_object", {"Bucket": "bucket", "Key": "source", "VersionId": "null"}) in s3.calls
    assert ("get_object_tagging", {"Bucket": "bucket", "Key": "source", "VersionId": "null"}) in s3.calls
    assert ("put_object_tagging", {"Bucket": "bucket", "Key": "source", "Tagging": {"TagSet": [{"Key": "k", "Value": "v"}]}, "VersionId": "null"}) in s3.calls
    copy_calls = [kwargs for name, kwargs in s3.calls if name == "copy_object"]
    assert copy_calls[0]["CopySource"]["VersionId"] == "null"


def test_restore_literal_null_version_reads_null_not_current():
    class NullRestoreAdapter:
        def __init__(self):
            self.current = {("b", "source"): "current"}
            self.objects = {
                ("b", "source", "current"): {"bucket": "b", "key": "source", "version_id": "current", "size": len(b"current-version"), "checksum_sha256": hashlib.sha256(b"current-version").hexdigest(), "content_type": "text/plain", "metadata": {}},
                ("b", "source", "null"): {"bucket": "b", "key": "source", "version_id": "null", "size": len(b"null-version"), "checksum_sha256": hashlib.sha256(b"null-version").hexdigest(), "content_type": "text/plain", "metadata": {}},
            }
            self.put_body = None

        def head_object(self, bucket, key, version_id=None):
            if key == "restored":
                if self.put_body is None:
                    raise KeyError(key)
                return {"bucket": bucket, "key": key, "version_id": "put-v1", "size": len(self.put_body), "checksum_sha256": hashlib.sha256(self.put_body).hexdigest(), "content_type": "text/plain", "metadata": {}}
            version = version_id if version_id is not None else self.current[(bucket, key)]
            return dict(self.objects[(bucket, key, version)])

        def get_object_bytes(self, bucket, key, version_id=None):
            if key == "restored":
                return self.put_body
            version = version_id if version_id is not None else self.current[(bucket, key)]
            return b"null-version" if version == "null" else b"current-version"

        def get_object_bytes_if_match(self, bucket, key, version_id, etag):
            assert version_id == 'null' and etag == 'null-etag'
            return self.get_object_bytes(bucket, key, version_id)

        def put_object(self, bucket, key, data, metadata, content_type):
            self.put_body = data

    adapter = NullRestoreAdapter()
    adapter.objects[('b', 'source', 'null')]['etag'] = 'null-etag'
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    result = enhancements.restore_object_version(conn, adapter, scope_id="s1", bucket="b", key="source", version_id="null", target_key="restored")
    assert result["status"] == "succeeded"
    assert adapter.put_body == b"null-version"
    conn.close()


def test_target_checksum_read_has_32mb_budget():
    fake = FakeS3()
    fake.objects[("bucket", "large.webp")] = {
        "Body": b"x" * (32 * 1024 * 1024 + 1),
        "ETag": '"large"',
        "ContentLength": 32 * 1024 * 1024 + 1,
        "Metadata": {"swc-variant-id": "variant"},
    }
    adapter = enhancement_api.S3EnhancementAdapter(fake)

    with pytest.raises(Exception):
        enhancement_api._verify_existing_variant_target(adapter, adapter.head_object("bucket", "large.webp"), "bucket", "large.webp", "variant", "sha")


def test_manifest_entries_must_match_scope_object_and_version(settings):
    setup_variant(settings)
    with db.connect(settings) as conn:
        scope = core.ensure_scope_permission(conn, "s", "object:read")
        with pytest.raises(Exception):
            enhancement_api._validate_manifest_entries(conn, scope, [{"bucket": "other", "key": "in.jpg", "entity_type": "p", "entity_id": "1", "role": "primary"}])
        with pytest.raises(Exception):
            enhancement_api._validate_manifest_entries(conn, scope, [{"object_id": "o", "key": "other.jpg", "entity_type": "p", "entity_id": "1", "role": "primary"}])
        enhancement_api._validate_manifest_entries(conn, scope, [{"object_id": "o", "key": "in.jpg", "entity_type": "p", "entity_id": "1", "role": "primary"}])


def test_upload_scope_binding_is_enforced(settings):
    setup_variant(settings)
    with db.connect(settings) as conn:
        enhancements.initialize(conn)
        scope = core.ensure_scope_permission(conn, "s", "object:write")
        upload = enhancements.start_multipart_upload(conn, enhancement_api.S3EnhancementAdapter(FakeS3()), scope_id="s", bucket="bucket", key="upload.bin")
        conn.commit()
        assert enhancement_api._upload_for_scope(conn, scope, upload["id"])["id"] == upload["id"]
        wrong_scope = dict(scope)
        wrong_scope["id"] = "other"
        with pytest.raises(Exception):
            enhancement_api._upload_for_scope(conn, wrong_scope, upload["id"])


def test_s3_multipart_complete_requires_if_none_match_and_refuses_existing_target():
    fake = FakeS3()
    adapter = enhancement_api.S3EnhancementAdapter(fake)
    adapter.complete_multipart_upload("bucket", "new-multipart", "upload-1", [{"part_number": 1, "etag": '"p1"'}])
    assert fake.multipart_completed
    with pytest.raises(Exception):
        adapter.complete_multipart_upload("bucket", "new-multipart", "upload-2", [{"part_number": 1, "etag": '"p1"'}])


def test_metadata_copy_leaves_original_and_writes_new_target(settings, monkeypatch):
    _variant, _job = setup_variant(settings)
    fake = FakeS3()
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user={"id": "admin"}), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:write" if write else "object:read"))
    monkeypatch.setattr(enhancement_api.core, "require_object", lambda request, scope_id, object_id: core.get_object(db.connect(settings), scope_id, object_id))

    result = enhancement_api.put_metadata(request, "s", "o", {"target_key": "meta-copy.jpg", "metadata": {"edited": "true"}})

    assert result["original_unchanged"] is True
    assert ("bucket", "in.jpg") in fake.objects
    assert fake.objects[("bucket", "meta-copy.jpg")]["Metadata"]["edited"] == "true"


def test_owned_trash_mark_and_restore(settings, monkeypatch):
    variant, _job = setup_variant(settings)
    with db.connect(settings) as conn:
        conn.execute("UPDATE derived_variants SET status='succeeded' WHERE id=?", (variant["id"],))
        conn.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user={"id": "admin"}), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:write" if write else "object:read"))

    marked = enhancement_api.owned_trash_mark(request, "s", variant["id"], {"reason": "test"})
    restored = enhancement_api.owned_trash_restore(request, "s", variant["id"])

    assert marked["physical_delete_enabled"] is False
    assert restored["status"] == "restored"


def test_bucket_config_api_hash_ack_and_rollback_contract(settings, monkeypatch):
    setup_variant(settings)
    fake = FakeS3()
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda conn, settings, scope: enhancement_api.S3EnhancementAdapter(fake))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "manage_bucket" if manage_bucket else ("object:write" if write else "object:read")))

    current = enhancement_api.get_bucket_config(request, "s", "cors")
    repeated = enhancement_api.get_bucket_config(request, "s", "cors")
    assert current["current_hash"] == enhancements.sha256_text(enhancements.canonical_json(current["config"]))
    assert repeated["current_hash"] == current["current_hash"]
    assert "ResponseMetadata" not in current["config"]

    with pytest.raises(Exception):
        enhancement_api.put_bucket_config(request, "s", "cors", {"config": {"CORSRules": [{"AllowedOrigins": ["https://app.test"], "AllowedMethods": ["GET"]}]}, "expected_current_hash": current["current_hash"]})

    saved = enhancement_api.put_bucket_config(
        request,
        "s",
        "cors",
        {"config": {"CORSRules": [{"AllowedOrigins": ["https://app.test"], "AllowedMethods": ["GET"]}]}, "expected_current_hash": current["current_hash"], "exclusive_writer_ack": True},
    )
    assert saved["before_snapshot_id"]
    assert "no remote CAS" in saved["warning"]
    assert "ResponseMetadata" not in saved["config"]

    rollback_hash = enhancements.sha256_text(enhancements.canonical_json(fake.cors))
    rolled = enhancement_api.rollback_bucket_config(
        request,
        "s",
        "cors",
        {"snapshot_id": saved["before_snapshot_id"], "expected_current_hash": rollback_hash, "exclusive_writer_ack": True},
    )
    assert rolled["config"]["CORSRules"][0]["AllowedOrigins"] == ["https://example.test"]
    assert "ResponseMetadata" not in rolled["config"]


def test_bucket_config_adapter_strips_transport_metadata_from_semantic_config():
    class MetadataS3:
        def get_bucket_cors(self, Bucket):
            return {"CORSRules": [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET"]}], "ResponseMetadata": {"RequestId": "a"}}

        def get_bucket_lifecycle_configuration(self, Bucket):
            return {"Rules": [{"ID": "expire", "Status": "Enabled"}], "ResponseMetadata": {"RequestId": "b"}}

        def get_bucket_policy(self, Bucket):
            return {"Policy": json.dumps({"Version": "2012-10-17", "Statement": []}), "ResponseMetadata": {"RequestId": "c"}}

    adapter = enhancement_api.S3EnhancementAdapter(MetadataS3())
    assert adapter.get_bucket_cors("bucket") == {"CORSRules": [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET"]}]}
    assert adapter.get_bucket_lifecycle("bucket") == {"Rules": [{"ID": "expire", "Status": "Enabled"}]}
    assert adapter.get_bucket_policy("bucket") == {"Version": "2012-10-17", "Statement": []}


def test_capacity_trends_prefers_auto_samples_and_marks_manual_unknowns(settings, monkeypatch):
    setup_variant(settings)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:read"))
    with db.connect(settings) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS capacity_samples(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO capacity_samples(id,project_id,result_json,created_at) VALUES(?,?,?,?)",
            (
                "scan1",
                "p",
                json.dumps({"scope_id": "s", "scan_id": "scan1", "current_logical_bytes": 6, "by_format": {"unknown": 6}, "coverage": "complete_enumeration", "version_bytes": None, "physical_disk_bytes": None}),
                jobs.now(),
            ),
        )
        conn.commit()

    manual = enhancement_api.capacity_snapshot(request, "s", {})
    trends = enhancement_api.capacity_trends(request, "s")
    thresholds = enhancement_api.capacity_thresholds(request, "s", {"thresholds": {"version_bytes": 0}})

    assert manual["source"] == "administrator_declared"
    assert manual["source_bytes"] is None
    assert manual["temporary_bytes"] is None
    assert manual["version_bytes"] is None
    assert manual["coverage"] == "partial_index"
    assert trends["items"][0]["source"] == "auto_scan"
    assert trends["items"][0]["version_bytes"] is None
    assert trends["manual_snapshots"][0]["physical_disk_bytes"] is None
    assert thresholds["status"] == "unknown"
    assert thresholds["unknown_fields"] == ["version_bytes"]


def test_capacity_snapshot_marks_legacy_null_output_size_as_unknown(settings, monkeypatch):
    variant, _job = setup_variant(settings)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:read"))
    with db.connect(settings) as conn:
        conn.execute("UPDATE derived_variants SET status='succeeded', output_size=NULL WHERE id=?", (variant["id"],))
        conn.commit()

    manual = enhancement_api.capacity_snapshot(request, "s", {})
    thresholds = enhancement_api.capacity_thresholds(request, "s", {"thresholds": {"derived_bytes": 0}})

    assert manual["derived_bytes"] is None
    assert "derived_bytes" in manual["unmeasured"]
    assert thresholds["status"] == "unknown"
    assert thresholds["unknown_fields"] == ["derived_bytes"]


def test_capacity_trends_orders_manual_snapshots_by_insert_when_observed_at_ties(settings, monkeypatch):
    setup_variant(settings)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), state=SimpleNamespace(user=SimpleNamespace(id="admin")), headers={}, cookies={}, base_url="http://testserver/")
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda request, scope_id, write=False, manage_bucket=False: core.ensure_scope_permission(db.connect(settings), scope_id, "object:read"))
    observed_at = "2026-10-05T00:00:00.000000Z"
    with db.connect(settings) as conn:
        enhancements.initialize(conn)
        conn.execute(
            """
            INSERT INTO capacity_snapshots
              (id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
               unknown_bytes, sample_complete, observed_at, coverage)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            ("older", "s", 1, 0, None, None, None, 0, observed_at, "partial_index"),
        )
        conn.execute(
            """
            INSERT INTO capacity_snapshots
              (id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
               unknown_bytes, sample_complete, observed_at, coverage)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            ("newer", "s", None, 0, None, None, 5, 0, observed_at, "partial_index"),
        )
        conn.commit()

    trends = enhancement_api.capacity_trends(request, "s")

    assert [item["id"] for item in trends["manual_snapshots"][:2]] == ["newer", "older"]
    assert trends["manual_snapshots"][0]["unknown_bytes"] == 5
    assert trends["manual_snapshots"][0]["unknown_fields"] == ["source_bytes", "temporary_bytes", "version_bytes"]


@pytest.mark.parametrize("status", ["active", "initializing", "completing", "completed", "aborted", "failed", "abort_failed", "needs_review"])
def test_multipart_part_only_dispatches_for_active_upload(settings, monkeypatch, status):
    setup_variant(settings)
    with db.connect(settings) as conn:
        scope = core.ensure_scope_permission(conn, "s", "object:write")
        upload = enhancements.start_multipart_upload(
            conn, enhancement_api.S3EnhancementAdapter(FakeS3()),
            scope_id="s", bucket="bucket", key="inactive-upload.bin",
        )
        conn.execute("UPDATE multipart_uploads SET status=? WHERE id=?", (status, upload["id"]))

    class RecordingAdapter:
        def __init__(self):
            self.calls = []

        def upload_part(self, *args):
            self.calls.append(args)
            return {"ETag": '"synthetic-part"'}

    adapter = RecordingAdapter()

    async def stream():
        yield b"synthetic-part-bytes"

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), stream=stream)
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))
    monkeypatch.setattr(enhancement_api, "_require_scope", lambda *args, **kwargs: scope)
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda *args: adapter)

    if status == "active":
        result = asyncio.run(enhancement_api.multipart_part(request, "s", upload["id"], 1))
        assert result == {"ETag": '"synthetic-part"'}
        assert adapter.calls == [("bucket", "inactive-upload.bin", upload["upload_id"], 1, b"synthetic-part-bytes")]
        return

    with pytest.raises(AppError) as exc:
        asyncio.run(enhancement_api.multipart_part(request, "s", upload["id"], 1))

    assert exc.value.status == 409
    assert adapter.calls == []
    with db.connect(settings) as conn:
        assert enhancements.get_multipart_upload(conn, upload["id"])["status"] == status



def test_multipart_part_rechecks_upload_state_after_stream(settings, monkeypatch):
    setup_variant(settings)
    with db.connect(settings) as conn:
        upload = enhancements.start_multipart_upload(conn, enhancement_api.S3EnhancementAdapter(FakeS3()),
                                                     scope_id='s', bucket='bucket', key='part.bin')
    calls = []

    def require_scope(request, scope_id, **kwargs):
        with db.connect(settings) as conn:
            return core.ensure_scope_permission(conn, scope_id, 'object:write')

    async def stream():
        with db.connect(settings) as conn:
            conn.execute("UPDATE multipart_uploads SET status='aborted' WHERE id=?", (upload['id'],))
        yield b'bytes'

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), stream=stream)
    monkeypatch.setattr(enhancement_api, 'mutation', lambda request: SimpleNamespace(id='admin'))
    monkeypatch.setattr(enhancement_api, '_require_scope', require_scope)
    monkeypatch.setattr(enhancement_api, '_adapter_for_scope', lambda *args: calls.append(args))
    with pytest.raises(AppError) as error:
        asyncio.run(enhancement_api.multipart_part(request, 's', upload['id'], 1))
    assert error.value.code == 'UPLOAD_NOT_ACTIVE'
    assert calls == []


@pytest.mark.parametrize('change', ['archive', 'readonly', 'epoch'])
def test_multipart_part_revalidates_authorization_and_upload_after_stream(settings, monkeypatch, change):
    setup_variant(settings)
    with db.connect(settings) as conn:
        upload = enhancements.start_multipart_upload(conn, enhancement_api.S3EnhancementAdapter(FakeS3()),
                                                     scope_id='s', bucket='bucket', key='part.bin')
    calls = []

    def require_scope(request, scope_id, **kwargs):
        with db.connect(settings) as conn:
            return core.ensure_scope_permission(conn, scope_id, 'object:write')

    async def stream():
        with db.connect(settings) as conn:
            if change == 'archive':
                core.archive_scope(conn, 's')
            elif change == 'readonly':
                conn.execute("UPDATE scopes SET writable=0 WHERE id='s'")
            elif change == 'epoch':
                conn.execute("UPDATE scopes SET authz_epoch=authz_epoch+1 WHERE id='s'")
        yield b'bytes'

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), stream=stream)
    monkeypatch.setattr(enhancement_api, 'mutation', lambda request: SimpleNamespace(id='admin'))
    monkeypatch.setattr(enhancement_api, '_require_scope', require_scope)
    monkeypatch.setattr(enhancement_api, '_adapter_for_scope', lambda *args: calls.append(args))
    with pytest.raises(AppError) as error:
        asyncio.run(enhancement_api.multipart_part(request, 's', upload['id'], 1))
    assert error.value.code == 'FORBIDDEN_SCOPE'
    assert calls == []


def test_manifest_connection_identity_must_match_authoritative_scope(settings):
    setup_variant(settings)
    entry = {'connection_id': 'foreign-connection', 'key': 'in.jpg', 'entity_type': 'p', 'entity_id': '1', 'role': 'primary'}
    with db.connect(settings) as conn:
        scope = core.ensure_scope_permission(conn, 's', 'object:read')
        with pytest.raises(AppError) as error:
            enhancement_api._validate_manifest_entries(conn, scope, [entry])
        assert error.value.status == 422
        enhancements.initialize(conn)
        manifest = enhancements.start_reference_manifest(conn, scope_id='s', schema_version=1, coverage={'scope_id': 's', 'bucket': 'bucket', 'prefix': ''}, declared_count=1, waterline='2026-10-10T00:00:00Z')
        with pytest.raises(enhancements.ValidationError):
            enhancements.import_reference_manifest_entries(conn, manifest_id=manifest['id'], entries=[entry])
        assert conn.execute('SELECT count(*) FROM reference_manifest_entries').fetchone()[0] == 0


@pytest.mark.parametrize("prefix", ["allowed/", "literal", "raw//../"])
def test_default_derived_output_stays_within_literal_scope(settings, monkeypatch, prefix):
    variant, _ = setup_variant(settings)
    with db.connect(settings) as conn:
        conn.execute(
            "INSERT INTO scopes(id,project_id,connection_id,display_name,bucket,prefix,writable,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("out", "p", "con", "output", "bucket", prefix, 1, jobs.now(), jobs.now()),
        )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), headers={"Idempotency-Key": "derived-default"})
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))

    def require_scope(request, scope_id, **kwargs):
        with db.connect(settings) as conn:
            return core.ensure_scope_permission(conn, scope_id, "object:read")

    monkeypatch.setattr(enhancement_api, "_require_scope", require_scope)
    body = {"object_id": "o", "preset_id": variant["preset_id"], "output_scope_id": "out"}
    created = enhancement_api.submit_derived_variant(request, "s", body)
    replay = enhancement_api.submit_derived_variant(request, "s", body)
    assert created["variant"]["output_key"].startswith(prefix + "derived/")
    assert replay["job"]["id"] == created["job"]["id"]
    with db.connect(settings) as conn:
        claimed = jobs.claim(conn)
    fake = FakeS3()
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda *args: enhancement_api.S3EnhancementAdapter(fake))
    enhancement_api.execute_variant_job(claimed, settings)
    assert fake.puts == 1
    assert ("bucket", created["variant"]["output_key"]) in fake.objects



def test_derived_worker_rechecks_changed_output_scope_prefix_before_storage(settings, monkeypatch):
    variant, claimed = setup_variant(settings)
    with db.connect(settings) as conn:
        conn.execute("UPDATE scopes SET prefix='in' WHERE id='s'")
    storage_calls = []
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda *args: storage_calls.append(args))
    with pytest.raises(AppError) as error:
        enhancement_api.execute_variant_job(claimed, settings)
    assert error.value.code == "FORBIDDEN_SCOPE"
    assert storage_calls == []
    with db.connect(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_intents WHERE operation_type='derived.put'").fetchone()[0] == 0


def test_metadata_copy_rejects_source_change_after_initial_head(settings, monkeypatch):
    setup_variant(settings)

    class RacingS3(FakeS3):
        source_heads = 0

        def head_object(self, Bucket, Key, **kwargs):
            head = super().head_object(Bucket, Key, **kwargs)
            if Key == "in.jpg":
                self.source_heads += 1
                if self.source_heads == 1:
                    self.objects[(Bucket, Key)].update(
                        Body=b"changed", ETag='"changed"', ContentLength=7,
                        ChecksumSHA256=hashlib.sha256(b"changed").hexdigest(),
                    )
            return head

    fake = RacingS3()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), headers={})
    monkeypatch.setattr(enhancement_api, "mutation", lambda request: SimpleNamespace(id="admin"))

    def require_scope(request, scope_id, **kwargs):
        with db.connect(settings) as conn:
            return core.ensure_scope_permission(conn, scope_id, "object:write")

    def require_object(request, scope_id, object_id):
        with db.connect(settings) as conn:
            return core.get_object(conn, scope_id, object_id)

    monkeypatch.setattr(enhancement_api, "_require_scope", require_scope)
    monkeypatch.setattr(core, "require_object", require_object)
    monkeypatch.setattr(enhancement_api, "_adapter_for_scope", lambda *args: enhancement_api.S3EnhancementAdapter(fake))
    with pytest.raises(AppError) as error:
        enhancement_api.put_metadata(request, "s", "o", {"target_key": "copy.jpg", "metadata": {"label": "test"}})
    assert error.value.status == 409
    assert fake.puts == 0
    assert ("bucket", "copy.jpg") not in fake.objects


def test_metadata_copy_conditional_get_rejects_intervening_source_before_target_put(settings, monkeypatch):
    setup_variant(settings)

    class RacingS3(FakeS3):
        source_gets = []

        def get_object(self, Bucket, Key, **kwargs):
            if Key == 'in.jpg':
                self.source_gets.append(dict(kwargs))
                # A newer revision becomes current after both preliminary heads.
                original = dict(self.objects[(Bucket, Key)])
                self.objects[(Bucket, Key)].update(Body=b'foreign', ETag='"foreign"', ContentLength=7)
                try:
                    return super().get_object(Bucket=Bucket, Key=Key, **kwargs)
                finally:
                    # Restore A to demonstrate that a later HEAD alone cannot guard the GET.
                    self.objects[(Bucket, Key)] = original
            return super().get_object(Bucket=Bucket, Key=Key, **kwargs)

    fake = RacingS3()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    monkeypatch.setattr(enhancement_api, 'mutation', lambda request: SimpleNamespace(id='admin'))

    def require_scope(request, scope_id, **kwargs):
        with db.connect(settings) as conn:
            return core.ensure_scope_permission(conn, scope_id, 'object:write')

    def require_object(request, scope_id, object_id):
        with db.connect(settings) as conn:
            return core.get_object(conn, scope_id, object_id)

    monkeypatch.setattr(enhancement_api, '_require_scope', require_scope)
    monkeypatch.setattr(core, 'require_object', require_object)
    monkeypatch.setattr(enhancement_api, '_adapter_for_scope', lambda *args: enhancement_api.S3EnhancementAdapter(fake))
    with pytest.raises(AppError) as error:
        enhancement_api.put_metadata(request, 's', 'o', {'target_key': 'new.jpg', 'metadata': {}})
    assert error.value.status == 412
    assert fake.source_gets == [{'IfMatch': '"source-etag"'}]
    assert fake.puts == 0 and ('bucket', 'new.jpg') not in fake.objects



@pytest.mark.parametrize('version_id', [None, 'null', 'v123'])
def test_conditional_adapter_read_preserves_exact_version_and_etag(version_id):
    class RecordingS3:
        def get_object(self, **kwargs):
            self.kwargs = kwargs
            return {'Body': Body(b'source')}
    s3 = RecordingS3()
    adapter = enhancement_api.S3EnhancementAdapter(s3)
    assert adapter.get_object_bytes_if_match('bucket', 'key', version_id, '"etag"') == b'source'
    assert s3.kwargs == {'Bucket': 'bucket', 'Key': 'key', 'IfMatch': '"etag"', **({'VersionId': version_id} if version_id is not None else {})}
