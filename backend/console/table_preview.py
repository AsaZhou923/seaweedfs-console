"""Scope-authorized Iceberg manifest and raw Parquet sample preview."""
from __future__ import annotations

import time
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request

from . import core, db, management_native, management_objects
from .security import AppError, mutation, utc_now

router = APIRouter(prefix="/api/v1/management")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFESTS = 16
MAX_FILES = 1000
MAX_PREVIEW_SECONDS = 60


def _catalog_identity(catalog):
    return {field: catalog.get(field) for field in ("versionToken", "metadataLocation", "metadataVersion", "warehouseLocation", "format")}


def location_key(location: str, scope: dict, table_root: str | None = None) -> str:
    if not isinstance(location, str) or not location or "\x00" in location or "\\" in location:
        raise AppError("INVALID_TABLE_LOCATION", "表文件位置无效", 422)
    if "://" in location:
        uri = urlsplit(location)
        if uri.scheme != "s3" or uri.netloc != scope["bucket"] or uri.query or uri.fragment or uri.username or uri.password:
            raise AppError("TABLE_LOCATION_FORBIDDEN", "表文件不在已授权的 S3 桶内", 403)
        key = unquote(uri.path.lstrip("/"))
    elif location.startswith("/buckets/"):
        bucket, separator, key = location[len("/buckets/"):].partition("/")
        if not separator or bucket != scope["bucket"]:
            raise AppError("TABLE_LOCATION_FORBIDDEN", "表文件不在已授权的桶内", 403)
    elif location.startswith("/"):
        raise AppError("TABLE_LOCATION_FORBIDDEN", "不读取原生系统路径", 403)
    else:
        if table_root is None:
            raise AppError("INVALID_TABLE_LOCATION", "相对文件位置缺少表目录", 422)
        if any(part in (".", "..") for part in location.split("/")):
            raise AppError("TABLE_LOCATION_FORBIDDEN", "相对表文件位置不能穿越目录", 403)
        key = table_root.rstrip("/") + "/" + location
    # Iceberg locations resolve to Filer-backed paths. Validate every derived
    # form before the literal scope-prefix check; encoded segments must not
    # become a normalized path outside the approved table directory.
    witness = key
    for _ in range(3):
        if not witness or "\x00" in witness or "\\" in witness or any(part in (".", "..") for part in witness.split("/")):
            raise AppError("TABLE_LOCATION_FORBIDDEN", "表文件位置不能穿越目录", 403)
        decoded = unquote(witness)
        if decoded == witness:
            break
        witness = decoded
    core.assert_key_in_scope(scope, key)
    return key


class Reader:
    def __init__(self, request, scope):
        self.request, self.scope = request, scope
        self.client = core.get_client(request, scope)
        self.used = 0
        self.deadline = time.monotonic() + MAX_PREVIEW_SECONDS
        self.evidence = []

    def close(self):
        management_objects._close(self.client)

    def read(self, key):
        self.check()
        core.assert_key_in_scope(self.scope, key)
        with db.connect(self.request.app.state.settings) as conn:
            current = core.ensure_scope_permission(conn, self.scope["id"], "object:download_original")
            if current["authz_epoch"] != self.scope["authz_epoch"]:
                raise AppError("TABLE_SCOPE_CHANGED", "存储范围权限已经改变", 403)
        kwargs = {"Bucket": self.scope["bucket"], "Key": key}
        head = management_objects._call(self.client.head_object, **kwargs)
        size = head.get("ContentLength")
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES or self.used + size > MAX_TOTAL_BYTES:
            raise AppError("TABLE_FILE_LIMIT", "表文件超出本次读取预算", 413)
        if head.get("VersionId") is not None:
            kwargs["VersionId"] = head["VersionId"]
        etag = head.get("ETag")
        if not etag:
            raise AppError("TABLE_SOURCE_IDENTITY_UNKNOWN", "无法核对表文件身份", 409)
        response = management_objects._call(self.client.get_object, **kwargs, IfMatch=etag)
        data = bytearray()
        try:
            body = response["Body"]
            while True:
                self.check()
                chunk = body.read(min(1024 * 1024, MAX_FILE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_FILE_BYTES or self.used + len(data) > MAX_TOTAL_BYTES:
                    raise AppError("TABLE_FILE_LIMIT", "表文件超出本次读取预算", 413)
        finally:
            response["Body"].close()
        after = management_objects._call(self.client.head_object, **kwargs)
        if len(data) != size or after.get("ETag") != etag or after.get("ContentLength") != size:
            raise AppError("TABLE_SOURCE_CHANGED", "表文件在读取期间发生变化", 409)
        self.used += len(data)
        self.evidence.append({"key": key, "bytes": len(data), "etag": etag, "version_id": head.get("VersionId")})
        return bytes(data)

    def check(self):
        if time.monotonic() > self.deadline:
            raise AppError("TABLE_PREVIEW_TIMEOUT", "表预览超出时间预算", 413)


def _decode(data, kind, limit):
    from .table_decoder import decode
    result = decode(data, kind=kind, limit=limit)
    if result.get("status") != "decoded":
        raise AppError("TABLE_DECODE_" + str(result.get("status", "FAILED")).upper(), "表文件解析失败或超过资源预算", 422,
                       detail={"decode_status": result.get("status"), "reason": result.get("reason")})
    return result


def preview(request, management_id, body):
    scope_id = body.get("scope_id")
    bucket_arn, namespace, name = (body.get(field) for field in ("bucket_arn", "namespace", "name"))
    if not all(isinstance(value, str) and value for value in (scope_id, bucket_arn, namespace, name)):
        raise AppError("INVALID_REQUEST", "需提供存储范围、Table Bucket、namespace 和表名", 422)
    limit, snapshot_id = body.get("limit", 50), body.get("snapshot_id", 0)
    if isinstance(snapshot_id, str) and snapshot_id.isascii() and snapshot_id.isdecimal() and len(snapshot_id) <= 19:
        snapshot_id = int(snapshot_id)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise AppError("INVALID_REQUEST", "预览行数需为 1–100", 422)
    if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or not 0 <= snapshot_id <= 2**63 - 1:
        raise AppError("INVALID_REQUEST", "snapshot_id 需为非负整数", 422)
    manager, scope = management_objects._bound(request, management_id, scope_id, "object:download_original")
    info = management_native.fetch_table_details(request, management_id, bucket_arn, namespace, name)
    if info.get("status") != "supported":
        return {**info, "rows": None, "files": None}
    catalog = info["details"]["catalog"]
    table_format = str(catalog.get("format", "")).upper()
    if table_format == "LANCE":
        if snapshot_id or body.get("file_location") is not None:
            raise AppError("WORKER_PREVIEW_SELECTION_UNSUPPORTED", "Worker 样本预览不支持 Iceberg 快照或数据文件选择", 501)
        from . import table_worker_preview
        return table_worker_preview.preview(request, manager, scope, catalog, bucket_arn, namespace, name, limit)
    if table_format != "ICEBERG":
        raise AppError("TABLE_FORMAT_UNSUPPORTED", "当前表格式没有已核实的预览读取方式", 501)
    location = catalog.get("metadataLocation")
    if not isinstance(location, str) or not location:
        raise AppError("TABLE_METADATA_NOT_CONFIGURED", "表未提供 metadataLocation", 409)
    metadata_key = location_key(location, scope)
    root_location = catalog.get("warehouseLocation")
    table_root = location_key(root_location, scope) if isinstance(root_location, str) and root_location else metadata_key.rsplit("/metadata/", 1)[0]
    reader = Reader(request, scope)
    try:
        metadata = _decode(reader.read(metadata_key), "json", 1).get("payload")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("snapshots"), list):
            raise AppError("TABLE_METADATA_INVALID", "Iceberg metadata 结构无效", 422)
        snapshots = metadata.get("snapshots", [])
        requested_id = snapshot_id or metadata.get("current-snapshot-id")
        snap = next((item for item in snapshots if isinstance(item, dict) and item.get("snapshot-id") == requested_id), None)
        if snap is None:
            if snapshot_id:
                raise AppError("TABLE_SNAPSHOT_NOT_FOUND", "所选快照不存在", 404)
            if snapshots:
                raise AppError("TABLE_SNAPSHOT_ID_UNKNOWN", "metadata 未提供可核对的当前快照", 409)
            return {"status": "no_snapshots", "source": "scope_authorized_iceberg_metadata", "rows": [], "files": [],
                    "snapshot_id": None, "total_rows": None, "checked_at": utc_now()}
        manifest_location = snap.get("manifest-list")
        list_key = location_key(manifest_location, scope, table_root)
        manifest_list = _decode(reader.read(list_key), "avro", 10000)
        files, notes, has_deletes = [], [], False
        complete = not bool(manifest_list.get("truncated"))
        manifests = manifest_list.get("records", [])
        if len(manifests) > MAX_MANIFESTS:
            complete = False
            notes.append("仅扫描本次预算内的前 16 个 manifest")
        for manifest in manifests[:MAX_MANIFESTS]:
            reader.check()
            if not isinstance(manifest, dict):
                raise AppError("TABLE_MANIFEST_INVALID", "Manifest list 结构无效", 422)
            if manifest.get("content", 0) != 0:
                has_deletes = True
                continue
            path = manifest.get("manifest_path")
            entries = _decode(reader.read(location_key(path, scope, table_root)), "avro", 10000)
            complete = complete and not bool(entries.get("truncated"))
            for entry in entries.get("records", []):
                if not isinstance(entry, dict) or not isinstance(entry.get("status"), int) or isinstance(entry.get("status"), bool) or entry["status"] not in (0, 1, 2):
                    raise AppError("TABLE_MANIFEST_INVALID", "Manifest entry 状态无效", 422)
                if entry["status"] == 2:
                    continue
                df = entry.get("data_file")
                if not isinstance(df, dict):
                    raise AppError("TABLE_MANIFEST_INVALID", "Manifest 缺少 data_file", 422)
                if df.get("content", 0) != 0:
                    has_deletes = True
                    continue
                path = df.get("file_path")
                key = location_key(path, scope, table_root)
                if len(files) >= MAX_FILES:
                    complete = False
                    break
                files.append({"location": path, "key": key, "format": df.get("file_format"),
                              "record_count": df.get("record_count"), "size_bytes": df.get("file_size_in_bytes")})
        requested_file = body.get("file_location")
        if requested_file is not None and not isinstance(requested_file, str):
            raise AppError("INVALID_REQUEST", "file_location 需为字符串", 422)
        selected = next((item for item in files if item["location"] == requested_file), None) if requested_file else next((item for item in files if str(item["format"]).upper() == "PARQUET"), None)
        if requested_file and selected is None:
            raise AppError("TABLE_FILE_NOT_IN_SNAPSHOT" if complete else "TABLE_MEMBERSHIP_INCOMPLETE",
                           "无法证明所选文件属于该快照", 404 if complete else 409)
        rows, columns, sample_truncated = [], [], False
        if selected:
            if str(selected["format"]).upper() != "PARQUET":
                raise AppError("TABLE_FILE_FORMAT_UNSUPPORTED", "本预览仅解析 Parquet 数据文件", 501)
            decoded = _decode(reader.read(selected["key"]), "parquet", limit)
            rows, columns = decoded.get("rows", []), decoded.get("columns", [])
            sample_truncated = bool(decoded.get("truncated"))
        latest = management_native.fetch_table_details(request, management_id, bucket_arn, namespace, name)
        if latest.get("status") != "supported" or _catalog_identity(latest["details"]["catalog"]) != _catalog_identity(catalog):
            raise AppError("TABLE_CATALOG_CHANGED", "表 catalog 在读取期间发生变化", 409)
        with db.connect(request.app.state.settings) as conn:
            current = core.ensure_scope_permission(conn, scope_id, "object:download_original")
            if current["authz_epoch"] != scope["authz_epoch"]:
                raise AppError("TABLE_SCOPE_CHANGED", "存储范围权限已经改变", 403)
        if has_deletes:
            notes.append("存在 Iceberg delete 文件；当前仅预览原始 Parquet 文件，未应用逻辑删除")
        return {"status": "supported", "source": "scope_authorized_iceberg_manifest_parquet", "preview_kind": "raw_file_sample",
                "scope_id": scope_id, "snapshot_id": str(requested_id) if isinstance(requested_id, int) and abs(requested_id) > 2**53 - 1 else requested_id, "files": files, "file_list_complete": complete,
                "selected_file": selected, "rows": rows, "columns": columns, "sample_truncated": sample_truncated,
                "has_delete_files": True if has_deletes else False if complete else None, "deletes_applied": False, "total_rows": None,
                "bytes_read": reader.used, "source_evidence": reader.evidence, "notes": notes, "checked_at": utc_now()}
    finally:
        reader.close()


@router.post("/{management_id}/modules/s3-tables/table-preview")
def table_preview(request: Request, management_id: str, body: dict):
    mutation(request)
    return preview(request, management_id, body)
