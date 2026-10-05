from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from console import core, db
from console.config import Settings


class ClientError(Exception):
    def __init__(self, status: int, code: str):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class Body(io.BytesIO):
    def __init__(self, data: bytes, tracker: list["Body"]):
        super().__init__(data)
        self.closed_by_stream = False
        tracker.append(self)

    def close(self):
        self.closed_by_stream = True
        super().close()


class VersionedS3:
    def __init__(self):
        self.versions: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.head_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.bodies: list[Body] = []

    def seed(self, bucket: str, key: str, version_id: str | None, data: bytes, *, delete_marker: bool = False) -> None:
        self.versions[(bucket, key, version_id)] = {
            "data": data,
            "etag": f'"{key}-{version_id or "current"}-{len(data)}"',
            "content_type": "image/jpeg",
            "delete_marker": delete_marker,
        }

    def head_object(self, **kwargs):
        self.head_calls.append(dict(kwargs))
        item = self._lookup(kwargs)
        if item["delete_marker"]:
            return {"ResponseMetadata": {"HTTPStatusCode": 200}, "DeleteMarker": True}
        return self._head(kwargs, item)

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        item = self._lookup(kwargs)
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != item["etag"]:
            raise ClientError(412, "PreconditionFailed")
        return {**self._head(kwargs, item), "Body": Body(item["data"], self.bodies)}

    def _lookup(self, kwargs):
        key = (kwargs["Bucket"], kwargs["Key"], kwargs.get("VersionId"))
        if key not in self.versions:
            raise ClientError(404, "NoSuchVersion")
        return self.versions[key]

    def _head(self, kwargs, item):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(item["data"]),
            "ContentType": item["content_type"],
            "ETag": item["etag"],
            "VersionId": kwargs.get("VersionId"),
        }


@pytest.fixture()
def foundation(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_password="admin-pass",
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        dev_insecure_cookie=True,
        cookie_secure=False,
    )
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)
        storage_conn = core.create_connection(conn, {"display_name": "mock", "endpoint_url": "mock://s3", "secret_ref": "mock-secret"})
        project = core.create_project(conn, {"project_key": "demo", "display_name": "Demo"})
        readable = core.create_scope(
            conn,
            project["id"],
            {
                "connection_id": storage_conn["id"],
                "display_name": "Readable",
                "bucket": "images",
                "prefix": "raw/",
                "scope_policy": {"allow_original_download": True},
            },
        )
        other = core.create_scope(
            conn,
            project["id"],
            {
                "connection_id": storage_conn["id"],
                "display_name": "Other",
                "bucket": "images",
                "prefix": "other/",
                "scope_policy": {"allow_original_download": True},
                "overlap_ack": True,
            },
        )
        denied = core.create_scope(
            conn,
            project["id"],
            {"connection_id": storage_conn["id"], "display_name": "Denied", "bucket": "images", "prefix": "private/", "overlap_ack": True},
        )
    return settings, readable, other, denied


def upsert(conn, scope: dict[str, Any], key: str, *, etag: str = '"current"', version_id: str | None = None):
    return core.upsert_object(
        conn,
        scope["id"],
        {"key": key, "size": 7, "etag": etag, "version_id": version_id, "content_type": "image/jpeg"},
    )


def test_requested_historical_version_downloads_exact_version_and_closes_body(foundation):
    settings, scope, _other, _denied = foundation
    s3 = VersionedS3()
    s3.seed("images", "raw/file.jpg", None, b"current")
    s3.seed("images", "raw/file.jpg", "v1", b"older")
    with db.connect(settings) as conn:
        obj = upsert(conn, scope, "raw/file.jpg", etag='"current"')
        download = core.download_object(conn, settings, scope["id"], obj["id"], s3, requested_version_id="v1")

    assert b"".join(download.chunks) == b"older"
    assert s3.head_calls == [{"Bucket": "images", "Key": "raw/file.jpg", "VersionId": "v1"}]
    assert s3.get_calls == [{"Bucket": "images", "Key": "raw/file.jpg", "VersionId": "v1"}]
    assert s3.bodies[0].closed_by_stream


def test_requested_null_version_is_passed_literally_and_conditioned(foundation):
    settings, scope, _other, _denied = foundation
    s3 = VersionedS3()
    s3.seed("images", "raw/file.jpg", "null", b"nullver")
    with db.connect(settings) as conn:
        obj = upsert(conn, scope, "raw/file.jpg", etag='"indexed"', version_id="v2")
        download = core.download_object(conn, settings, scope["id"], obj["id"], s3, requested_version_id="null")

    assert b"".join(download.chunks) == b"nullver"
    assert s3.head_calls[0]["VersionId"] == "null"
    assert s3.get_calls[0]["VersionId"] == "null"
    assert s3.get_calls[0]["IfMatch"] == '"raw/file.jpg-null-7"'


def test_default_download_uses_index_identity_without_head_or_version_id(foundation):
    settings, scope, _other, _denied = foundation
    s3 = VersionedS3()
    s3.seed("images", "raw/file.jpg", None, b"current")
    with db.connect(settings) as conn:
        obj = upsert(conn, scope, "raw/file.jpg", etag='"raw/file.jpg-current-7"')
        download = core.download_object(conn, settings, scope["id"], obj["id"], s3)

    assert b"".join(download.chunks) == b"current"
    assert s3.head_calls == []
    assert "VersionId" not in s3.get_calls[0]
    assert s3.get_calls[0]["IfMatch"] == '"raw/file.jpg-current-7"'


def test_cross_scope_object_denied_before_storage(foundation):
    settings, scope, other, _denied = foundation
    s3 = VersionedS3()
    with db.connect(settings) as conn:
        obj = upsert(conn, scope, "raw/file.jpg")
        with pytest.raises(core.AppError) as exc:
            core.download_object(conn, settings, other["id"], obj["id"], s3, requested_version_id="v1")
    assert exc.value.code == "OBJECT_NOT_FOUND"
    assert s3.head_calls == []
    assert s3.get_calls == []


def test_original_download_grant_required_before_storage(foundation):
    settings, _scope, _other, denied = foundation
    s3 = VersionedS3()
    with db.connect(settings) as conn:
        obj = upsert(conn, denied, "private/file.jpg")
        with pytest.raises(core.AppError) as exc:
            core.download_object(conn, settings, denied["id"], obj["id"], s3, requested_version_id="v1")
    assert exc.value.code == "FORBIDDEN_SCOPE"
    assert s3.head_calls == []
    assert s3.get_calls == []


def test_requested_delete_marker_version_is_not_downloaded(foundation):
    settings, scope, _other, _denied = foundation
    s3 = VersionedS3()
    s3.seed("images", "raw/file.jpg", "gone", b"", delete_marker=True)
    with db.connect(settings) as conn:
        obj = upsert(conn, scope, "raw/file.jpg")
        with pytest.raises(core.AppError) as exc:
            core.download_object(conn, settings, scope["id"], obj["id"], s3, requested_version_id="gone")
    assert exc.value.code == "VERSION_DELETE_MARKER"
    assert s3.get_calls == []

