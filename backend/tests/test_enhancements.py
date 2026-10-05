import sqlite3
import hashlib

import pytest

from backend.console import enhancements as enh


class FakeAdapter:
    def __init__(self):
        self.objects = {
            ("b", "source", "v1"): {
                "bucket": "b",
                "key": "source",
                "version_id": "v1",
                "size": len(b"source-bytes"),
                "etag": "e1",
                "checksum_sha256": hashlib.sha256(b"source-bytes").hexdigest(),
                "body": b"source-bytes",
                "content_type": "application/octet-stream",
                "metadata": {},
            }
        }
        self.current = {("b", "source"): "v1"}
        self.multipart = {}
        self.deleted = []
        self.config = {"cors": {"AllowedOrigins": ["https://example.test"]}}
        self.tags = {}

    def head_object(self, bucket, key, version_id=None):
        if version_id is None:
            version_id = self.current[(bucket, key)]
        return dict(self.objects[(bucket, key, version_id)])

    def copy_object(self, source_bucket, source_key, target_bucket, target_key, source_version_id=None, metadata=None):
        source = self.head_object(source_bucket, source_key, source_version_id)
        version_id = "copy-v1"
        copied = dict(source)
        copied.update({"bucket": target_bucket, "key": target_key, "version_id": version_id, "etag": "copy-etag"})
        self.objects[(target_bucket, target_key, version_id)] = copied
        self.current[(target_bucket, target_key)] = version_id

    def delete_object(self, bucket, key, version_id=None):
        self.deleted.append((bucket, key, version_id))

    def create_multipart_upload(self, bucket, key, metadata):
        upload_id = f"upload-{len(self.multipart) + 1}"
        self.multipart[upload_id] = {"bucket": bucket, "key": key, "metadata": metadata}
        return upload_id

    def complete_multipart_upload(self, bucket, key, upload_id, parts):
        assert upload_id in self.multipart
        version_id = "multipart-v1"
        self.objects[(bucket, key, version_id)] = {
            "bucket": bucket,
            "key": key,
            "version_id": version_id,
            "size": sum(int(part["size"]) for part in parts),
            "etag": "multipart-etag",
            "checksum_sha256": "multipart-sha",
        }
        self.current[(bucket, key)] = version_id

    def abort_multipart_upload(self, bucket, key, upload_id):
        self.multipart.pop(upload_id, None)

    def list_object_versions(self, bucket, key):
        return [dict(value) for (b, k, _), value in self.objects.items() if b == bucket and k == key]

    def get_object_tags(self, bucket, key, version_id=None):
        return dict(self.tags.get((bucket, key, version_id), {}))

    def put_object_tags(self, bucket, key, tags, version_id=None):
        self.tags[(bucket, key, version_id)] = dict(tags)

    def get_bucket_cors(self, bucket):
        return dict(self.config["cors"])

    def put_bucket_cors(self, bucket, config):
        self.config["cors"] = dict(config)

    def get_object_bytes(self, bucket, key, version_id=None):
        if version_id is None:
            version_id = self.current[(bucket, key)]
        return self.objects[(bucket, key, version_id)]["body"]

    def put_object(self, bucket, key, data, metadata, content_type):
        if (bucket, key) in self.current:
            raise enh.ConflictError("target exists")
        version_id = "put-v1"
        self.objects[(bucket, key, version_id)] = {
            "bucket": bucket,
            "key": key,
            "version_id": version_id,
            "size": len(data),
            "etag": "put-etag",
            "checksum_sha256": enh.hashlib.sha256(data).hexdigest(),
            "content_type": content_type,
            "metadata": dict(metadata),
            "body": data,
        }
        self.current[(bucket, key)] = version_id


class FakeImaging:
    PIPELINE_VERSION = "pillow-v1"

    @staticmethod
    def decode(data, params):
        return {"width": params["width"], "height": params["height"]}, b"derived-bytes"


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    enh.initialize(db)
    return db


def preset_params(**overrides):
    params = {
        "mode": "fit",
        "width": 640,
        "height": 480,
        "quality": 82,
        "format": "webp",
        "alpha_policy": "preserve",
        "orientation": "auto",
        "color_policy": "srgb",
        "metadata_policy": "strip",
    }
    params.update(overrides)
    return params


def test_preset_versions_are_immutable_and_idempotent(conn):
    first = enh.create_preset(conn, project_id="p1", name="gallery", params=preset_params())
    same = enh.create_preset(conn, project_id="p1", name="gallery", params=preset_params())
    changed = enh.create_preset(conn, project_id="p1", name="gallery", params=preset_params(quality=90))

    assert same["id"] == first["id"]
    assert first["version"] == 1
    assert changed["version"] == 2
    assert first["immutable"] is True


def test_invalid_preset_rejects_unsafe_parameters(conn):
    with pytest.raises(enh.ValidationError):
        enh.create_preset(conn, project_id="p1", name="bad", params=preset_params(width=10000))
    with pytest.raises(enh.ValidationError):
        enh.create_preset(conn, project_id="p1", name="bad", params=preset_params(format="svg"))
    with pytest.raises(enh.ValidationError):
        enh.create_preset(conn, project_id="p1", name="bad", params=preset_params(mode="focal"))


def test_derived_variant_intent_is_deterministic_and_readback_can_succeed(conn):
    adapter = FakeAdapter()
    preset = enh.create_preset(conn, project_id="p1", name="thumb", params=preset_params())
    first = enh.create_derived_variant_intent(
        conn,
        project_id="p1",
        scope_id="s1",
        source_object_id="obj1",
        source_revision="rev1",
        preset_id=preset["id"],
        output_scope_id="derived-scope",
        output_bucket="b",
    )
    second = enh.create_derived_variant_intent(
        conn,
        project_id="p1",
        scope_id="s1",
        source_object_id="obj1",
        source_revision="rev1",
        preset_id=preset["id"],
        output_scope_id="derived-scope",
        output_bucket="b",
    )
    adapter.copy_object("b", "source", "b", first["output_key"], "v1")
    done = enh.record_derived_write_result(conn, adapter, variant_id=first["id"], expected_checksum_sha256=adapter.objects[("b", "source", "v1")]["checksum_sha256"])

    assert first["id"] == second["id"]
    assert done["status"] == "succeeded"
    assert done["manifest"]["readback"]["checksum_sha256"] == adapter.objects[("b", "source", "v1")]["checksum_sha256"]


def test_multipart_complete_persists_intent_before_remote_complete(conn):
    adapter = FakeAdapter()
    upload = enh.start_multipart_upload(conn, adapter, scope_id="s1", bucket="b", key="new", metadata={"x": "1"})
    completed = enh.complete_multipart_upload(conn, adapter, upload_row_id=upload["id"], parts=[{"part_number": 1, "size": 4}])

    assert completed["status"] == "completed"
    intent_count = conn.execute("SELECT COUNT(*) FROM operation_intents WHERE operation_type='multipart.complete'").fetchone()[0]
    version_count = conn.execute("SELECT COUNT(*) FROM object_version_observations WHERE key='new'").fetchone()[0]
    assert intent_count == 1
    assert version_count == 1


def test_multipart_reserves_active_target_until_complete_or_abort(conn):
    adapter = FakeAdapter()
    upload = enh.start_multipart_upload(conn, adapter, scope_id="s1", bucket="b", key="reserved")
    with pytest.raises(enh.ConflictError):
        enh.start_multipart_upload(conn, adapter, scope_id="s1", bucket="b", key="reserved")
    enh.abort_multipart_upload(conn, adapter, upload_row_id=upload["id"])
    second = enh.start_multipart_upload(conn, adapter, scope_id="s1", bucket="b", key="reserved")
    assert second["status"] == "active"


def test_multipart_abort_records_failure_without_claiming_success(conn):
    adapter = FakeAdapter()
    upload = enh.start_multipart_upload(conn, adapter, scope_id="s1", bucket="b", key="new")
    aborted = enh.abort_multipart_upload(conn, adapter, upload_row_id=upload["id"])

    assert aborted["status"] == "aborted"
    assert upload["upload_id"] not in adapter.multipart


def test_copy_verifies_result_and_move_refuses_without_guard(conn):
    adapter = FakeAdapter()
    copied = enh.copy_object_safely(
        conn,
        adapter,
        scope_id="s1",
        source_bucket="b",
        source_key="source",
        source_version_id="v1",
        target_bucket="b",
        target_key="copy",
    )
    assert copied["status"] == "succeeded"

    with pytest.raises(enh.ProtectedError):
        enh.move_object_protected(
            conn,
            adapter,
            scope_id="s1",
            source_bucket="b",
            source_key="source",
            source_version_id="v1",
            target_bucket="b",
            target_key="moved",
        )
    assert adapter.deleted == []


def test_copy_refuses_selected_revision_drift_before_remote_write(conn):
    adapter = FakeAdapter()
    stale_revision = enh.make_observed_revision("b", "source", {"etag": "old", "size": len(b"source-bytes")}, "v1-stale")

    with pytest.raises(enh.ConflictError, match="selected index revision"):
        enh.copy_object_safely(
            conn,
            adapter,
            scope_id="s1",
            source_bucket="b",
            source_key="source",
            source_version_id="v1",
            expected_source_revision=stale_revision,
            target_bucket="b",
            target_key="copy",
        )

    assert ("b", "copy") not in adapter.current


def test_copy_refuses_concurrent_target_without_overwrite(conn):
    adapter = FakeAdapter()
    adapter.current[("b", "copy")] = "existing"
    adapter.objects[("b", "copy", "existing")] = {"bucket": "b", "key": "copy", "version_id": "existing", "size": 1, "checksum_sha256": "other"}
    with pytest.raises(enh.ConflictError):
        enh.copy_object_safely(
            conn,
            adapter,
            scope_id="s1",
            source_bucket="b",
            source_key="source",
            source_version_id="v1",
            target_bucket="b",
            target_key="copy",
        )
    assert adapter.objects[("b", "copy", "existing")]["checksum_sha256"] == "other"


def test_copy_rejects_corrupt_same_size_target_readback(conn):
    class CorruptingAdapter(FakeAdapter):
        def put_object(self, bucket, key, data, metadata, content_type):
            super().put_object(bucket, key, b"X" + data[1:], metadata, content_type)

    adapter = CorruptingAdapter()
    with pytest.raises(enh.ConflictError, match="checksum"):
        enh.copy_object_safely(
            conn,
            adapter,
            scope_id="s1",
            source_bucket="b",
            source_key="source",
            source_version_id="v1",
            target_bucket="b",
            target_key="copy",
        )


def test_restore_version_writes_new_target_only(conn):
    adapter = FakeAdapter()
    result = enh.restore_object_version(conn, adapter, scope_id="s1", bucket="b", key="source", version_id="v1", target_key="restored")
    assert result["status"] == "succeeded"
    with pytest.raises(enh.ConflictError):
        enh.restore_object_version(conn, adapter, scope_id="s1", bucket="b", key="source", version_id="v1", target_key="restored")


def test_tags_roundtrip_and_retention_unsupported(conn):
    adapter = FakeAdapter()
    result = enh.set_object_tags(conn, adapter, scope_id="s1", bucket="b", key="source", tags={"kind": "source"}, version_id="v1")
    assert result["tags"] == {"kind": "source"}

    with pytest.raises(enh.CapabilityUnsupported):
        enh.get_retention(conn, adapter, bucket="b", key="source", version_id="v1")


def test_bucket_config_requires_ack_hash_and_records_non_cas_warning(conn):
    adapter = FakeAdapter()
    current_hash = enh.sha256_text(enh.canonical_json(adapter.config["cors"]))
    with pytest.raises(enh.PermissionDenied):
        enh.update_bucket_config(conn, adapter, scope_id="s1", bucket="b", kind="cors", new_config={}, admin_manage_bucket=False)
    with pytest.raises(enh.ValidationError):
        enh.update_bucket_config(conn, adapter, scope_id="s1", bucket="b", kind="cors", new_config={}, admin_manage_bucket=True)

    result = enh.update_bucket_config(
        conn,
        adapter,
        scope_id="s1",
        bucket="b",
        kind="cors",
        new_config={"AllowedOrigins": ["https://app.test"]},
        expected_current_hash=current_hash,
        exclusive_writer_ack=True,
        admin_manage_bucket=True,
    )
    assert result["status"] == "succeeded_with_warning"
    assert "no remote CAS" in result["warning"]
    assert result["before_snapshot_id"]


def test_bucket_config_rejects_stale_hash_unknown_field_and_readback_diff(conn):
    adapter = FakeAdapter()
    with pytest.raises(enh.ConflictError):
        enh.update_bucket_config(
            conn,
            adapter,
            scope_id="s1",
            bucket="b",
            kind="cors",
            new_config={"AllowedOrigins": ["https://app.test"]},
            expected_current_hash="stale",
            exclusive_writer_ack=True,
            admin_manage_bucket=True,
        )
    current_hash = enh.sha256_text(enh.canonical_json(adapter.config["cors"]))
    with pytest.raises(enh.ValidationError, match="unsupported bucket config field"):
        enh.update_bucket_config(
            conn,
            adapter,
            scope_id="s1",
            bucket="b",
            kind="cors",
            new_config={"Unknown": True},
            expected_current_hash=current_hash,
            exclusive_writer_ack=True,
            admin_manage_bucket=True,
        )
    with pytest.raises(enh.CapabilityUnsupported):
        enh.update_bucket_config(
            conn,
            adapter,
            scope_id="s1",
            bucket="b",
            kind="lifecycle",
            new_config={"Rules": [{"Status": "Enabled", "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]}]},
            expected_current_hash=enh.sha256_text(enh.canonical_json({})),
            exclusive_writer_ack=True,
            admin_manage_bucket=True,
        )

    class DiffAdapter(FakeAdapter):
        def put_bucket_cors(self, bucket, config):
            self.config["cors"] = {"AllowedOrigins": ["https://different.test"]}

    diff = DiffAdapter()
    result = enh.update_bucket_config(
        conn,
        diff,
        scope_id="s1",
        bucket="b",
        kind="cors",
        new_config={"AllowedOrigins": ["https://app.test"]},
        expected_current_hash=enh.sha256_text(enh.canonical_json(diff.config["cors"])),
        exclusive_writer_ack=True,
        admin_manage_bucket=True,
    )
    assert result["status"] == "needs_review"
    assert "readback differs" in result["warning"]


def test_bucket_config_rollback_refuses_newer_save(conn):
    adapter = FakeAdapter()
    first_hash = enh.sha256_text(enh.canonical_json(adapter.config["cors"]))
    first = enh.update_bucket_config(
        conn,
        adapter,
        scope_id="s1",
        bucket="b",
        kind="cors",
        new_config={"AllowedOrigins": ["https://app.test"]},
        expected_current_hash=first_hash,
        exclusive_writer_ack=True,
        admin_manage_bucket=True,
    )
    second_hash = enh.sha256_text(enh.canonical_json(adapter.config["cors"]))
    enh.update_bucket_config(
        conn,
        adapter,
        scope_id="s1",
        bucket="b",
        kind="cors",
        new_config={"AllowedOrigins": ["https://newer.test"]},
        expected_current_hash=second_hash,
        exclusive_writer_ack=True,
        admin_manage_bucket=True,
    )
    with pytest.raises(enh.ConflictError):
        enh.rollback_bucket_config(
            conn,
            adapter,
            scope_id="s1",
            bucket="b",
            kind="cors",
            snapshot_id=first["before_snapshot_id"],
            expected_current_hash=enh.sha256_text(enh.canonical_json(adapter.config["cors"])),
            exclusive_writer_ack=True,
            admin_manage_bucket=True,
        )


def test_manifest_finalize_complete_partial_invalid_unknown_protection(conn):
    entry = {"object_id": "obj1", "bucket": "b", "key": "source", "entity_type": "product", "entity_id": "sku1", "role": "primary"}
    entry_hash = enh.sha256_text(enh.canonical_json(entry))
    declared_hash = enh.sha256_text(enh.canonical_json([entry_hash]))
    manifest = enh.start_reference_manifest(
        conn,
        scope_id="s1",
        schema_version=1,
        coverage={"scope_id": "s1", "prefix": ""},
        declared_count=1,
        declared_hash=declared_hash,
    )
    enh.import_reference_manifest_entries(conn, manifest_id=manifest["id"], entries=[entry])
    finalized = enh.finalize_reference_manifest(conn, manifest_id=manifest["id"])
    assert finalized["status"] == "complete"

    partial = enh.start_reference_manifest(conn, scope_id="s1", schema_version=1, coverage={"scope_id": "s1"}, declared_count=2)
    enh.import_reference_manifest_entries(conn, manifest_id=partial["id"], entries=[entry])
    assert enh.finalize_reference_manifest(conn, manifest_id=partial["id"])["status"] == "partial"

    invalid = enh.start_reference_manifest(conn, scope_id="s1", schema_version=99, coverage={"scope_id": "s1"}, declared_count=0)
    assert invalid["status"] == "invalid"


def test_reference_manifest_rejects_key_entry_without_bucket_identity(conn):
    manifest = enh.start_reference_manifest(conn, scope_id="s1", schema_version=1, coverage={"scope_id": "s1"}, declared_count=1)

    with pytest.raises(enh.ValidationError, match="bucket"):
        enh.import_reference_manifest_entries(
            conn,
            manifest_id=manifest["id"],
            entries=[{"key": "source", "entity_type": "product", "entity_id": "sku1", "role": "primary"}],
        )

    assert conn.execute("SELECT COUNT(*) FROM reference_manifest_entries WHERE manifest_id=?", (manifest["id"],)).fetchone()[0] == 0


def test_reference_manifest_normalizes_scope_bucket_before_hash(conn):
    conn.execute("CREATE TABLE scopes(id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, bucket TEXT NOT NULL)")
    conn.execute("INSERT INTO scopes(id, connection_id, bucket) VALUES(?,?,?)", ("s1", "conn1", "b"))
    entry = {"key": "source", "entity_type": "product", "entity_id": "sku1", "role": "primary"}
    normalized = dict(entry, bucket="b", connection_id="conn1")
    entry_hash = enh.sha256_text(enh.canonical_json(normalized))
    manifest = enh.start_reference_manifest(
        conn,
        scope_id="s1",
        schema_version=1,
        coverage={"scope_id": "s1"},
        declared_count=1,
        declared_hash=enh.sha256_text(enh.canonical_json([entry_hash])),
    )

    enh.import_reference_manifest_entries(conn, manifest_id=manifest["id"], entries=[entry])
    stored = conn.execute("SELECT entry_hash,bucket,relation_json FROM reference_manifest_entries WHERE manifest_id=?", (manifest["id"],)).fetchone()

    assert stored[0] == entry_hash
    assert stored[1] == "b"
    assert enh.finalize_reference_manifest(conn, manifest_id=manifest["id"])["status"] == "complete"
    assert enh.json.loads(stored[2])["connection_id"] == "conn1"


def test_health_and_capacity_keep_unknown_separate_from_zero(conn):
    preset = enh.create_preset(conn, project_id="p1", name="thumb", params=preset_params())
    enh.create_derived_variant_intent(
        conn,
        project_id="p1",
        scope_id="s1",
        source_object_id="obj1",
        source_revision="rev1",
        preset_id=preset["id"],
        output_scope_id="derived-scope",
        output_bucket="b",
    )
    health = enh.derived_health(conn, scope_id="s1")
    assert health["planned"] == 1
    assert health["unknown_external_relations"] is True

    enh.add_capacity_snapshot(conn, scope_id="s1", source_bytes=10, unknown_bytes=5, sample_complete=False)
    thresholds = enh.capacity_thresholds(conn, scope_id="s1", thresholds={"unknown_bytes": 1})
    assert thresholds["status"] == "alerting"
    assert thresholds["sample_complete"] is False

    unknown = enh.add_capacity_snapshot(conn, scope_id="s1", source_bytes=10, version_bytes=None, sample_complete=False)
    thresholds = enh.capacity_thresholds(conn, scope_id="s1", thresholds={"version_bytes": 0})
    assert unknown["version_bytes"] is None
    assert thresholds["status"] == "unknown"
    assert thresholds["unknown_fields"] == ["version_bytes"]

    mixed = enh.add_capacity_snapshot(conn, scope_id="s1", source_bytes=10, unknown_bytes=5, version_bytes=None, sample_complete=False)
    thresholds = enh.capacity_thresholds(conn, scope_id="s1", thresholds={"unknown_bytes": 1, "version_bytes": 0})
    assert mixed["source_bytes"] is None
    assert mixed["unknown_bytes"] == 5
    assert thresholds["status"] == "alerting"
    assert thresholds["alerts"] == [{"field": "unknown_bytes", "value": 5, "threshold": 1}]
    assert thresholds["unknown_fields"] == ["version_bytes"]


def test_capacity_snapshot_old_schema_migrates_nullable_unmeasured_fields():
    old = sqlite3.connect(":memory:")
    old.execute(
        """
        CREATE TABLE capacity_snapshots (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          source_bytes INTEGER NOT NULL DEFAULT 0,
          derived_bytes INTEGER NOT NULL DEFAULT 0,
          temporary_bytes INTEGER NOT NULL DEFAULT 0,
          version_bytes INTEGER NOT NULL DEFAULT 0,
          unknown_bytes INTEGER NOT NULL DEFAULT 0,
          sample_complete INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL
        )
        """
    )
    old.execute(
        "INSERT INTO capacity_snapshots(id,scope_id,source_bytes,derived_bytes,temporary_bytes,version_bytes,unknown_bytes,sample_complete,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("legacy", "s1", 0, 0, 0, 0, 0, 1, "2026-10-05T00:00:00.000000Z"),
    )

    enh.initialize(old)
    enh.initialize(old)
    migrated = enh.get_capacity_snapshot(old, "legacy")
    threshold = enh.capacity_thresholds(old, scope_id="s1", thresholds={"version_bytes": 0})
    snapshot = enh.add_capacity_snapshot(old, scope_id="s1", source_bytes=1, version_bytes=None, sample_complete=False)
    columns = {row[1]: row for row in old.execute("PRAGMA table_info(capacity_snapshots)").fetchall()}

    assert old.execute("SELECT COUNT(*) FROM capacity_snapshots WHERE id='legacy'").fetchone()[0] == 1
    assert old.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='capacity_snapshots_new'").fetchone()[0] == 0
    assert columns["version_bytes"][3] == 0
    assert migrated["coverage"] == "legacy_unknown"
    assert migrated["legacy_unknown"] is True
    assert migrated["legacy_values"]["version_bytes"] == 0
    assert migrated["version_bytes"] is None
    assert threshold["status"] == "unknown"
    assert threshold["unknown_fields"] == ["version_bytes"]
    assert snapshot["version_bytes"] is None
    assert snapshot["coverage"] == "partial_index"


def test_capacity_snapshot_migration_rolls_back_on_failure():
    old = sqlite3.connect(":memory:")
    old.isolation_level = None
    old.execute(
        """
        CREATE TABLE capacity_snapshots (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          source_bytes INTEGER NOT NULL DEFAULT 0,
          derived_bytes INTEGER NOT NULL DEFAULT 0,
          temporary_bytes INTEGER NOT NULL DEFAULT 0,
          version_bytes INTEGER NOT NULL DEFAULT 0,
          unknown_bytes INTEGER NOT NULL DEFAULT 0,
          sample_complete INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL
        )
        """
    )
    old.execute("BEGIN")
    old.execute(
        "INSERT INTO capacity_snapshots(id,scope_id,source_bytes,derived_bytes,temporary_bytes,version_bytes,unknown_bytes,sample_complete,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("legacy", "s1", 0, 0, 0, 0, 0, 1, "2026-10-05T00:00:00.000000Z"),
    )

    class FailingMigration:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            if str(sql).lstrip().upper().startswith("INSERT OR IGNORE INTO CAPACITY_SNAPSHOTS_NEW"):
                raise sqlite3.OperationalError("synthetic migration failure")
            return self.inner.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError):
        enh._migrate_capacity_snapshots_nullable(FailingMigration(old))
    old.execute("ROLLBACK")
    rows = old.execute("PRAGMA table_info(capacity_snapshots)").fetchall()

    assert {row[1]: row for row in rows}["version_bytes"][3] == 1
    assert old.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='capacity_snapshots_new'").fetchone()[0] == 0


def test_capacity_thresholds_uses_latest_insert_when_observed_at_ties(conn):
    observed_at = "2026-10-05T00:00:00.000000Z"
    conn.execute(
        """
        INSERT INTO capacity_snapshots
          (id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
           unknown_bytes, sample_complete, observed_at, coverage)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        ("older", "s1", 1, 0, None, None, None, 0, observed_at, "partial_index"),
    )
    conn.execute(
        """
        INSERT INTO capacity_snapshots
          (id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
           unknown_bytes, sample_complete, observed_at, coverage)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        ("newer", "s1", None, 0, None, None, 5, 0, observed_at, "partial_index"),
    )

    thresholds = enh.capacity_thresholds(conn, scope_id="s1", thresholds={"unknown_bytes": 1, "version_bytes": 0})

    assert thresholds["snapshot"]["id"] == "newer"
    assert thresholds["status"] == "alerting"
    assert thresholds["alerts"] == [{"field": "unknown_bytes", "value": 5, "threshold": 1}]
    assert thresholds["unknown_fields"] == ["version_bytes"]


def test_run_job_processes_one_immutable_variant_with_checkpoint(conn):
    adapter = FakeAdapter()
    preset = enh.create_preset(conn, project_id="p1", name="thumb", params=preset_params())
    checkpoints = []
    result = enh.run_job(
        {
            "kind": "derived_variant",
            "actor": "admin",
            "params": {
                "project_id": "p1",
                "scope_id": "s1",
                "source_object_id": "obj1",
                "source_revision": "rev1",
                "preset_id": preset["id"],
                "source_bucket": "b",
                "source_key": "source",
                "source_version_id": "v1",
                "output_scope_id": "derived-scope",
                "output_bucket": "b",
            },
        },
        {"conn": conn, "adapter": adapter, "imaging": FakeImaging},
        checkpoints.append,
    )

    assert result["processed"] == 1
    assert result["state"] == "succeeded"
    assert [checkpoint["phase"] for checkpoint in checkpoints] == ["intent_persisted", "completed"]
    variant = enh.get_derived_variant(conn, result["checkpoint"]["variant_id"])
    assert adapter.objects[("b", variant["output_key"], "put-v1")]["metadata"]["swc-variant-id"] == variant["id"]
