from __future__ import annotations

from pathlib import Path

from console import core, db, storage
from console.config import Settings


class ClientError(Exception):
    def __init__(self, status: int, code: str):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class Body:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ListBucketsOnly:
    def list_buckets(self):
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"server": "SeaweedFS 30GB 4.48"}}}


class ScopedReadClient:
    def __init__(self):
        self.body = Body()
        self.get_kwargs = None

    def list_objects_v2(self, **kwargs):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"server": "SeaweedFS 30GB 4.48"}},
            "Contents": [{"Key": "gallery/a.jpg"}],
        }

    def head_object(self, **kwargs):
        assert kwargs["Bucket"] == "images"
        assert kwargs["Key"] == "gallery/a.jpg"
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"server": "SeaweedFS 30GB 4.48"}}}

    def get_object(self, **kwargs):
        self.get_kwargs = kwargs
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Body": self.body}

    def get_bucket_cors(self, **kwargs):
        raise ClientError(404, "NoSuchCORSConfiguration")

    def get_bucket_versioning(self, **kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Status": "Enabled"}


class DeniedScopedClient(ScopedReadClient):
    def list_objects_v2(self, **kwargs):
        raise ClientError(403, "AccessDenied")


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "console.db",
        admin_password="admin-pass",
        cursor_signing_key="cursor-key",
        csrf_signing_key="csrf-key",
        cookie_secure=False,
        dev_insecure_cookie=True,
    )


def seed_connection(conn):
    return core.create_connection(
        conn,
        {
            "display_name": "S3",
            "endpoint_url": "http://127.0.0.1:8333",
            "region": "us-east-1",
            "secret_ref": "server-test",
            "addressing_style": "path",
            "verify_tls": False,
        },
    )


def test_list_buckets_does_not_prove_object_listing():
    result = storage.probe_read_capabilities(ListBucketsOnly())
    assert result["capabilities"]["list_buckets"] == "supported"
    assert result["capabilities"]["list_objects_v2"] == "not_checked"
    assert result["server_version"] == "4.48"
    assert result["version_source"] == "s3_server_header"
    assert result["evidence"][0]["operation"] == "ListBuckets"


def test_scoped_probe_head_range_get_closes_body_and_cors_absent_supported():
    fake = ScopedReadClient()
    result = storage.probe_read_capabilities(fake, bucket="images", prefix="gallery/")
    assert result["capabilities"]["list_objects_v2"] == "supported"
    assert result["capabilities"]["head_object"] == "supported"
    assert result["capabilities"]["get_object"] == "supported"
    assert result["capabilities"]["bucket_cors"] == "supported"
    assert any(item["operation"] == "GetBucketCors" and item["result"] == "not_configured" for item in result["evidence"])
    assert fake.get_kwargs["Range"] == "bytes=0-0"
    assert fake.body.closed is True


def test_permission_denied_does_not_mark_unchecked_head_get_supported():
    result = storage.probe_read_capabilities(DeniedScopedClient(), bucket="images", prefix="gallery/")
    assert result["capabilities"]["list_objects_v2"] == "permission_denied"
    assert result["capabilities"]["head_object"] == "not_checked"
    assert result["capabilities"]["get_object"] == "not_checked"


def test_core_probe_persists_server_version(tmp_path: Path):
    cfg = settings(tmp_path)
    db.initialize(cfg)
    with db.connect(cfg) as conn:
        connection = seed_connection(conn)
        result = core.probe_connection(conn, cfg, connection["id"], bucket="images", prefix="gallery/", s3_client=ScopedReadClient())
        persisted = core.get_connection(conn, connection["id"])
    assert result["server_version"] == "4.48"
    assert persisted["server_version"] == "4.48"
    assert persisted["version_source"] == "s3_server_header"
    assert persisted["version_observed_at"] is not None
