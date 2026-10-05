"""Actual S3-backed S3 Tables preview smoke with a catalog stub.

Creates one random swc-management-tablepreview-<hex> bucket and four table
fixture files. The S3 reads are real; only the S3 Tables catalog response is
stubbed inside the temporary TestClient app and is recorded as such.
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient

from console import core, db, management, management_native
from console.config import Settings
from console.main import create_app


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real S3-backed table preview integration smoke.")
    parser.add_argument("--admin-url", default="http://10.34.158.137:23646")
    parser.add_argument("--s3-endpoint", default="http://10.34.158.137:8333")
    parser.add_argument("--secrets-file", default=str(ROOT / "output" / "server-secrets.json"))
    parser.add_argument("--admin-secret-ref", default="server-admin")
    parser.add_argument("--s3-secret-ref", default="server-test")
    parser.add_argument("--bucket-prefix", default="swc-management-tablepreview")
    parser.add_argument("--keep-tmp", action="store_true")
    return parser.parse_args(argv)


def load_secret(path: Path, ref: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    registry = raw.get("secrets", raw) if isinstance(raw, dict) else {}
    if ref not in registry or not isinstance(registry[ref], dict):
        raise AssertionError(f"secret_ref {ref!r} is missing from registry")
    return dict(registry[ref])


def s3_secret_kwargs(secret: dict[str, Any]) -> dict[str, str]:
    access = secret.get("access_key_id") or secret.get("aws_access_key_id") or secret.get("access_key")
    key = secret.get("secret_access_key") or secret.get("aws_secret_access_key") or secret.get("secret_key")
    token = secret.get("session_token") or secret.get("aws_session_token")
    if not access or not key:
        raise AssertionError("S3 secret is missing access_key_id/secret_access_key")
    kwargs = {"aws_access_key_id": str(access), "aws_secret_access_key": str(key)}
    if token:
        kwargs["aws_session_token"] = str(token)
    return kwargs


def s3_client(endpoint: str, secret: dict[str, Any]):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        **s3_secret_kwargs(secret),
    )


def raw_secret_values(*entries: dict[str, Any]) -> list[str]:
    values: list[str] = []
    sensitive_markers = ("secret", "key", "token", "password")
    public_key_names = {"allowed_endpoint_url", "endpoint_url", "region", "addressing_style"}
    for entry in entries:
        for key, value in entry.items():
            lowered = str(key).lower()
            if lowered in public_key_names:
                continue
            if any(marker in lowered for marker in sensitive_markers) and isinstance(value, str) and value:
                values.append(value)
    return values


def assert_no_secret_leaks(value: Any, secrets_to_check: Sequence[str]) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for secret in secrets_to_check:
        if secret and secret in text:
            raise AssertionError("table preview evidence contains a raw secret value")


def assert_ok(response, context: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AssertionError(f"{context} failed: HTTP {response.status_code} {response.text[:800]}")
    return response.json()


def assert_error(response, context: str, status: int, code: str) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(f"{context} expected HTTP {status}, got {response.status_code}: {response.text[:800]}")
    data = response.json()
    observed = data.get("error", {}).get("code")
    if observed != code:
        raise AssertionError(f"{context} expected error {code}, got {observed}: {data}")
    return data


def csrf_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": token}


def avro_bytes(schema: dict[str, Any], records: list[dict[str, Any]]) -> bytes:
    import fastavro

    stream = io.BytesIO()
    fastavro.writer(stream, schema, records)
    return stream.getvalue()


def parquet_bytes() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    sink = pa.BufferOutputStream()
    pq.write_table(pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]}), sink, compression="NONE")
    return sink.getvalue().to_pybytes()


def fixture_objects(bucket: str) -> dict[str, tuple[bytes, str]]:
    parquet = parquet_bytes()
    datafile = {
        "type": "record",
        "name": "datafile",
        "fields": [
            {"name": "file_path", "type": "string"},
            {"name": "file_format", "type": "string"},
            {"name": "record_count", "type": "long"},
            {"name": "file_size_in_bytes", "type": "long"},
        ],
    }
    entry = {
        "type": "record",
        "name": "entry",
        "fields": [{"name": "status", "type": "int"}, {"name": "data_file", "type": datafile}],
    }
    manifest = avro_bytes(
        entry,
        [
            {
                "status": 1,
                "data_file": {
                    "file_path": f"s3://{bucket}/table/data/a.parquet",
                    "file_format": "PARQUET",
                    "record_count": 3,
                    "file_size_in_bytes": len(parquet),
                },
            }
        ],
    )
    manifest_list = avro_bytes(
        {"type": "record", "name": "manifestlist", "fields": [{"name": "manifest_path", "type": "string"}, {"name": "content", "type": "int"}]},
        [{"manifest_path": f"s3://{bucket}/table/metadata/manifest.avro", "content": 0}],
    )
    metadata = {"current-snapshot-id": 1, "snapshots": [{"snapshot-id": 1, "manifest-list": f"s3://{bucket}/table/metadata/list.avro"}]}
    return {
        "table/metadata/v1.json": (json.dumps(metadata, separators=(",", ":")).encode("utf-8"), "application/json"),
        "table/metadata/list.avro": (manifest_list, "application/octet-stream"),
        "table/metadata/manifest.avro": (manifest, "application/octet-stream"),
        "table/data/a.parquet": (parquet, "application/octet-stream"),
    }


def delete_bucket_contents(s3, bucket: str) -> None:
    while True:
        try:
            versions = s3.list_object_versions(Bucket=bucket)
        except Exception:
            versions = {"Versions": [], "DeleteMarkers": []}
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in [*versions.get("Versions", []), *versions.get("DeleteMarkers", [])]
            if item.get("Key") and item.get("VersionId")
        ]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        if not versions.get("IsTruncated"):
            break
    while True:
        try:
            listed = s3.list_objects_v2(Bucket=bucket)
        except Exception:
            return
        objects = [{"Key": item["Key"]} for item in listed.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        if not listed.get("IsTruncated"):
            break


def bucket_head_state(s3, bucket: str) -> str:
    try:
        s3.head_bucket(Bucket=bucket)
        return "exists"
    except Exception as exc:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))


def run(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    secrets_file = Path(args.secrets_file).resolve()
    admin_secret = load_secret(secrets_file, args.admin_secret_ref)
    s3_secret = load_secret(secrets_file, args.s3_secret_ref)
    secret_values = raw_secret_values(admin_secret, s3_secret)
    s3 = s3_client(args.s3_endpoint, s3_secret)
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-table-preview-"))
    output_dir = ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    evidence_path = output_dir / f"table-preview-{bucket}.json"
    evidence: dict[str, Any] = {
        "bucket": bucket,
        "catalog_stub": True,
        "catalog_stub_name": "fixture_catalog_contract",
        "live_table_catalog_written": False,
        "admin_url": args.admin_url,
        "s3_endpoint": args.s3_endpoint,
        "fixture_keys": [],
        "steps": [],
        "cleanup": {},
    }
    original_fetch = management_native.fetch_table_details
    try:
        s3.create_bucket(Bucket=bucket)
        fixtures = fixture_objects(bucket)
        for key, (body, content_type) in fixtures.items():
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        evidence["fixture_keys"] = sorted(fixtures)

        settings = Settings(
            data_dir=tmp_dir,
            database_path=tmp_dir / "console.db",
            admin_password="integration-table-preview-password",
            secrets_file=secrets_file,
            dev_insecure_cookie=True,
            cookie_secure=False,
            allowed_origins=["http://testserver"],
        )
        client = TestClient(create_app(settings))
        login = assert_ok(client.post("/api/v1/auth/login", headers={"Origin": "http://testserver"}, json={"username": "admin", "password": settings.admin_password}), "login")
        headers = csrf_headers(login["csrf_token"])
        with db.connect(settings) as conn:
            s3_connection = core.create_connection(
                conn,
                {
                    "display_name": "Table Preview S3",
                    "endpoint_url": args.s3_endpoint,
                    "region": "us-east-1",
                    "secret_ref": args.s3_secret_ref,
                    "addressing_style": "path",
                    "verify_tls": False,
                },
            )
            project = core.create_project(conn, {"project_key": f"table-preview-{bucket[-10:]}", "display_name": "Table Preview"})
            scope = core.create_scope(
                conn,
                project["id"],
                {
                    "connection_id": s3_connection["id"],
                    "display_name": "Table Fixture",
                    "bucket": bucket,
                    "prefix": "table/",
                    "scope_policy": {"allow_original_download": True, "allow_preview": False},
                },
            )
            manager = management.create_connection(
                conn,
                settings,
                {
                    "name": "Table Preview Admin",
                    "admin_url": args.admin_url,
                    "admin_secret_ref": args.admin_secret_ref,
                    "s3_connection_id": s3_connection["id"],
                },
            )
            conn.commit()
        catalog = {
            "format": "ICEBERG",
            "versionToken": "fixture-v1",
            "metadataLocation": f"s3://{bucket}/table/metadata/v1.json",
            "warehouseLocation": f"s3://{bucket}/table/",
        }

        def fixture_catalog_contract(_request, _management_id, _bucket_arn, _namespace, _name):
            return {"connection_id": manager["id"], "source": "fixture_catalog_contract", "status": "supported", "details": {"catalog": dict(catalog)}}

        management_native.fetch_table_details = fixture_catalog_contract
        body = {"scope_id": scope["id"], "bucket_arn": "arn:aws:s3tables:fixture:0:bucket/table", "namespace": "ns", "name": "sample", "limit": 2}
        preview_url = f"/api/v1/management/{manager['id']}/modules/s3-tables/table-preview"
        preview = assert_ok(client.post(preview_url, headers=headers, json=body), "table preview")
        if preview.get("rows") != [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]:
            raise AssertionError(f"unexpected preview rows: {preview}")
        if preview.get("preview_kind") != "raw_file_sample" or preview.get("deletes_applied") is not False or preview.get("total_rows") is not None:
            raise AssertionError(f"preview did not report raw-file sample semantics: {preview}")
        if preview.get("snapshot_id") != 1 or preview.get("sample_truncated") is not True:
            raise AssertionError(f"preview snapshot/truncation mismatch: {preview}")
        evidence_items = preview.get("source_evidence") or []
        if len(evidence_items) != 4 or not all(item.get("etag") and "bytes" in item for item in evidence_items):
            raise AssertionError(f"source evidence missing ETag/bytes for real S3 reads: {preview}")
        evidence["steps"].append(
            {
                "step": "preview_stubbed_catalog_real_s3",
                "rows": preview["rows"],
                "snapshot_id": preview["snapshot_id"],
                "preview_kind": preview["preview_kind"],
                "deletes_applied": preview["deletes_applied"],
                "total_rows": preview["total_rows"],
                "source_evidence": evidence_items,
            }
        )
        missing_origin = assert_error(client.post(preview_url, json=body), "missing origin rejected", 403, "FORBIDDEN_ORIGIN")
        evidence["steps"].append({"step": "missing_origin_rejected", "code": missing_origin["error"]["code"]})
        non_member = assert_error(
            client.post(preview_url, headers=headers, json={**body, "file_location": f"s3://{bucket}/table/data/not-in-snapshot.parquet"}),
            "non-member file rejected",
            404,
            "TABLE_FILE_NOT_IN_SNAPSHOT",
        )
        evidence["steps"].append({"step": "non_member_file_rejected", "code": non_member["error"]["code"]})
        catalog["metadataLocation"] = f"s3://{bucket}/other/metadata/v1.json"
        wrong_scope = assert_error(client.post(preview_url, headers=headers, json=body), "scope mismatch rejected", 403, "FORBIDDEN_SCOPE")
        evidence["steps"].append({"step": "scope_mismatch_rejected", "code": wrong_scope["error"]["code"]})

        management_native.fetch_table_details = original_fetch
        live_probe = client.get(
            f"/api/v1/management/{manager['id']}/modules/s3-tables/table-details",
            params={"bucket_arn": "arn:aws:s3tables:fixture:0:bucket/nonexistent", "namespace": "ns", "name": "not-created"},
        )
        evidence["live_table_details_probe"] = {
            "status_code": live_probe.status_code,
            "body": live_probe.json() if live_probe.headers.get("content-type", "").startswith("application/json") else live_probe.text[:500],
            "created_global_table": False,
        }
        assert_no_secret_leaks(evidence, secret_values)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"TABLE_PREVIEW_PASS bucket={bucket} evidence={evidence_path}")
        return 0
    except Exception as exc:
        management_native.fetch_table_details = original_fetch
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"TABLE_PREVIEW_FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"evidence={evidence_path}")
        return 1
    finally:
        management_native.fetch_table_details = original_fetch
        try:
            delete_bucket_contents(s3, bucket)
            if bucket_head_state(s3, bucket) == "exists":
                s3.delete_bucket(Bucket=bucket)
            evidence["cleanup"]["head_after_delete"] = bucket_head_state(s3, bucket)
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as cleanup_exc:
            print(f"WARN cleanup failed for table preview bucket {bucket}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if not args.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
