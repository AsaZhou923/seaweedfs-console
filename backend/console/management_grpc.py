from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from pathlib import Path
from typing import Any, Callable
from fastapi import APIRouter, Request

from . import db, management
from .config import Settings, load_secret_registry
from .security import AppError


router = APIRouter(prefix="/api/v1/management")

GRPC_TIMEOUT_SECONDS = 5.0
GRPC_MAX_RECEIVE_BYTES = 8 * 1024 * 1024
GRPC_MAX_CERT_BYTES = 256 * 1024
MOUNT_CLIENT_TYPES = ["mount", "sw-vfs"]
_CHANNEL_FACTORY: Callable[[dict[str, Any]], Any] | None = None


@router.get("/{management_id}/modules/mount-clients")
def mount_clients(request: Request, management_id: str):
    settings = request.app.state.settings
    public = management.require_management(request, management_id)
    with db.connect(settings) as conn:
        management.initialize(conn)
        mgmt = management.require_management_row(conn, public["id"])
        secret = _admin_secret(settings, mgmt["admin_secret_ref"])
    config = _grpc_endpoint(settings, secret, "filer")
    if config is None:
        return _not_configured(management_id)
    try:
        result = _list_mount_clients(config)
    except AppError as exc:
        return _response(management_id, _status_from_error(exc), None, [], error_code=exc.code)
    return _response(management_id, "supported", len(result), result)


def _not_configured(management_id: str) -> dict[str, Any]:
    return _response(management_id, "not_configured", None, [])


def _response(management_id: str, status: str, count: int | None, items: list[dict[str, Any]], *, error_code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "connection_id": management_id,
        "health_scope": "configured_filer",
        "source": "filer_grpc_ListMetadataSubscribers",
        "checked_at": _utc_now(),
        "status": status,
        "count": count,
        "items": items,
    }
    if error_code:
        result["error_code"] = error_code
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _admin_secret(settings: Settings, secret_ref: str) -> dict[str, Any]:
    entry = load_secret_registry(settings).get(secret_ref)
    if not isinstance(entry, dict) or entry.get("kind") != "seaweed_admin":
        raise AppError("ADMIN_SECRET_NOT_FOUND", "Admin secret reference is not registered.", 403)
    return entry


def _grpc_endpoint(settings: Settings, secret: dict[str, Any], name: str) -> dict[str, Any] | None:
    endpoints = secret.get("allowed_grpc_endpoints") or {}
    if not isinstance(endpoints, dict):
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint registry is invalid.", 403)
    raw = endpoints.get(name)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint approval is invalid.", 403)
    target = _validate_target(raw.get("target"))
    transport = raw.get("transport")
    if transport not in {"plaintext", "tls"}:
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint transport is invalid.", 403)
    allow_plaintext_non_loopback = raw.get("allow_plaintext_non_loopback") is True
    if transport == "plaintext" and not allow_plaintext_non_loopback and not _is_loopback_target(target):
        raise AppError("GRPC_ENDPOINT_DENIED", "plaintext gRPC endpoints must be loopback unless explicitly approved.", 403)
    config: dict[str, Any] = {"target": target, "transport": transport, "root_cert_ref": raw.get("root_cert_ref")}
    if transport == "tls":
        root_cert_ref = raw.get("root_cert_ref")
        if not isinstance(root_cert_ref, str) or not root_cert_ref.strip():
            raise AppError("GRPC_ENDPOINT_DENIED", "TLS gRPC endpoint must reference configured certificate material.", 403)
        config["root_certificates"] = _load_tls_root_certificate(settings, root_cert_ref.strip())
    return config


def _load_tls_root_certificate(settings: Settings, root_cert_ref: str) -> bytes:
    entry = load_secret_registry(settings).get(root_cert_ref)
    if not isinstance(entry, dict) or entry.get("kind") != "grpc_tls":
        raise AppError("GRPC_ENDPOINT_DENIED", "TLS gRPC certificate reference is not registered.", 403)
    if entry.get("requires_mtls") is True:
        raise AppError("GRPC_ENDPOINT_DENIED", "mTLS gRPC endpoints require client certificate configuration.", 403)
    path_value = entry.get("root_certificate_file")
    if not isinstance(path_value, str) or not path_value.strip():
        raise AppError("GRPC_ENDPOINT_DENIED", "TLS gRPC certificate file is not configured.", 403)
    cert_path = Path(path_value)
    try:
        if not cert_path.is_file() or cert_path.stat().st_size > GRPC_MAX_CERT_BYTES:
            raise OSError("invalid certificate file")
        data = cert_path.read_bytes()
    except OSError as exc:
        raise AppError("GRPC_ENDPOINT_DENIED", "TLS gRPC certificate file is unavailable.", 403) from exc
    if not data:
        raise AppError("GRPC_ENDPOINT_DENIED", "TLS gRPC certificate file is empty.", 403)
    return data


def _validate_target(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint target is required.", 403)
    raw = value.strip()
    if "://" in raw or "/" in raw or "?" in raw or "#" in raw or "@" in raw:
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint target must be host:port only.", 403)
    host_port = raw.rsplit(":", 1)
    if len(host_port) != 2 or not host_port[0] or not host_port[1].isdigit():
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint target must include a numeric port.", 403)
    host = host_port[0]
    if host.startswith("[") != host.endswith("]") or host in {"[]", ""}:
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint host is invalid.", 403)
    port = int(host_port[1])
    if port <= 0 or port > 65535:
        raise AppError("GRPC_ENDPOINT_DENIED", "gRPC endpoint port is invalid.", 403)
    return raw


def _is_loopback_target(target: str) -> bool:
    host = target.rsplit(":", 1)[0].strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _list_mount_clients(config: dict[str, Any]) -> list[dict[str, Any]]:
    channel = None
    try:
        pb2, pb2_grpc = _mount_modules()
        factory = _CHANNEL_FACTORY or _default_channel_factory
        channel = factory(config)
        stub = pb2_grpc.SeaweedFilerStub(channel)
        request = pb2.ListMetadataSubscribersRequest(client_types=MOUNT_CLIENT_TYPES)
        response = stub.ListMetadataSubscribers(request, timeout=GRPC_TIMEOUT_SECONDS)
        subscribers = getattr(response, "subscribers", response if isinstance(response, list) else None)
        if subscribers is None:
            raise AppError("GRPC_BAD_RESPONSE", "Filer gRPC response schema is unsupported.", 502)
        items = [_subscriber_to_dict(item) for item in subscribers]
        return [item for item in items if item["client_type"] in MOUNT_CLIENT_TYPES]
    except AppError:
        raise
    except Exception as exc:
        raise _classify_rpc_error(exc) from exc
    finally:
        close = getattr(channel, "close", None)
        if callable(close):
            close()


def _classify_rpc_error(exc: Exception) -> AppError:
    status_code = getattr(getattr(exc, "code", None), "__call__", lambda: None)()
    status_name = getattr(status_code, "name", "")
    if status_name in {"PERMISSION_DENIED", "UNAUTHENTICATED"}:
        return AppError("GRPC_PERMISSION_DENIED", "Filer gRPC permission was denied.", 403)
    if status_name == "UNIMPLEMENTED":
        return AppError("GRPC_UNIMPLEMENTED", "Filer gRPC method is unsupported.", 501)
    if status_name in {"RESOURCE_EXHAUSTED", "DATA_LOSS", "INTERNAL"}:
        return AppError("GRPC_BAD_RESPONSE", "Filer gRPC response is invalid or too large.", 502)
    if status_name in {"UNAVAILABLE", "DEADLINE_EXCEEDED", ""}:
        return AppError("GRPC_UNREACHABLE", "Filer gRPC endpoint is unreachable.", 503)
    return AppError("GRPC_UNKNOWN", "Filer gRPC returned an unclassified status.", 502)


def _mount_modules():
    try:
        from .vendor import seaweed_mount_pb2, seaweed_mount_pb2_grpc
    except ImportError as exc:
        raise AppError("GRPC_PROTO_UNAVAILABLE", "SeaweedFS gRPC client stubs are not available.", 501) from exc
    return seaweed_mount_pb2, seaweed_mount_pb2_grpc


def _default_channel_factory(config: dict[str, Any]):
    try:
        import grpc
    except ImportError as exc:
        raise AppError("GRPC_UNAVAILABLE", "grpcio is not installed.", 501) from exc
    options = [("grpc.max_receive_message_length", GRPC_MAX_RECEIVE_BYTES), ("grpc.enable_http_proxy", 0)]
    if config["transport"] == "plaintext":
        return grpc.insecure_channel(config["target"], options=options)
    credentials = grpc.ssl_channel_credentials(root_certificates=config["root_certificates"])
    return grpc.secure_channel(config["target"], credentials, options=options)


def _subscriber_to_dict(item: Any) -> dict[str, Any]:
    get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
    try:
        connected_at_ns = int(get("connected_at_ns", get("connectedAtNs", 0)) or 0)
        client_id = int(get("client_id", get("clientId", 0)) or 0)
        client_epoch = int(get("client_epoch", get("clientEpoch", 0)) or 0)
    except (TypeError, ValueError) as exc:
        raise AppError("GRPC_BAD_RESPONSE", "Filer gRPC subscriber fields are invalid.", 502) from exc
    client_name = str(get("client_name", get("clientName", get("name", ""))) or "")
    client_type = str(get("client_type", get("clientType", "")) or "")
    path_prefix = str(get("path_prefix", get("pathPrefix", get("path", get("mount_path", "")))) or "")
    filer_address = str(get("filer_address", get("filerAddress", "")) or "")
    address_value = get("address", "")
    try:
        connected_at = _ns_to_utc(connected_at_ns)
    except (OverflowError, OSError, ValueError) as exc:
        raise AppError("GRPC_BAD_RESPONSE", "Filer gRPC subscriber timestamp is invalid.", 502) from exc
    return {
        "id": str(get("id", client_id or client_name) or ""),
        "client_name": client_name,
        "client_id": client_id or None,
        "client_epoch": client_epoch or None,
        "client_type": client_type,
        "address": str(address_value) if address_value is not None else "",
        "path": path_prefix,
        "path_prefix": path_prefix,
        "filer_address": filer_address,
        "connected_at": connected_at,
        "connected_at_ns": connected_at_ns or None,
    }


def _ns_to_utc(value: int) -> str | None:
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _status_from_error(exc: AppError) -> str:
    return {
        "GRPC_PROTO_UNAVAILABLE": "unsupported",
        "GRPC_UNAVAILABLE": "unsupported",
        "GRPC_ENDPOINT_DENIED": "permission_denied",
        "GRPC_PERMISSION_DENIED": "permission_denied",
        "GRPC_UNIMPLEMENTED": "unsupported",
        "GRPC_BAD_RESPONSE": "bad_response",
        "GRPC_UNREACHABLE": "unreachable",
        "GRPC_UNKNOWN": "unknown",
    }.get(exc.code, "unknown")
