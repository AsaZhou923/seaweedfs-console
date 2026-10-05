"""P1/P2 enhancement services for the SeaweedFS Console backend.

This module is intentionally stdlib-first.  The parent application can mount the
optional FastAPI router, but the core contracts are plain functions over a
SQLite connection and a storage adapter protocol.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


ISO_UTC = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_IMAGE_DIMENSION = 8192
MAX_WATERMARK_TEXT = 256
SUPPORTED_PRESET_FORMATS = {"jpeg", "png", "webp"}
SUPPORTED_FIT_MODES = {"fit", "fill", "focal"}
SUPPORTED_ALPHA_POLICIES = {"preserve", "flatten", "reject"}
SUPPORTED_ORIENTATION_POLICIES = {"auto", "keep", "strip"}
SUPPORTED_COLOR_POLICIES = {"preserve", "srgb"}
SUPPORTED_METADATA_POLICIES = {"strip", "keep_safe"}
CONFIG_KINDS = {"cors", "lifecycle", "policy"}
CAPACITY_BYTE_FIELDS = ("source_bytes", "derived_bytes", "temporary_bytes", "version_bytes", "unknown_bytes")


@contextmanager
def _transaction(conn: sqlite3.Connection):
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


class EnhancementError(ValueError):
    """Base class for user-visible enhancement contract failures."""

    code = "ENHANCEMENT_ERROR"

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self)}


class ValidationError(EnhancementError):
    code = "VALIDATION_ERROR"


class ConflictError(EnhancementError):
    code = "CONFLICT"


class ProtectedError(EnhancementError):
    code = "PROTECTED_BY_REFERENCE"


class CapabilityUnsupported(EnhancementError):
    code = "UNSUPPORTED_CAPABILITY"


class PermissionDenied(EnhancementError):
    code = "PERMISSION_DENIED"


class NotFound(EnhancementError):
    code = "NOT_FOUND"


@dataclass(frozen=True)
class ObjectIdentity:
    bucket: str
    key: str
    version_id: str | None
    etag: str | None
    size: int | None
    checksum_sha256: str | None = None
    last_modified: str | None = None
    retention_until: str | None = None
    legal_hold: bool | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def initialize(conn: sqlite3.Connection) -> None:
    """Create enhancement-owned tables.

    Core tables such as users, scopes, objects and jobs may be owned elsewhere.
    These tables therefore store stable string identifiers and enforce local
    invariants without requiring cross-module foreign keys.
    """

    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS enhancement_presets (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          name TEXT NOT NULL,
          version INTEGER NOT NULL,
          params_json TEXT NOT NULL,
          params_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          created_by TEXT,
          UNIQUE(project_id, name, version),
          UNIQUE(project_id, name, params_hash)
        );

        CREATE TABLE IF NOT EXISTS derived_variants (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          source_object_id TEXT NOT NULL,
          source_revision TEXT NOT NULL,
          preset_id TEXT NOT NULL,
          output_scope_id TEXT NOT NULL,
          output_bucket TEXT NOT NULL,
          output_key TEXT NOT NULL,
          input_binding_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          manifest_json TEXT NOT NULL DEFAULT '{}',
          source_sha256 TEXT,
          output_sha256 TEXT,
          output_size INTEGER,
          output_mime TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(input_binding_hash)
        );

        CREATE TABLE IF NOT EXISTS derived_variant_batches (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          project_id TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          idempotency_key TEXT,
          status TEXT NOT NULL,
          planned_count INTEGER NOT NULL,
          items_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(scope_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS image_groups (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          name TEXT NOT NULL,
          source TEXT NOT NULL,
          source_ref TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS image_group_members (
          group_id TEXT NOT NULL,
          object_id TEXT NOT NULL,
          relation_source TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(group_id, object_id),
          FOREIGN KEY(group_id) REFERENCES image_groups(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS object_relationships (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          source_object_id TEXT NOT NULL,
          target_object_id TEXT NOT NULL,
          relation_type TEXT NOT NULL,
          relation_source TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(scope_id, source_object_id, target_object_id, relation_type, relation_source)
        );

        CREATE TABLE IF NOT EXISTS multipart_uploads (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          key TEXT NOT NULL,
          upload_id TEXT,
          metadata_json TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          aborted_at TEXT,
          error TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS active_multipart_target
        ON multipart_uploads(scope_id, bucket, key)
        WHERE status IN ('initializing','active','completing');

        CREATE TABLE IF NOT EXISTS operation_intents (
          id TEXT PRIMARY KEY,
          operation_type TEXT NOT NULL,
          scope_id TEXT,
          target_json TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          result_json TEXT NOT NULL DEFAULT '{}',
          error TEXT
        );

        CREATE TABLE IF NOT EXISTS object_version_observations (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          object_id TEXT,
          bucket TEXT NOT NULL,
          key TEXT NOT NULL,
          version_id TEXT,
          is_delete_marker INTEGER NOT NULL DEFAULT 0,
          size INTEGER,
          etag TEXT,
          checksum_sha256 TEXT,
          observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS object_tag_observations (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          key TEXT NOT NULL,
          version_id TEXT,
          tags_json TEXT NOT NULL,
          observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bucket_config_snapshots (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          kind TEXT NOT NULL,
          config_json TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          warning TEXT,
          observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reference_manifests (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          coverage_json TEXT NOT NULL,
          declared_count INTEGER NOT NULL,
          declared_hash TEXT,
          status TEXT NOT NULL,
          waterline TEXT,
          created_at TEXT NOT NULL,
          finalized_at TEXT,
          reason TEXT
        );

        CREATE TABLE IF NOT EXISTS reference_manifest_entries (
          manifest_id TEXT NOT NULL,
          entry_hash TEXT NOT NULL,
          object_id TEXT,
          bucket TEXT,
          key TEXT,
          version_id TEXT,
          entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          role TEXT NOT NULL,
          relation_json TEXT NOT NULL,
          PRIMARY KEY(manifest_id, entry_hash),
          FOREIGN KEY(manifest_id) REFERENCES reference_manifests(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS capacity_snapshots (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          source_bytes INTEGER NOT NULL DEFAULT 0,
          derived_bytes INTEGER NOT NULL DEFAULT 0,
          temporary_bytes INTEGER NOT NULL DEFAULT 0,
          version_bytes INTEGER NOT NULL DEFAULT 0,
          unknown_bytes INTEGER NOT NULL DEFAULT 0,
          sample_complete INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS owned_object_trash (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          variant_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          key TEXT NOT NULL,
          status TEXT NOT NULL,
          reason TEXT NOT NULL,
          actor_id TEXT,
          marked_at TEXT NOT NULL,
          restored_at TEXT,
          UNIQUE(scope_id, variant_id)
        );

        CREATE TABLE IF NOT EXISTS enhancement_audit_events (
          id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          actor_id TEXT,
          scope_id TEXT,
          target_json TEXT NOT NULL,
          result TEXT NOT NULL,
          detail_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    _migrate_capacity_snapshots_nullable(conn)
    conn.commit()


def _migrate_capacity_snapshots_nullable(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(capacity_snapshots)").fetchall()
    if not rows:
        return
    columns = {row[1]: row for row in rows}
    if "coverage" not in columns:
        conn.execute("ALTER TABLE capacity_snapshots ADD COLUMN coverage TEXT")
    if not any(columns[name][3] for name in CAPACITY_BYTE_FIELDS if name in columns):
        return
    conn.execute("DROP TABLE IF EXISTS capacity_snapshots_new")
    conn.execute(
        """
        CREATE TABLE capacity_snapshots_new (
          id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          source_bytes INTEGER,
          derived_bytes INTEGER,
          temporary_bytes INTEGER,
          version_bytes INTEGER,
          unknown_bytes INTEGER,
          sample_complete INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL,
          coverage TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO capacity_snapshots_new
          (id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
           unknown_bytes, sample_complete, observed_at, coverage)
        SELECT id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
               unknown_bytes, sample_complete, observed_at,
               COALESCE(coverage, 'legacy_unknown')
        FROM capacity_snapshots
        """
    )
    conn.execute("DROP TABLE capacity_snapshots")
    conn.execute("ALTER TABLE capacity_snapshots_new RENAME TO capacity_snapshots")


def create_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    actor_id: str | None,
    scope_id: str | None,
    target: Mapping[str, Any],
    result: str,
    detail: Mapping[str, Any] | None = None,
) -> str:
    event_id = new_id("audit")
    conn.execute(
        """
        INSERT INTO enhancement_audit_events
          (id, event_type, actor_id, scope_id, target_json, result, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            actor_id,
            scope_id,
            canonical_json(dict(target)),
            result,
            canonical_json(dict(detail or {})),
            utc_now(),
        ),
    )
    return event_id


def _require_nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    return value.strip()


def validate_preset_params(params: Mapping[str, Any]) -> dict[str, Any]:
    mode = params.get("mode")
    if mode not in SUPPORTED_FIT_MODES:
        raise ValidationError(f"mode must be one of {sorted(SUPPORTED_FIT_MODES)}")

    width = int(params.get("width", 0))
    height = int(params.get("height", 0))
    if width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValidationError(f"width and height must be between 1 and {MAX_IMAGE_DIMENSION}")

    quality = int(params.get("quality", 85))
    if quality < 1 or quality > 100:
        raise ValidationError("quality must be between 1 and 100")

    output_format = str(params.get("format", "")).lower()
    if output_format not in SUPPORTED_PRESET_FORMATS:
        raise ValidationError(f"format must be one of {sorted(SUPPORTED_PRESET_FORMATS)}")

    alpha_policy = params.get("alpha_policy", "preserve")
    if alpha_policy not in SUPPORTED_ALPHA_POLICIES:
        raise ValidationError("alpha_policy is invalid")

    orientation = params.get("orientation", "auto")
    if orientation not in SUPPORTED_ORIENTATION_POLICIES:
        raise ValidationError("orientation is invalid")

    color_policy = params.get("color_policy", "srgb")
    if color_policy not in SUPPORTED_COLOR_POLICIES:
        raise ValidationError("color_policy is invalid")

    metadata_policy = params.get("metadata_policy", "strip")
    if metadata_policy not in SUPPORTED_METADATA_POLICIES:
        raise ValidationError("metadata_policy is invalid")

    focal_point = params.get("focal_point")
    if mode == "focal":
        if not isinstance(focal_point, Mapping):
            raise ValidationError("focal mode requires focal_point")
        x = float(focal_point.get("x", -1))
        y = float(focal_point.get("y", -1))
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValidationError("focal_point x/y must be between 0 and 1")

    watermark = params.get("watermark")
    if watermark is not None:
        if not isinstance(watermark, Mapping):
            raise ValidationError("watermark must be an object")
        text = str(watermark.get("text", ""))
        if len(text) > MAX_WATERMARK_TEXT:
            raise ValidationError("watermark text is too long")
        opacity = float(watermark.get("opacity", 0.35))
        if not (0 <= opacity <= 1):
            raise ValidationError("watermark opacity must be between 0 and 1")

    return {
        "mode": mode,
        "width": width,
        "height": height,
        "quality": quality,
        "format": output_format,
        "alpha_policy": alpha_policy,
        "orientation": orientation,
        "color_policy": color_policy,
        "metadata_policy": metadata_policy,
        "focal_point": dict(focal_point) if isinstance(focal_point, Mapping) else None,
        "watermark": dict(watermark) if isinstance(watermark, Mapping) else None,
    }


def create_preset(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    name: str,
    params: Mapping[str, Any],
    actor_id: str | None = None,
) -> dict[str, Any]:
    initialize(conn)
    project_id = _require_nonempty(project_id, "project_id")
    name = _require_nonempty(name, "name")
    normalized = validate_preset_params(params)
    params_json = canonical_json(normalized)
    params_hash = sha256_text(params_json)
    existing = conn.execute(
        """
        SELECT id, version, params_json FROM enhancement_presets
        WHERE project_id=? AND name=? AND params_hash=?
        """,
        (project_id, name, params_hash),
    ).fetchone()
    if existing:
        return {
            "id": existing[0],
            "project_id": project_id,
            "name": name,
            "version": existing[1],
            "params": json.loads(existing[2]),
            "immutable": True,
        }

    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM enhancement_presets WHERE project_id=? AND name=?",
        (project_id, name),
    ).fetchone()
    version = int(row[0])
    preset_id = new_id("preset")
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO enhancement_presets
              (id, project_id, name, version, params_json, params_hash, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (preset_id, project_id, name, version, params_json, params_hash, utc_now(), actor_id),
        )
        create_audit_event(
            conn,
            "preset.created",
            actor_id=actor_id,
            scope_id=None,
            target={"project_id": project_id, "preset_id": preset_id, "name": name, "version": version},
            result="succeeded",
            detail={"params_hash": params_hash},
        )
    return {
        "id": preset_id,
        "project_id": project_id,
        "name": name,
        "version": version,
        "params": normalized,
        "immutable": True,
    }


def list_presets(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    initialize(conn)
    rows = conn.execute(
        """
        SELECT id, project_id, name, version, params_json, params_hash, created_at
        FROM enhancement_presets
        WHERE project_id=?
        ORDER BY name, version
        """,
        (_require_nonempty(project_id, "project_id"),),
    ).fetchall()
    return [
        {
            "id": row[0],
            "project_id": row[1],
            "name": row[2],
            "version": row[3],
            "params": json.loads(row[4]),
            "params_hash": row[5],
            "created_at": row[6],
            "immutable": True,
        }
        for row in rows
    ]


def _operation_intent(
    conn: sqlite3.Connection,
    operation_type: str,
    *,
    scope_id: str | None,
    target: Mapping[str, Any],
) -> tuple[str, str]:
    request_hash = sha256_text(canonical_json({"operation_type": operation_type, "scope_id": scope_id, "target": target}))
    intent_id = new_id("op")
    conn.execute(
        """
        INSERT INTO operation_intents
          (id, operation_type, scope_id, target_json, request_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (intent_id, operation_type, scope_id, canonical_json(dict(target)), request_hash, utc_now(), utc_now()),
    )
    return intent_id, request_hash


def _finish_intent(
    conn: sqlite3.Connection,
    intent_id: str,
    status: str,
    *,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE operation_intents
        SET status=?, result_json=?, error=?, updated_at=?
        WHERE id=?
        """,
        (status, canonical_json(dict(result or {})), error, utc_now(), intent_id),
    )


def create_derived_variant_intent(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    scope_id: str,
    source_object_id: str,
    source_revision: str,
    preset_id: str,
    output_scope_id: str,
    output_bucket: str,
    output_key: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    initialize(conn)
    preset = conn.execute(
        "SELECT params_json, params_hash FROM enhancement_presets WHERE id=?",
        (_require_nonempty(preset_id, "preset_id"),),
    ).fetchone()
    if not preset:
        raise NotFound("preset not found")

    if output_key is None:
        output_format = json.loads(preset[0])["format"]
        seed = canonical_json(
            {
                "scope_id": scope_id,
                "source_object_id": source_object_id,
                "source_revision": source_revision,
                "preset_id": preset_id,
                "output_scope_id": output_scope_id,
            }
        )
        output_key = f"derived/{sha256_text(seed)[:32]}.{output_format}"

    binding = {
        "project_id": project_id,
        "scope_id": scope_id,
        "source_object_id": source_object_id,
        "source_revision": source_revision,
        "preset_id": preset_id,
        "preset_hash": preset[1],
        "output_scope_id": output_scope_id,
        "output_bucket": output_bucket,
        "output_key": output_key,
    }
    binding_hash = sha256_text(canonical_json(binding))
    existing = conn.execute("SELECT * FROM derived_variants WHERE input_binding_hash=?", (binding_hash,)).fetchone()
    if existing:
        return _variant_row_to_dict(existing)

    variant_id = f"variant_{binding_hash[:32]}"
    now = utc_now()
    manifest = {
        "manifest_version": 1,
        "variant_id": variant_id,
        "input_binding_hash": binding_hash,
        "status": "planned",
        "output": {"scope_id": output_scope_id, "bucket": output_bucket, "key": output_key},
    }
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO derived_variants
              (id, project_id, scope_id, source_object_id, source_revision, preset_id,
               output_scope_id, output_bucket, output_key, input_binding_hash, status,
               manifest_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
            """,
            (
                variant_id,
                project_id,
                scope_id,
                source_object_id,
                source_revision,
                preset_id,
                output_scope_id,
                output_bucket,
                output_key,
                binding_hash,
                canonical_json(manifest),
                now,
                now,
            ),
        )
        create_audit_event(
            conn,
            "derived.intent",
            actor_id=actor_id,
            scope_id=scope_id,
            target={"variant_id": variant_id, "source_object_id": source_object_id, "output_key": output_key},
            result="planned",
            detail={"input_binding_hash": binding_hash},
        )
    return get_derived_variant(conn, variant_id)


def _variant_row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "project_id": row[1],
        "scope_id": row[2],
        "source_object_id": row[3],
        "source_revision": row[4],
        "preset_id": row[5],
        "output_scope_id": row[6],
        "output_bucket": row[7],
        "output_key": row[8],
        "input_binding_hash": row[9],
        "status": row[10],
        "manifest": json.loads(row[11]),
        "source_sha256": row[12],
        "output_sha256": row[13],
        "output_size": row[14],
        "output_mime": row[15],
        "last_error": row[16],
        "created_at": row[17],
        "updated_at": row[18],
    }


def get_derived_variant(conn: sqlite3.Connection, variant_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, project_id, scope_id, source_object_id, source_revision, preset_id,
               output_scope_id, output_bucket, output_key, input_binding_hash, status,
               manifest_json, source_sha256, output_sha256, output_size, output_mime,
               last_error, created_at, updated_at
        FROM derived_variants WHERE id=?
        """,
        (variant_id,),
    ).fetchone()
    if not row:
        raise NotFound("derived variant not found")
    return _variant_row_to_dict(row)


def create_or_get_batch(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    project_id: str,
    idempotency_key: str,
    request_body: Mapping[str, Any],
    planned_count: int,
    items: list[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    initialize(conn)
    if not idempotency_key:
        raise ValidationError("idempotency_key is required for derived variant batches")
    request_hash = sha256_text(canonical_json(dict(request_body)))
    existing = conn.execute("SELECT * FROM derived_variant_batches WHERE scope_id=? AND idempotency_key=?", (scope_id, idempotency_key)).fetchone()
    if existing:
        if existing["request_hash"] != request_hash:
            raise ConflictError("derived batch idempotency key was reused with a different request body")
        return get_derived_batch(conn, existing["id"]), request_hash
    batch_id = new_id("batch")
    now = utc_now()
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO derived_variant_batches
              (id, scope_id, project_id, request_hash, idempotency_key, status, planned_count, items_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'planning', ?, ?, ?, ?)
            """,
            (batch_id, scope_id, project_id, request_hash, idempotency_key, int(planned_count), canonical_json([dict(item) for item in items]), now, now),
        )
    return None, request_hash


def update_batch_items(conn: sqlite3.Connection, batch_id: str, *, status: str, items: list[Mapping[str, Any]]) -> dict[str, Any]:
    with _transaction(conn):
        conn.execute(
            "UPDATE derived_variant_batches SET status=?, items_json=?, updated_at=? WHERE id=?",
            (status, canonical_json([dict(item) for item in items]), utc_now(), batch_id),
        )
    return get_derived_batch(conn, batch_id)


def get_derived_batch(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    initialize(conn)
    row = conn.execute("SELECT * FROM derived_variant_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise NotFound("derived variant batch not found")
    items = json.loads(row["items_json"])
    return {
        "id": row["id"],
        "scope_id": row["scope_id"],
        "project_id": row["project_id"],
        "request_hash": row["request_hash"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "planned_count": row["planned_count"],
        "items": items,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def record_derived_write_result(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    variant_id: str,
    expected_checksum_sha256: str | None = None,
    expected_mime: str | None = None,
) -> dict[str, Any]:
    initialize(conn)
    variant = get_derived_variant(conn, variant_id)
    head = _call_required(adapter, "head_object", variant["output_bucket"], variant["output_key"])
    checksum = _get_attr(head, "checksum_sha256")
    mime = _get_attr(head, "content_type") or _get_attr(head, "mime")
    size = _get_attr(head, "size")
    if expected_checksum_sha256 and checksum and checksum != expected_checksum_sha256:
        status = "needs_review"
        error = "output checksum mismatch"
    elif expected_mime and mime and mime != expected_mime:
        status = "needs_review"
        error = "output mime mismatch"
    else:
        status = "succeeded"
        error = None

    manifest = variant["manifest"] | {
        "status": status,
        "readback": {"checksum_sha256": checksum, "mime": mime, "size": size, "observed_at": utc_now()},
    }
    with _transaction(conn):
        conn.execute(
            """
            UPDATE derived_variants
            SET status=?, manifest_json=?, output_sha256=?, output_size=?, output_mime=?,
                last_error=?, updated_at=?
            WHERE id=?
            """,
            (status, canonical_json(manifest), checksum, size, mime, error, utc_now(), variant_id),
        )
    return get_derived_variant(conn, variant_id)


def run_job(job: Any, settings: Any, checkpoint_callback: Callable[[Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """Process one enhancement job submitted by the parent jobs module.

    Expected job kind: ``derived_variant`` or ``derived-variant``.
    Expected params may either contain an existing ``variant_id`` or the fields
    required by :func:`create_derived_variant_intent`, plus source bucket/key.
    The function persists/reuses the variant intent before any remote write and
    only writes a new output object.
    """

    kind = _job_get(job, "kind")
    if kind not in {"derived_variant", "derived-variant", "variant"}:
        return {"processed": 0, "errors": [f"unsupported enhancement job kind: {kind}"], "state": "failed"}

    conn = _settings_get(settings, "conn") or _settings_get(settings, "connection")
    if conn is None:
        db_path = _settings_get(settings, "db_path")
        if not db_path:
            raise ValidationError("settings.conn or settings.db_path is required for enhancement jobs")
        conn = sqlite3.connect(str(db_path))
    adapter = _settings_get(settings, "adapter") or _settings_get(settings, "storage_adapter")
    if adapter is None:
        raise ValidationError("settings.adapter is required for enhancement jobs")

    params = _job_params(job)
    initialize(conn)
    try:
        if params.get("variant_id"):
            variant = get_derived_variant(conn, params["variant_id"])
        else:
            variant = create_derived_variant_intent(
                conn,
                project_id=params["project_id"],
                scope_id=params["scope_id"],
                source_object_id=params["source_object_id"],
                source_revision=params["source_revision"],
                preset_id=params["preset_id"],
                output_scope_id=params["output_scope_id"],
                output_bucket=params["output_bucket"],
                output_key=params.get("output_key"),
                actor_id=params.get("actor_id") or _job_get(job, "actor"),
            )

        if checkpoint_callback:
            checkpoint_callback({"phase": "intent_persisted", "variant_id": variant["id"], "output_key": variant["output_key"]})

        preset_row = conn.execute("SELECT params_json FROM enhancement_presets WHERE id=?", (variant["preset_id"],)).fetchone()
        if not preset_row:
            raise NotFound("preset not found for variant job")
        preset_params = json.loads(preset_row[0])

        source_bucket = params["source_bucket"]
        source_key = params["source_key"]
        source_version_id = params.get("source_version_id")
        source_head = _call_required(adapter, "head_object", source_bucket, source_key, source_version_id)
        source_revision = _identity_payload(source_head)
        if variant["source_revision"] and params.get("source_revision") and variant["source_revision"] != params.get("source_revision"):
            raise ConflictError("job source revision does not match variant binding")

        source_bytes = _read_object_bytes(adapter, source_bucket, source_key, source_version_id)
        properties, output_bytes = _decode_image(settings, source_bytes, preset_params)
        output_mime = _mime_for_format(preset_params["format"])
        output_sha = hashlib.sha256(output_bytes).hexdigest()
        metadata = {
            "swc-variant-id": variant["id"],
            "swc-source-object-id": variant["source_object_id"],
            "swc-input-binding-hash": variant["input_binding_hash"],
            "swc-pipeline-version": str(_pipeline_version(settings)),
        }
        _put_object_bytes(adapter, variant["output_bucket"], variant["output_key"], output_bytes, metadata, output_mime)
        completed = record_derived_write_result(
            conn,
            adapter,
            variant_id=variant["id"],
            expected_checksum_sha256=output_sha,
            expected_mime=output_mime,
        )
        manifest = completed["manifest"] | {
            "source": source_revision,
            "properties": properties,
            "pipeline_version": _pipeline_version(settings),
        }
        with _transaction(conn):
            conn.execute(
                "UPDATE derived_variants SET manifest_json=?, source_sha256=?, updated_at=? WHERE id=?",
                (canonical_json(manifest), _get_attr(source_head, "checksum_sha256"), utc_now(), variant["id"]),
            )
        if checkpoint_callback:
            checkpoint_callback({"phase": "completed", "variant_id": variant["id"], "status": completed["status"]})
        return {"processed": 1, "errors": [], "checkpoint": {"variant_id": variant["id"]}, "state": completed["status"]}
    except Exception as exc:
        variant_id = params.get("variant_id")
        if "variant" in locals():
            variant_id = variant["id"]
        if variant_id:
            with _transaction(conn):
                conn.execute(
                    "UPDATE derived_variants SET status='failed', last_error=?, updated_at=? WHERE id=?",
                    (str(exc), utc_now(), variant_id),
                )
        return {"processed": 0, "errors": [str(exc)], "checkpoint": {"variant_id": variant_id}, "state": "failed"}


def create_group(conn: sqlite3.Connection, *, project_id: str, name: str, source: str = "manual") -> dict[str, Any]:
    initialize(conn)
    group_id = new_id("group")
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO image_groups (id, project_id, name, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, _require_nonempty(project_id, "project_id"), _require_nonempty(name, "name"), source, utc_now()),
        )
    return {"id": group_id, "project_id": project_id, "name": name, "source": source}


def add_group_member(conn: sqlite3.Connection, *, group_id: str, object_id: str, relation_source: str = "manual") -> None:
    initialize(conn)
    with _transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO image_group_members (group_id, object_id, relation_source, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, _require_nonempty(object_id, "object_id"), relation_source, utc_now()),
        )


def add_object_relationship(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    scope_id: str,
    source_object_id: str,
    target_object_id: str,
    relation_type: str,
    relation_source: str,
    evidence: Mapping[str, Any] | None = None,
) -> str:
    initialize(conn)
    relation_id = new_id("rel")
    with _transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO object_relationships
              (id, project_id, scope_id, source_object_id, target_object_id, relation_type,
               relation_source, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation_id,
                project_id,
                scope_id,
                source_object_id,
                target_object_id,
                relation_type,
                relation_source,
                canonical_json(dict(evidence or {})),
                utc_now(),
            ),
        )
    return relation_id


def start_multipart_upload(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    bucket: str,
    key: str,
    metadata: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    initialize(conn)
    upload_row_id = new_id("upload")
    now = utc_now()
    with _transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO multipart_uploads
                  (id, scope_id, bucket, key, metadata_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'initializing', ?, ?)
                """,
                (upload_row_id, scope_id, bucket, key, canonical_json(dict(metadata or {})), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("active multipart upload already reserves this target") from exc
    try:
        upload_id = _call_required(adapter, "create_multipart_upload", bucket, key, dict(metadata or {}))
    except Exception as exc:
        with _transaction(conn):
            conn.execute(
                "UPDATE multipart_uploads SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(exc), utc_now(), upload_row_id),
            )
        raise
    with _transaction(conn):
        conn.execute(
            "UPDATE multipart_uploads SET upload_id=?, status='active', updated_at=? WHERE id=?",
            (str(upload_id), utc_now(), upload_row_id),
        )
    return get_multipart_upload(conn, upload_row_id)


def get_multipart_upload(conn: sqlite3.Connection, upload_row_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, scope_id, bucket, key, upload_id, metadata_json, status, created_at,
               updated_at, completed_at, aborted_at, error
        FROM multipart_uploads WHERE id=?
        """,
        (upload_row_id,),
    ).fetchone()
    if not row:
        raise NotFound("multipart upload not found")
    return {
        "id": row[0],
        "scope_id": row[1],
        "bucket": row[2],
        "key": row[3],
        "upload_id": row[4],
        "metadata": json.loads(row[5]),
        "status": row[6],
        "created_at": row[7],
        "updated_at": row[8],
        "completed_at": row[9],
        "aborted_at": row[10],
        "error": row[11],
    }


def complete_multipart_upload(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    upload_row_id: str,
    parts: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    initialize(conn)
    upload = get_multipart_upload(conn, upload_row_id)
    if upload["status"] != "active":
        raise ConflictError("multipart upload is not active")
    parts_list = [dict(part) for part in parts]
    with _transaction(conn):
        updated = conn.execute(
            "UPDATE multipart_uploads SET status='completing', updated_at=? WHERE id=? AND status='active'",
            (utc_now(), upload_row_id),
        ).rowcount
        if updated != 1:
            raise ConflictError("multipart upload is not active")
        upload = get_multipart_upload(conn, upload_row_id)
        intent_id, _ = _operation_intent(conn, "multipart.complete", scope_id=upload["scope_id"], target=upload | {"parts": parts_list})
    try:
        _call_required(adapter, "complete_multipart_upload", upload["bucket"], upload["key"], upload["upload_id"], parts_list)
        head = _call_required(adapter, "head_object", upload["bucket"], upload["key"])
        result = _identity_payload(head)
        with _transaction(conn):
            conn.execute(
                "UPDATE multipart_uploads SET status='completed', completed_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), upload_row_id),
            )
            _finish_intent(conn, intent_id, "succeeded", result=result)
            _record_version_observation(conn, upload["scope_id"], upload["bucket"], upload["key"], head)
    except Exception as exc:
        with _transaction(conn):
            _finish_intent(conn, intent_id, "needs_review", error=str(exc))
            conn.execute(
                "UPDATE multipart_uploads SET status='needs_review', error=?, updated_at=? WHERE id=?",
                (str(exc), utc_now(), upload_row_id),
            )
        raise
    return get_multipart_upload(conn, upload_row_id)


def abort_multipart_upload(conn: sqlite3.Connection, adapter: Any, *, upload_row_id: str) -> dict[str, Any]:
    initialize(conn)
    upload = get_multipart_upload(conn, upload_row_id)
    if upload["status"] not in {"active", "initializing", "failed", "needs_review"}:
        raise ConflictError("multipart upload cannot be aborted in its current state")
    try:
        if upload["upload_id"]:
            _call_required(adapter, "abort_multipart_upload", upload["bucket"], upload["key"], upload["upload_id"])
        status = "aborted"
        error = upload["error"]
    except Exception as exc:
        status = "abort_failed"
        error = str(exc)
    with _transaction(conn):
        conn.execute(
            "UPDATE multipart_uploads SET status=?, aborted_at=?, error=?, updated_at=? WHERE id=?",
            (status, utc_now(), error, utc_now(), upload_row_id),
        )
    return get_multipart_upload(conn, upload_row_id)


def copy_object_safely(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    source_bucket: str,
    source_key: str,
    target_bucket: str,
    target_key: str,
    source_version_id: str | None = None,
    expected_source_revision: str | None = None,
    metadata: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    initialize(conn)
    source_head = _call_required(adapter, "head_object", source_bucket, source_key, source_version_id)
    if expected_source_revision:
        observed_revision = make_observed_revision(source_bucket, source_key, source_head, source_version_id)
        if observed_revision != expected_source_revision:
            raise ConflictError("source object revision does not match selected index revision")
    with _transaction(conn):
        intent_id, _ = _operation_intent(
            conn,
            "object.copy",
            scope_id=scope_id,
            target={
                "source": _identity_payload(source_head) | {"bucket": source_bucket, "key": source_key},
                "target": {"bucket": target_bucket, "key": target_key},
            },
        )
    try:
        data = _call_required(adapter, "get_object_bytes", source_bucket, source_key, source_version_id)
        fresh_source = _call_required(adapter, "head_object", source_bucket, source_key, source_version_id)
        _verify_copy_result(source_head, fresh_source)
        content_type = _get_attr(source_head, "content_type") or "application/octet-stream"
        merged_metadata = dict(_get_attr(source_head, "metadata") or {})
        merged_metadata.update(dict(metadata or {}))
        _call_required(adapter, "put_object", target_bucket, target_key, data, merged_metadata, content_type)
        target_head = _call_required(adapter, "head_object", target_bucket, target_key)
        _verify_copy_result(source_head, target_head)
        target_data = _call_required(adapter, "get_object_bytes", target_bucket, target_key, _get_attr(target_head, "version_id"))
        if hashlib.sha256(target_data).hexdigest() != hashlib.sha256(data).hexdigest():
            raise ConflictError("copied object byte checksum mismatch")
        _verify_copy_attributes(target_head, merged_metadata, content_type)
        result = {"source": _identity_payload(source_head), "target": _identity_payload(target_head)}
        with _transaction(conn):
            _finish_intent(conn, intent_id, "succeeded", result=result)
        return {"status": "succeeded", **result}
    except Exception as exc:
        with _transaction(conn):
            _finish_intent(conn, intent_id, "needs_review", error=str(exc))
        raise


def move_object_protected(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    source_bucket: str,
    source_key: str,
    target_bucket: str,
    target_key: str,
    source_version_id: str | None = None,
    protection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not protection or protection.get("state") != "valid" or not protection.get("guard_id"):
        raise ProtectedError("external object move is disabled without a complete reference protection guard")
    copied = copy_object_safely(
        conn,
        adapter,
        scope_id=scope_id,
        source_bucket=source_bucket,
        source_key=source_key,
        target_bucket=target_bucket,
        target_key=target_key,
        source_version_id=source_version_id,
    )
    source_now = _call_required(adapter, "head_object", source_bucket, source_key, source_version_id)
    if _identity_payload(source_now).get("checksum_sha256") != copied["source"].get("checksum_sha256"):
        raise ProtectedError("source changed after copy; protected delete refused")
    if _get_attr(source_now, "legal_hold") or _get_attr(source_now, "retention_until"):
        raise ProtectedError("source is under retention or legal hold")
    with _transaction(conn):
        intent_id, _ = _operation_intent(
            conn,
            "object.move.delete_source",
            scope_id=scope_id,
            target={"source": {"bucket": source_bucket, "key": source_key, "version_id": source_version_id}, "guard": dict(protection)},
        )
    try:
        _call_required(adapter, "delete_object", source_bucket, source_key, source_version_id)
        with _transaction(conn):
            _finish_intent(conn, intent_id, "succeeded", result={"deleted_source": True})
        return {"status": "succeeded", "copy": copied, "deleted_source": True}
    except Exception as exc:
        with _transaction(conn):
            _finish_intent(conn, intent_id, "needs_review", error=str(exc))
        raise


def list_object_versions(conn: sqlite3.Connection, adapter: Any, *, scope_id: str, bucket: str, key: str) -> dict[str, Any]:
    initialize(conn)
    try:
        versions = _call_required(adapter, "list_object_versions", bucket, key)
    except AttributeError as exc:
        raise CapabilityUnsupported("versioning read is not supported by this adapter") from exc
    for item in versions:
        _record_version_observation(conn, scope_id, bucket, key, item)
    conn.commit()
    return {"status": "succeeded", "versions": [_identity_payload(item) | {"is_delete_marker": bool(_get_attr(item, "is_delete_marker"))} for item in versions]}


def restore_object_version(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    bucket: str,
    key: str,
    version_id: str,
    target_key: str,
) -> dict[str, Any]:
    try:
        _call_required(adapter, "head_object", bucket, target_key)
    except Exception:
        pass
    else:
        raise ConflictError("restore target already exists; restore only writes a new target")
    return copy_object_safely(
        conn,
        adapter,
        scope_id=scope_id,
        source_bucket=bucket,
        source_key=key,
        target_bucket=bucket,
        target_key=target_key,
        source_version_id=version_id,
    )


def get_object_tags(conn: sqlite3.Connection, adapter: Any, *, scope_id: str, bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
    initialize(conn)
    try:
        tags = _call_required(adapter, "get_object_tags", bucket, key, version_id)
    except AttributeError as exc:
        raise CapabilityUnsupported("object tags are not supported by this adapter") from exc
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO object_tag_observations
              (id, scope_id, bucket, key, version_id, tags_json, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("tags"), scope_id, bucket, key, version_id, canonical_json(dict(tags)), utc_now()),
        )
    return {"status": "succeeded", "tags": dict(tags)}


def set_object_tags(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    bucket: str,
    key: str,
    tags: Mapping[str, str],
    version_id: str | None = None,
) -> dict[str, Any]:
    initialize(conn)
    with _transaction(conn):
        intent_id, _ = _operation_intent(conn, "object.tags.put", scope_id=scope_id, target={"bucket": bucket, "key": key, "version_id": version_id, "tags": dict(tags)})
    try:
        _call_required(adapter, "put_object_tags", bucket, key, dict(tags), version_id)
        readback = get_object_tags(conn, adapter, scope_id=scope_id, bucket=bucket, key=key, version_id=version_id)
        if readback["tags"] != dict(tags):
            raise ConflictError("tag readback mismatch")
        with _transaction(conn):
            _finish_intent(conn, intent_id, "succeeded", result=readback)
        return readback
    except Exception as exc:
        with _transaction(conn):
            _finish_intent(conn, intent_id, "failed", error=str(exc))
        raise


def get_retention(conn: sqlite3.Connection, adapter: Any, *, bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
    initialize(conn)
    try:
        retention = _call_required(adapter, "get_object_retention", bucket, key, version_id)
        hold = _call_required(adapter, "get_object_legal_hold", bucket, key, version_id)
    except AttributeError as exc:
        raise CapabilityUnsupported("retention/legal hold capability is unavailable") from exc
    return {"status": "succeeded", "retention": retention, "legal_hold": hold}


def update_bucket_config(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    bucket: str,
    kind: str,
    new_config: Mapping[str, Any],
    actor_id: str | None = None,
    expected_current_hash: str | None = None,
    exclusive_writer_ack: bool = False,
    admin_manage_bucket: bool = False,
) -> dict[str, Any]:
    initialize(conn)
    if kind not in CONFIG_KINDS:
        raise ValidationError("unknown bucket config kind")
    if not admin_manage_bucket:
        raise PermissionDenied("bucket config edit requires manage_bucket admin grant")
    if not expected_current_hash:
        raise ValidationError("expected_current_hash is required for bucket config edits")
    if exclusive_writer_ack is not True:
        raise ValidationError("exclusive_writer_ack=true is required for cooperative bucket config edits")
    _validate_bucket_config(kind, new_config)
    _ensure_no_active_bucket_config_intent(conn, scope_id, bucket, kind)
    get_name = f"get_bucket_{kind}"
    put_name = f"put_bucket_{kind}"
    current = _call_required(adapter, get_name, bucket)
    current_hash = sha256_text(canonical_json(current))
    if expected_current_hash != current_hash:
        raise ConflictError("bucket config changed before save")
    with _transaction(conn):
        intent_id, _ = _operation_intent(
            conn,
            f"bucket.{kind}.put",
            scope_id=scope_id,
            target={"bucket": bucket, "kind": kind, "before_hash": current_hash, "after_hash": sha256_text(canonical_json(new_config))},
        )
        before_snapshot_id = new_id("cfg")
        conn.execute(
            """
            INSERT INTO bucket_config_snapshots
              (id, scope_id, bucket, kind, config_json, config_hash, warning, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (before_snapshot_id, scope_id, bucket, kind, canonical_json(current), current_hash, "before_write", utc_now()),
        )
    try:
        _call_required(adapter, put_name, bucket, dict(new_config))
        readback = _call_required(adapter, get_name, bucket)
        readback_hash = sha256_text(canonical_json(readback))
        requested_hash = sha256_text(canonical_json(new_config))
        warnings = ["backend has no remote CAS; save used cooperative hash reread plus readback"]
        status = "succeeded_with_warning"
        if requested_hash != readback_hash:
            warnings.append("readback differs from requested config")
            status = "needs_review"
        warning = "; ".join(warnings)
        with _transaction(conn):
            _finish_intent(conn, intent_id, status, result={"readback_hash": readback_hash, "warning": warning, "before_snapshot_id": before_snapshot_id})
            conn.execute(
                """
                INSERT INTO bucket_config_snapshots
                  (id, scope_id, bucket, kind, config_json, config_hash, warning, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("cfg"), scope_id, bucket, kind, canonical_json(readback), readback_hash, "readback:" + status, utc_now()),
            )
            create_audit_event(
                conn,
                f"bucket.{kind}.updated",
                actor_id=actor_id,
                scope_id=scope_id,
                target={"bucket": bucket, "kind": kind},
                result=status,
                detail={"before_hash": current_hash, "readback_hash": readback_hash, "requested_hash": requested_hash, "warning": warning, "before_snapshot_id": before_snapshot_id},
            )
        return {"status": status, "before_hash": current_hash, "readback_hash": readback_hash, "requested_hash": requested_hash, "warning": warning, "warnings": warnings, "before_snapshot_id": before_snapshot_id, "config": readback}
    except Exception as exc:
        with _transaction(conn):
            _finish_intent(conn, intent_id, "failed", error=str(exc))
        raise


def rollback_bucket_config(
    conn: sqlite3.Connection,
    adapter: Any,
    *,
    scope_id: str,
    bucket: str,
    kind: str,
    snapshot_id: str,
    expected_current_hash: str,
    exclusive_writer_ack: bool = False,
    actor_id: str | None = None,
    admin_manage_bucket: bool = False,
) -> dict[str, Any]:
    initialize(conn)
    if kind not in CONFIG_KINDS:
        raise ValidationError("unknown bucket config kind")
    if not admin_manage_bucket:
        raise PermissionDenied("bucket config rollback requires manage_bucket admin grant")
    if not expected_current_hash:
        raise ValidationError("expected_current_hash is required for bucket config rollback")
    if exclusive_writer_ack is not True:
        raise ValidationError("exclusive_writer_ack=true is required for cooperative bucket config rollback")
    _ensure_no_active_bucket_config_intent(conn, scope_id, bucket, kind)
    snapshot = conn.execute(
        "SELECT * FROM bucket_config_snapshots WHERE id=? AND scope_id=? AND bucket=? AND kind=?",
        (snapshot_id, scope_id, bucket, kind),
    ).fetchone()
    if not snapshot:
        raise ValidationError("bucket config snapshot was not found for this scope")
    if snapshot[6] != "before_write":
        raise ValidationError("only before-write snapshots can be rolled back")
    latest_before = conn.execute(
        """
        SELECT id FROM bucket_config_snapshots
        WHERE scope_id=? AND bucket=? AND kind=? AND warning='before_write'
        ORDER BY rowid DESC LIMIT 1
        """,
        (scope_id, bucket, kind),
    ).fetchone()
    if not latest_before or latest_before[0] != snapshot_id:
        raise ConflictError("newer bucket config save exists; rollback snapshot is no longer the last applied condition")
    config = json.loads(snapshot[4])
    _validate_bucket_config(kind, config)
    get_name = f"get_bucket_{kind}"
    put_name = f"put_bucket_{kind}"
    current = _call_required(adapter, get_name, bucket)
    current_hash = sha256_text(canonical_json(current))
    if current_hash != expected_current_hash:
        raise ConflictError("bucket config changed before rollback")
    with _transaction(conn):
        intent_id, _ = _operation_intent(
            conn,
            f"bucket.{kind}.rollback",
            scope_id=scope_id,
            target={"bucket": bucket, "kind": kind, "snapshot_id": snapshot_id, "before_hash": current_hash, "restore_hash": snapshot[5]},
        )
    try:
        _call_required(adapter, put_name, bucket, config)
        readback = _call_required(adapter, get_name, bucket)
        readback_hash = sha256_text(canonical_json(readback))
        warnings = ["backend has no remote CAS; rollback used cooperative hash reread plus readback"]
        status = "succeeded_with_warning"
        if readback_hash != snapshot[5]:
            warnings.append("readback differs from rollback snapshot")
            status = "needs_review"
        warning = "; ".join(warnings)
        with _transaction(conn):
            _finish_intent(conn, intent_id, status, result={"readback_hash": readback_hash, "warning": warning})
            conn.execute(
                """
                INSERT INTO bucket_config_snapshots
                  (id, scope_id, bucket, kind, config_json, config_hash, warning, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("cfg"), scope_id, bucket, kind, canonical_json(readback), readback_hash, "readback:" + status, utc_now()),
            )
            create_audit_event(
                conn,
                f"bucket.{kind}.rollback",
                actor_id=actor_id,
                scope_id=scope_id,
                target={"bucket": bucket, "kind": kind, "snapshot_id": snapshot_id},
                result=status,
                detail={"before_hash": current_hash, "readback_hash": readback_hash, "restore_hash": snapshot[5], "warning": warning},
            )
        return {"status": status, "before_hash": current_hash, "readback_hash": readback_hash, "restored_hash": snapshot[5], "warning": warning, "warnings": warnings, "config": readback}
    except Exception as exc:
        with _transaction(conn):
            _finish_intent(conn, intent_id, "failed", error=str(exc))
        raise


def start_reference_manifest(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    schema_version: int,
    coverage: Mapping[str, Any],
    declared_count: int,
    declared_hash: str | None = None,
    waterline: str | None = None,
) -> dict[str, Any]:
    initialize(conn)
    if schema_version != 1:
        status = "invalid"
        reason = "unknown schema version"
    elif coverage.get("scope_id") != scope_id:
        status = "invalid"
        reason = "coverage scope mismatch"
    else:
        status = "importing"
        reason = None
    manifest_id = new_id("manifest")
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO reference_manifests
              (id, scope_id, schema_version, coverage_json, declared_count, declared_hash,
               status, waterline, created_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (manifest_id, scope_id, schema_version, canonical_json(dict(coverage)), int(declared_count), declared_hash, status, waterline, utc_now(), reason),
        )
    return get_reference_manifest(conn, manifest_id)


def import_reference_manifest_entries(conn: sqlite3.Connection, *, manifest_id: str, entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    initialize(conn)
    manifest = get_reference_manifest(conn, manifest_id)
    if manifest["status"] == "invalid":
        raise ValidationError("cannot import entries into invalid manifest")
    count = 0
    with _transaction(conn):
        for entry in entries:
            if not {"entity_type", "entity_id", "role"} <= set(entry):
                raise ValidationError("manifest entry is missing required fields")
            normalized_entry = _normalize_reference_manifest_entry(conn, manifest, entry)
            entry_hash = sha256_text(canonical_json(normalized_entry))
            conn.execute(
                """
                INSERT OR IGNORE INTO reference_manifest_entries
                  (manifest_id, entry_hash, object_id, bucket, key, version_id, entity_type,
                   entity_id, role, relation_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest_id,
                    entry_hash,
                    normalized_entry.get("object_id"),
                    normalized_entry.get("bucket"),
                    normalized_entry.get("key"),
                    normalized_entry.get("version_id"),
                    normalized_entry["entity_type"],
                    normalized_entry["entity_id"],
                    normalized_entry["role"],
                    canonical_json(normalized_entry),
                ),
            )
            count += 1
    return {"status": "imported", "count": count}


def _normalize_reference_manifest_entry(
    conn: sqlite3.Connection,
    manifest: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = dict(entry)
    scope_identity = _scope_identity(conn, str(manifest["scope_id"]))
    key = normalized.get("key")
    object_id = normalized.get("object_id")
    if not key and not object_id:
        raise ValidationError("manifest entry requires object_id or key")
    if key:
        bucket = normalized.get("bucket") or scope_identity.get("bucket") or manifest.get("coverage", {}).get("bucket")
        if not bucket:
            raise ValidationError("manifest entry bucket is required for key references")
        if scope_identity.get("bucket") and bucket != scope_identity["bucket"]:
            raise ValidationError("manifest entry bucket does not match manifest scope")
        normalized["bucket"] = bucket
    elif normalized.get("bucket") is None and scope_identity.get("bucket"):
        normalized["bucket"] = scope_identity["bucket"]
    if scope_identity.get("connection_id") and not normalized.get("connection_id"):
        normalized["connection_id"] = scope_identity["connection_id"]
    return normalized


def _scope_identity(conn: sqlite3.Connection, scope_id: str) -> dict[str, Any]:
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scopes'").fetchone()
    if not table:
        return {}
    row = conn.execute("SELECT connection_id, bucket FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if not row:
        return {}
    return {"connection_id": row[0], "bucket": row[1]}


def finalize_reference_manifest(conn: sqlite3.Connection, *, manifest_id: str) -> dict[str, Any]:
    initialize(conn)
    manifest = get_reference_manifest(conn, manifest_id)
    if manifest["status"] == "invalid":
        return manifest
    rows = conn.execute(
        "SELECT entry_hash FROM reference_manifest_entries WHERE manifest_id=? ORDER BY entry_hash",
        (manifest_id,),
    ).fetchall()
    actual_count = len(rows)
    actual_hash = sha256_text(canonical_json([row[0] for row in rows]))
    if actual_count == 0 or actual_count < manifest["declared_count"]:
        status = "partial"
        reason = "entry coverage incomplete"
    elif actual_count > manifest["declared_count"]:
        status = "invalid"
        reason = "entry count exceeds declaration"
    elif manifest["declared_hash"] and manifest["declared_hash"] != actual_hash:
        status = "invalid"
        reason = "entry hash mismatch"
    else:
        status = "complete"
        reason = None
    with _transaction(conn):
        conn.execute(
            "UPDATE reference_manifests SET status=?, finalized_at=?, reason=? WHERE id=?",
            (status, utc_now(), reason, manifest_id),
        )
    return get_reference_manifest(conn, manifest_id) | {"actual_count": actual_count, "actual_hash": actual_hash}


def get_reference_manifest(conn: sqlite3.Connection, manifest_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, scope_id, schema_version, coverage_json, declared_count, declared_hash,
               status, waterline, created_at, finalized_at, reason
        FROM reference_manifests WHERE id=?
        """,
        (manifest_id,),
    ).fetchone()
    if not row:
        raise NotFound("reference manifest not found")
    return {
        "id": row[0],
        "scope_id": row[1],
        "schema_version": row[2],
        "coverage": json.loads(row[3]),
        "declared_count": row[4],
        "declared_hash": row[5],
        "status": row[6],
        "waterline": row[7],
        "created_at": row[8],
        "finalized_at": row[9],
        "reason": row[10],
    }


def derived_health(conn: sqlite3.Connection, *, scope_id: str) -> dict[str, Any]:
    initialize(conn)
    rows = conn.execute(
        """
        SELECT status, COUNT(*) FROM derived_variants
        WHERE scope_id=?
        GROUP BY status
        """,
        (scope_id,),
    ).fetchall()
    counts = {row[0]: row[1] for row in rows}
    return {
        "scope_id": scope_id,
        "known_relations_only": True,
        "succeeded": counts.get("succeeded", 0),
        "planned": counts.get("planned", 0),
        "needs_review": counts.get("needs_review", 0),
        "failed": counts.get("failed", 0),
        "unknown_external_relations": True,
    }


def add_capacity_snapshot(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    source_bytes: int | None = None,
    derived_bytes: int | None = None,
    temporary_bytes: int | None = None,
    version_bytes: int | None = None,
    unknown_bytes: int | None = None,
    sample_complete: bool = False,
    coverage: str | None = None,
) -> dict[str, Any]:
    initialize(conn)
    values = [source_bytes, derived_bytes, temporary_bytes, version_bytes, unknown_bytes]
    if any(value is not None and int(value) < 0 for value in values):
        raise ValidationError("capacity bytes cannot be negative")
    resolved_coverage = coverage or ("complete_enumeration" if sample_complete else "partial_index")
    snapshot_id = new_id("capacity")
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO capacity_snapshots
              (id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
               unknown_bytes, sample_complete, observed_at, coverage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                scope_id,
                source_bytes,
                derived_bytes,
                temporary_bytes,
                version_bytes,
                unknown_bytes,
                1 if sample_complete else 0,
                utc_now(),
                resolved_coverage,
            ),
        )
    return get_capacity_snapshot(conn, snapshot_id)


def get_capacity_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, scope_id, source_bytes, derived_bytes, temporary_bytes, version_bytes,
               unknown_bytes, sample_complete, observed_at, coverage
        FROM capacity_snapshots WHERE id=?
        """,
        (snapshot_id,),
    ).fetchone()
    if not row:
        raise NotFound("capacity snapshot not found")
    raw_values = dict(zip(CAPACITY_BYTE_FIELDS, [row[2], row[3], row[4], row[5], row[6]], strict=True))
    coverage = row[9] or ("complete_enumeration" if row[7] else "partial_index")
    values = _capacity_public_values(raw_values, coverage=coverage, sample_complete=bool(row[7]))
    parts = [values[name] for name in CAPACITY_BYTE_FIELDS]
    total = sum(int(value) for value in parts) if all(value is not None for value in parts) else None
    unknown_fields = [name for name, value in values.items() if value is None]
    return {
        "id": row[0],
        "scope_id": row[1],
        "source_bytes": values["source_bytes"],
        "derived_bytes": values["derived_bytes"],
        "temporary_bytes": values["temporary_bytes"],
        "version_bytes": values["version_bytes"],
        "unknown_bytes": values["unknown_bytes"],
        "sample_complete": bool(row[7]),
        "observed_at": row[8],
        "coverage": coverage,
        "measurement": {name: ("measured" if name not in unknown_fields else "unknown") for name in CAPACITY_BYTE_FIELDS},
        "legacy_unknown": coverage == "legacy_unknown",
        "legacy_values": raw_values if coverage == "legacy_unknown" else {},
        "unknown_fields": unknown_fields,
        "logical_total_bytes": total,
    }


def _capacity_public_values(raw_values: Mapping[str, Any], *, coverage: str, sample_complete: bool) -> dict[str, Any]:
    if coverage == "legacy_unknown":
        return {name: None for name in CAPACITY_BYTE_FIELDS}
    values = {name: raw_values.get(name) for name in CAPACITY_BYTE_FIELDS}
    if not sample_complete:
        values["source_bytes"] = None
    values["temporary_bytes"] = None
    values["version_bytes"] = None
    return values


def capacity_thresholds(conn: sqlite3.Connection, *, scope_id: str, thresholds: Mapping[str, int]) -> dict[str, Any]:
    initialize(conn)
    row = conn.execute(
        """
        SELECT id FROM capacity_snapshots
        WHERE scope_id=?
        ORDER BY observed_at DESC, rowid DESC
        LIMIT 1
        """,
        (scope_id,),
    ).fetchone()
    if not row:
        return {"status": "unknown", "alerts": ["no capacity snapshot"]}
    snapshot = get_capacity_snapshot(conn, row[0])
    alerts = []
    unknown_fields = []
    for field, limit in thresholds.items():
        if field not in snapshot:
            raise ValidationError(f"unknown threshold field: {field}")
        if snapshot[field] is None:
            unknown_fields.append(field)
            continue
        if int(snapshot[field]) > int(limit):
            alerts.append({"field": field, "value": snapshot[field], "threshold": int(limit)})
    status = "alerting" if alerts else ("unknown" if unknown_fields else "ok")
    if unknown_fields:
        return {
            "status": status,
            "snapshot": snapshot,
            "alerts": alerts,
            "unknown_fields": unknown_fields,
            "sample_complete": snapshot["sample_complete"],
            "coverage": snapshot["coverage"],
        }
    return {
        "status": "alerting" if alerts else "ok",
        "snapshot": snapshot,
        "alerts": alerts,
        "sample_complete": snapshot["sample_complete"],
        "coverage": snapshot["coverage"],
    }


def _record_version_observation(conn: sqlite3.Connection, scope_id: str, bucket: str, key: str, identity: Any) -> None:
    conn.execute(
        """
        INSERT INTO object_version_observations
          (id, scope_id, bucket, key, version_id, is_delete_marker, size, etag,
           checksum_sha256, observed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("ver"),
            scope_id,
            bucket,
            key,
            _get_attr(identity, "version_id"),
            1 if _get_attr(identity, "is_delete_marker") else 0,
            _get_attr(identity, "size"),
            _get_attr(identity, "etag"),
            _get_attr(identity, "checksum_sha256"),
            utc_now(),
        ),
    )


def _job_get(job: Any, name: str) -> Any:
    if isinstance(job, Mapping):
        return job.get(name)
    return getattr(job, name, None)


def _job_params(job: Any) -> dict[str, Any]:
    params = _job_get(job, "params") or {}
    if isinstance(params, str):
        return json.loads(params)
    return dict(params)


def _settings_get(settings: Any, name: str) -> Any:
    if isinstance(settings, Mapping):
        value = settings.get(name)
    else:
        value = getattr(settings, name, None)
    if callable(value) and name in {"conn", "connection", "adapter", "storage_adapter"} and not hasattr(value, "execute"):
        return value()
    return value


def _read_object_bytes(adapter: Any, bucket: str, key: str, version_id: str | None) -> bytes:
    for method_name in ("get_object_bytes", "read_object", "get_object"):
        method = getattr(adapter, method_name, None)
        if method is None:
            continue
        value = method(bucket, key, version_id)
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, Mapping) and "body" in value:
            body = value["body"]
            return body if isinstance(body, bytes) else bytes(body)
    raise CapabilityUnsupported("adapter cannot read object bytes for derived processing")


def _decode_image(settings: Any, data: bytes, params: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    imaging = _settings_get(settings, "imaging")
    if imaging is None:
        try:
            from backend.console import imaging as imaging_module
        except Exception as exc:
            raise CapabilityUnsupported("imaging.decode is unavailable for derived processing") from exc
        imaging = imaging_module
    decoder = getattr(imaging, "decode", None)
    if decoder is None:
        raise CapabilityUnsupported("imaging.decode is unavailable for derived processing")
    properties, output = decoder(data, params=dict(params))
    return dict(properties or {}), bytes(output)


def _pipeline_version(settings: Any) -> str:
    value = _settings_get(settings, "PIPELINE_VERSION") or _settings_get(settings, "pipeline_version")
    if value:
        return str(value)
    imaging = _settings_get(settings, "imaging")
    if imaging is not None and getattr(imaging, "PIPELINE_VERSION", None):
        return str(getattr(imaging, "PIPELINE_VERSION"))
    return "unknown"


def _mime_for_format(output_format: str) -> str:
    if output_format == "jpeg":
        return "image/jpeg"
    if output_format == "png":
        return "image/png"
    if output_format == "webp":
        return "image/webp"
    raise ValidationError("unknown output format")


def _put_object_bytes(
    adapter: Any,
    bucket: str,
    key: str,
    data: bytes,
    metadata: Mapping[str, str],
    content_type: str,
) -> None:
    method = getattr(adapter, "put_object", None)
    if method is not None:
        try:
            method(bucket, key, data, dict(metadata), content_type)
        except TypeError:
            method(bucket, key, data, metadata=dict(metadata), content_type=content_type)
        return
    method = getattr(adapter, "put_object_bytes", None)
    if method is not None:
        method(bucket, key, data, dict(metadata), content_type)
        return
    raise CapabilityUnsupported("adapter cannot write object bytes for derived processing")


def _call_required(adapter: Any, method_name: str, *args: Any) -> Any:
    method = getattr(adapter, method_name)
    return method(*args)


def _get_attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def make_observed_revision(bucket: str, key: str, identity: Any, source_version_id: str | None = None) -> str:
    version_id = source_version_id if source_version_id is not None else _get_attr(identity, "version_id")
    if version_id and version_id != "null":
        return f"version:{version_id}"
    observed = json.dumps(
        {
            "bucket": bucket,
            "key": key,
            "version_id_marker": "literal_null" if version_id == "null" else None,
            "size": _get_attr(identity, "size"),
            "etag": _get_attr(identity, "etag"),
            "last_modified": _get_attr(identity, "last_modified"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "observed:" + hashlib.sha256(observed.encode("utf-8")).hexdigest()


def _identity_payload(identity: Any) -> dict[str, Any]:
    return {
        "bucket": _get_attr(identity, "bucket"),
        "key": _get_attr(identity, "key"),
        "version_id": _get_attr(identity, "version_id"),
        "etag": _get_attr(identity, "etag"),
        "size": _get_attr(identity, "size"),
        "checksum_sha256": _get_attr(identity, "checksum_sha256"),
        "last_modified": _get_attr(identity, "last_modified"),
    }


def _verify_copy_result(source: Any, target: Any) -> None:
    source_size = _get_attr(source, "size")
    target_size = _get_attr(target, "size")
    if source_size is not None and target_size is not None and source_size != target_size:
        raise ConflictError("copied object size mismatch")
    source_checksum = _get_attr(source, "checksum_sha256")
    target_checksum = _get_attr(target, "checksum_sha256")
    if source_checksum and target_checksum and source_checksum != target_checksum:
        raise ConflictError("copied object checksum mismatch")


def _verify_copy_attributes(target: Any, metadata: Mapping[str, str], content_type: str) -> None:
    target_type = _get_attr(target, "content_type")
    if target_type and target_type != content_type:
        raise ConflictError("copied object content type mismatch")
    target_metadata = _get_attr(target, "metadata")
    if target_metadata is not None and dict(target_metadata) != dict(metadata):
        raise ConflictError("copied object metadata mismatch")


def _ensure_no_active_bucket_config_intent(conn: sqlite3.Connection, scope_id: str, bucket: str, kind: str) -> None:
    rows = conn.execute(
        """
        SELECT target_json FROM operation_intents
        WHERE scope_id=? AND operation_type IN (?, ?) AND status IN ('pending','needs_review')
        """,
        (scope_id, f"bucket.{kind}.put", f"bucket.{kind}.rollback"),
    ).fetchall()
    for row in rows:
        try:
            target = json.loads(row["target_json"] if isinstance(row, sqlite3.Row) else row[0])
        except Exception:
            continue
        if target.get("bucket") == bucket and target.get("kind") == kind:
            raise ConflictError("bucket config has an unresolved pending or needs_review intent")


def _validate_bucket_config(kind: str, config: Mapping[str, Any]) -> None:
    if not isinstance(config, Mapping):
        raise ValidationError("bucket config must be an object")
    if kind == "cors":
        allowed_top = {"CORSRules", "AllowedOrigins", "AllowedMethods", "AllowedHeaders", "ExposeHeaders", "MaxAgeSeconds"}
        _reject_unknown_fields(config, allowed_top)
        if "CORSRules" in config:
            if not isinstance(config["CORSRules"], list):
                raise ValidationError("CORSRules must be a list")
            allowed_rule = {"ID", "AllowedOrigins", "AllowedMethods", "AllowedHeaders", "ExposeHeaders", "MaxAgeSeconds"}
            for rule in config["CORSRules"]:
                if not isinstance(rule, Mapping):
                    raise ValidationError("CORSRules entries must be objects")
                _reject_unknown_fields(rule, allowed_rule)
    elif kind == "lifecycle":
        _reject_unknown_fields(config, {"Rules"})
        rules = config.get("Rules", [])
        if not isinstance(rules, list):
            raise ValidationError("lifecycle Rules must be a list")
        allowed_rule = {"ID", "Filter", "Prefix", "Status", "Expiration", "NoncurrentVersionExpiration", "AbortIncompleteMultipartUpload"}
        for rule in rules:
            if not isinstance(rule, Mapping):
                raise ValidationError("lifecycle Rules entries must be objects")
            if "Transition" in rule or "Transitions" in rule or "NoncurrentVersionTransitions" in rule:
                raise CapabilityUnsupported("lifecycle transition rules are not verified for this backend")
            _reject_unknown_fields(rule, allowed_rule)
    elif kind == "policy":
        _reject_unknown_fields(config, {"Version", "Id", "Statement"})


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in value.keys() if key not in allowed)
    if unknown:
        raise ValidationError("unsupported bucket config field: " + ", ".join(unknown))


def register(app: Any, conn_provider: Callable[[], sqlite3.Connection], adapter_provider: Callable[[], Any]) -> Any:
    if router is None:
        raise RuntimeError("FastAPI is not installed; use service functions directly")
    app.include_router(build_router(conn_provider, adapter_provider), prefix="/api/v1")
    return app


def build_router(conn_provider: Callable[[], sqlite3.Connection], adapter_provider: Callable[[], Any]) -> Any:
    if APIRouter is None:
        raise RuntimeError("FastAPI is not installed")
    api = APIRouter()

    @api.get("/projects/{project_id}/presets")
    def route_list_presets(project_id: str) -> list[dict[str, Any]]:
        return list_presets(conn_provider(), project_id)

    @api.post("/projects/{project_id}/presets")
    def route_create_preset(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return create_preset(conn_provider(), project_id=project_id, name=body["name"], params=body["params"], actor_id=body.get("actor_id"))

    @api.post("/scopes/{scope_id}/derived-variants")
    def route_create_variant(scope_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return create_derived_variant_intent(conn_provider(), scope_id=scope_id, **body)

    @api.post("/scopes/{scope_id}/uploads/multipart")
    def route_start_upload(scope_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return start_multipart_upload(conn_provider(), adapter_provider(), scope_id=scope_id, bucket=body["bucket"], key=body["key"], metadata=body.get("metadata"))

    @api.post("/uploads/multipart/{upload_row_id}/complete")
    def route_complete_upload(upload_row_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return complete_multipart_upload(conn_provider(), adapter_provider(), upload_row_id=upload_row_id, parts=body.get("parts", []))

    @api.post("/uploads/multipart/{upload_row_id}/abort")
    def route_abort_upload(upload_row_id: str) -> dict[str, Any]:
        return abort_multipart_upload(conn_provider(), adapter_provider(), upload_row_id=upload_row_id)

    return api


try:  # FastAPI is optional for the current stdlib test environment.
    from fastapi import APIRouter
except Exception:  # pragma: no cover - exercised implicitly when FastAPI is absent.
    APIRouter = None
    router = None
else:
    router = APIRouter()

