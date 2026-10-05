from __future__ import annotations

from contextlib import closing
import re
from typing import Any, Iterator

from .config import Settings, load_secret_registry


class StorageError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.status = status


def secret_for_connection(settings: Settings, connection: dict[str, Any]) -> dict[str, str | None]:
    registry = load_secret_registry(settings)
    secret_ref = connection["secret_ref"]
    entry = registry.get(secret_ref)
    if not isinstance(entry, dict):
        raise StorageError("SECRET_REF_NOT_FOUND", "Secret reference is not registered.", 403)
    secret = {
        "aws_access_key_id": entry.get("access_key_id"),
        "aws_secret_access_key": entry.get("secret_access_key"),
        "aws_session_token": entry.get("session_token"),
    }
    if not secret["aws_access_key_id"] or not secret["aws_secret_access_key"]:
        raise StorageError("SECRET_REF_INCOMPLETE", "Secret reference is missing required S3 credentials.", 403)
    allowed_connection_id = entry.get("allowed_connection_id")
    if allowed_connection_id and allowed_connection_id != connection["id"]:
        raise StorageError("SECRET_REF_FORBIDDEN", "Secret reference is not bound to this connection.", 403)
    allowed_endpoint = entry.get("allowed_endpoint_url")
    if not isinstance(allowed_endpoint, str) or not allowed_endpoint.strip():
        raise StorageError("SECRET_ENDPOINT_REQUIRED", "Secret reference must be bound to an approved storage endpoint.", 403)
    if str(allowed_endpoint).strip().rstrip("/") != str(connection["endpoint_url"]).rstrip("/"):
        raise StorageError("SECRET_ENDPOINT_FORBIDDEN", "Secret reference is not bound to this storage endpoint.", 403)
    return secret


def client(connection: dict[str, Any], settings: Settings):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise StorageError("BOTO3_UNAVAILABLE", "boto3 is not installed.", 503) from exc

    secret = secret_for_connection(settings, connection)
    config = Config(
        connect_timeout=settings.s3_connect_timeout_seconds,
        read_timeout=settings.s3_read_timeout_seconds,
        s3={"addressing_style": connection.get("addressing_style") or "path"},
    )
    return boto3.client(
        "s3",
        endpoint_url=connection["endpoint_url"],
        region_name=connection.get("region"),
        verify=bool(connection.get("verify_tls", True)),
        config=config,
        **{k: v for k, v in secret.items() if v is not None},
    )


def probe_read_capabilities(s3: Any, bucket: str | None = None, prefix: str = "") -> dict[str, Any]:
    capabilities: dict[str, str] = {
        "list_buckets": "not_checked",
        "list_objects_v2": "not_checked",
        "head_object": "not_checked",
        "get_object": "not_checked",
        "put_object": "not_checked",
        "delete_object": "not_checked",
        "copy_object": "not_checked",
        "bucket_cors": "not_checked",
        "versioning": "not_checked",
        "object_lock": "not_checked",
        "conditional_delete_if_match": "unknown",
    }
    evidence: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {"server_version": None, "version_source": None}
    if bucket:
        page = _record_probe(capabilities, evidence, "list_objects_v2", "ListObjectsV2", lambda: s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1))
        _merge_metadata(metadata, _server_metadata(page))
        first_key = _first_key(page)
        if capabilities["list_objects_v2"] == "supported" and first_key:
            head = _record_probe(capabilities, evidence, "head_object", "HeadObject", lambda: s3.head_object(Bucket=bucket, Key=first_key))
            _merge_metadata(metadata, _server_metadata(head))
            if capabilities["head_object"] == "supported":
                response = _record_probe(
                    capabilities,
                    evidence,
                    "get_object",
                    "GetObjectRange",
                    lambda: s3.get_object(Bucket=bucket, Key=first_key, Range="bytes=0-0"),
                )
                _close_body(response)
                _merge_metadata(metadata, _server_metadata(response))
        _record_probe(capabilities, evidence, "bucket_cors", "GetBucketCors", lambda: s3.get_bucket_cors(Bucket=bucket), cors=True)
        _record_probe(capabilities, evidence, "versioning", "GetBucketVersioning", lambda: s3.get_bucket_versioning(Bucket=bucket))
    else:
        response = _record_probe(capabilities, evidence, "list_buckets", "ListBuckets", lambda: s3.list_buckets())
        _merge_metadata(metadata, _server_metadata(response))
    return {"capabilities": capabilities, "evidence": evidence, **metadata}


def _record_probe(
    capabilities: dict[str, str],
    evidence: list[dict[str, Any]],
    key: str,
    operation: str,
    fn,
    cors: bool = False,
) -> None:
    try:
        response = fn()
        status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)) if isinstance(response, dict) else 200
        capabilities[key] = "supported"
        evidence.append({"operation": operation, "result": "success", "storage_status": status})
        return response
    except Exception as exc:
        status = _status_from_exception(exc)
        error_code = _code_from_exception(exc)
        if status == 403:
            capabilities[key] = "permission_denied"
        elif cors and status == 404 and error_code in {"NoSuchCORSConfiguration", "NoSuchCORS"}:
            capabilities[key] = "supported"
            evidence.append({"operation": operation, "result": "not_configured", "storage_status": status})
            return None
        elif status == 404 and error_code in {"NoSuchBucket", "NoSuchKey", "NotFound", "404"}:
            capabilities[key] = "not_checked" if key in {"head_object", "get_object"} else "unknown"
        elif status in {501, 405} or error_code in {"NotImplemented", "NotSupported", "UnsupportedOperation"}:
            capabilities[key] = "unsupported"
        elif status is None:
            capabilities[key] = "unreachable"
        else:
            capabilities[key] = "unknown"
        evidence.append({"operation": operation, "result": capabilities[key], "storage_status": status})
        return None


def _status_from_exception(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return status
    return None


def _code_from_exception(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code is not None:
            return str(code)
    return None


def _first_key(page: Any) -> str | None:
    if not isinstance(page, dict):
        return None
    contents = page.get("Contents") or []
    if not contents:
        return None
    key = contents[0].get("Key")
    return str(key) if key else None


def _close_body(response: Any) -> None:
    if isinstance(response, dict):
        body = response.get("Body")
        if body is not None and hasattr(body, "close"):
            body.close()


def _server_metadata(response: Any) -> dict[str, str | None]:
    if not isinstance(response, dict):
        return {"server_version": None, "version_source": None}
    headers = response.get("ResponseMetadata", {}).get("HTTPHeaders") or {}
    server = headers.get("server") or headers.get("Server")
    if not isinstance(server, str):
        return {"server_version": None, "version_source": None}
    match = re.search(r"SeaweedFS\b.*?\b(\d+(?:\.\d+)+)\b", server)
    if not match:
        return {"server_version": None, "version_source": None}
    return {"server_version": match.group(1), "version_source": "s3_server_header"}


def _merge_metadata(target: dict[str, Any], candidate: dict[str, Any]) -> None:
    if not target.get("server_version") and candidate.get("server_version"):
        target.update(candidate)


def stream_object_body(body: Any, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with closing(body):
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk


def sanitize_storage_exception(exc: Exception) -> StorageError:
    status = _status_from_exception(exc)
    if status == 403:
        return StorageError("STORAGE_FORBIDDEN", "Storage denied the request.", 403)
    if status == 404:
        return StorageError("OBJECT_NOT_FOUND", "Storage object was not found.", 404)
    if status == 412:
        return StorageError("STORAGE_PRECONDITION_FAILED", "Storage precondition failed.", 412)
    return StorageError("STORAGE_UNAVAILABLE", "Storage is unavailable.", 503)
