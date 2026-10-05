"""Authenticated enhancement API integration for tasks 13-22."""

from __future__ import annotations

import json
import hashlib
from typing import Any, Mapping

from fastapi import APIRouter, Request

from . import catalog, core, db, enhancements, jobs, storage
from .security import AppError, current_user, mutation


router = APIRouter(prefix="/api/v1")


class S3EnhancementAdapter:
    def __init__(self, s3: Any):
        self.s3 = s3

    def head_object(self, bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
        kwargs = {"Bucket": bucket, "Key": key}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            return _identity(bucket, key, self.s3.head_object(**kwargs))
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def get_object_bytes(self, bucket: str, key: str, version_id: str | None = None, max_bytes: int = 32 * 1024 * 1024) -> bytes:
        kwargs = {"Bucket": bucket, "Key": key}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            response = self.s3.get_object(**kwargs)
            body = response["Body"]
            try:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = body.read(min(1024 * 1024, max_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise AppError("RESOURCE_LIMIT_EXCEEDED", "目标派生读取超过 32MiB 校验预算。", 429)
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                body.close()
        except AppError:
            raise
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_object(self, bucket: str, key: str, data: bytes, metadata: Mapping[str, str], content_type: str) -> None:
        try:
            self.s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                Metadata=dict(metadata),
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        target_bucket: str,
        target_key: str,
        source_version_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        copy_source: dict[str, Any] = {"Bucket": source_bucket, "Key": source_key}
        if _version_id_provided(source_version_id):
            copy_source["VersionId"] = source_version_id
        try:
            try:
                self.s3.head_object(Bucket=target_bucket, Key=target_key)
            except Exception as exc:
                sanitized = storage.sanitize_storage_exception(exc)
                if sanitized.status != 404:
                    raise sanitized from exc
            else:
                raise AppError("TARGET_EXISTS", "目标对象已存在；不会覆盖。", 409)
            kwargs: dict[str, Any] = {"Bucket": target_bucket, "Key": target_key, "CopySource": copy_source}
            if metadata:
                kwargs["Metadata"] = dict(metadata)
                kwargs["MetadataDirective"] = "REPLACE"
            self.s3.copy_object(**kwargs)
        except AppError:
            raise
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def delete_object(self, bucket: str, key: str, version_id: str | None = None) -> None:
        kwargs = {"Bucket": bucket, "Key": key}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            self.s3.delete_object(**kwargs)
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def create_multipart_upload(self, bucket: str, key: str, metadata: Mapping[str, str]) -> str:
        try:
            result = self.s3.create_multipart_upload(Bucket=bucket, Key=key, Metadata=dict(metadata))
            return result["UploadId"]
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def upload_part(self, bucket: str, key: str, upload_id: str, part_number: int, data: bytes) -> dict[str, Any]:
        try:
            result = self.s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=data)
            return {"part_number": part_number, "etag": result.get("ETag"), "size": len(data)}
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def complete_multipart_upload(self, bucket: str, key: str, upload_id: str, parts: list[Mapping[str, Any]]) -> None:
        payload = {
            "Parts": [
                {"PartNumber": int(part["part_number"]), "ETag": part["etag"]}
                for part in parts
            ]
        }
        try:
            self.s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload=payload, IfNoneMatch="*")
        except TypeError as exc:
            raise enhancements.CapabilityUnsupported("multipart completion requires IfNoneMatch support to prevent overwrite") from exc
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        try:
            self.s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def list_object_versions(self, bucket: str, key: str) -> list[dict[str, Any]]:
        try:
            response = self.s3.list_object_versions(Bucket=bucket, Prefix=key)
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc
        versions = []
        for item in response.get("Versions", []):
            if item.get("Key") == key:
                versions.append(_identity(bucket, key, item))
        for item in response.get("DeleteMarkers", []):
            if item.get("Key") == key:
                marker = _identity(bucket, key, item)
                marker["is_delete_marker"] = True
                versions.append(marker)
        return versions

    def get_object_tags(self, bucket: str, key: str, version_id: str | None = None) -> dict[str, str]:
        kwargs = {"Bucket": bucket, "Key": key}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            return {tag["Key"]: tag["Value"] for tag in self.s3.get_object_tagging(**kwargs).get("TagSet", [])}
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_object_tags(self, bucket: str, key: str, tags: Mapping[str, str], version_id: str | None = None) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Tagging": {"TagSet": [{"Key": key, "Value": value} for key, value in tags.items()]},
        }
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            self.s3.put_object_tagging(**kwargs)
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def get_object_retention(self, bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
        kwargs = {"Bucket": bucket, "Key": key}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            return self.s3.get_object_retention(**kwargs).get("Retention", {})
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def get_object_legal_hold(self, bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
        kwargs = {"Bucket": bucket, "Key": key}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            return self.s3.get_object_legal_hold(**kwargs).get("LegalHold", {})
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_object_retention(self, bucket: str, key: str, retention: Mapping[str, Any], version_id: str | None = None) -> None:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key, "Retention": dict(retention)}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            self.s3.put_object_retention(**kwargs)
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_object_legal_hold(self, bucket: str, key: str, legal_hold: Mapping[str, Any], version_id: str | None = None) -> None:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key, "LegalHold": dict(legal_hold)}
        if _version_id_provided(version_id):
            kwargs["VersionId"] = version_id
        try:
            self.s3.put_object_legal_hold(**kwargs)
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def get_bucket_cors(self, bucket: str) -> dict[str, Any]:
        try:
            response = self.s3.get_bucket_cors(Bucket=bucket)
            if "CORSRules" in response:
                return {"CORSRules": response["CORSRules"]}
            return {key: response[key] for key in ("AllowedOrigins", "AllowedMethods", "AllowedHeaders", "ExposeHeaders", "MaxAgeSeconds") if key in response}
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_bucket_cors(self, bucket: str, config: Mapping[str, Any]) -> None:
        try:
            self.s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=dict(config))
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def get_bucket_lifecycle(self, bucket: str) -> dict[str, Any]:
        try:
            response = self.s3.get_bucket_lifecycle_configuration(Bucket=bucket)
            return {"Rules": response.get("Rules", [])}
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_bucket_lifecycle(self, bucket: str, config: Mapping[str, Any]) -> None:
        try:
            self.s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=dict(config))
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def get_bucket_policy(self, bucket: str) -> dict[str, Any]:
        try:
            response = self.s3.get_bucket_policy(Bucket=bucket)
            return json.loads(response.get("Policy") or "{}")
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc

    def put_bucket_policy(self, bucket: str, config: Mapping[str, Any]) -> None:
        try:
            self.s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(dict(config), separators=(",", ":"), sort_keys=True))
        except Exception as exc:
            raise storage.sanitize_storage_exception(exc) from exc


def _identity(bucket: str, key: str, data: Mapping[str, Any]) -> dict[str, Any]:
    modified = data.get("LastModified") or data.get("last_modified")
    if hasattr(modified, "strftime"):
        modified = modified.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "bucket": bucket,
        "key": key,
        "version_id": data.get("VersionId") or data.get("version_id"),
        "etag": data.get("ETag") or data.get("etag"),
        "size": data.get("ContentLength") or data.get("Size") or data.get("size"),
        "checksum_sha256": data.get("ChecksumSHA256") or data.get("checksum_sha256"),
        "last_modified": modified,
        "content_type": data.get("ContentType") or data.get("content_type"),
        "metadata": data.get("Metadata") or data.get("metadata") or {},
        "is_delete_marker": bool(data.get("IsLatest") is False and data.get("DeleteMarker")),
    }


def _version_id_provided(version_id: str | None) -> bool:
    return version_id is not None and str(version_id) != ""


def _normalize_preset_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    params = dict(raw)
    if "mode" not in params:
        params["mode"] = params.get("fit", "fit")
    if "width" not in params and "long_edge" in params:
        params["width"] = int(params["long_edge"])
    if "height" not in params and "long_edge" in params:
        params["height"] = int(params["long_edge"])
    params.setdefault("quality", 85)
    params.setdefault("format", "webp")
    params.setdefault("alpha_policy", "preserve")
    params.setdefault("orientation", "auto")
    params.setdefault("color_policy", params.get("color", "srgb"))
    params.setdefault("metadata_policy", "strip")
    if params["mode"] == "fill":
        params.setdefault("focal_point", {"x": 0.5, "y": 0.5})
    return params


def _imaging_params(preset_params: Mapping[str, Any]) -> dict[str, Any]:
    mapped = dict(preset_params)
    mapped["fit"] = mapped.get("mode", "fit")
    mapped["color"] = mapped.get("color_policy", "srgb")
    if mapped.get("focal_point"):
        fp = mapped["focal_point"]
        mapped["focal"] = [fp.get("x", 0.5), fp.get("y", 0.5)]
    if mapped.get("alpha_policy") == "flatten":
        mapped.setdefault("background", "#ffffff")
    watermark = mapped.get("watermark")
    if isinstance(watermark, Mapping):
        mapped["watermark"] = watermark.get("text")
    return mapped


def _mime_for_format(output_format: str) -> str:
    return {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[str(output_format).lower()]


def _require_bucket_admin_scope(scope: Mapping[str, Any]) -> None:
    if scope.get("prefix"):
        raise AppError("FORBIDDEN_SCOPE", "桶级配置只允许 prefix 为空的整桶管理 scope。", 403)


def _assert_head_matches_indexed_object(scope: Mapping[str, Any], obj: Mapping[str, Any], head: Mapping[str, Any]) -> None:
    observed_revision = core.make_revision(
        scope["bucket"],
        obj["key"],
        {
            "etag": head.get("etag"),
            "size": head.get("size"),
            "last_modified": head.get("last_modified"),
            "version_id": obj.get("version_id"),
        },
    )
    if observed_revision != obj.get("revision"):
        raise AppError("OBJECT_REVISION_CHANGED", "Source object revision changed before metadata copy.", 409)

    if obj.get("checksum") and head.get("checksum_sha256") and obj["checksum"] != head["checksum_sha256"]:
        raise AppError("OBJECT_REVISION_CHANGED", "Source object checksum changed before metadata copy.", 409)


def _validate_manifest_entries(conn, scope: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> None:
    for entry in entries:
        if entry.get("bucket") and entry["bucket"] != scope["bucket"]:
            raise AppError("MANIFEST_SCOPE_MISMATCH", "manifest entry bucket 不属于当前 scope。", 422)
        if entry.get("key"):
            core.assert_key_in_scope(scope, entry["key"])
        object_id = entry.get("object_id")
        if object_id:
            obj = core.get_object(conn, scope["id"], object_id)
            if entry.get("key") and entry["key"] != obj["key"]:
                raise AppError("MANIFEST_OBJECT_MISMATCH", "manifest entry key 与 object_id 不一致。", 422)
            if entry.get("version_id") and entry["version_id"] != obj.get("version_id"):
                raise AppError("MANIFEST_OBJECT_MISMATCH", "manifest entry version 与 object_id 不一致。", 422)
        elif not entry.get("key"):
            raise AppError("MANIFEST_ENTRY_INCOMPLETE", "manifest entry 需要 object_id 或 key。", 422)


def _upload_for_scope(conn, scope: Mapping[str, Any], upload_id: str) -> dict[str, Any]:
    upload = enhancements.get_multipart_upload(conn, upload_id)
    if upload["scope_id"] != scope["id"]:
        raise AppError("UPLOAD_NOT_FOUND", "上传不存在。", 404)
    core.assert_key_in_scope(scope, upload["key"])
    return upload


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _latest_scan_complete(conn, scope_id: str) -> bool:
    if not _table_exists(conn, "scan_runs"):
        return False
    row = conn.execute(
        "SELECT enumeration FROM scan_runs WHERE scope_id=? ORDER BY completed_at DESC, started_at DESC LIMIT 1",
        (scope_id,),
    ).fetchone()
    return bool(row and row["enumeration"] == "complete")


def _computed_capacity_snapshot(conn, scope_id: str) -> dict[str, Any]:
    sample_complete = _latest_scan_complete(conn, scope_id)
    source_bytes = conn.execute(
        "SELECT COALESCE(SUM(size),0) FROM objects WHERE scope_id=? AND is_current=1 AND presence='present'",
        (scope_id,),
    ).fetchone()[0]
    derived_bytes = None
    if _table_exists(conn, "derived_variants"):
        row = conn.execute(
            """
            SELECT SUM(output_size) AS bytes,
                   SUM(CASE WHEN output_size IS NULL THEN 1 ELSE 0 END) AS unknown_outputs
            FROM derived_variants
            WHERE scope_id=? AND status='succeeded'
            """,
            (scope_id,),
        ).fetchone()
        derived_bytes = None if row["unknown_outputs"] else int(row["bytes"] or 0)
    return {
        "source_bytes": int(source_bytes or 0) if sample_complete else None,
        "derived_bytes": derived_bytes,
        "temporary_bytes": None,
        "version_bytes": None,
        "unknown_bytes": None,
        "sample_complete": sample_complete,
        "coverage": "complete_enumeration" if sample_complete else "partial_index",
    }


def _manual_capacity_public(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(snapshot)
    item["source"] = "administrator_declared"
    item["coverage"] = item.get("coverage") or ("complete_enumeration" if item.get("sample_complete") else "partial_index")
    item["physical_disk_bytes"] = None
    item["unmeasured"] = [
        name
        for name in ("source_bytes", "derived_bytes", "temporary_bytes", "version_bytes", "unknown_bytes", "physical_disk_bytes")
        if item.get(name) is None
    ]
    return item


def _auto_capacity_public(row: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(row["result_json"])
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "source": "auto_scan",
        "created_at": row["created_at"],
        "scope_id": result.get("scope_id"),
        "scan_id": result.get("scan_id") or row["id"],
        "current_logical_bytes": result.get("current_logical_bytes"),
        "object_count": result.get("object_count"),
        "by_format": result.get("by_format") or result.get("byformat") or {},
        "by_bucket": result.get("by_bucket") or {},
        "coverage": result.get("coverage") or "unknown",
        "version_bytes": result.get("version_bytes") if result.get("version_bytes") is not None else None,
        "physical_disk_bytes": result.get("physical_disk_bytes") if result.get("physical_disk_bytes") is not None else None,
        "private_cache_bytes": result.get("private_cache_bytes"),
        "unmeasured": [name for name in ("version_bytes", "physical_disk_bytes") if result.get(name) is None],
    }


def _job_by_id(conn, job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return jobs.public(dict(row)) if row else None


def _batch_public(conn, batch: Mapping[str, Any]) -> dict[str, Any]:
    items = []
    counts: dict[str, int] = {}
    for item in batch["items"]:
        current = dict(item)
        variant_id = current.get("variant_id")
        if variant_id:
            try:
                variant = enhancements.get_derived_variant(conn, variant_id)
                current["variant"] = variant
                current["state"] = variant["status"]
            except Exception:
                current["state"] = "unknown"
        current["job"] = _job_by_id(conn, current.get("job_id"))
        counts[current.get("state", "planned")] = counts.get(current.get("state", "planned"), 0) + 1
        items.append(current)
    return dict(batch) | {"items": items, "counts": counts, "known_count": len(items)}


def _raise(exc: Exception) -> None:
    if isinstance(exc, AppError):
        raise exc
    if isinstance(exc, storage.StorageError):
        raise AppError(exc.code, str(exc), exc.status) from exc
    if isinstance(exc, enhancements.EnhancementError):
        status = 422
        if isinstance(exc, enhancements.NotFound):
            status = 404
        elif isinstance(exc, enhancements.ConflictError):
            status = 409
        elif isinstance(exc, enhancements.ProtectedError):
            status = 423
        elif isinstance(exc, enhancements.PermissionDenied):
            status = 403
        elif isinstance(exc, enhancements.CapabilityUnsupported):
            status = 501
        raise AppError(exc.code, str(exc), status) from exc
    raise exc


def _adapter_for_scope(conn, settings, scope: Mapping[str, Any]) -> S3EnhancementAdapter:
    return S3EnhancementAdapter(catalog.s3_for(conn, scope, settings))


def _require_scope(request: Request, scope_id: str, *, write: bool = False, manage_bucket: bool = False) -> dict[str, Any]:
    scope = core.require_scope(request, scope_id, write=write)
    if manage_bucket:
        with db.connect(request.app.state.settings) as conn:
            scope = core.ensure_scope_permission(conn, scope_id, "config:write")
    return scope


@router.get("/projects/{project_id}/presets")
def list_presets(request: Request, project_id: str):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        return {"items": enhancements.list_presets(conn, project_id)}


@router.post("/projects/{project_id}/presets")
def create_preset(request: Request, project_id: str, body: dict[str, Any]):
    user = mutation(request)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        try:
            result = enhancements.create_preset(conn, project_id=project_id, name=body["name"], params=_normalize_preset_params(body.get("params", body)), actor_id=user.id)
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.get("/projects/{project_id}/groups")
def list_groups(request: Request, project_id: str):
    current_user(request)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        rows = conn.execute("SELECT * FROM image_groups WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        return {"items": [dict(row) for row in rows]}


@router.post("/projects/{project_id}/groups")
def create_group(request: Request, project_id: str, body: dict[str, Any]):
    mutation(request)
    with db.connect(request.app.state.settings) as conn:
        try:
            result = enhancements.create_group(conn, project_id=project_id, name=body["name"], source=body.get("source", "manual"))
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/projects/{project_id}/groups/{group_id}/members")
def add_group_member(request: Request, project_id: str, group_id: str, body: dict[str, Any]):
    mutation(request)
    with db.connect(request.app.state.settings) as conn:
        row = conn.execute("SELECT id FROM image_groups WHERE id=? AND project_id=?", (group_id, project_id)).fetchone()
        if not row:
            raise AppError("GROUP_NOT_FOUND", "分组不存在。", 404)
        enhancements.add_group_member(conn, group_id=group_id, object_id=body["object_id"], relation_source=body.get("source", "manual"))
        conn.commit()
        return {"ok": True}


@router.post("/scopes/{scope_id}/derived-variants")
def submit_derived_variant(request: Request, scope_id: str, body: dict[str, Any]):
    user = mutation(request)
    scope = _require_scope(request, scope_id)
    object_id = body["object_id"]
    output_scope_id = body.get("output_scope_id") or scope_id
    with db.connect(request.app.state.settings) as conn:
        source = core.get_object(conn, scope_id, object_id)
        output_scope = core.ensure_scope_permission(conn, output_scope_id, "object:write")
        enhancements.initialize(conn)
        if output_scope["project_id"] != scope["project_id"]:
            raise AppError("FORBIDDEN_SCOPE", "派生输出 scope 必须属于同一项目。", 403)
        preset_row = conn.execute("SELECT project_id FROM enhancement_presets WHERE id=?", (body["preset_id"],)).fetchone()
        if not preset_row or preset_row["project_id"] != scope["project_id"]:
            raise AppError("PRESET_NOT_FOUND", "preset 不存在或不属于当前项目。", 404)
        if body.get("output_key"):
            core.assert_key_in_scope(output_scope, body["output_key"])
        try:
            variant = enhancements.create_derived_variant_intent(
                conn,
                project_id=scope["project_id"],
                scope_id=scope_id,
                source_object_id=object_id,
                source_revision=source["revision"],
                preset_id=body["preset_id"],
                output_scope_id=output_scope_id,
                output_bucket=output_scope["bucket"],
                output_key=body.get("output_key"),
                actor_id=user.id,
            )
            job = jobs.submit_job(
                conn,
                scope_id,
                "derived_variant",
                {
                    "variant_id": variant["id"],
                    "source_bucket": scope["bucket"],
                    "source_key": source["key"],
                    "source_version_id": source.get("version_id"),
                    "source_revision": source["revision"],
                },
                actor=user.id,
                idempotency_key=request.headers.get("Idempotency-Key") or body.get("idempotency_key"),
                effect_class="remote_mutating",
            )
            conn.commit()
            return {"variant": variant, "job": job}
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/derived-variant-batches")
def submit_derived_variant_batch(request: Request, scope_id: str, body: dict[str, Any]):
    user = mutation(request)
    scope = _require_scope(request, scope_id)
    output_scope_id = body.get("output_scope_id") or scope_id
    object_ids = list(body.get("object_ids") or [])
    preset_ids = list(body.get("preset_ids") or [])
    if not object_ids or not preset_ids:
        raise AppError("INVALID_BATCH", "object_ids and preset_ids are required.", 422)
    planned_count = len(object_ids) * len(preset_ids)
    if planned_count > 200:
        raise AppError("BATCH_TOO_LARGE", "derived variant batch is limited to 200 target pairs.", 422)
    idempotency_key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    if not idempotency_key:
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key or idempotency_key is required for derived batches.", 400)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        output_scope = core.ensure_scope_permission(conn, output_scope_id, "object:write")
        if output_scope["project_id"] != scope["project_id"]:
            raise AppError("FORBIDDEN_SCOPE", "output_scope_id must belong to the same project.", 403)
        preset_rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, project_id, name, version, params_json FROM enhancement_presets WHERE id IN (%s)" % ",".join("?" for _ in preset_ids),
                preset_ids,
            ).fetchall()
        }
        if len(preset_rows) != len(set(preset_ids)) or any(row["project_id"] != scope["project_id"] for row in preset_rows.values()):
            raise AppError("PRESET_NOT_FOUND", "all presets must exist in the current project.", 404)
        sources = []
        for object_id in object_ids:
            source = core.get_object(conn, scope_id, object_id)
            sources.append(source)
        target_map = {(item["object_id"], item["preset_id"]): item["output_key"] for item in body.get("targets", [])}
        items = []
        for source in sources:
            for preset_id in preset_ids:
                preset_params = json.loads(preset_rows[preset_id]["params_json"])
                output_key = target_map.get((source["id"], preset_id))
                if not output_key:
                    seed = enhancements.sha256_text(enhancements.canonical_json({"object_id": source["id"], "source_revision": source["revision"], "preset_id": preset_id, "output_scope_id": output_scope_id}))
                    output_key = f"{body.get('output_prefix', 'derived').strip('/')}/{seed[:32]}.{preset_params['format']}"
                core.assert_key_in_scope(output_scope, output_key)
                items.append(
                    {
                        "object_id": source["id"],
                        "source_revision": source["revision"],
                        "preset_id": preset_id,
                        "preset_version": preset_rows[preset_id]["version"],
                        "output_scope_id": output_scope_id,
                        "output_key": output_key,
                        "state": "planned",
                    }
                )
        try:
            existing, _request_hash = enhancements.create_or_get_batch(
                conn,
                scope_id=scope_id,
                project_id=scope["project_id"],
                idempotency_key=idempotency_key,
                request_body=body,
                planned_count=planned_count,
                items=items,
            )
            if existing:
                return _batch_public(conn, existing)
            batch_row = conn.execute("SELECT * FROM derived_variant_batches WHERE scope_id=? AND idempotency_key=?", (scope_id, idempotency_key)).fetchone()
            batch_id = batch_row["id"]
            queued = []
            for item in items:
                variant = enhancements.create_derived_variant_intent(
                    conn,
                    project_id=scope["project_id"],
                    scope_id=scope_id,
                    source_object_id=item["object_id"],
                    source_revision=item["source_revision"],
                    preset_id=item["preset_id"],
                    output_scope_id=output_scope_id,
                    output_bucket=output_scope["bucket"],
                    output_key=item["output_key"],
                    actor_id=user.id,
                )
                job = jobs.submit_job(
                    conn,
                    scope_id,
                    "derived_variant",
                    {"variant_id": variant["id"], "source_revision": item["source_revision"]},
                    actor=user.id,
                    idempotency_key=f"{batch_id}:{item['object_id']}:{item['preset_id']}",
                    effect_class="remote_mutating",
                )
                queued.append(dict(item) | {"variant_id": variant["id"], "job_id": job["id"], "state": variant["status"]})
            batch = enhancements.update_batch_items(conn, batch_id, status="queued", items=queued)
            conn.commit()
            return _batch_public(conn, batch)
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/derived-variant-batches/{batch_id}")
def get_derived_variant_batch(request: Request, scope_id: str, batch_id: str):
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            batch = enhancements.get_derived_batch(conn, batch_id)
            if batch["scope_id"] != scope_id:
                raise AppError("BATCH_NOT_FOUND", "derived variant batch was not found in this scope.", 404)
            return _batch_public(conn, batch)
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/derived-variants")
def list_derived_variants(request: Request, scope_id: str):
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        rows = conn.execute("SELECT id FROM derived_variants WHERE scope_id=? ORDER BY created_at DESC LIMIT 200", (scope_id,)).fetchall()
        return {"items": [enhancements.get_derived_variant(conn, row["id"]) for row in rows]}


@router.get("/scopes/{scope_id}/derived-variants/health")
def derived_health(request: Request, scope_id: str):
    scope = _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        rows = conn.execute(
            "SELECT id FROM derived_variants WHERE scope_id=? ORDER BY updated_at DESC LIMIT 60",
            (scope_id,),
        ).fetchall()
        checks = []
        counts: dict[str, int] = {}
        output_adapters: dict[str, S3EnhancementAdapter] = {}
        for index, row in enumerate(rows[:20]):
            variant = enhancements.get_derived_variant(conn, row["id"])
            check = _check_derived_variant_health(conn, request.app.state.settings, scope, variant, output_adapters)
            check["position"] = "console_server"
            check["index"] = index
            checks.append(check)
            counts[check["state"]] = counts.get(check["state"], 0) + 1
        base = enhancements.derived_health(conn, scope_id=scope_id)
        return base | {
            "checks": checks,
            "counts_by_known_state": counts,
            "checked_count": len(checks),
            "known_count": len(rows),
            "publication_state": "not_connected",
            "external_relationships": "unknown",
        }


def _check_derived_variant_health(conn, settings: Any, scope: Mapping[str, Any], variant: Mapping[str, Any], adapters: dict[str, S3EnhancementAdapter]) -> dict[str, Any]:
    observed_at = jobs.now()
    state = "healthy" if variant["status"] == "succeeded" else variant["status"]
    binding_outdated = False
    reasons: list[str] = []
    source_state: dict[str, Any] = {"stored_input_revision": variant["source_revision"]}
    try:
        source = core.get_object(conn, variant["scope_id"], variant["source_object_id"])
        source_scope = core.ensure_scope_permission(conn, variant["scope_id"], "object:read")
        current_row = conn.execute(
            "SELECT * FROM objects WHERE scope_id=? AND key=? AND is_current=1 AND presence='present' ORDER BY observed_at DESC LIMIT 1",
            (variant["scope_id"], source["key"]),
        ).fetchone()
        current_revision = current_row["revision"] if current_row else source["revision"]
        source_state["current_indexed_revision"] = current_revision
        try:
            source_adapter = _adapter_for_scope(conn, settings, source_scope)
            current_head = source_adapter.head_object(source_scope["bucket"], source["key"])
            current_observed_revision = core.make_revision(
                source_scope["bucket"],
                source["key"],
                {
                    "etag": current_head.get("etag"),
                    "size": current_head.get("size"),
                    "last_modified": current_head.get("last_modified"),
                    "version_id": current_head.get("version_id"),
                },
            )
            source_state["current_observed_revision"] = current_observed_revision
            source_state["current_observed_at"] = observed_at
            if current_observed_revision != variant["source_revision"]:
                current_revision = current_observed_revision
        except storage.StorageError as exc:
            if exc.status in {401, 403}:
                source_state["current_observed_revision"] = None
                reasons.append("current source HEAD permission denied")
            elif exc.status == 404 or exc.code == "OBJECT_NOT_FOUND":
                current_revision = "__missing_current_source__"
                reasons.append("current source object missing")
            else:
                source_state["current_observed_revision"] = None
                reasons.append(f"current source HEAD failed: {exc.code}")
        if current_revision != variant["source_revision"]:
            state = "outdated_source"
            binding_outdated = True
            reasons.append("current source revision differs from stored input revision")
    except Exception as exc:
        state = "unknown"
        reasons.append(f"source check unavailable: {exc}")
    preset = conn.execute("SELECT project_id,name,version FROM enhancement_presets WHERE id=?", (variant["preset_id"],)).fetchone()
    if preset:
        newest = conn.execute(
            "SELECT MAX(version) FROM enhancement_presets WHERE project_id=? AND name=?",
            (preset["project_id"], preset["name"]),
        ).fetchone()[0]
        if newest and int(newest) > int(preset["version"]):
            state = "outdated_preset"
            binding_outdated = True
            reasons.append("newer preset version exists for same project/name")
    else:
        state = "unknown"
        reasons.append("preset missing")
    try:
        output_scope = core.ensure_scope_permission(conn, variant["output_scope_id"], "object:read")
        if output_scope["project_id"] != scope["project_id"]:
            state = "unknown"
            reasons.append("output scope project mismatch")
        adapter = adapters.get(output_scope["id"])
        if adapter is None:
            adapter = _adapter_for_scope(conn, settings, output_scope)
            adapters[output_scope["id"]] = adapter
        head = adapter.head_object(output_scope["bucket"], variant["output_key"])
        if variant.get("output_mime") and head.get("content_type") and head["content_type"] != variant["output_mime"]:
            if not binding_outdated:
                state = "corrupt_output"
            reasons.append("output MIME differs from recorded variant")
        expected_sha = variant.get("output_sha256")
        if expected_sha:
            observed_sha = head.get("checksum_sha256")
            if not observed_sha:
                if head.get("size") and int(head["size"]) > 32 * 1024 * 1024:
                    state = "unknown"
                    reasons.append("output checksum unknown and object exceeds 32MiB health budget")
                else:
                    observed_sha = hashlib.sha256(adapter.get_object_bytes(output_scope["bucket"], variant["output_key"], head.get("version_id"))).hexdigest()
            if observed_sha and observed_sha != expected_sha:
                if not binding_outdated:
                    state = "corrupt_output"
                reasons.append("output checksum mismatch")
        metadata = {str(k).lower(): str(v) for k, v in dict(head.get("metadata") or {}).items()}
        if metadata and metadata.get("swc-variant-id") != variant["id"]:
            if not binding_outdated:
                state = "corrupt_output"
            reasons.append("output ownership metadata mismatch")
    except storage.StorageError as exc:
        if exc.status == 404 or exc.code == "OBJECT_NOT_FOUND":
            state = "missing_output"
            reasons.append("output object missing")
        elif exc.status in {401, 403}:
            state = "unknown"
            reasons.append("output permission denied")
        else:
            state = "unknown"
            reasons.append(f"output storage check failed: {exc.code}")
    except KeyError:
        state = "missing_output"
        reasons.append("output object missing")
    except Exception as exc:
        state = "unknown"
        reasons.append(f"output check unavailable: {exc}")
    return {"variant_id": variant["id"], "state": state, "reasons": reasons, "observed_at": observed_at, **source_state}


@router.post("/scopes/{scope_id}/uploads/multipart")
def multipart_init(request: Request, scope_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    key = body["key"]
    core.assert_key_in_scope(scope, key)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            result = enhancements.start_multipart_upload(conn, adapter, scope_id=scope_id, bucket=scope["bucket"], key=key, metadata=body.get("metadata", {}))
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/uploads/multipart/{upload_id}/parts")
async def multipart_part(request: Request, scope_id: str, upload_id: str, part_number: int):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    if part_number < 1 or part_number > 10000:
        raise AppError("INVALID_PART_NUMBER", "part_number 必须在 1 到 10000 之间。", 422)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > 64 * 1024 * 1024:
            raise AppError("INVALID_PART_SIZE", "part 大小必须在 1B 到 64MiB 之间。", 422)
        if chunk:
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise AppError("INVALID_PART_SIZE", "part 大小必须在 1B 到 64MiB 之间。", 422)
    with db.connect(request.app.state.settings) as conn:
        upload = _upload_for_scope(conn, scope, upload_id)
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        return adapter.upload_part(scope["bucket"], upload["key"], upload["upload_id"], part_number, data)


@router.post("/scopes/{scope_id}/uploads/multipart/{upload_id}/complete")
def multipart_complete(request: Request, scope_id: str, upload_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    with db.connect(request.app.state.settings) as conn:
        _upload_for_scope(conn, scope, upload_id)
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            result = enhancements.complete_multipart_upload(conn, adapter, upload_row_id=upload_id, parts=body.get("parts", []))
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/uploads/multipart/{upload_id}/abort")
def multipart_abort(request: Request, scope_id: str, upload_id: str):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    with db.connect(request.app.state.settings) as conn:
        _upload_for_scope(conn, scope, upload_id)
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            result = enhancements.abort_multipart_upload(conn, adapter, upload_row_id=upload_id)
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/objects/copy")
def copy_object(request: Request, scope_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    target_key = body["target_key"]
    core.assert_key_in_scope(scope, target_key)
    with db.connect(request.app.state.settings) as conn:
        source = core.get_object(conn, scope_id, body["object_id"])
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            result = enhancements.copy_object_safely(
                conn,
                adapter,
                scope_id=scope_id,
                source_bucket=scope["bucket"],
                source_key=source["key"],
                source_version_id=source.get("version_id"),
                expected_source_revision=source.get("revision"),
                target_bucket=scope["bucket"],
                target_key=target_key,
                metadata=body.get("metadata", {}),
            )
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/objects/{object_id}/metadata")
def get_metadata(request: Request, scope_id: str, object_id: str):
    scope = _require_scope(request, scope_id)
    obj = core.require_object(request, scope_id, object_id)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            head = adapter.head_object(scope["bucket"], obj["key"], obj.get("version_id"))
            return {
                "bucket": scope["bucket"],
                "key": obj["key"],
                "version_id": obj.get("version_id"),
                "metadata": head.get("metadata", {}),
                "content_type": head.get("content_type"),
                "etag": head.get("etag"),
                "size": head.get("size"),
                "original_unchanged": True,
            }
        except Exception as exc:
            _raise(exc)


@router.put("/scopes/{scope_id}/objects/{object_id}/metadata")
def put_metadata(request: Request, scope_id: str, object_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    obj = core.require_object(request, scope_id, object_id)
    target_key = body["target_key"]
    core.assert_key_in_scope(scope, target_key)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            current = adapter.head_object(scope["bucket"], obj["key"], obj.get("version_id"))
            _assert_head_matches_indexed_object(scope, obj, current)
            result = enhancements.copy_object_safely(
                conn,
                adapter,
                scope_id=scope_id,
                source_bucket=scope["bucket"],
                source_key=obj["key"],
                source_version_id=obj.get("version_id"),
                target_bucket=scope["bucket"],
                target_key=target_key,
                metadata=body.get("metadata", {}),
            )
            conn.commit()
            return {"status": result["status"], "original_unchanged": True, "target": result["target"], "source": result["source"]}
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/objects/move")
def move_object(request: Request, scope_id: str, body: dict[str, Any]):
    mutation(request)
    _require_scope(request, scope_id, write=True)
    raise AppError("PROTECTED_BY_REFERENCE", "外部源对象移动需要服务端注册的引用保护 adapter；客户端传入 guard 不能授权删除源对象。", 423)


@router.get("/scopes/{scope_id}/objects/{object_id}/versions")
def versions(request: Request, scope_id: str, object_id: str):
    scope = _require_scope(request, scope_id)
    obj = core.require_object(request, scope_id, object_id)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            return enhancements.list_object_versions(conn, adapter, scope_id=scope_id, bucket=scope["bucket"], key=obj["key"])
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/objects/{object_id}/restore")
def restore_version(request: Request, scope_id: str, object_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    obj = core.require_object(request, scope_id, object_id)
    target_key = body["target_key"]
    core.assert_key_in_scope(scope, target_key)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        try:
            result = enhancements.restore_object_version(conn, adapter, scope_id=scope_id, bucket=scope["bucket"], key=obj["key"], version_id=body["version_id"], target_key=target_key)
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/objects/{object_id}/tags")
def get_tags(request: Request, scope_id: str, object_id: str, version_id: str | None = None):
    scope = _require_scope(request, scope_id)
    obj = core.require_object(request, scope_id, object_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            return enhancements.get_object_tags(conn, _adapter_for_scope(conn, request.app.state.settings, scope), scope_id=scope_id, bucket=scope["bucket"], key=obj["key"], version_id=version_id)
        except Exception as exc:
            _raise(exc)


@router.put("/scopes/{scope_id}/objects/{object_id}/tags")
def put_tags(request: Request, scope_id: str, object_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    obj = core.require_object(request, scope_id, object_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            result = enhancements.set_object_tags(conn, _adapter_for_scope(conn, request.app.state.settings, scope), scope_id=scope_id, bucket=scope["bucket"], key=obj["key"], tags=body.get("tags", {}), version_id=body.get("version_id"))
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/objects/{object_id}/retention")
def retention(request: Request, scope_id: str, object_id: str, version_id: str | None = None):
    scope = _require_scope(request, scope_id)
    obj = core.require_object(request, scope_id, object_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            return enhancements.get_retention(conn, _adapter_for_scope(conn, request.app.state.settings, scope), bucket=scope["bucket"], key=obj["key"], version_id=version_id)
        except Exception as exc:
            _raise(exc)


@router.put("/scopes/{scope_id}/objects/{object_id}/retention")
@router.put("/scopes/{scope_id}/objects/{object_id}/legal-hold")
def retention_write(request: Request, scope_id: str, object_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, write=True)
    obj = core.require_object(request, scope_id, object_id)
    version_id = body.get("version_id") or obj.get("version_id")
    if not version_id or version_id == "null":
        raise AppError("UNSUPPORTED_CAPABILITY", "retention/legal hold 写入需要固定对象版本。", 501)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        enhancements.initialize(conn)
        capabilities = {}
        try:
            connection = core._connection_private(conn, scope["connection_id"])
            capabilities = json.loads(connection.get("capabilities_json") or "{}")
        except Exception:
            capabilities = {}
        capability_values = json.dumps(capabilities).lower()
        if "object_lock" in capabilities and capabilities.get("object_lock") not in ("supported", True):
            raise AppError("UNSUPPORTED_CAPABILITY", "当前连接未证明支持 object lock/retention 写入。", 501)
        if "retention" in body and "object_lock" not in capability_values and "retention" not in capability_values:
            raise AppError("UNSUPPORTED_CAPABILITY", "当前连接未证明支持 retention 写入。", 501)
        intent_id, _ = enhancements._operation_intent(
            conn,
            "object.retention.put",
            scope_id=scope_id,
            target={"bucket": scope["bucket"], "key": obj["key"], "version_id": version_id, "body": body},
        )
        conn.commit()
        try:
            if "retention" in body:
                adapter.put_object_retention(scope["bucket"], obj["key"], body["retention"], version_id)
            if "legal_hold" in body:
                adapter.put_object_legal_hold(scope["bucket"], obj["key"], body["legal_hold"], version_id)
            readback = enhancements.get_retention(conn, adapter, bucket=scope["bucket"], key=obj["key"], version_id=version_id)
            enhancements._finish_intent(conn, intent_id, "succeeded", result=readback)
            conn.commit()
            return readback
        except Exception as exc:
            enhancements._finish_intent(conn, intent_id, "failed", error=str(exc))
            conn.commit()
            _raise(exc)


@router.get("/scopes/{scope_id}/owned-objects/trash-preview")
def owned_trash_preview(request: Request, scope_id: str):
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        rows = conn.execute(
            """
            SELECT v.* FROM derived_variants v
            LEFT JOIN owned_object_trash t ON t.variant_id=v.id AND t.status='trashed'
            WHERE v.scope_id=? AND v.status='succeeded' AND t.id IS NULL
            ORDER BY v.updated_at DESC LIMIT 200
            """,
            (scope_id,),
        ).fetchall()
        return {
            "physical_delete_enabled": False,
            "source_auto_delete_enabled": False,
            "items": [enhancements.get_derived_variant(conn, row["id"]) for row in rows],
        }


@router.post("/scopes/{scope_id}/owned-objects/{variant_id}/trash")
def owned_trash_mark(request: Request, scope_id: str, variant_id: str, body: dict[str, Any] | None = None):
    user = mutation(request)
    _require_scope(request, scope_id, write=True)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        variant = enhancements.get_derived_variant(conn, variant_id)
        if variant["scope_id"] != scope_id or variant["status"] != "succeeded":
            raise AppError("OWNED_OBJECT_NOT_ELIGIBLE", "只能逻辑回收当前 scope 下已成功的面板派生对象。", 409)
        conn.execute(
            """
            INSERT INTO owned_object_trash(id,scope_id,variant_id,bucket,key,status,reason,actor_id,marked_at)
            VALUES(?,?,?,?,?,'trashed',?,?,?)
            ON CONFLICT(scope_id,variant_id) DO UPDATE SET status='trashed', reason=excluded.reason, actor_id=excluded.actor_id, marked_at=excluded.marked_at, restored_at=NULL
            """,
            (enhancements.new_id("trash"), scope_id, variant_id, variant["output_bucket"], variant["output_key"], (body or {}).get("reason", "manual logical trash"), user.id, jobs.now()),
        )
        conn.commit()
        return {"status": "trashed", "physical_delete_enabled": False, "variant_id": variant_id}


@router.post("/scopes/{scope_id}/owned-objects/{variant_id}/restore")
def owned_trash_restore(request: Request, scope_id: str, variant_id: str):
    mutation(request)
    _require_scope(request, scope_id, write=True)
    with db.connect(request.app.state.settings) as conn:
        enhancements.initialize(conn)
        row = conn.execute("SELECT * FROM owned_object_trash WHERE scope_id=? AND variant_id=? AND status='trashed'", (scope_id, variant_id)).fetchone()
        if not row:
            raise AppError("TRASH_RECORD_NOT_FOUND", "逻辑回收记录不存在。", 404)
        conn.execute("UPDATE owned_object_trash SET status='restored', restored_at=? WHERE id=?", (jobs.now(), row["id"]))
        conn.commit()
        return {"status": "restored", "variant_id": variant_id}


@router.get("/scopes/{scope_id}/bucket/{kind}")
def get_bucket_config(request: Request, scope_id: str, kind: str):
    scope = _require_scope(request, scope_id, manage_bucket=True)
    _require_bucket_admin_scope(scope)
    if kind not in enhancements.CONFIG_KINDS:
        raise AppError("INVALID_CONFIG_KIND", "未知桶配置类型。", 404)
    with db.connect(request.app.state.settings) as conn:
        adapter = _adapter_for_scope(conn, request.app.state.settings, scope)
        method = getattr(adapter, f"get_bucket_{kind}")
        config = method(scope["bucket"])
        return {"kind": kind, "bucket": scope["bucket"], "config": config, "current_hash": enhancements.sha256_text(enhancements.canonical_json(config)), "cas_supported": False}


@router.post("/scopes/{scope_id}/bucket/{kind}")
def put_bucket_config(request: Request, scope_id: str, kind: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, manage_bucket=True)
    _require_bucket_admin_scope(scope)
    with db.connect(request.app.state.settings) as conn:
        try:
            result = enhancements.update_bucket_config(
                conn,
                _adapter_for_scope(conn, request.app.state.settings, scope),
                scope_id=scope_id,
                bucket=scope["bucket"],
                kind=kind,
                new_config=body.get("config", body),
                actor_id=getattr(request.state.user, "id", None),
                expected_current_hash=body.get("expected_current_hash"),
                exclusive_writer_ack=body.get("exclusive_writer_ack") is True,
                admin_manage_bucket=True,
            )
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/bucket/{kind}/rollback")
def rollback_bucket_config(request: Request, scope_id: str, kind: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id, manage_bucket=True)
    _require_bucket_admin_scope(scope)
    with db.connect(request.app.state.settings) as conn:
        try:
            result = enhancements.rollback_bucket_config(
                conn,
                _adapter_for_scope(conn, request.app.state.settings, scope),
                scope_id=scope_id,
                bucket=scope["bucket"],
                kind=kind,
                snapshot_id=body["snapshot_id"],
                expected_current_hash=body.get("expected_current_hash"),
                exclusive_writer_ack=body.get("exclusive_writer_ack") is True,
                actor_id=getattr(request.state.user, "id", None),
                admin_manage_bucket=True,
            )
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/reference-manifests")
def reference_manifest(request: Request, scope_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            entries = body.get("entries", [])
            _validate_manifest_entries(conn, scope, entries)
            manifest = enhancements.start_reference_manifest(
                conn,
                scope_id=scope_id,
                schema_version=int(body.get("schema_version", 1)),
                coverage=body.get("coverage", {"scope_id": scope_id}),
                declared_count=int(body.get("declared_count", len(body.get("entries", [])))),
                declared_hash=body.get("declared_hash"),
                waterline=body.get("waterline"),
            )
            if entries:
                enhancements.import_reference_manifest_entries(conn, manifest_id=manifest["id"], entries=entries)
            if body.get("finalize", True):
                manifest = enhancements.finalize_reference_manifest(conn, manifest_id=manifest["id"])
            conn.commit()
            return manifest
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/reference-manifests/{manifest_id}/entries")
def reference_manifest_entries(request: Request, scope_id: str, manifest_id: str, body: dict[str, Any]):
    mutation(request)
    scope = _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            manifest = enhancements.get_reference_manifest(conn, manifest_id)
            if manifest["scope_id"] != scope_id:
                raise AppError("MANIFEST_NOT_FOUND", "manifest 不属于当前 scope。", 404)
            entries = body.get("entries", [])
            _validate_manifest_entries(conn, scope, entries)
            result = enhancements.import_reference_manifest_entries(conn, manifest_id=manifest_id, entries=entries)
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.post("/scopes/{scope_id}/reference-manifests/{manifest_id}/finalize")
def reference_manifest_finalize(request: Request, scope_id: str, manifest_id: str):
    mutation(request)
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            manifest = enhancements.get_reference_manifest(conn, manifest_id)
            if manifest["scope_id"] != scope_id:
                raise AppError("MANIFEST_NOT_FOUND", "manifest 不属于当前 scope。", 404)
            result = enhancements.finalize_reference_manifest(conn, manifest_id=manifest_id)
            conn.commit()
            return result
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/audits")
def audits(request: Request, scope_id: str):
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        rows = conn.execute(
            "SELECT * FROM enhancement_audit_events WHERE scope_id=? OR scope_id IS NULL ORDER BY created_at DESC LIMIT 200",
            (scope_id,),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}


@router.post("/cache/evict")
def cache_evict(request: Request, body: dict[str, Any]):
    mutation(request)
    return catalog.evict_cache(request.app.state.settings, max_bytes=body.get("max_bytes"))


@router.post("/scopes/{scope_id}/capacity-snapshots")
def capacity_snapshot(request: Request, scope_id: str, body: dict[str, Any]):
    mutation(request)
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            result = enhancements.add_capacity_snapshot(conn, scope_id=scope_id, **_computed_capacity_snapshot(conn, scope_id))
            conn.commit()
            return _manual_capacity_public(result)
        except Exception as exc:
            _raise(exc)


@router.get("/scopes/{scope_id}/capacity-trends")
def capacity_trends(request: Request, scope_id: str):
    scope = _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        auto_items = []
        if _table_exists(conn, "capacity_samples"):
            auto_rows = conn.execute(
                """
                SELECT * FROM capacity_samples
                WHERE project_id=? AND json_extract(result_json,'$.scope_id')=?
                ORDER BY created_at DESC LIMIT 200
                """,
                (scope["project_id"], scope_id),
            ).fetchall()
            auto_items = [_auto_capacity_public(row) for row in auto_rows]
        manual_rows = conn.execute("SELECT * FROM capacity_snapshots WHERE scope_id=? ORDER BY observed_at DESC, rowid DESC LIMIT 200", (scope_id,)).fetchall()
        manual_items = [_manual_capacity_public(enhancements.get_capacity_snapshot(conn, row["id"])) for row in manual_rows]
        return {"items": auto_items + manual_items, "auto_samples": auto_items, "manual_snapshots": manual_items}


@router.post("/scopes/{scope_id}/capacity-thresholds")
def capacity_thresholds(request: Request, scope_id: str, body: dict[str, Any]):
    mutation(request)
    _require_scope(request, scope_id)
    with db.connect(request.app.state.settings) as conn:
        try:
            return enhancements.capacity_thresholds(conn, scope_id=scope_id, thresholds=body.get("thresholds", body))
        except Exception as exc:
            _raise(exc)


def execute_variant_job(job: dict[str, Any], settings: Any) -> None:
    params = json.loads(job["params_json"])
    with db.connect(settings) as conn:
        enhancements.initialize(conn)
        variant = enhancements.get_derived_variant(conn, params["variant_id"])
        source = core.get_object(conn, variant["scope_id"], variant["source_object_id"])
        source_scope = core.ensure_scope_permission(conn, variant["scope_id"], "object:read")
        output_scope = core.ensure_scope_permission(conn, variant["output_scope_id"], "object:write")
        if source_scope["project_id"] != output_scope["project_id"] or source_scope["project_id"] != variant["project_id"]:
            raise AppError("FORBIDDEN_SCOPE", "派生输入和输出必须属于同一项目。", 403)
        preset_row = conn.execute("SELECT params_json FROM enhancement_presets WHERE id=? AND project_id=?", (variant["preset_id"], variant["project_id"])).fetchone()
        if not preset_row:
            raise AppError("PRESET_NOT_FOUND", "preset 不存在或不属于当前项目。", 404)
        preset_params = json.loads(preset_row["params_json"])
        source_adapter = _adapter_for_scope(conn, settings, source_scope)
        output_adapter = _adapter_for_scope(conn, settings, output_scope)

    _assert_job_current(settings, job)
    source_bytes = catalog.read_bound(source_adapter.s3, source_scope, source)
    properties, output = catalog.imaging.decode(source_bytes, params=_imaging_params(preset_params))
    if output is None:
        raise AppError("DERIVED_DECODE_FAILED", "派生处理没有生成输出。", 422, detail=properties)
    output_sha = hashlib.sha256(output).hexdigest()
    output_mime = properties.get("output_mime") or _mime_for_format(preset_params["format"])
    metadata = {
        "swc-variant-id": variant["id"],
        "swc-source-object-id": variant["source_object_id"],
        "swc-input-binding-hash": variant["input_binding_hash"],
        "swc-pipeline-version": catalog.imaging.PIPELINE_VERSION,
    }

    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, job)
        intent_id, _ = enhancements._operation_intent(
            conn,
            "derived.put",
            scope_id=variant["output_scope_id"],
            target={"variant_id": variant["id"], "bucket": output_scope["bucket"], "key": variant["output_key"], "sha256": output_sha},
        )
        jobs.checkpoint(conn, job, state="running", processed=0, errors=0, data={"phase": "dispatch", "variant_id": variant["id"], "intent_id": intent_id})
        conn.commit()

    target = _head_or_none(output_adapter, output_scope["bucket"], variant["output_key"])
    if target is not None:
        result = _verify_existing_variant_target(output_adapter, target, output_scope["bucket"], variant["output_key"], variant["id"], output_sha)
        if result["status"] != "succeeded":
            _finalize_variant_job(settings, job, variant, intent_id, "needs_review", result, output_sha, None, output_mime, properties)
            return
    else:
        _assert_job_current(settings, job)
        output_adapter.put_object(output_scope["bucket"], variant["output_key"], output, metadata, output_mime)
        target = output_adapter.head_object(output_scope["bucket"], variant["output_key"])
        result = _verify_existing_variant_target(output_adapter, target, output_scope["bucket"], variant["output_key"], variant["id"], output_sha)
        if result["status"] != "succeeded":
            _finalize_variant_job(settings, job, variant, intent_id, "needs_review", result, output_sha, None, output_mime, properties)
            return

    target_size = result.get("target", {}).get("size")
    _finalize_variant_job(settings, job, variant, intent_id, "succeeded", result, output_sha, target_size, output_mime, properties)


def _assert_job_current(settings: Any, job: Mapping[str, Any]) -> None:
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, dict(job))
        conn.commit()


def _head_or_none(adapter: S3EnhancementAdapter, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return adapter.head_object(bucket, key)
    except storage.StorageError as exc:
        if exc.status == 404 or exc.code == "OBJECT_NOT_FOUND":
            return None
        raise
    except KeyError:
        return None


def _verify_existing_variant_target(
    adapter: S3EnhancementAdapter,
    head: Mapping[str, Any],
    bucket: str,
    key: str,
    variant_id: str,
    expected_sha256: str,
) -> dict[str, Any]:
    metadata = {str(k).lower(): str(v) for k, v in dict(head.get("metadata") or {}).items()}
    if metadata.get("swc-variant-id") != variant_id:
        return {"status": "needs_review", "reason": "target exists without matching variant ownership metadata", "target": dict(head)}
    checksum = head.get("checksum_sha256")
    if not checksum:
        if head.get("size") and int(head["size"]) > 32 * 1024 * 1024:
            raise AppError("RESOURCE_LIMIT_EXCEEDED", "目标派生读取超过 32MiB 校验预算。", 429)
        checksum = hashlib.sha256(adapter.get_object_bytes(bucket, key, head.get("version_id"))).hexdigest()
    if checksum != expected_sha256:
        return {"status": "needs_review", "reason": "target checksum mismatch", "target": dict(head), "observed_sha256": checksum}
    return {"status": "succeeded", "target": dict(head), "observed_sha256": checksum, "idempotent_existing": True}


def _finalize_variant_job(
    settings: Any,
    job: Mapping[str, Any],
    variant: Mapping[str, Any],
    intent_id: str,
    state: str,
    result: Mapping[str, Any],
    output_sha: str,
    output_size: int | None,
    output_mime: str,
    properties: Mapping[str, Any],
) -> None:
    with db.connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.fenced(conn, dict(job))
        manifest = dict(variant["manifest"])
        manifest.update({"status": state, "readback": dict(result), "properties": dict(properties), "pipeline_version": catalog.imaging.PIPELINE_VERSION})
        conn.execute(
            """
            UPDATE derived_variants
            SET status=?, manifest_json=?, output_sha256=?, output_size=?, output_mime=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (
                state,
                enhancements.canonical_json(manifest),
                output_sha,
                output_size,
                output_mime,
                None if state == "succeeded" else result.get("reason", "needs review"),
                jobs.now(),
                variant["id"],
            ),
        )
        enhancements._finish_intent(conn, intent_id, "succeeded" if state == "succeeded" else "needs_review", result=dict(result))
        jobs.checkpoint(
            conn,
            dict(job),
            state=state,
            processed=1 if state == "succeeded" else 0,
            errors=0 if state == "succeeded" else 1,
            data={"variant_id": variant["id"], "result": dict(result)},
            error_code=None if state == "succeeded" else "VARIANT_TARGET_NEEDS_REVIEW",
        )
        conn.commit()


jobs.HANDLERS["derived_variant"] = execute_variant_job
