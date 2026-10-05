from __future__ import annotations

from typing import Any, Callable, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Request

from . import db
from .security import AppError, mutation


router = APIRouter(prefix="/api/v1/management")

SECRET_FIELDS = {"secret_key", "secret_access_key", "SecretKey", "SecretAccessKey"}
PUBLIC_KEY_FIELDS = {"access_key", "access_key_id", "AccessKey", "AccessKeyId"}
INT64_MAX = 2**63 - 1
QUOTA_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 * 1024,
    "GB": 1024 * 1024 * 1024,
    "TB": 1024 * 1024 * 1024 * 1024,
}


def _management_module():
    try:
        from . import management  # type: ignore
    except Exception as exc:
        raise AppError("MANAGEMENT_BACKEND_UNAVAILABLE", "management backend contract is not available", 501) from exc
    return management


def _management_ops_module():
    try:
        from . import management_ops  # type: ignore
    except Exception as exc:
        raise AppError("MANAGEMENT_BACKEND_UNAVAILABLE", "management operation journal is not available", 501) from exc
    return management_ops


def _require_management(request: Request, management_id: str, *, write: bool = False) -> dict[str, Any]:
    return dict(_management_module().require_management(request, management_id, write=write))


def _admin_client(settings: Any, conn: Any, management: Mapping[str, Any]):
    return _management_module().admin_client(settings, conn, management)


def _close_resource(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _admin_get_json(settings: Any, conn: Any, management: Mapping[str, Any], path: str, params: Mapping[str, Any] | None = None) -> Any:
    client = _admin_client(settings, conn, management)
    try:
        return client.get_json(path, params=dict(params) if params else None)
    finally:
        _close_resource(client)


def _admin_request_json(settings: Any, conn: Any, management: Mapping[str, Any], method: str, path: str, body: Mapping[str, Any]) -> Any:
    client = _admin_client(settings, conn, management)
    try:
        return client.request_json(method, path, dict(body))
    finally:
        _close_resource(client)


def _admin_read_json_for_management(settings: Any, management: Mapping[str, Any], path: str) -> Any:
    with db.connect(settings) as conn:
        return _admin_get_json(settings, conn, management, path)


def _admin_read_json_or_none(settings: Any, management: Mapping[str, Any], path: str) -> Any:
    try:
        return _admin_read_json_for_management(settings, management, path)
    except AppError as exc:
        if exc.status == 404 or exc.code == "ADMIN_ENDPOINT_NOT_FOUND":
            return None
        raise


def _mutate(
    request: Request,
    management: Mapping[str, Any],
    *,
    action: str,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
    readback_path: str | None = None,
    readback_params: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    expected_before_hash: str | None = None,
    verify: Callable[[Any, Any, Any], bool] | None = None,
    invoke: Callable[[], Any] | None = None,
    readback: Callable[[], Any] | None = None,
    snapshot_filter: Callable[[Any], Any] | None = None,
    return_created_secret: bool = False,
) -> dict[str, Any]:
    user = mutation(request)
    idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    result = _management_ops_module().perform_operation(
        request.app.state.settings,
        dict(management),
        getattr(user, "id", user.get("id") if isinstance(user, Mapping) else "admin"),
        action,
        method,
        path,
        dict(payload or {}),
        readback_path=readback_path,
        readback_params=dict(readback_params or {}) if readback_params else None,
        idempotency_key=idempotency_key,
        expected_before_hash=expected_before_hash,
        verify=verify,
        invoke=invoke,
        readback=readback,
        snapshot_filter=snapshot_filter,
        params=dict(params or {}) if params else None,
        return_created_secret=return_created_secret,
    )
    return _redact(result, allow_created_secret=return_created_secret)


def _q(value: str) -> str:
    return quote(value, safe="")


def _redact(value: Any, *, allow_created_secret: bool = False) -> Any:
    if isinstance(value, list):
        return [_redact(item, allow_created_secret=allow_created_secret) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in SECRET_FIELDS and not allow_created_secret:
            continue
        if key in SECRET_FIELDS and allow_created_secret:
            out[key] = item
            continue
        out[key] = _redact(item, allow_created_secret=allow_created_secret)
    return out


def _reject_manual_secret(payload: Mapping[str, Any]) -> None:
    present = SECRET_FIELDS.intersection(payload.keys())
    if present:
        raise AppError("MANUAL_SECRET_IMPORT_UNSUPPORTED", "manual secret import is not supported; generate secrets server-side", 422)


def _string_list(payload: Mapping[str, Any], field: str) -> list[str]:
    value = payload.get(field, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AppError("INVALID_REQUEST", f"{field} must be a list of strings", 422, field=field)
    return value


def _required_string(payload: Mapping[str, Any], field: str, *, code: str = "INVALID_REQUEST") -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise AppError(code, f"{field} is required", 422, field=field)
    return value


def _optional_string(payload: Mapping[str, Any], field: str, default: str = "") -> str:
    value = payload.get(field, default)
    if not isinstance(value, str):
        raise AppError("INVALID_REQUEST", f"{field} must be a string", 422, field=field)
    return value


def _required_bool(payload: Mapping[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise AppError("INVALID_REQUEST", f"{field} must be a boolean", 422, field=field)
    return value


def _optional_bool(payload: Mapping[str, Any], field: str, default: bool) -> bool:
    if field not in payload:
        return default
    return _required_bool(payload, field)


def _required_int(payload: Mapping[str, Any], field: str, *, minimum: int = 0) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise AppError("INVALID_REQUEST", f"{field} must be an integer >= {minimum}", 422, field=field)
    return value


def _canonical_access_key(raw: Mapping[str, Any], *, details_simulated_created_at: bool = False) -> dict[str, Any]:
    created = raw.get("created_at") or raw.get("CreatedAt")
    item = {
        "access_key": raw.get("access_key") or raw.get("AccessKey") or raw.get("access_key_id") or raw.get("AccessKeyId"),
        "status": raw.get("status") or raw.get("Status"),
    }
    if details_simulated_created_at:
        item["created_at"] = None
        item["created_at_source"] = "upstream_simulated_not_authoritative"
        return {k: v for k, v in item.items() if v is not None or k == "created_at"}
    else:
        item["created_at"] = created
        item["created_at_source"] = "upstream"
    return {k: v for k, v in item.items() if v is not None}


def _canonical_user(raw: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    access_keys = raw.get("access_keys") or raw.get("AccessKeys") or []
    if "is_static" in raw:
        is_static: bool | None = bool(raw["is_static"])
        is_static_source = "upstream"
    elif "IsStatic" in raw:
        is_static = bool(raw["IsStatic"])
        is_static_source = "upstream"
    else:
        is_static = None
        is_static_source = "unknown"
    return {
        "username": raw.get("username") or raw.get("Username"),
        "email": raw.get("email") or raw.get("Email") or "",
        "actions": raw.get("actions") or raw.get("Actions") or raw.get("permissions") or raw.get("Permissions") or [],
        "policy_names": raw.get("policy_names") or raw.get("PolicyNames") or [],
        "groups": raw.get("groups") or raw.get("Groups") or [],
        "access_keys": [_canonical_access_key(dict(key), details_simulated_created_at=detail) for key in access_keys],
        "is_static": is_static,
        "is_static_source": is_static_source,
    }


def _canonical_principal(value: str) -> dict[str, Any]:
    kind = "unknown"
    name = value
    if value == "*":
        kind = "wildcard"
    elif ":user/" in value:
        kind = "user"
        name = value.rsplit(":user/", 1)[-1]
    elif ":role/" in value:
        kind = "role"
        name = value.rsplit(":role/", 1)[-1]
    return {"principal": value, "type": kind, "name": name}


def _canonical_service_account(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id") or raw.get("ID") or raw.get("service_account_id") or raw.get("ServiceAccountId"),
        "parent_user": raw.get("parent_user") or raw.get("ParentUser"),
        "description": raw.get("description") or raw.get("Description") or "",
        "access_key_id": raw.get("access_key_id") or raw.get("AccessKeyId"),
        "status": raw.get("status") or raw.get("Status"),
        "create_date": raw.get("create_date") or raw.get("CreateDate"),
        "expiration": raw["expiration"] if "expiration" in raw else raw.get("Expiration"),
    }


def _canonical_bucket(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "name",
        "created_at",
        "logical_size",
        "physical_size",
        "last_modified",
        "quota",
        "quota_enabled",
        "read_only",
        "versioning_status",
        "object_lock_enabled",
        "object_lock_mode",
        "object_lock_duration",
        "owner",
        "lifecycle_rule_count",
        "lifecycle_enabled_count",
        "policy_statement_count",
    )
    return {field: raw.get(field) for field in fields if field in raw}


def _extract_access_keys(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        return []
    keys = raw.get("access_keys") or raw.get("AccessKeys") or []
    return [key for key in keys if isinstance(key, Mapping)]


def _access_key_id(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("access_key") or raw.get("AccessKey") or raw.get("access_key_id") or raw.get("AccessKeyId")
    return str(value) if value else None


def _collection_contains(value: Any, expected: str, *fields: str) -> bool:
    if isinstance(value, list):
        return any(item == expected or (isinstance(item, Mapping) and any(item.get(field) == expected for field in fields)) for item in value)
    if isinstance(value, Mapping):
        for key in ("items", "members", "policies", "policy_names", "PolicyNames", "groups", "Groups", "users", "Users"):
            if key in value and _collection_contains(value[key], expected, *fields):
                return True
    return False


def _collection_absent(value: Any, expected: str, *fields: str) -> bool:
    return not _collection_contains(value, expected, *fields)


def _ensure_bucket_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "name",
        "region",
        "quota_size",
        "quota_unit",
        "quota_enabled",
        "versioning_enabled",
        "object_lock_enabled",
        "object_lock_mode",
        "set_default_retention",
        "object_lock_duration",
        "owner",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AppError("UNSUPPORTED_BUCKET_FIELD", "unsupported bucket field: " + ", ".join(unknown), 422)
    name = str(payload.get("name") or "")
    if not name:
        raise AppError("INVALID_BUCKET_NAME", "bucket name is required", 422)
    result = dict(payload)
    if "region" in result and not isinstance(result["region"], str):
        raise AppError("INVALID_REQUEST", "region must be a string", 422, field="region")
    for field in ("quota_enabled", "versioning_enabled", "object_lock_enabled", "set_default_retention"):
        if field in result:
            result[field] = _required_bool(result, field)
    if "quota_size" in result:
        result["quota_size"] = _required_int(result, "quota_size")
    quota_fields = {"quota_size", "quota_unit", "quota_enabled"}.intersection(result)
    if "quota_unit" in result:
        unit = _optional_string(result, "quota_unit")
        if unit not in QUOTA_UNITS:
            raise AppError("INVALID_REQUEST", "quota_unit must be one of B, KB, MB, GB, TB", 422, field="quota_unit")
    if quota_fields:
        if "quota_size" not in result:
            raise AppError("INVALID_REQUEST", "quota_size is required when quota settings are provided", 422, field="quota_size")
        result.setdefault("quota_unit", "MB")
        result.setdefault("quota_enabled", True)
        _validate_quota_bounds(result["quota_size"], result["quota_unit"], result["quota_enabled"])
    if "object_lock_duration" in result:
        result["object_lock_duration"] = _required_int(result, "object_lock_duration", minimum=1)
    if "object_lock_mode" in result:
        mode = _optional_string(result, "object_lock_mode")
        if mode and mode not in {"GOVERNANCE", "COMPLIANCE"}:
            raise AppError("INVALID_REQUEST", "object_lock_mode must be GOVERNANCE or COMPLIANCE", 422, field="object_lock_mode")
    if "owner" in result:
        result["owner"] = _optional_string(result, "owner")
    return result


def _ensure_empty_bucket_for_delete(request: Request, management: Mapping[str, Any], bucket: str) -> None:
    client_factory = getattr(_management_module(), "associated_s3_client", None)
    if client_factory is None:
        raise AppError("ASSOCIATED_S3_REQUIRED", "associated S3 client is required for safe bucket delete", 501)
    s3 = client_factory(request.app.state.settings, dict(management))
    try:
        objects = s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
        if objects.get("KeyCount") or objects.get("Contents"):
            raise AppError("BUCKET_NOT_EMPTY", "bucket delete requires empty current objects", 409)
        versions = s3.list_object_versions(Bucket=bucket, MaxKeys=1)
        if versions.get("Versions") or versions.get("DeleteMarkers"):
            raise AppError("BUCKET_NOT_EMPTY", "bucket delete requires empty version history", 409)
        multipart = s3.list_multipart_uploads(Bucket=bucket, MaxUploads=1)
        if multipart.get("Uploads"):
            raise AppError("BUCKET_NOT_EMPTY", "bucket delete requires no active multipart uploads", 409)
    finally:
        _close_resource(s3)


def _associated_s3(request: Request, management: Mapping[str, Any]):
    client_factory = getattr(_management_module(), "associated_s3_client", None)
    if client_factory is None:
        raise AppError("ASSOCIATED_S3_REQUIRED", "associated S3 client is required for standard S3 bucket settings", 501)
    return client_factory(request.app.state.settings, dict(management))


def _try_associated_s3(request: Request, management: Mapping[str, Any]):
    try:
        return _associated_s3(request, management)
    except AppError as exc:
        if exc.code == "ASSOCIATED_S3_REQUIRED":
            return None
        raise


def _s3_error_code(exc: Exception) -> str | None:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return str(code) if code else None


def _safe_s3_error_detail(exc: Exception) -> dict[str, Any]:
    code = _s3_error_code(exc)
    safe_codes = {
        "403",
        "404",
        "AccessDenied",
        "InvalidRequest",
        "NoSuchBucket",
        "NotFound",
        "NotImplemented",
        "NotSupported",
        "ObjectLockConfigurationNotFoundError",
        "NoSuchObjectLockConfiguration",
    }
    return {"s3_error_code": code} if code in safe_codes else {}


def _missing_capability(exc: Exception, capability: str) -> AppError:
    code = _s3_error_code(exc)
    if code in {"NotImplemented", "NotSupported", "NoSuchObjectLockConfiguration", "ObjectLockConfigurationNotFoundError"}:
        return AppError("UNSUPPORTED_CAPABILITY", f"{capability} is not supported by this SeaweedFS/S3 endpoint", 501)
    return AppError("S3_CONFIGURATION_UNAVAILABLE", f"failed to read {capability} from associated S3 endpoint", 502, detail=_safe_s3_error_detail(exc))


def _object_lock_not_configured(exc: Exception) -> bool:
    return _s3_error_code(exc) in {"NoSuchObjectLockConfiguration", "ObjectLockConfigurationNotFoundError"}


def _not_found_error(exc: Exception) -> bool:
    if isinstance(exc, AppError) and exc.status == 404:
        return True
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return code in {"404", "NoSuchBucket", "NotFound"}


def _same_named_resource(name_field: str, expected: str) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        raw = after.get("bucket") if isinstance(after, dict) and isinstance(after.get("bucket"), dict) else after
        return isinstance(raw, Mapping) and raw.get(name_field) == expected

    return verify


def _bucket_fields_match(expected: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        raw = after.get("bucket") if isinstance(after, dict) and isinstance(after.get("bucket"), dict) else after
        return isinstance(raw, Mapping) and all(raw.get(field) == value for field, value in expected.items())

    return verify


def _quota_bytes(size: int, unit: str) -> int:
    if size <= 0:
        return 0
    value = size * QUOTA_UNITS[unit]
    if value > INT64_MAX:
        raise AppError("INVALID_REQUEST", "quota_size and quota_unit exceed int64 quota limit", 422, field="quota_size")
    return value


def _validate_quota_bounds(size: int, unit: str, enabled: bool) -> int:
    value = _quota_bytes(size, unit)
    if enabled and value <= 0:
        raise AppError("INVALID_REQUEST", "enabled quota must be greater than 0 bytes", 422, field="quota_size")
    return value


def _bucket_quota_match(bucket: str, quota_size: int, quota_unit: str, quota_enabled: bool) -> Callable[[Any, Any, Any], bool]:
    expected_quota = _quota_bytes(quota_size, quota_unit)
    if not quota_enabled and expected_quota > 0:
        expected_quota = -expected_quota

    def verify(_before: Any, after: Any, _response: Any) -> bool:
        raw = after.get("bucket") if isinstance(after, dict) and isinstance(after.get("bucket"), dict) else after
        return isinstance(raw, Mapping) and raw.get("name") == bucket and raw.get("quota") == expected_quota and raw.get("quota_enabled") is quota_enabled

    return verify


def _created_bucket_matches(payload: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    expected = {field: payload[field] for field in ("name", "quota", "quota_enabled", "read_only", "versioning_status", "object_lock_enabled", "object_lock_mode", "object_lock_duration", "owner") if field in payload}
    if "quota_size" in payload:
        unit = str(payload.get("quota_unit") or "MB").upper()
        quota = _quota_bytes(int(payload["quota_size"]), unit)
        if not payload.get("quota_enabled") and quota > 0:
            quota = -quota
        expected["quota"] = quota

    return _bucket_fields_match(expected)


def _deleted(_before: Any, after: Any, _response: Any) -> bool:
    return after is None


def _user_fields_match(username: str, expected: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        return isinstance(after, Mapping) and after.get("username") == username and all(after.get(field) == value for field, value in expected.items())

    return verify


def _created_user_matches(username: str, expected: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, response: Any) -> bool:
        if not _user_fields_match(username, {field: expected[field] for field in ("email", "actions", "policy_names") if field in expected})(_before, after, response):
            return False
        if expected.get("generate_key"):
            key_id = _access_key_id(response) if isinstance(response, Mapping) else None
            return bool(key_id) and any(_access_key_id(key) == key_id for key in _extract_access_keys(after))
        return True

    return verify


def _created_access_key_visible(username: str) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, response: Any) -> bool:
        key_id = _access_key_id(response) if isinstance(response, Mapping) else None
        return bool(key_id) and isinstance(after, Mapping) and after.get("username") == username and any(_access_key_id(key) == key_id for key in _extract_access_keys(after))

    return verify


def _access_key_absent(username: str, access_key_id: str) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        return isinstance(after, Mapping) and after.get("username") == username and all(_access_key_id(key) != access_key_id for key in _extract_access_keys(after))

    return verify


def _access_key_status(username: str, access_key_id: str, status: str) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping) or after.get("username") != username:
            return False
        return any(_access_key_id(key) == access_key_id and (key.get("status") or key.get("Status")) == status for key in _extract_access_keys(after))

    return verify


def _user_policies_match(actions: list[str]) -> Callable[[Any, Any, Any], bool]:
    expected = sorted(actions)

    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping):
            return False
        if "policies" in after:
            actual = after["policies"]
        elif "Policies" in after:
            actual = after["Policies"]
        else:
            return False
        return isinstance(actual, list) and sorted(actual) == expected

    return verify


def _created_service_account_visible(parent_user: str) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, response: Any) -> bool:
        if not isinstance(response, Mapping) or not isinstance(after, Mapping):
            return False
        created = response.get("service_account") if isinstance(response.get("service_account"), Mapping) else response
        account_id = created.get("id") or created.get("ID") or created.get("service_account_id") or created.get("ServiceAccountId")
        if not account_id:
            return False
        accounts = after.get("service_accounts") or after.get("ServiceAccounts") or after.get("items") or []
        return any(isinstance(account, Mapping) and (account.get("id") or account.get("ID") or account.get("service_account_id") or account.get("ServiceAccountId")) == account_id and (account.get("parent_user") or account.get("ParentUser")) == parent_user for account in accounts)

    return verify


def _service_account_fields(account_id: str, expected: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping):
            return False
        canonical = _canonical_service_account(after)
        return canonical.get("id") == account_id and all(canonical.get(field) == value for field, value in expected.items())

    return verify


def _group_status(name: str, status: str) -> Callable[[Any, Any, Any], bool]:
    enabled = status == "enabled"

    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping) or after.get("name") != name:
            return False
        if "status" in after or "Status" in after:
            return (after.get("status") or after.get("Status")) == status
        if "enabled" in after:
            return after["enabled"] is enabled
        if "Enabled" in after:
            return after["Enabled"] is enabled
        return False

    return verify


def _contains_item(expected: str, *fields: str) -> Callable[[Any, Any, Any], bool]:
    return lambda _before, after, _response: _collection_contains(after, expected, *fields)


def _omits_item(expected: str, *fields: str) -> Callable[[Any, Any, Any], bool]:
    return lambda _before, after, _response: _collection_absent(after, expected, *fields)


def _policy_document_match(name: str, document: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping) or after.get("name") != name:
            return False
        actual = after.get("document") or after.get("policy") or after.get("Document") or after.get("Policy")
        return actual == dict(document)

    return verify


def _bucket_lifecycle_match(config: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    expected_rules = _normalize_lifecycle_config(config)["rules"]

    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping):
            return False
        try:
            actual_rules = _normalize_lifecycle_config(after)["rules"]
        except AppError:
            return False
        return actual_rules == expected_rules

    return verify


def _normalize_lifecycle_rule(raw: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        "days": "expiration_days",
        "expirationDays": "expiration_days",
        "expiration_date": "expiration_date",
        "expirationDate": "expiration_date",
        "delete_marker": "expired_object_delete_marker",
        "expiredObjectDeleteMarker": "expired_object_delete_marker",
        "noncurrent_days": "noncurrent_version_expiration_days",
        "noncurrentVersionExpirationDays": "noncurrent_version_expiration_days",
        "newerNoncurrentVersions": "newer_noncurrent_versions",
        "abortMultipartDays": "abort_multipart_days",
        "sizeGreaterThan": "size_greater_than",
        "sizeLessThan": "size_less_than",
    }
    allowed = {
        "id",
        "status",
        "prefix",
        "tags",
        "size_greater_than",
        "size_less_than",
        "expiration_days",
        "expiration_date",
        "expired_object_delete_marker",
        "noncurrent_version_expiration_days",
        "newer_noncurrent_versions",
        "abort_multipart_days",
    }
    rule: dict[str, Any] = {}
    for key, value in raw.items():
        target = aliases.get(key, key)
        if target == "enabled":
            continue
        if target not in allowed:
            raise AppError("INVALID_LIFECYCLE_RULE", f"unsupported lifecycle rule field: {key}", 422, field=f"lifecycle.rules.{key}")
        rule[target] = value
    if "status" not in rule:
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                raise AppError("INVALID_LIFECYCLE_RULE", "enabled must be a boolean", 422, field="lifecycle.rules.enabled")
            rule["status"] = "Enabled" if raw["enabled"] else "Disabled"
        else:
            rule["status"] = "Enabled"
    if rule["status"] not in {"Enabled", "Disabled"}:
        raise AppError("INVALID_LIFECYCLE_RULE", "status must be Enabled or Disabled", 422, field="lifecycle.rules.status")
    if "tags" in rule and not isinstance(rule["tags"], Mapping):
        raise AppError("INVALID_LIFECYCLE_RULE", "tags must be an object", 422, field="lifecycle.rules.tags")
    for field in ("size_greater_than", "size_less_than", "expiration_days", "noncurrent_version_expiration_days", "newer_noncurrent_versions", "abort_multipart_days"):
        if field in rule and (not isinstance(rule[field], int) or isinstance(rule[field], bool) or rule[field] < 0):
            raise AppError("INVALID_LIFECYCLE_RULE", f"{field} must be a non-negative integer", 422, field=f"lifecycle.rules.{field}")
    return {key: value for key, value in rule.items() if value not in (None, "", {}, [])}


def _normalize_lifecycle_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    source = raw.get("lifecycle") or raw
    if not isinstance(source, Mapping):
        raise AppError("INVALID_LIFECYCLE_CONFIGURATION", "lifecycle must be an object", 422, field="lifecycle")
    if "Lifecycle" in raw or "Rules" in source:
        raise AppError("INVALID_LIFECYCLE_CONFIGURATION", "use lower-case lifecycle.rules fields, not AWS-style upper-case fields", 422, field="lifecycle.rules")
    rules = source.get("rules")
    if rules is None:
        rules = []
    if not isinstance(rules, list) or not all(isinstance(rule, Mapping) for rule in rules):
        raise AppError("INVALID_LIFECYCLE_CONFIGURATION", "lifecycle.rules must be a list of objects", 422, field="lifecycle.rules")
    return {"rules": [_normalize_lifecycle_rule(rule) for rule in rules]}


def _bucket_policy_match(policy: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    expected = dict(policy)

    def verify(_before: Any, after: Any, _response: Any) -> bool:
        if not isinstance(after, Mapping):
            return False
        actual = after.get("policy") or after.get("Policy") or after
        return actual == expected

    return verify


def _config_deleted(_before: Any, after: Any, _response: Any) -> bool:
    return after in (None, {}, [])


def _versioning_matches(status: str) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        return isinstance(after, Mapping) and after.get("Status") == status

    return verify


def _object_lock_matches(config: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    def verify(_before: Any, after: Any, _response: Any) -> bool:
        return isinstance(after, Mapping) and after.get("ObjectLockConfiguration") == dict(config)

    return verify


def _bucket_quota_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if not {"quota_size", "quota_unit", "quota_enabled"}.intersection(payload):
        return None
    return {
        "quota_size": int(payload["quota_size"]),
        "quota_unit": str(payload.get("quota_unit") or "MB"),
        "quota_enabled": bool(payload.get("quota_enabled", True)),
    }


def _object_lock_requested(payload: Mapping[str, Any]) -> bool:
    return any(
        key in payload and bool(payload.get(key))
        for key in ("object_lock_enabled", "set_default_retention", "object_lock_mode", "object_lock_duration")
    )


def _default_retention_requested(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("set_default_retention")) or bool(payload.get("object_lock_mode")) or bool(payload.get("object_lock_duration"))


def _object_lock_config_from_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _object_lock_requested(payload):
        return None
    config: dict[str, Any] = {"ObjectLockEnabled": "Enabled"}
    if _default_retention_requested(payload):
        mode = payload.get("object_lock_mode")
        duration = payload.get("object_lock_duration")
        if not mode or not duration:
            raise AppError("INVALID_OBJECT_LOCK_CONFIGURATION", "object_lock_mode and object_lock_duration are required for default retention", 422)
        config["Rule"] = {"DefaultRetention": {"Mode": str(mode), "Days": int(duration)}}
    return config


def _s3_bucket_readback(s3: Any, bucket: str) -> Callable[[], Any]:
    def readback() -> Any:
        try:
            s3.head_bucket(Bucket=bucket)
            return {"bucket": bucket}
        except Exception as exc:
            if _not_found_error(exc):
                raise AppError("BUCKET_NOT_FOUND", "bucket was not found", 404) from exc
            raise

    return readback


def _s3_bucket_exists_readback(s3: Any, bucket: str) -> Callable[[], Any]:
    def readback() -> Any:
        try:
            s3.head_bucket(Bucket=bucket)
            return {"name": bucket}
        except Exception as exc:
            if _not_found_error(exc):
                raise AppError("BUCKET_NOT_FOUND", "bucket was not found", 404) from exc
            raise

    return readback


def _basic_s3_create_payload(payload: Mapping[str, Any]) -> bool:
    advanced_truthy = (
        "quota_size" in payload,
        "quota_unit" in payload,
        bool(payload.get("quota_enabled")),
        bool(payload.get("versioning_enabled")),
        bool(payload.get("object_lock_enabled")),
        bool(payload.get("set_default_retention")),
        bool(payload.get("owner")),
        bool(payload.get("object_lock_mode")),
        bool(payload.get("object_lock_duration")),
    )
    return not any(advanced_truthy)


def _s3_create_bucket_invoke(s3: Any, payload: Mapping[str, Any]) -> Callable[[], Any]:
    bucket = str(payload["name"])
    region = str(payload.get("region") or "")

    def invoke() -> Any:
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception as exc:
            if not _not_found_error(exc):
                raise
        else:
            raise AppError("BUCKET_ALREADY_EXISTS", "bucket already exists", 409)
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if region and region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        return s3.create_bucket(**kwargs)

    return invoke


def _advanced_s3_create_bucket_invoke(settings: Any, management: Mapping[str, Any], s3: Any, payload: Mapping[str, Any]) -> Callable[[], Any]:
    bucket = str(payload["name"])
    region = str(payload.get("region") or "")
    quota_payload = _bucket_quota_payload(payload)
    lock_config = _object_lock_config_from_payload(payload)
    enable_versioning = bool(payload.get("versioning_enabled")) or lock_config is not None
    owner = str(payload.get("owner") or "")

    def partial(applied_steps: list[str], step: str, exc: Exception) -> dict[str, Any]:
        detail = _safe_s3_error_detail(exc)
        if isinstance(exc, AppError):
            detail["code"] = exc.code
        return {
            "applied_steps": applied_steps,
            "partial": True,
            "partial_error": {"step": step, **detail},
        }

    def admin_write(method: str, path: str, body: Mapping[str, Any]) -> Any:
        with db.connect(settings) as conn:
            return _admin_request_json(settings, conn, management, method, path, body)

    def invoke() -> Any:
        applied_steps: list[str] = []
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception as exc:
            if not _not_found_error(exc):
                raise
        else:
            raise AppError("BUCKET_ALREADY_EXISTS", "bucket already exists", 409)
        create_kwargs: dict[str, Any] = {"Bucket": bucket}
        if region and region != "us-east-1":
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        if lock_config is not None:
            create_kwargs["ObjectLockEnabledForBucket"] = True
        s3.create_bucket(**create_kwargs)
        applied_steps.append("create_bucket")
        if enable_versioning:
            try:
                s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
                applied_steps.append("put_bucket_versioning")
            except Exception as exc:
                return partial(applied_steps, "put_bucket_versioning", exc)
        if lock_config is not None:
            try:
                s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration=lock_config)
                applied_steps.append("put_object_lock_configuration")
            except Exception as exc:
                return partial(applied_steps, "put_object_lock_configuration", exc)
        if quota_payload is not None:
            try:
                admin_write("PUT", f"/api/s3/buckets/{_q(bucket)}/quota", {**quota_payload, "bucket": bucket})
                applied_steps.append("put_bucket_quota")
            except Exception as exc:
                return partial(applied_steps, "put_bucket_quota", exc)
        if owner:
            try:
                admin_write("PUT", f"/api/s3/buckets/{_q(bucket)}/owner", {"owner": owner, "bucket": bucket})
                applied_steps.append("put_bucket_owner")
            except Exception as exc:
                return partial(applied_steps, "put_bucket_owner", exc)
        return {"applied_steps": applied_steps, "partial": False}

    return invoke


def _advanced_s3_bucket_readback(settings: Any, management: Mapping[str, Any], s3: Any, payload: Mapping[str, Any]) -> Callable[[], Any]:
    bucket = str(payload["name"])
    quota_payload = _bucket_quota_payload(payload)
    lock_config = _object_lock_config_from_payload(payload)
    read_versioning = bool(payload.get("versioning_enabled")) or lock_config is not None
    read_admin = quota_payload is not None or bool(payload.get("owner"))

    def admin_read(path: str) -> Any:
        with db.connect(settings) as conn:
            return _admin_get_json(settings, conn, management, path)

    def readback() -> dict[str, Any]:
        result: dict[str, Any] = {"name": bucket, "exists": False}
        try:
            s3.head_bucket(Bucket=bucket)
            result["exists"] = True
        except Exception as exc:
            if _not_found_error(exc):
                return result
            result["head_error"] = _safe_s3_error_detail(exc)
            return result
        if read_versioning:
            try:
                result["versioning"] = s3.get_bucket_versioning(Bucket=bucket)
            except Exception as exc:
                result["versioning_error"] = _safe_s3_error_detail(exc)
        if lock_config is not None:
            try:
                result["object_lock"] = s3.get_object_lock_configuration(Bucket=bucket)
            except Exception as exc:
                if _object_lock_not_configured(exc):
                    result["object_lock"] = {"ObjectLockConfiguration": {}}
                else:
                    result["object_lock_error"] = _safe_s3_error_detail(exc)
        if read_admin:
            try:
                admin_data = admin_read(f"/api/s3/buckets/{_q(bucket)}")
                result["admin"] = admin_data.get("bucket") if isinstance(admin_data, Mapping) and isinstance(admin_data.get("bucket"), Mapping) else admin_data
            except Exception as exc:
                result["admin_error"] = {"code": getattr(exc, "code", "ADMIN_READ_FAILED")}
        return result

    return readback


def _advanced_s3_bucket_matches(payload: Mapping[str, Any]) -> Callable[[Any, Any, Any], bool]:
    bucket = str(payload["name"])
    quota_payload = _bucket_quota_payload(payload)
    lock_config = _object_lock_config_from_payload(payload)
    expect_versioning = bool(payload.get("versioning_enabled")) or lock_config is not None
    owner = str(payload.get("owner") or "")

    def verify(_before: Any, after: Any, response: Any) -> bool:
        if not isinstance(response, Mapping) or response.get("partial") is True:
            return False
        if not isinstance(after, Mapping) or after.get("name") != bucket or after.get("exists") is not True:
            return False
        if expect_versioning and (not isinstance(after.get("versioning"), Mapping) or after["versioning"].get("Status") != "Enabled"):
            return False
        if lock_config is not None and (not isinstance(after.get("object_lock"), Mapping) or after["object_lock"].get("ObjectLockConfiguration") != lock_config):
            return False
        admin_data = after.get("admin")
        if quota_payload is not None:
            if not isinstance(admin_data, Mapping):
                return False
            expected_quota = _quota_bytes(quota_payload["quota_size"], quota_payload["quota_unit"])
            if not quota_payload["quota_enabled"] and expected_quota > 0:
                expected_quota = -expected_quota
            if admin_data.get("quota") != expected_quota or admin_data.get("quota_enabled") is not quota_payload["quota_enabled"]:
                return False
        if owner and (not isinstance(admin_data, Mapping) or admin_data.get("owner") != owner):
            return False
        return True

    return verify


@router.get("/{management_id}/buckets")
def list_buckets(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, "/api/s3/buckets")
    buckets = data.get("buckets") or data.get("Buckets") or []
    return {"items": [_canonical_bucket(dict(bucket)) for bucket in buckets], "total": data.get("total") or data.get("total_buckets") or len(buckets)}


@router.get("/{management_id}/buckets/{bucket}")
def get_bucket(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/s3/buckets/{_q(bucket)}")
    raw_bucket = data.get("bucket") or data
    return {"bucket": _canonical_bucket(dict(raw_bucket)), "updated_at": data.get("updated_at")}


@router.post("/{management_id}/buckets")
def create_bucket(request: Request, management_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    payload = _ensure_bucket_payload(body)
    s3 = _try_associated_s3(request, mgmt)
    if s3 is not None and _basic_s3_create_payload(payload):
        try:
            return _mutate(
                request,
                mgmt,
                action="bucket.manage",
                method="POST",
                path=f"/s3/buckets/{_q(str(payload['name']))}",
                payload=payload,
                invoke=_s3_create_bucket_invoke(s3, payload),
                readback=_s3_bucket_exists_readback(s3, str(payload["name"])),
                verify=_created_bucket_matches(payload),
            )
        finally:
            _close_resource(s3)
    if s3 is not None:
        try:
            return _mutate(
                request,
                mgmt,
                action="bucket.manage",
                method="POST",
                path=f"/s3/buckets/{_q(str(payload['name']))}/advanced-create",
                payload=payload,
                invoke=_advanced_s3_create_bucket_invoke(request.app.state.settings, mgmt, s3, payload),
                readback=_advanced_s3_bucket_readback(request.app.state.settings, mgmt, s3, payload),
                verify=_advanced_s3_bucket_matches(payload),
            )
        finally:
            _close_resource(s3)
    _close_resource(s3)
    return _mutate(request, mgmt, action="bucket.manage", method="POST", path="/api/s3/buckets", payload=payload, readback_path=f"/api/s3/buckets/{_q(payload['name'])}", verify=_created_bucket_matches(payload))


@router.delete("/{management_id}/buckets/{bucket}")
def delete_bucket(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id, write=True)
    _ensure_empty_bucket_for_delete(request, mgmt, bucket)
    s3 = _associated_s3(request, mgmt)
    try:
        return _mutate(
            request,
            mgmt,
            action="bucket.manage",
            method="DELETE",
            path=f"/s3/buckets/{_q(bucket)}",
            payload={"bucket": bucket},
            invoke=lambda: s3.delete_bucket(Bucket=bucket),
            readback=_s3_bucket_readback(s3, bucket),
            verify=_deleted,
        )
    finally:
        _close_resource(s3)


@router.get("/{management_id}/buckets/{bucket}/versioning")
def get_bucket_versioning(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id)
    s3 = _associated_s3(request, mgmt)
    try:
        data = s3.get_bucket_versioning(Bucket=bucket)
    except Exception as exc:
        raise _missing_capability(exc, "bucket versioning") from exc
    finally:
        _close_resource(s3)
    return {
        "bucket": bucket,
        "status": data.get("Status") or "Off",
        "mfa_delete": data.get("MFADelete"),
        "source": "associated_s3",
    }


@router.put("/{management_id}/buckets/{bucket}/versioning")
def update_bucket_versioning(request: Request, management_id: str, bucket: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    status = body.get("status")
    if status not in {"Enabled", "Suspended"}:
        raise AppError("INVALID_VERSIONING_STATUS", "status must be Enabled or Suspended", 422)
    s3 = _associated_s3(request, mgmt)
    payload = {"bucket": bucket, "versioning_configuration": {"Status": status}}
    try:
        return _mutate(
            request,
            mgmt,
            action="bucket.manage",
            method="PUT",
            path=f"/s3/buckets/{_q(bucket)}/versioning",
            payload=payload,
            invoke=lambda: s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": status}),
            readback=lambda: s3.get_bucket_versioning(Bucket=bucket),
            verify=_versioning_matches(status),
        )
    finally:
        _close_resource(s3)


@router.get("/{management_id}/buckets/{bucket}/object-lock")
def get_bucket_object_lock(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id)
    s3 = _associated_s3(request, mgmt)
    try:
        data = s3.get_object_lock_configuration(Bucket=bucket)
    except Exception as exc:
        if _object_lock_not_configured(exc):
            return {
                "bucket": bucket,
                "status": "Off",
                "configuration_state": "not_configured",
                "object_lock_configuration": {},
                "source": "associated_s3",
                "capability": "not_checked",
            }
        raise _missing_capability(exc, "bucket object lock") from exc
    finally:
        _close_resource(s3)
    return {
        "bucket": bucket,
        "status": "Enabled" if data.get("ObjectLockConfiguration") else "Off",
        "configuration_state": "configured" if data.get("ObjectLockConfiguration") else "not_configured",
        "object_lock_configuration": data.get("ObjectLockConfiguration") or {},
        "source": "associated_s3",
        "capability": "available",
    }


@router.put("/{management_id}/buckets/{bucket}/object-lock")
def update_bucket_object_lock(request: Request, management_id: str, bucket: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    config = body.get("object_lock_configuration") or body.get("ObjectLockConfiguration")
    if not isinstance(config, Mapping):
        raise AppError("INVALID_OBJECT_LOCK_CONFIGURATION", "object_lock_configuration is required", 422)
    s3 = _associated_s3(request, mgmt)
    payload = {"bucket": bucket, "object_lock_configuration": dict(config)}
    try:
        return _mutate(
            request,
            mgmt,
            action="bucket.manage",
            method="PUT",
            path=f"/s3/buckets/{_q(bucket)}/object-lock",
            payload=payload,
            invoke=lambda: s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration=dict(config)),
            readback=lambda: s3.get_object_lock_configuration(Bucket=bucket),
            verify=_object_lock_matches(config),
        )
    finally:
        _close_resource(s3)


@router.put("/{management_id}/buckets/{bucket}/quota")
def update_bucket_quota(request: Request, management_id: str, bucket: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    unit = _optional_string(body, "quota_unit", "GB")
    if unit not in QUOTA_UNITS:
        raise AppError("INVALID_REQUEST", "quota_unit must be one of B, KB, MB, GB, TB", 422, field="quota_unit")
    payload = {"quota_size": _required_int(body, "quota_size"), "quota_unit": unit, "quota_enabled": _optional_bool(body, "quota_enabled", True)}
    _validate_quota_bounds(payload["quota_size"], payload["quota_unit"], payload["quota_enabled"])
    return _mutate(request, mgmt, action="bucket.manage", method="PUT", path=f"/api/s3/buckets/{_q(bucket)}/quota", payload={**payload, "bucket": bucket}, readback_path=f"/api/s3/buckets/{_q(bucket)}", verify=_bucket_quota_match(bucket, payload["quota_size"], payload["quota_unit"], payload["quota_enabled"]))


@router.put("/{management_id}/buckets/{bucket}/owner")
def update_bucket_owner(request: Request, management_id: str, bucket: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    payload = {"owner": _required_string(body, "owner")}
    return _mutate(request, mgmt, action="bucket.manage", method="PUT", path=f"/api/s3/buckets/{_q(bucket)}/owner", payload={**payload, "bucket": bucket}, readback_path=f"/api/s3/buckets/{_q(bucket)}", verify=_bucket_fields_match({"name": bucket, **payload}))


@router.get("/{management_id}/buckets/{bucket}/lifecycle")
def get_bucket_lifecycle(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/s3/buckets/{_q(bucket)}/lifecycle")


@router.put("/{management_id}/buckets/{bucket}/lifecycle")
def update_bucket_lifecycle(request: Request, management_id: str, bucket: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    lifecycle = _normalize_lifecycle_config(body)
    payload = {**lifecycle, "bucket": bucket}
    return _mutate(request, mgmt, action="bucket.manage", method="PUT", path=f"/api/s3/buckets/{_q(bucket)}/lifecycle", payload=payload, readback_path=f"/api/s3/buckets/{_q(bucket)}/lifecycle", verify=_bucket_lifecycle_match(lifecycle))


@router.delete("/{management_id}/buckets/{bucket}/lifecycle")
def delete_bucket_lifecycle(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id, write=True)
    return _mutate(request, mgmt, action="bucket.manage", method="DELETE", path=f"/api/s3/buckets/{_q(bucket)}/lifecycle", payload={"bucket": bucket}, readback_path=f"/api/s3/buckets/{_q(bucket)}/lifecycle", verify=_config_deleted)


@router.get("/{management_id}/buckets/{bucket}/policy")
def get_bucket_policy(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/s3/buckets/{_q(bucket)}/policy")


@router.put("/{management_id}/buckets/{bucket}/policy")
def update_bucket_policy(request: Request, management_id: str, bucket: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    policy = body.get("policy") or body.get("Policy") or body
    if not isinstance(policy, Mapping):
        raise AppError("INVALID_BUCKET_POLICY", "policy must be an object", 422, field="policy")
    payload = {"bucket": bucket, "policy": dict(policy)}
    return _mutate(request, mgmt, action="bucket.manage", method="PUT", path=f"/api/s3/buckets/{_q(bucket)}/policy", payload=payload, readback_path=f"/api/s3/buckets/{_q(bucket)}/policy", verify=_bucket_policy_match(policy))


@router.delete("/{management_id}/buckets/{bucket}/policy")
def delete_bucket_policy(request: Request, management_id: str, bucket: str):
    mgmt = _require_management(request, management_id, write=True)
    return _mutate(request, mgmt, action="bucket.manage", method="DELETE", path=f"/api/s3/buckets/{_q(bucket)}/policy", payload={"bucket": bucket}, readback_path=f"/api/s3/buckets/{_q(bucket)}/policy", verify=_config_deleted)


@router.get("/{management_id}/iam/users")
def list_users(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, "/api/users")
    users = data.get("users") or data.get("Users") or []
    return {"items": [_canonical_user(dict(user)) for user in users], "total": data.get("total_users") or len(users), "source": "object_store_users_api"}


@router.get("/{management_id}/iam/users/{username}")
def get_user(request: Request, management_id: str, username: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/users/{_q(username)}")
    return _canonical_user(dict(data), detail=True)


@router.post("/{management_id}/iam/users")
def create_user(request: Request, management_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    _reject_manual_secret(body)
    payload = {
        "username": body.get("username"),
        "email": _optional_string(body, "email", ""),
        "actions": _string_list(body, "actions"),
        "generate_key": _optional_bool(body, "generate_key", False),
        "policy_names": _string_list(body, "policy_names"),
    }
    if not payload["username"]:
        raise AppError("INVALID_USERNAME", "username is required", 422)
    readback_path = f"/api/users/{_q(payload['username'])}"
    return _mutate(request, mgmt, action="iam.manage", method="POST", path="/api/users", payload=payload, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, readback_path), verify=_created_user_matches(str(payload["username"]), payload), return_created_secret=payload["generate_key"])


@router.put("/{management_id}/iam/users/{username}")
def update_user(request: Request, management_id: str, username: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    _reject_manual_secret(body)
    payload = {"email": _optional_string(body, "email", ""), "actions": _string_list(body, "actions"), "policy_names": _string_list(body, "policy_names")}
    return _mutate(request, mgmt, action="iam.manage", method="PUT", path=f"/api/users/{_q(username)}", payload=payload, readback_path=f"/api/users/{_q(username)}", verify=_user_fields_match(username, payload))


@router.delete("/{management_id}/iam/users/{username}")
def delete_user(request: Request, management_id: str, username: str):
    mgmt = _require_management(request, management_id, write=True)
    path = f"/api/users/{_q(username)}"
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=path, payload={}, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, path), verify=_deleted)


@router.post("/{management_id}/iam/users/{username}/access-keys")
def create_access_key(request: Request, management_id: str, username: str, body: dict[str, Any] | None = None):
    mgmt = _require_management(request, management_id, write=True)
    body = body or {}
    if "access_key" in body or "secret_key" in body:
        raise AppError("MANUAL_SECRET_IMPORT_UNSUPPORTED", "manual access key import is not supported", 422)
    return _mutate(request, mgmt, action="iam.manage", method="POST", path=f"/api/users/{_q(username)}/access-keys", payload={}, readback_path=f"/api/users/{_q(username)}", verify=_created_access_key_visible(username), return_created_secret=True)


@router.delete("/{management_id}/iam/users/{username}/access-keys/{access_key_id}")
def delete_access_key(request: Request, management_id: str, username: str, access_key_id: str):
    mgmt = _require_management(request, management_id, write=True)
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=f"/api/users/{_q(username)}/access-keys/{_q(access_key_id)}", payload={}, readback_path=f"/api/users/{_q(username)}", verify=_access_key_absent(username, access_key_id))


@router.put("/{management_id}/iam/users/{username}/access-keys/{access_key_id}/status")
def update_access_key_status(request: Request, management_id: str, username: str, access_key_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    status = body.get("status")
    if status not in {"Active", "Inactive"}:
        raise AppError("INVALID_ACCESS_KEY_STATUS", "status must be Active or Inactive", 422)
    return _mutate(request, mgmt, action="iam.manage", method="PUT", path=f"/api/users/{_q(username)}/access-keys/{_q(access_key_id)}/status", payload={"status": status}, readback_path=f"/api/users/{_q(username)}", verify=_access_key_status(username, access_key_id, status))


@router.get("/{management_id}/iam/users/{username}/policies")
def get_user_policies(request: Request, management_id: str, username: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/users/{_q(username)}/policies")


@router.put("/{management_id}/iam/users/{username}/policies")
def update_user_policies(request: Request, management_id: str, username: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    actions = _string_list(body, "actions")
    return _mutate(request, mgmt, action="iam.manage", method="PUT", path=f"/api/users/{_q(username)}/policies", payload={"actions": actions}, readback_path=f"/api/users/{_q(username)}/policies", verify=_user_policies_match(actions))


@router.get("/{management_id}/iam/principals")
def list_principals(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, "/api/principals")
    if isinstance(data, Mapping):
        principals = data["principals"] if "principals" in data else data.get("Principals")
        # Go's nil []string is JSON null when there are no candidates.
        if principals is None and ("principals" in data or "Principals" in data):
            principals = []
    else:
        principals = data
    if not isinstance(principals, list) or not all(isinstance(item, str) for item in principals):
        raise AppError("INVALID_PRINCIPALS_RESPONSE", "official principals endpoint returned an unexpected shape", 502)
    redacted = _redact(dict(data)) if isinstance(data, Mapping) else {"principals": principals}
    return {"items": [_canonical_principal(item) for item in principals], "principals": list(principals),
            "total": len(principals), "source": "official_admin_principals_api",
            "coverage": "suggestions_only_not_identity_inventory",
            "upstream": {key: value for key, value in redacted.items() if key not in {"principals", "Principals"}}}


@router.get("/{management_id}/iam/service-accounts")
def list_service_accounts(request: Request, management_id: str, parent_user: str | None = None):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, "/api/service-accounts", params={"parent_user": parent_user} if parent_user else None)
    accounts = data.get("service_accounts") or data.get("ServiceAccounts") or []
    return {"items": [_canonical_service_account(dict(account)) for account in accounts], "total": len(accounts)}


@router.get("/{management_id}/iam/service-accounts/{account_id}")
def get_service_account(request: Request, management_id: str, account_id: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/service-accounts/{_q(account_id)}")
    return _canonical_service_account(dict(data))


@router.post("/{management_id}/iam/service-accounts")
def create_service_account(request: Request, management_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    _reject_manual_secret(body)
    payload = {"parent_user": body.get("parent_user"), "description": _optional_string(body, "description", ""), "expiration": _optional_string(body, "expiration", "")}
    if not payload["parent_user"]:
        raise AppError("INVALID_PARENT_USER", "parent_user is required", 422)
    return _mutate(request, mgmt, action="iam.manage", method="POST", path="/api/service-accounts", payload=payload, readback_path="/api/service-accounts", verify=_created_service_account_visible(str(payload["parent_user"])), return_created_secret=True)


@router.put("/{management_id}/iam/service-accounts/{account_id}")
def update_service_account(request: Request, management_id: str, account_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    _reject_manual_secret(body)
    status = _optional_string(body, "status", "")
    if status and status not in {"Active", "Inactive"}:
        raise AppError("INVALID_SERVICE_ACCOUNT_STATUS", "status must be Active or Inactive", 422, field="status")
    payload = {"status": status, "description": _optional_string(body, "description", ""), "expiration": _optional_string(body, "expiration", "")}
    return _mutate(request, mgmt, action="iam.manage", method="PUT", path=f"/api/service-accounts/{_q(account_id)}", payload=payload, readback_path=f"/api/service-accounts/{_q(account_id)}", verify=_service_account_fields(account_id, payload))


@router.delete("/{management_id}/iam/service-accounts/{account_id}")
def delete_service_account(request: Request, management_id: str, account_id: str):
    mgmt = _require_management(request, management_id, write=True)
    path = f"/api/service-accounts/{_q(account_id)}"
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=path, payload={}, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, path), verify=_deleted)


@router.get("/{management_id}/iam/groups")
def list_groups(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        data = _admin_get_json(request.app.state.settings, conn, mgmt, "/api/groups")
    groups = data.get("groups") or data.get("Groups") or []
    return {"items": groups, "total": data.get("total_groups") or len(groups)}


@router.post("/{management_id}/iam/groups")
def create_group(request: Request, management_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    name = str(body.get("name") or "")
    if not name:
        raise AppError("INVALID_GROUP_NAME", "group name is required", 422)
    readback_path = f"/api/groups/{_q(name)}"
    return _mutate(request, mgmt, action="iam.manage", method="POST", path="/api/groups", payload={"name": name}, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, readback_path), verify=_same_named_resource("name", name))


@router.get("/{management_id}/iam/groups/{name}")
def get_group(request: Request, management_id: str, name: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/groups/{_q(name)}")


@router.delete("/{management_id}/iam/groups/{name}")
def delete_group(request: Request, management_id: str, name: str):
    mgmt = _require_management(request, management_id, write=True)
    path = f"/api/groups/{_q(name)}"
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=path, payload={}, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, path), verify=_deleted)


@router.put("/{management_id}/iam/groups/{name}/status")
def set_group_status(request: Request, management_id: str, name: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    status = body.get("status")
    if status not in {"enabled", "disabled"}:
        raise AppError("INVALID_GROUP_STATUS", "status must be enabled or disabled", 422)
    return _mutate(request, mgmt, action="iam.manage", method="PUT", path=f"/api/groups/{_q(name)}/status", payload={"enabled": status == "enabled"}, readback_path=f"/api/groups/{_q(name)}", verify=_group_status(name, status))


@router.get("/{management_id}/iam/groups/{name}/members")
def get_group_members(request: Request, management_id: str, name: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/groups/{_q(name)}/members")


@router.post("/{management_id}/iam/groups/{name}/members")
def add_group_member(request: Request, management_id: str, name: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    username = str(body.get("username") or "")
    if not username:
        raise AppError("INVALID_USERNAME", "username is required", 422)
    return _mutate(request, mgmt, action="iam.manage", method="POST", path=f"/api/groups/{_q(name)}/members", payload={"username": username}, readback_path=f"/api/groups/{_q(name)}/members", verify=_contains_item(username, "username", "name"))


@router.delete("/{management_id}/iam/groups/{name}/members/{username}")
def remove_group_member(request: Request, management_id: str, name: str, username: str):
    mgmt = _require_management(request, management_id, write=True)
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=f"/api/groups/{_q(name)}/members/{_q(username)}", payload={}, readback_path=f"/api/groups/{_q(name)}/members", verify=_omits_item(username, "username", "name"))


@router.get("/{management_id}/iam/groups/{name}/policies")
def get_group_policies(request: Request, management_id: str, name: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/groups/{_q(name)}/policies")


@router.post("/{management_id}/iam/groups/{name}/policies")
def attach_group_policy(request: Request, management_id: str, name: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    policy_name = str(body.get("policy_name") or body.get("name") or "")
    if not policy_name:
        raise AppError("INVALID_POLICY_NAME", "policy_name is required", 422)
    return _mutate(request, mgmt, action="iam.manage", method="POST", path=f"/api/groups/{_q(name)}/policies", payload={"policy_name": policy_name}, readback_path=f"/api/groups/{_q(name)}/policies", verify=_contains_item(policy_name, "policy_name", "name"))


@router.delete("/{management_id}/iam/groups/{name}/policies/{policy_name}")
def detach_group_policy(request: Request, management_id: str, name: str, policy_name: str):
    mgmt = _require_management(request, management_id, write=True)
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=f"/api/groups/{_q(name)}/policies/{_q(policy_name)}", payload={}, readback_path=f"/api/groups/{_q(name)}/policies", verify=_omits_item(policy_name, "policy_name", "name"))


@router.get("/{management_id}/iam/policies")
def list_policies(request: Request, management_id: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, "/api/object-store/policies")


@router.get("/{management_id}/iam/policies/{name}")
def get_policy(request: Request, management_id: str, name: str):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_get_json(request.app.state.settings, conn, mgmt, f"/api/object-store/policies/{_q(name)}")


@router.post("/{management_id}/iam/policies/validate")
def validate_policy(request: Request, management_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id)
    with db.connect(request.app.state.settings) as conn:
        return _admin_request_json(request.app.state.settings, conn, mgmt, "POST", "/api/object-store/policies/validate", body)


@router.post("/{management_id}/iam/policies")
def create_policy(request: Request, management_id: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    name = str(body.get("name") or "")
    document = body.get("document") or body.get("policy")
    if not name or not isinstance(document, Mapping):
        raise AppError("INVALID_POLICY", "name and document are required", 422)
    readback_path = f"/api/object-store/policies/{_q(name)}"
    return _mutate(request, mgmt, action="iam.manage", method="POST", path="/api/object-store/policies", payload={"name": name, "document": dict(document)}, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, readback_path), verify=_policy_document_match(name, document))


@router.put("/{management_id}/iam/policies/{name}")
def update_policy(request: Request, management_id: str, name: str, body: dict[str, Any]):
    mgmt = _require_management(request, management_id, write=True)
    document = body.get("document") or body.get("policy")
    if not isinstance(document, Mapping):
        raise AppError("INVALID_POLICY", "document is required", 422)
    return _mutate(request, mgmt, action="iam.manage", method="PUT", path=f"/api/object-store/policies/{_q(name)}", payload={"document": dict(document)}, readback_path=f"/api/object-store/policies/{_q(name)}", verify=_policy_document_match(name, document))


@router.delete("/{management_id}/iam/policies/{name}")
def delete_policy(request: Request, management_id: str, name: str):
    mgmt = _require_management(request, management_id, write=True)
    path = f"/api/object-store/policies/{_q(name)}"
    return _mutate(request, mgmt, action="iam.manage", method="DELETE", path=path, payload={}, readback=lambda: _admin_read_json_or_none(request.app.state.settings, mgmt, path), verify=_deleted)

