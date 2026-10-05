"""Live S3 browsing shares the existing explicit storage scopes and object APIs."""
from urllib.parse import quote
from datetime import datetime, timezone
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from . import core, db, management, management_ops, storage
from .security import AppError, mutation, sign_json, unsign_json, utc_add, utc_now

router = APIRouter(prefix="/api/v1/management")


def _close(client):
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _call(operation, **kwargs):
    try:
        return operation(**kwargs)
    except Exception as exc:
        status = storage._status_from_exception(exc)
        response = getattr(exc, "response", {})
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) if isinstance(response, dict) else {}
        if kwargs.get("VersionId") is not None and headers.get("x-amz-delete-marker") == "true":
            raise AppError("VERSION_DELETE_MARKER", "该版本是删除标记", 409) from None
        raise AppError("S3_OBJECT_REQUEST_FAILED", "实时对象请求被存储服务拒绝或无法完成",
                       status if status in (403, 404, 409, 412) else 502) from None


def _bound(request, management_id, scope_id, permission="object:read"):
    manager = management.require_management(request, management_id)
    scope = core.require_scope(request, scope_id)
    if not manager.get("s3_connection_id") or scope["connection_id"] != manager["s3_connection_id"]:
        raise AppError("MANAGEMENT_SCOPE_MISMATCH", "存储范围未关联到此管理连接", 403)
    with db.connect(request.app.state.settings) as conn:
        scope = core.ensure_scope_permission(conn, scope_id, permission)
    return manager, scope


@router.get("/{management_id}/objects")
def browse_objects(request: Request, management_id: str, scope_id: str, prefix: str | None = None,
                   delimiter: str = "/", limit: int = 100, cursor: str | None = None):
    manager, scope = _bound(request, management_id, scope_id)
    prefix = scope["prefix"] if prefix is None else prefix
    core.assert_key_in_scope(scope, prefix)
    if delimiter not in ("", "/") or not 1 <= limit <= 1000:
        raise AppError("INVALID_REQUEST", "分页数量需为 1–1000，分隔符需为空或 /", 422)
    settings = request.app.state.settings
    binding = {"management": manager["id"], "scope": scope_id, "epoch": scope["authz_epoch"],
               "prefix": prefix, "delimiter": delimiter, "limit": limit}
    kwargs = {"Bucket": scope["bucket"], "Prefix": prefix, "MaxKeys": limit, "Delimiter": delimiter}
    if cursor:
        try:
            state = unsign_json(cursor, settings.require_cursor_key(), "management-live:v1")
            if state["binding"] != binding or state["expires"] <= utc_now() or not isinstance(state["token"], str):
                raise ValueError()
            kwargs["ContinuationToken"] = state["token"]
        except (KeyError, TypeError, ValueError):
            raise AppError("INVALID_CURSOR", "范围或查询已变化，请重新查询", 400)
    s3 = core.get_client(request, scope)
    try:
        page = _call(s3.list_objects_v2, **kwargs)
    finally:
        _close(s3)
    items = []
    for obj in page.get("Contents", []):
        core.assert_key_in_scope(scope, obj["Key"])
        modified = obj.get("LastModified")
        items.append({"key": obj["Key"], "size": obj.get("Size"), "etag": obj.get("ETag"),
                      "last_modified": modified.isoformat() if hasattr(modified, "isoformat") else modified,
                      "storage_class": obj.get("StorageClass"), "version_id": None})
    folders = [item["Prefix"] for item in page.get("CommonPrefixes", [])]
    for folder in folders:
        core.assert_key_in_scope(scope, folder)
    token = page.get("NextContinuationToken")
    truncated = bool(page.get("IsTruncated"))
    if truncated and not token:
        raise AppError("S3_PAGINATION_INVALID", "存储服务缺少后续分页标记", 502)
    next_cursor = sign_json({"binding": binding, "token": token, "expires": utc_add(1800)},
                            settings.require_cursor_key(), "management-live:v1") if truncated else None
    return {"source": "live_s3_list_objects_v2", "bucket": scope["bucket"], "prefix": prefix,
            "scope_id": scope_id, "items": items, "folders": folders, "next_cursor": next_cursor,
            "total": None, "is_truncated": truncated, "version_ids_require_head": True}


@router.post("/{management_id}/objects/select")
def select_object(request: Request, management_id: str, body: dict):
    mutation(request)
    scope_id, key = body.get("scope_id"), body.get("key")
    if not isinstance(scope_id, str) or not isinstance(key, str) or not key:
        raise AppError("INVALID_REQUEST", "需提供存储范围和字面对象 key", 422)
    _, scope = _bound(request, management_id, scope_id)
    core.assert_key_in_scope(scope, key)
    s3 = core.get_client(request, scope)
    try:
        head = _call(s3.head_object, Bucket=scope["bucket"], Key=key)
    finally:
        _close(s3)
    # Preserve image properties for an already indexed revision.
    metadata = {"version_id": head.get("VersionId"), "size": head.get("ContentLength", 0),
                "etag": head.get("ETag"), "last_modified": core._head_last_modified(head)}
    revision = core.make_revision(scope["bucket"], key, metadata)
    with db.connect(request.app.state.settings) as conn:
        existing = conn.execute("SELECT id FROM objects WHERE scope_id=? AND key=? AND revision=? AND is_current=1",
                                (scope_id, key, revision)).fetchone()
        obj = core.get_object(conn, scope_id, existing["id"]) if existing else core.upsert_object_metadata(conn, scope, key, head)
    return {"object": obj, "source": "live_s3_head", "actions_use_existing_scope_object_api": True}


@router.get("/{management_id}/objects/download")
def live_download(request: Request, management_id: str, scope_id: str, key: str, version_id: str | None = None):
    _, scope = _bound(request, management_id, scope_id, "object:download_original")
    core.assert_key_in_scope(scope, key)
    kwargs = {"Bucket": scope["bucket"], "Key": key}
    if version_id is not None:
        kwargs["VersionId"] = version_id
    s3 = core.get_client(request, scope)
    try:
        head = _call(s3.head_object, **kwargs)
        if head.get("DeleteMarker"):
            raise AppError("VERSION_DELETE_MARKER", "该版本是删除标记", 409)
        if version_id is None and head.get("VersionId") is not None:
            kwargs["VersionId"] = head["VersionId"]
        if head.get("ETag"):
            kwargs["IfMatch"] = head["ETag"]
        result = _call(s3.get_object, **kwargs)
    except Exception:
        _close(s3)
        raise
    def chunks():
        try:
            yield from storage.stream_object_body(result["Body"])
        finally:
            _close(s3)
    return StreamingResponse(chunks(), media_type="application/octet-stream",
                             headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(key.rsplit("/", 1)[-1], safe=""),
                                      "Cache-Control": "no-store"})


def _head_identity(scope, key, head):
    metadata = {"version_id": head.get("VersionId"), "size": head.get("ContentLength", 0),
                "etag": head.get("ETag"), "last_modified": core._head_last_modified(head)}
    return {**metadata, "revision": core.make_revision(scope["bucket"], key, metadata)}


def _mutable_delete_supported(settings, scope, head):
    try:
        from . import management_conditions
    except Exception:
        return False
    supported = getattr(management_conditions, "supported", None)
    if not callable(supported):
        return False
    try:
        return supported(settings, scope, head) is True
    except Exception:
        return False


def _validate_versioning_off(s3, bucket):
    state = _call(s3.get_bucket_versioning, Bucket=bucket)
    status = state.get("Status") if isinstance(state, dict) else None
    if status in {"Enabled", "Suspended"}:
        raise AppError("BUCKET_VERSIONING_ACTIVE", "未版本化删除只允许在未启用版本控制的桶中执行，避免生成删除标记", 409)
    if status is not None:
        raise AppError("BUCKET_VERSIONING_UNKNOWN", "无法确认桶未启用版本控制，不能删除未版本化对象", 409)
    return status


def _mode_from_version_id(version_id):
    if version_id is None:
        return "unversioned"
    if not isinstance(version_id, str) or not version_id:
        raise AppError("INVALID_REQUEST", "版本 ID 必须为非空字符串、literal null 或省略表示未版本化对象", 422)
    return "null" if version_id == "null" else "strong"


def _unlocked_version(s3, bucket, key, version_id):
    try:
        config = s3.get_object_lock_configuration(Bucket=bucket)
    except Exception as exc:
        code = storage._code_from_exception(exc)
        if code in {"ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"}:
            return
        error = storage.sanitize_storage_exception(exc)
        raise AppError(error.code, str(error), error.status) from None
    if config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") != "Enabled":
        raise AppError("OBJECT_RETENTION_UNKNOWN", "无法核对桶的保留能力", 409)
    def setting(operation, field, missing_codes, missing_value):
        try:
            return operation(Bucket=bucket, Key=key, VersionId=version_id).get(field, {})
        except Exception as exc:
            if storage._code_from_exception(exc) in missing_codes:
                return missing_value
            error = storage.sanitize_storage_exception(exc)
            raise AppError(error.code, str(error), error.status) from None
    retention = setting(s3.get_object_retention, "Retention",
                        {"ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"}, {})
    hold = setting(s3.get_object_legal_hold, "LegalHold", {"NoSuchObjectLegalHold"}, {"Status": "OFF"})
    if hold.get("Status") == "ON":
        raise AppError("OBJECT_LEGAL_HOLD", "指定版本处于 Legal Hold，不能删除", 409)
    if hold.get("Status") != "OFF":
        raise AppError("OBJECT_RETENTION_UNKNOWN", "无法核对指定版本的 Legal Hold", 409)
    until = retention.get("RetainUntilDate")
    if until is not None:
        try:
            until = datetime.fromisoformat(until.replace("Z", "+00:00")) if isinstance(until, str) else until
            if until.tzinfo is None or until.astimezone(timezone.utc) > datetime.now(timezone.utc):
                raise AppError("OBJECT_RETENTION_ACTIVE", "指定版本的保留期尚未结束", 409)
        except (AttributeError, ValueError, TypeError):
            raise AppError("OBJECT_RETENTION_UNKNOWN", "无法核对指定版本的保留状态", 409) from None


def _unlocked_unversioned_bucket(s3, bucket):
    try:
        config = s3.get_object_lock_configuration(Bucket=bucket)
    except Exception as exc:
        code = storage._code_from_exception(exc)
        if code in {"ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"}:
            return
        error = storage.sanitize_storage_exception(exc)
        raise AppError(error.code, str(error), error.status) from None
    if config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled":
        raise AppError("OBJECT_RETENTION_UNKNOWN", "桶启用了对象锁，未版本化删除无法按版本核对保留状态", 409)


@router.post("/{management_id}/objects/delete-version")
def delete_version(request: Request, management_id: str, body: dict):
    user = mutation(request)
    scope_id, key, expected_etag = (body.get(field) for field in ("scope_id", "key", "expected_etag"))
    version_id = body.get("version_id") if "version_id" in body else None
    if not isinstance(scope_id, str) or not scope_id or not isinstance(key, str) or not key or not isinstance(expected_etag, str) or not expected_etag:
        raise AppError("INVALID_REQUEST", "需提供范围、对象 key 和 ETag；version_id 可省略表示未版本化对象", 422)
    mode = _mode_from_version_id(version_id)
    if body.get("confirm_version_delete") is not True or body.get("acknowledge_unknown_references") is not True:
        raise AppError("OBJECT_DELETE_CONFIRMATION_REQUIRED", "永久版本删除需确认目标及外部引用未知的边界", 422)
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "删除请求需要固定幂等标记", 422)
    manager, scope = _bound(request, management_id, scope_id, "object:write")
    core.assert_key_in_scope(scope, key)
    s3 = core.get_client(request, scope)
    target = {"Bucket": scope["bucket"], "Key": key}
    if mode != "unversioned":
        target["VersionId"] = version_id
    captured_identity = {}

    def read_head():
        return _call(s3.head_object, **target)

    def readback():
        return {"exists": True, **_head_identity(scope, key, read_head())}

    def invoke():
        with db.connect(request.app.state.settings) as conn:
            current = core.ensure_scope_permission(conn, scope_id, "object:write")
            if current["authz_epoch"] != scope["authz_epoch"]:
                raise AppError("FORBIDDEN_SCOPE", "存储范围授权已经变化", 403)
        if mode == "unversioned":
            _validate_versioning_off(s3, scope["bucket"])
        fresh_head = read_head()
        fresh = {"exists": True, **_head_identity(scope, key, fresh_head)}
        if mode == "strong":
            if fresh["version_id"] != version_id or fresh["etag"] != expected_etag:
                raise AppError("OBJECT_VERSION_CHANGED", "指定版本身份与确认值不符", 409)
            _unlocked_version(s3, scope["bucket"], key, version_id)
        elif mode == "null":
            if fresh["version_id"] != "null" or fresh["etag"] != expected_etag:
                raise AppError("OBJECT_VERSION_CHANGED", "null 版本身份与确认值不符", 409)
            _unlocked_version(s3, scope["bucket"], key, "null")
            if not _mutable_delete_supported(request.app.state.settings, scope, fresh_head):
                raise AppError("MUTABLE_VERSION_DELETE_UNSUPPORTED", "缺少可验证的条件删除能力证据，不能删除 null 版本", 501)
        else:
            if fresh["version_id"] is not None or fresh["etag"] != expected_etag:
                raise AppError("OBJECT_VERSION_CHANGED", "未版本化对象身份与确认值不符", 409)
            _unlocked_unversioned_bucket(s3, scope["bucket"])
            if not _mutable_delete_supported(request.app.state.settings, scope, fresh_head):
                raise AppError("MUTABLE_VERSION_DELETE_UNSUPPORTED", "缺少可验证的条件删除能力证据，不能删除未版本化对象", 501)
        captured_identity.clear()
        captured_identity.update(fresh)
        return _call(s3.delete_object, **target, IfMatch=expected_etag)

    try:
        receipt = management_ops.perform_operation(request.app.state.settings, manager, user.id,
            "object.version.delete", "DELETE", "/s3/object-version", {"scope_id": scope_id, "bucket": scope["bucket"],
            "key": key, "version_id": version_id, "identity_mode": mode, "expected_etag": expected_etag,
            "reference_state": "unknown_acknowledged"},
            idempotency_key=idempotency_key, invoke=invoke, readback=readback,
            verify=lambda before, after, response: before is not None and before.get("etag") == expected_etag
            and after is None and isinstance(response, dict) and response.get("DeleteMarker") is not True)
        if receipt["status"] == "confirmed" and not receipt.get("replayed"):
            try:
                revision = captured_identity.get("revision")
                if revision:
                    with db.connect(request.app.state.settings) as conn:
                        conn.execute("DELETE FROM preview_cache WHERE object_id IN (SELECT id FROM objects WHERE scope_id=? AND key=? AND revision=?)",
                                     (scope_id, key, revision))
                        result = conn.execute("UPDATE objects SET is_current=0,presence='missing_confirmed',observed_at=? WHERE scope_id=? AND key=? AND revision=?",
                                              (utc_now(), scope_id, key, revision))
                        if result.rowcount:
                            conn.execute("UPDATE scopes SET index_generation=index_generation+1 WHERE id=?", (scope_id,))
                    receipt["local_index_status"] = "updated"
            except sqlite3.Error:
                receipt["local_index_status"] = "needs_review"
        return receipt
    finally:
        _close(s3)


@router.get("/{management_id}/objects/version-info")
def version_info(request: Request, management_id: str, scope_id: str, key: str, version_id: str | None = None):
    _, scope = _bound(request, management_id, scope_id)
    core.assert_key_in_scope(scope, key)
    s3 = core.get_client(request, scope)
    kwargs = {"Bucket": scope["bucket"], "Key": key}
    if version_id is not None:
        mode = _mode_from_version_id(version_id)
        kwargs["VersionId"] = version_id
    else:
        mode = "current"
    can_delete = False
    delete_reason = None
    requires_probe = False
    try:
        head = _call(s3.head_object, **kwargs)
        identity = _head_identity(scope, key, head)
        if mode == "current":
            mode = _mode_from_version_id(identity.get("version_id"))
        if mode == "unversioned":
            try:
                _validate_versioning_off(s3, scope["bucket"])
            except AppError as exc:
                delete_reason = exc.code
        if mode == "strong":
            try:
                _unlocked_version(s3, scope["bucket"], key, identity.get("version_id"))
                can_delete = True
            except AppError as exc:
                delete_reason = exc.code
        else:
            requires_probe = True
            if mode == "null" and identity.get("version_id") != "null":
                delete_reason = "OBJECT_VERSION_CHANGED"
            elif mode == "unversioned" and identity.get("version_id") is not None:
                delete_reason = delete_reason or "OBJECT_VERSION_CHANGED"
            elif delete_reason is None:
                try:
                    if mode == "null":
                        _unlocked_version(s3, scope["bucket"], key, "null")
                    else:
                        _unlocked_unversioned_bucket(s3, scope["bucket"])
                    if _mutable_delete_supported(request.app.state.settings, scope, head):
                        can_delete = True
                    else:
                        delete_reason = "MUTABLE_VERSION_DELETE_UNSUPPORTED"
                except AppError as exc:
                    delete_reason = exc.code
            else:
                delete_reason = delete_reason or "MUTABLE_VERSION_DELETE_UNSUPPORTED"
    finally:
        _close(s3)
    until = head.get("ObjectLockRetainUntilDate")
    return {"key": key, "version_id": identity.get("version_id"), "etag": identity.get("etag"), "size": identity.get("size"),
            "last_modified": identity.get("last_modified"), "revision": identity.get("revision"),
            "legal_hold": head.get("ObjectLockLegalHoldStatus"), "retention_until": until.isoformat() if hasattr(until, "isoformat") else until,
            "identity_strength": "strong" if identity.get("version_id") not in (None, "", "null") else "mutable",
            "can_delete": can_delete, "delete_reason": None if can_delete else delete_reason,
            "delete_block_reason": None if can_delete else delete_reason,
            "requires_probe": requires_probe}
