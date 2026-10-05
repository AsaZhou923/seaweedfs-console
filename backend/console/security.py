from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .config import Settings, get_settings


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        field: str | None = None,
        detail: dict[str, Any] | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field
        self.detail = detail or {}
        self.retryable = retryable

    def to_response(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": request_id,
                "field": self.field,
                "retryable": self.retryable,
                "detail": self.detail,
            }
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_add(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str = "") -> str:
    value = secrets.token_urlsafe(24)
    return f"{prefix}{value}" if prefix else value


def hash_password(password: str, *, salt: str | None = None) -> str:
    raw_salt = base64.b64decode(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, 260_000)
    return "pbkdf2_sha256$260000$%s$%s" % (
        base64.b64encode(raw_salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(rounds))
    return hmac.compare_digest(base64.b64encode(raw).decode("ascii"), digest)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    return sha256_text(token)


def sign_json(payload: dict[str, Any], key: bytes, label: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(key, label.encode("utf-8") + b":" + body, hashlib.sha256).digest()
    return _b64encode(body) + "." + _b64encode(sig)


def unsign_json(token: str, key: bytes, label: str) -> dict[str, Any]:
    try:
        body_raw, sig_raw = token.split(".", 1)
        body = _b64decode(body_raw)
        sig = _b64decode(sig_raw)
    except ValueError as exc:
        raise ValueError("Malformed signed token") from exc
    expected = hmac.new(key, label.encode("utf-8") + b":" + body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid signature")
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Signed token payload must be an object")
    return decoded


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def csrf_token(session_id: str, nonce: str, secret: str) -> str:
    data = f"csrf:v1:{session_id}:{nonce}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def make_csrf_token(session_id: str, csrf_nonce: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return sign_json({"sid": session_id, "nonce": csrf_nonce}, settings.require_csrf_key(), "csrf:v1")


def verify_csrf_token(token: str, session_id: str, csrf_nonce: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    try:
        payload = unsign_json(token, settings.require_csrf_key(), "csrf:v1")
    except ValueError:
        return False
    return payload.get("sid") == session_id and payload.get("nonce") == csrf_nonce


def validate_origin(origin: str | None, settings: Settings | None = None, host_url: str | None = None) -> None:
    settings = settings or get_settings()
    if not origin:
        raise PermissionError("Missing Origin header")
    normalized = origin.rstrip("/")
    if host_url and same_origin(normalized, host_url.rstrip("/")):
        return
    if normalized in set(settings.allowed_origins):
        return
    raise PermissionError("Origin is not allowed")


def same_origin(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    return (
        left_parts.scheme == right_parts.scheme
        and left_parts.hostname == right_parts.hostname
        and (left_parts.port or _default_port(left_parts.scheme))
        == (right_parts.port or _default_port(right_parts.scheme))
    )


def _default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


class Principal(dict):
    @property
    def id(self) -> str:
        return str(self["id"])

    @property
    def username(self) -> str:
        return str(self["username"])


def current_user(request):
    from . import db
    from .core import current_user_from_token

    settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise AppError("UNAUTHENTICATED", "Session is missing or expired.", 401)
    with db.connect(settings) as conn:
        principal, session = current_user_from_token(conn, token)
    request.state.user = principal
    request.state.session = session
    return principal


def mutation(request):
    user = current_user(request)
    settings = request.app.state.settings
    try:
        validate_origin(request.headers.get("origin"), settings, host_url=str(request.base_url).rstrip("/"))
    except PermissionError as exc:
        raise AppError("FORBIDDEN_ORIGIN", str(exc), 403) from exc
    token = request.headers.get("x-csrf-token") or request.headers.get("x-swc-csrf")
    if not token or not verify_csrf_token(token, request.state.session["id"], request.state.session["csrf_nonce"], settings):
        raise AppError("CSRF_INVALID", "CSRF token is missing or invalid.", 403)
    return user
