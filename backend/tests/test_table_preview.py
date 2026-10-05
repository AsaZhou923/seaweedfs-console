import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from console import core, db, management, management_native, storage, table_preview
from console.config import Settings
from console.main import create_app


def avro_bytes(schema, records):
    import fastavro
    stream = io.BytesIO()
    fastavro.writer(stream, schema, records)
    return stream.getvalue()


def samples():
    import pyarrow as pa
    import pyarrow.parquet as pq
    sink = pa.BufferOutputStream()
    pq.write_table(pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]}), sink, compression="NONE")
    parquet = sink.getvalue().to_pybytes()
    datafile = {"type": "record", "name": "datafile", "fields": [
        {"name": "file_path", "type": "string"}, {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"}, {"name": "file_size_in_bytes", "type": "long"}]}
    entry = {"type": "record", "name": "entry", "fields": [{"name": "status", "type": "int"}, {"name": "data_file", "type": datafile}]}
    manifest = avro_bytes(entry, [{"status": 1, "data_file": {"file_path": "s3://bucket/ns/sample/data/a.parquet",
        "file_format": "PARQUET", "record_count": 3, "file_size_in_bytes": len(parquet)}}])
    manifestlist = avro_bytes({"type": "record", "name": "manifestlist", "fields": [
        {"name": "manifest_path", "type": "string"}, {"name": "content", "type": "int"}]},
        [{"manifest_path": "s3://bucket/ns/sample/metadata/manifest.avro", "content": 0}])
    metadata = {"current-snapshot-id": 1, "snapshots": [{"snapshot-id": 1, "manifest-list": "s3://bucket/ns/sample/metadata/list.avro"}]}
    return {"ns/sample/metadata/v1.json": json.dumps(metadata).encode(), "ns/sample/metadata/list.avro": manifestlist,
            "ns/sample/metadata/manifest.avro": manifest, "ns/sample/data/a.parquet": parquet}


@pytest.fixture
def api(tmp_path, monkeypatch):
    secret_file = tmp_path / "secrets.json"
    secret_file.write_text(json.dumps({"a": {"kind": "seaweed_admin", "username": "u", "password": "p", "allowed_endpoint_url": "http://admin.local"}}))
    cfg = Settings(data_dir=tmp_path, database_path=tmp_path / "db", admin_password="table-test-password",
                   secrets_file=secret_file, dev_insecure_cookie=True, allowed_origins=["http://testserver"])
    client = TestClient(create_app(cfg))
    with db.connect(cfg) as conn:
        connection = core.create_connection(conn, {"display_name": "S3", "endpoint_url": "http://s3.local", "secret_ref": "s"})
        project = core.create_project(conn, {"project_key": "p", "display_name": "P"})
        scope = core.create_scope(conn, project["id"], {"connection_id": connection["id"], "display_name": "Table",
               "bucket": "bucket", "prefix": "ns/sample/", "scope_policy": {"allow_original_download": True}})
        manager = management.create_connection(conn, cfg, {"name": "Admin", "admin_url": "http://admin.local", "admin_secret_ref": "a", "s3_connection_id": connection["id"]})
    objects = samples()
    class S3:
        calls = []
        changed = False
        enforce_table_bucket_layout = True
        def _maybe_reject_layout(self, key):
            if self.enforce_table_bucket_layout and not _valid_table_bucket_key(key):
                exc = Exception("table bucket forbidden")
                exc.response = {"Error": {"Code": "AccessDenied", "Message": "object must be under namespace/table/data or metadata"},
                                "ResponseMetadata": {"HTTPStatusCode": 403}}
                raise exc
        def head_object(self, **kwargs):
            self.calls.append(("head", kwargs))
            self._maybe_reject_layout(kwargs["Key"])
            return {"ContentLength": len(objects[kwargs["Key"]]), "ETag": '"changed"' if self.changed else '"stable"', "VersionId": "null"}
        def get_object(self, **kwargs):
            self.calls.append(("get", kwargs))
            self._maybe_reject_layout(kwargs["Key"])
            return {"Body": io.BytesIO(objects[kwargs["Key"]])}
        def close(self):
            pass
    s3 = S3()
    monkeypatch.setattr(storage, "client", lambda *args: s3)
    catalog = {"format": "ICEBERG", "versionToken": "1", "metadataLocation": "s3://bucket/ns/sample/metadata/v1.json", "warehouseLocation": "s3://bucket/ns/sample/"}
    monkeypatch.setattr(management_native, "fetch_table_details", lambda *args: {"status": "supported", "details": {"catalog": dict(catalog)}})
    auth = client.post("/api/v1/auth/login", headers={"Origin": "http://testserver"}, json={"username": "admin", "password": cfg.admin_password})
    headers = {"Origin": "http://testserver", "X-CSRF-Token": auth.json()["csrf_token"]}
    body = {"scope_id": scope["id"], "bucket_arn": "arn:aws:s3tables:r:0:bucket/table", "namespace": "ns", "name": "sample", "limit": 2}
    url = f"/api/v1/management/{manager['id']}/modules/s3-tables/table-preview"
    return SimpleNamespace(client=client, cfg=cfg, scope=scope, manager=manager, headers=headers, body=body,
                           url=url, s3=s3, objects=objects, catalog=catalog)


def _valid_table_bucket_key(key):
    parts = key.strip("/").split("/")
    if len(parts) == 3 and parts[2] in {"metadata", "data"}:
        return False
    return len(parts) >= 4


def test_real_manifest_and_parquet_roundtrip_sample_is_not_a_table_query(api):
    response = api.client.post(api.url, headers=api.headers, json=api.body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rows"] == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert data["snapshot_id"] == 1 and data["sample_truncated"] is True
    assert data["preview_kind"] == "raw_file_sample" and data["deletes_applied"] is False
    assert data["total_rows"] is None
    assert all(call[1]["VersionId"] == "null" and call[1]["IfMatch"] == '"stable"' for call in api.s3.calls if call[0] == "get")


def test_official_table_bucket_layout_requires_namespace_table_before_metadata_or_data(api):
    api.catalog["metadataLocation"] = "s3://bucket/table/metadata/v1.json"
    api.catalog["warehouseLocation"] = "s3://bucket/table/"
    with db.connect(api.cfg) as conn:
        conn.execute("UPDATE scopes SET prefix='table/' WHERE id=?", (api.scope["id"],))
    api.objects["table/metadata/v1.json"] = api.objects["ns/sample/metadata/v1.json"]

    response = api.client.post(api.url, headers=api.headers, json=api.body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "S3_OBJECT_REQUEST_FAILED"
    assert not any(call[0] == "get" for call in api.s3.calls)


def test_scope_and_snapshot_membership_cannot_be_bypassed(api):
    assert api.client.post(api.url, json=api.body).status_code == 403
    api.catalog["metadataLocation"] = "https://other-host/secret"
    assert api.client.post(api.url, headers=api.headers, json=api.body).status_code == 403
    assert not api.s3.calls
    api.catalog["metadataLocation"] = "s3://bucket/ns/sample/metadata/v1.json"
    response = api.client.post(api.url, headers=api.headers, json={**api.body, "file_location": "s3://bucket/ns/sample/data/not-in-snapshot.parquet"})
    assert response.status_code == 404
    assert not any(item[1]["Key"].endswith("not-in-snapshot.parquet") for item in api.s3.calls)
    with db.connect(api.cfg) as conn:
        conn.execute("UPDATE scopes SET allow_original_download=0 WHERE id=?", (api.scope["id"],))
    assert api.client.post(api.url, headers=api.headers, json=api.body).status_code == 403


def test_unknown_current_snapshot_does_not_become_empty_table(api):
    api.objects["ns/sample/metadata/v1.json"] = json.dumps({"snapshots": [{"snapshot-id": 1}]}).encode()
    response = api.client.post(api.url, headers=api.headers, json=api.body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TABLE_SNAPSHOT_ID_UNKNOWN"


def test_snapshot_long_id_uses_decimal_string_without_browser_rounding(api):
    identifier = 9223372036854775807
    api.objects["ns/sample/metadata/v1.json"] = json.dumps({"current-snapshot-id": identifier, "snapshots": [{
        "snapshot-id": identifier, "manifest-list": "s3://bucket/ns/sample/metadata/list.avro"}]}).encode()
    result = api.client.post(api.url, headers=api.headers, json={**api.body, "snapshot_id": str(identifier)})
    assert result.status_code == 200, result.text
    assert result.json()["snapshot_id"] == str(identifier)


def test_catalog_location_change_is_detected_even_when_version_token_is_missing(api, monkeypatch):
    api.catalog["versionToken"] = None
    calls = []

    def catalog(*args):
        value = dict(api.catalog)
        if calls:
            value["metadataLocation"] = "s3://bucket/ns/sample/metadata/v2.json"
        calls.append(True)
        return {"status": "supported", "details": {"catalog": value}}

    monkeypatch.setattr(management_native, "fetch_table_details", catalog)
    result = api.client.post(api.url, headers=api.headers, json=api.body)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "TABLE_CATALOG_CHANGED"


def test_literal_location_and_relative_traversal_bounds():
    scope = {"bucket": "b", "prefix": "table/"}
    assert table_preview.location_key("s3://b/table/literal%25.parquet", scope) == "table/literal%.parquet"
    assert table_preview.location_key("/buckets/b/table/a.parquet", scope) == "table/a.parquet"
    for value in ("s3://other/table/a", "../private", "/etc/config", "https://b/table/a", "s3://b/table/%2e%2e/private",
                  "s3://b/table/../private", "/buckets/b/table/../private", "s3://b/table/a%00b",
                  "s3://b/table/%252e%252e/private", "/buckets/b/table/%2e%2e/private"):
        with pytest.raises(Exception):
            table_preview.location_key(value, scope, "table")
