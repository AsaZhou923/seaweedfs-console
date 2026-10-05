from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest

from console import core, db
from console.config import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "console.db",
        admin_password="correct horse battery staple",
        allowed_origins=["http://localhost:5173"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        dev_insecure_cookie=True,
        cookie_secure=False,
    )


@pytest.fixture()
def conn(tmp_path: Path):
    settings = make_settings(tmp_path)
    db.initialize(settings)
    connection = db.connect(settings)
    try:
        core.bootstrap_admin(connection, settings)
        connection.commit()
        yield connection, settings
    finally:
        connection.close()


def seed_scope(connection: sqlite3.Connection) -> dict:
    created = core.create_connection(
        connection,
        {
            "display_name": "Local SeaweedFS",
            "endpoint_url": "http://127.0.0.1:8333",
            "region": "us-east-1",
            "secret_ref": "local-file:seaweedfs/test",
            "addressing_style": "path",
            "verify_tls": False,
        },
    )
    project = core.create_project(
        connection,
        {"project_key": "demo", "display_name": "Demo", "description": "Demo project"},
    )
    return core.create_scope(
        connection,
        project["id"],
        {
            "connection_id": created["id"],
            "display_name": "Images",
            "bucket": "images",
            "prefix": "uploads/",
            "scope_policy": {"allow_preview": True, "allow_original_download": True},
        },
    )


def test_login_session_and_csrf(conn):
    connection, settings = conn
    login = core.login(connection, settings, "admin", "correct horse battery staple")
    assert login["session_token"]
    assert login["csrf_token"]
    principal, session = core.current_user_from_token(connection, login["session_token"])
    assert principal.username == "admin"
    me = core.me(connection, principal, session, settings)
    assert me["user"]["username"] == "admin"

    with pytest.raises(core.AppError) as exc:
        core.login(connection, settings, "admin", "wrong")
    assert exc.value.code == "UNAUTHENTICATED"


def test_connections_projects_scopes_and_archive(conn):
    connection, _settings = conn
    scope = seed_scope(connection)
    scopes = core.list_scopes(connection, scope["project_id"])
    assert scopes[0]["bucket"] == "images"
    assert scopes[0]["prefix"] == "uploads/"

    archived = core.archive_scope(connection, scope["id"])
    assert archived["is_active"] is False
    with pytest.raises(core.AppError) as exc:
        core.get_scope(connection, scope["id"])
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_scope_overlap_requires_ack(conn):
    connection, _settings = conn
    scope = seed_scope(connection)
    with pytest.raises(core.AppError) as exc:
        core.create_scope(
            connection,
            scope["project_id"],
            {
                "connection_id": scope["connection_id"],
                "display_name": "Nested",
                "bucket": "images",
                "prefix": "uploads/2026/",
            },
        )
    assert exc.value.code == "SCOPE_OVERLAP_ACK_REQUIRED"


def test_object_upsert_listing_cursor_and_literal_prefix(conn):
    connection, settings = conn
    scope = seed_scope(connection)
    first = core.upsert_object(
        connection,
        scope["id"],
        {
            "key": "uploads/%2F/raw.jpg",
            "version_id": "v1",
            "size": 10,
            "etag": '"etag"',
            "content_type": "image/jpeg",
            "properties": {"format": "jpeg"},
        },
    )
    repeated = core.upsert_object(
        connection,
        scope["id"],
        {
            "key": "uploads/%2F/raw.jpg",
            "version_id": "v1",
            "size": 10,
            "etag": '"etag"',
            "checksum": "sha256-later-evidence",
            "content_type": "image/jpeg",
            "properties": {"format": "jpeg", "preview_state": "ready"},
        },
    )
    second = core.upsert_object(
        connection,
        scope["id"],
        {
            "key": "uploads/second.jpg",
            "size": 20,
            "etag": '"etag2"',
            "content_type": "image/jpeg",
        },
    )
    assert first["key"] == "uploads/%2F/raw.jpg"
    assert repeated["id"] == first["id"]
    assert repeated["asset_id"] == first["asset_id"]
    assert repeated["first_seen_at"] == first["first_seen_at"]
    assert first["effective_identity_strength"] == "strong_version"
    assert second["effective_identity_strength"] == "weak_observation"

    page = core.list_objects(connection, settings, scope["id"], limit=1)
    assert len(page["items"]) == 1
    assert page["next_cursor"]
    next_page = core.list_objects(connection, settings, scope["id"], limit=1, cursor=page["next_cursor"])
    assert len(next_page["items"]) == 1

    with pytest.raises(core.AppError) as exc:
        core.upsert_object(connection, scope["id"], {"key": "outside/raw.jpg"})
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_null_version_and_checksum_do_not_break_weak_revision(conn):
    connection, _settings = conn
    scope = seed_scope(connection)
    first = core.upsert_object(
        connection,
        scope["id"],
        {
            "key": "uploads/null-version.jpg",
            "version_id": "null",
            "size": 11,
            "etag": '"null-etag"',
            "last_modified": "2026-10-04T00:00:00.000000Z",
            "checksum": "first-checksum",
        },
    )
    second = core.upsert_object(
        connection,
        scope["id"],
        {
            "key": "uploads/null-version.jpg",
            "version_id": "null",
            "size": 11,
            "etag": '"null-etag"',
            "last_modified": "2026-10-04T00:00:00.000000Z",
            "checksum": "newly-discovered-checksum",
        },
    )
    changed = core.upsert_object(
        connection,
        scope["id"],
        {
            "key": "uploads/null-version.jpg",
            "version_id": "null",
            "size": 12,
            "etag": '"null-etag-2"',
            "last_modified": "2026-10-04T00:00:01.000000Z",
        },
    )
    assert first["revision"].startswith("observed:")
    assert second["revision"] == first["revision"]
    assert second["asset_id"] == first["asset_id"]
    assert changed["revision"] != first["revision"]
    assert changed["asset_id"] == first["asset_id"]


def test_archived_scope_blocks_objects(conn):
    connection, settings = conn
    scope = seed_scope(connection)
    core.archive_scope(connection, scope["id"])
    with pytest.raises(core.AppError) as exc:
        core.list_objects(connection, settings, scope["id"])
    assert exc.value.code == "FORBIDDEN_SCOPE"


class FakeBody(io.BytesIO):
    def close(self):
        super().close()


class FakeS3:
    def list_objects_v2(self, **_kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Contents": []}

    def get_bucket_cors(self, **_kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "CORSRules": []}

    def get_bucket_versioning(self, **_kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Status": "Enabled"}

    def get_object(self, **kwargs):
        assert kwargs["Bucket"] == "images"
        assert kwargs["Key"] == "uploads/file.jpg"
        assert kwargs["IfMatch"] == '"download-etag"'
        return {"Body": FakeBody(b"abc"), "ContentType": "image/jpeg", "ContentLength": 3}


def test_probe_and_download_use_injected_storage(conn):
    connection, settings = conn
    scope = seed_scope(connection)
    result = core.probe_connection(connection, settings, scope["connection_id"], "images", "uploads/", FakeS3())
    assert result["capabilities"]["list_objects_v2"] == "supported"
    obj = core.upsert_object(
        connection,
        scope["id"],
        {"key": "uploads/file.jpg", "size": 3, "etag": '"download-etag"', "content_type": "image/jpeg"},
    )
    download = core.download_object(connection, settings, scope["id"], obj["id"], FakeS3())
    assert download.content_type == "image/jpeg"
    assert b"".join(download.chunks) == b"abc"
