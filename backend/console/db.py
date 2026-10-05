from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        result = False
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
        except sqlite3.ProgrammingError:
            # Some tests intentionally keep a yielded connection while worker
            # helpers use independent short-lived connections. If a fixture or
            # caller has already closed this handle, context teardown should be
            # idempotent rather than masking the real assertion.
            result = False
        finally:
            try:
                self.close()
            except sqlite3.ProgrammingError:
                pass
        return result


def database_path(settings: Any | None = None) -> Path:
    if settings is not None and hasattr(settings, "database_path"):
        return Path(settings.database_path).resolve()
    return Path(os.getenv("SEAWEEDFS_CONSOLE_DB", "data/console.db")).resolve()


def connect(settings: Any | None = None) -> sqlite3.Connection:
    path = database_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if settings is not None and hasattr(settings, "data_dir"):
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def initialize(settings: Any | None = None) -> None:
    with connect(settings) as conn:
        conn.executescript(FOUNDATION_SCHEMA)
        conn.commit()


FOUNDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  token_hash TEXT NOT NULL UNIQUE,
  csrf_nonce TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  absolute_expires_at TEXT NOT NULL,
  revoked_at TEXT,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_connections (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  endpoint_url TEXT NOT NULL,
  region TEXT,
  secret_ref TEXT NOT NULL,
  addressing_style TEXT NOT NULL DEFAULT 'path',
  verify_tls INTEGER NOT NULL DEFAULT 1,
  approved_endpoints_json TEXT NOT NULL DEFAULT '{}',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  server_version TEXT,
  version_source TEXT,
  version_observed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  project_key TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);

CREATE TABLE IF NOT EXISTS scopes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  connection_id TEXT NOT NULL REFERENCES storage_connections(id) ON DELETE RESTRICT,
  display_name TEXT NOT NULL,
  bucket TEXT NOT NULL,
  prefix TEXT NOT NULL DEFAULT '',
  scope_kind TEXT NOT NULL DEFAULT 'source',
  allow_preview INTEGER NOT NULL DEFAULT 0,
  allow_original_download INTEGER NOT NULL DEFAULT 0,
  writable INTEGER NOT NULL DEFAULT 0,
  manage_bucket INTEGER NOT NULL DEFAULT 0,
  authz_epoch INTEGER NOT NULL DEFAULT 1,
  index_generation INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 1,
  archived_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(connection_id, bucket, prefix)
);

CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  first_seen_at TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS objects (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
  scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE RESTRICT,
  key TEXT NOT NULL,
  revision TEXT NOT NULL,
  version_id TEXT,
  size INTEGER NOT NULL DEFAULT 0,
  etag TEXT,
  last_modified TEXT,
  first_seen_at TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1,
  presence TEXT NOT NULL DEFAULT 'present',
  properties_json TEXT NOT NULL DEFAULT '{}',
  checksum TEXT,
  scan_id TEXT,
  content_type TEXT,
  effective_identity_strength TEXT NOT NULL DEFAULT 'weak_observation',
  UNIQUE(scope_id, key, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_current_scope_key
ON objects(scope_id, key)
WHERE is_current = 1;

CREATE INDEX IF NOT EXISTS idx_objects_scope_seen
ON objects(scope_id, first_seen_at DESC, id DESC);
"""


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
