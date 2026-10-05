from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _split_origins(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    admin_username: str = "admin"
    admin_password: str | None = None
    allowed_origins: list[str] = field(default_factory=list)
    secrets_file: Path | None = None
    session_cookie_name: str = "swc_session"
    session_ttl_seconds: int = 8 * 60 * 60
    session_absolute_ttl_seconds: int = 24 * 60 * 60
    cookie_secure: bool = True
    dev_insecure_cookie: bool = False
    cursor_signing_key: str | None = None
    csrf_signing_key: str | None = None
    s3_connect_timeout_seconds: float = 3.0
    s3_read_timeout_seconds: float = 10.0

    @property
    def effective_cookie_secure(self) -> bool:
        return self.cookie_secure and not self.dev_insecure_cookie

    def require_admin_password(self) -> str:
        if not self.admin_password:
            raise RuntimeError("CONSOLE_ADMIN_PASSWORD is required before admin auth can be used")
        return self.admin_password

    def require_cursor_key(self) -> bytes:
        return (self.cursor_signing_key or self.require_admin_password()).encode("utf-8")

    def require_csrf_key(self) -> bytes:
        return (self.csrf_signing_key or self.require_admin_password()).encode("utf-8")


def get_settings() -> Settings:
    data_dir = Path(os.getenv("CONSOLE_DATA_DIR", "./data")).resolve()
    database_path = Path(os.getenv("CONSOLE_DATABASE_PATH", str(data_dir / "console.db"))).resolve()
    secrets_file_raw = os.getenv("CONSOLE_SECRETS_FILE")
    secrets_file = Path(secrets_file_raw).resolve() if secrets_file_raw else None
    return Settings(
        data_dir=data_dir,
        database_path=database_path,
        admin_username=os.getenv("CONSOLE_ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("CONSOLE_ADMIN_PASSWORD"),
        allowed_origins=_split_origins(os.getenv("CONSOLE_ALLOWED_ORIGINS")),
        secrets_file=secrets_file,
        session_cookie_name=os.getenv("CONSOLE_SESSION_COOKIE", "swc_session"),
        session_ttl_seconds=int(os.getenv("CONSOLE_SESSION_TTL_SECONDS", str(8 * 60 * 60))),
        session_absolute_ttl_seconds=int(
            os.getenv("CONSOLE_SESSION_ABSOLUTE_TTL_SECONDS", str(24 * 60 * 60))
        ),
        cookie_secure=not _env_bool("CONSOLE_DEV_INSECURE_COOKIE", False),
        dev_insecure_cookie=_env_bool("CONSOLE_DEV_INSECURE_COOKIE", False),
        cursor_signing_key=os.getenv("CONSOLE_CURSOR_SIGNING_KEY"),
        csrf_signing_key=os.getenv("CONSOLE_CSRF_SIGNING_KEY"),
        s3_connect_timeout_seconds=float(os.getenv("CONSOLE_S3_CONNECT_TIMEOUT_SECONDS", "3")),
        s3_read_timeout_seconds=float(os.getenv("CONSOLE_S3_READ_TIMEOUT_SECONDS", "10")),
    )


def load_secret_registry(settings: Settings) -> dict[str, Any]:
    if settings.secrets_file is None:
        return {}
    with settings.secrets_file.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and "secrets" in raw and isinstance(raw["secrets"], dict):
        return raw["secrets"]
    if isinstance(raw, dict):
        return raw
    raise ValueError("Secret registry must be a JSON object")
