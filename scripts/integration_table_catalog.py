"""Actual S3 Tables catalog + table preview smoke.

Creates one random swc-management-tablecatalog-* owned bucket, writes Iceberg
fixture files into that bucket, creates a real S3 Tables bucket/namespace/table
through the console management routes, then verifies table-details and
table-preview without monkeypatching the catalog.
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

from console import core, db, management
from console.config import Settings
from console.main import create_app


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real S3 Tables catalog integration smoke.")
    parser.add_argument("--admin-url", default="http://10.34.158.137:23646")
    parser.add_argument("--s3-endpoint", default="http://10.34.158.137:8333")
    parser.add_argument("--filer-url", default="")
    parser.add_argument("--secrets-file", default=str(ROOT / "output" / "server-secrets.json"))
    parser.add_argument("--admin-secret-ref", default="server-admin")
    parser.add_argument("--s3-secret-ref", default="server-test")
    parser.add_argument("--bucket-prefix", default="swc-management-tablecatalog")
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
    public_key_names = {"allowed_endpoint_url", "allowed_endpoints", "allowed_grpc_endpoints", "endpoint_url", "region", "addressing_style"}
    for entry in entries:
        for key, value in entry.items():
            lowered = str(key).lower()
            if lowered in public_key_names:
                continue
            if any(marker in lowered for marker in sensitive_markers) and isinstance(value, str) and value:
                values.append(value)
    return values


def safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): safe_json(item) for key, item in value.items() if "secret" not in str(key).lower()}
    if isinstance(value, list):
        return [safe_json(item) for item in value]
    return value


def assert_no_secret_leaks(value: Any, secrets_to_check: Sequence[str]) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for secret in secrets_to_check:
        if secret and secret in text:
            raise AssertionError("evidence contains a raw secret value")


def assert_ok(response, context: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AssertionError(f"{context} failed: HTTP {response.status_code} {response.text[:1200]}")
    return response.json()


def csrf_headers(token: str, *, idem: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token}
    if idem:
        headers["Idempotency-Key"] = idem
    return headers


def require_confirmed(data: dict[str, Any], context: str) -> None:
    if data.get("status") != "confirmed" or data.get("result", {}).get("confirmed") is not True:
        raise AssertionError(f"{context} was not confirmed by readback: {data}")


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


def fixture_objects(bucket: str, namespace: str, table_name: str) -> tuple[dict[str, tuple[bytes, str]], dict[str, Any]]:
    table_root = f"{namespace}/{table_name}"
    table_uri = f"s3://{bucket}/{table_root}"
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
                    "file_path": f"{table_uri}/data/a.parquet",
                    "file_format": "PARQUET",
                    "record_count": 3,
                    "file_size_in_bytes": len(parquet),
                },
            }
        ],
    )
    manifest_list = avro_bytes(
        {"type": "record", "name": "manifestlist", "fields": [{"name": "manifest_path", "type": "string"}, {"name": "content", "type": "int"}]},
        [{"manifest_path": f"{table_uri}/metadata/manifest.avro", "content": 0}],
    )
    metadata = {
        "format-version": 2,
        "table-uuid": secrets.token_hex(16),
        "location": table_uri,
        "last-sequence-number": 1,
        "last-updated-ms": 0,
        "last-column-id": 2,
        "current-schema-id": 0,
        "schemas": [{"schema-id": 0, "type": "struct", "fields": [{"id": 1, "name": "id", "required": False, "type": "long"}, {"id": 2, "name": "name", "required": False, "type": "string"}]}],
        "default-spec-id": 0,
        "partition-specs": [{"spec-id": 0, "fields": []}],
        "last-partition-id": 0,
        "default-sort-order-id": 0,
        "sort-orders": [{"order-id": 0, "fields": []}],
        "current-snapshot-id": 1,
        "snapshots": [{"snapshot-id": 1, "sequence-number": 1, "timestamp-ms": 0, "manifest-list": f"{table_uri}/metadata/list.avro", "summary": {"operation": "append"}}],
        "snapshot-log": [{"timestamp-ms": 0, "snapshot-id": 1}],
        "metadata-log": [],
        "properties": {},
    }
    objects = {
        f"{table_root}/metadata/v1.metadata.json": (json.dumps(metadata, separators=(",", ":")).encode("utf-8"), "application/json"),
        f"{table_root}/metadata/list.avro": (manifest_list, "application/octet-stream"),
        f"{table_root}/metadata/manifest.avro": (manifest, "application/octet-stream"),
        f"{table_root}/data/a.parquet": (parquet, "application/octet-stream"),
    }
    return objects, metadata


def bucket_head_state(s3, bucket: str) -> str:
    try:
        s3.head_bucket(Bucket=bucket)
        return "exists"
    except Exception as exc:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))


def cleanup_bucket(s3, bucket: str) -> str:
    cleanup_bucket_objects(s3, bucket)
    if bucket_head_state(s3, bucket) == "exists":
        s3.delete_bucket(Bucket=bucket)
    return bucket_head_state(s3, bucket)


def cleanup_bucket_objects(s3, bucket: str) -> list[str]:
    deleted_keys: list[str] = []
    while True:
        try:
            uploads = s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
        except Exception:
            uploads = []
        for upload in uploads:
            s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
        if not uploads:
            break
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
            deleted_keys.extend(str(item["Key"]) for item in objects)
        if not versions.get("IsTruncated"):
            break
    while True:
        try:
            listed = s3.list_objects_v2(Bucket=bucket)
        except Exception:
            break
        objects = [{"Key": item["Key"]} for item in listed.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            deleted_keys.extend(str(item["Key"]) for item in objects)
        if not listed.get("IsTruncated"):
            break
    return deleted_keys


def put_filer_object(filer_url: str, bucket: str, key: str, body: bytes, content_type: str) -> None:
    from urllib.parse import quote
    from urllib.request import Request, urlopen

    path = "/buckets/" + quote(bucket, safe="") + "/" + "/".join(quote(part, safe="") for part in key.split("/"))
    request = Request(filer_url.rstrip("/") + path, data=body, method="PUT", headers={"Content-Type": content_type})
    with urlopen(request, timeout=8) as response:
        if not 200 <= response.status < 300:
            raise AssertionError(f"Filer PUT failed for {key}: HTTP {response.status}")


def result_response(operation: dict[str, Any]) -> dict[str, Any]:
    value = operation.get("result", {}).get("response")
    if not isinstance(value, dict):
        raise AssertionError(f"operation response missing: {operation}")
    return value


def s3tables_target(endpoint: str, secret: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    url = endpoint.rstrip("/") + "/"
    headers = {"Accept": "application/json", "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": f"S3Tables.{operation}"}
    credentials = Credentials(
        str(secret["access_key_id"]),
        str(secret["secret_access_key"]),
        str(secret.get("session_token") or "") or None,
    )
    request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "s3tables", "us-east-1").add_auth(request)
    with httpx.Client(trust_env=False, timeout=8.0) as client:
        response = client.post(url, headers=dict(request.headers.items()), content=body)
    if not 200 <= response.status_code < 300:
        raise AssertionError(f"S3Tables.{operation} failed: HTTP {response.status_code} {response.text[:800]}")
    if "json" not in response.headers.get("content-type", "").lower():
        raise AssertionError(f"S3Tables.{operation} did not return JSON: {response.text[:300]}")
    return response.json()


def s3tables_rest(endpoint: str, secret: dict[str, Any], operation: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    body = b"" if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    url = endpoint.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    credentials = Credentials(
        str(secret["access_key_id"]),
        str(secret["secret_access_key"]),
        str(secret.get("session_token") or "") or None,
    )
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, "s3tables", "us-east-1").add_auth(request)
    with httpx.Client(trust_env=False, timeout=8.0) as client:
        response = client.request(method, url, headers=dict(request.headers.items()), content=body)
    if not 200 <= response.status_code < 300:
        raise AssertionError(f"S3Tables REST {operation} failed: HTTP {response.status_code} {response.text[:800]}")
    if not response.content:
        return {}
    if "json" not in response.headers.get("content-type", "").lower():
        return {"raw": response.text[:300]}
    return response.json()


def run(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    namespace = "qa"
    table_name = "events"
    table_root = f"{namespace}/{table_name}"
    metadata_location = f"s3://{bucket}/{table_root}/metadata/v1.metadata.json"
    secrets_file = Path(args.secrets_file).resolve()
    admin_secret = load_secret(secrets_file, args.admin_secret_ref)
    s3_secret = load_secret(secrets_file, args.s3_secret_ref)
    secret_values = raw_secret_values(admin_secret, s3_secret)
    s3 = s3_client(args.s3_endpoint, s3_secret)
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-table-catalog-"))
    output_dir = ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    evidence_path = output_dir / f"table-catalog-{bucket}.json"
    evidence: dict[str, Any] = {
        "bucket": bucket,
        "namespace": namespace,
        "table": table_name,
        "admin_url": args.admin_url,
        "s3_endpoint": args.s3_endpoint,
        "catalog_stub": False,
        "global_table_catalog_written": False,
        "steps": [],
        "cleanup": {},
    }
    management_id: str | None = None
    bucket_arn: str | None = None
    table_created = False
    namespace_created = False
    table_bucket_created = False
    try:
        settings = Settings(
            data_dir=tmp_dir,
            database_path=tmp_dir / "console.db",
            admin_password="integration-table-catalog-password",
            secrets_file=secrets_file,
            dev_insecure_cookie=True,
            cookie_secure=False,
            allowed_origins=["http://testserver"],
        )
        client = TestClient(create_app(settings))
        login = assert_ok(client.post("/api/v1/auth/login", headers={"Origin": "http://testserver"}, json={"username": "admin", "password": settings.admin_password}), "login")
        csrf = login["csrf_token"]
        with db.connect(settings) as conn:
            s3_connection = core.create_connection(conn, {
                "display_name": "Table catalog S3",
                "endpoint_url": args.s3_endpoint,
                "region": "us-east-1",
                "secret_ref": args.s3_secret_ref,
                "addressing_style": "path",
                "verify_tls": False,
            })
            project = core.create_project(conn, {"project_key": f"table-catalog-{bucket[-10:]}", "display_name": "Table Catalog"})
            scope = core.create_scope(conn, project["id"], {
                "connection_id": s3_connection["id"],
                "display_name": "Table catalog owned scope",
                "bucket": bucket,
                "prefix": f"{table_root}/",
                "scope_policy": {"allow_original_download": True},
            })
            management_endpoints = {"s3": args.s3_endpoint}
            if args.filer_url:
                management_endpoints["filer"] = args.filer_url
            manager = management.create_connection(conn, settings, {
                "name": "Table catalog management",
                "admin_url": args.admin_url,
                "admin_secret_ref": args.admin_secret_ref,
                "s3_connection_id": s3_connection["id"],
                "endpoints": management_endpoints,
            })
            conn.commit()
        management_id = manager["id"]
        policy = assert_ok(client.put(
            f"/api/v1/management/connections/{management_id}/access-policy",
            headers=csrf_headers(csrf),
            json={
                "management_write_enabled": True,
                "acknowledge_management_write": True,
                "permissions": {"table.manage": True},
                "reason": "isolated S3 Tables catalog integration smoke for one random bucket",
            },
        ), "enable table management policy")
        if policy.get("permissions", {}).get("table.manage") is not True:
            raise AssertionError(f"table.manage policy did not apply: {policy}")
        evidence["steps"].append({"step": "policy_enabled", "table_manage": True, "endpoints": management_endpoints})

        preflight = assert_ok(client.get(f"/api/v1/management/{management_id}/modules/s3-tables/buckets"), "S3 Tables buckets preflight")
        evidence["steps"].append({
            "step": "s3tables_preflight",
            "total_buckets": preflight.get("total_buckets"),
            "iceberg_port": preflight.get("iceberg_port"),
            "lance_port": preflight.get("lance_port"),
            "buckets_shape": type(preflight.get("buckets")).__name__,
        })
        if preflight.get("iceberg_port") in (0, "0", None) and not preflight.get("buckets"):
            raise AssertionError(f"S3 Tables Iceberg catalog service is not enabled on the target Admin endpoint: {preflight}")

        created_bucket = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/buckets",
                headers=csrf_headers(csrf, idem="create-table-bucket"),
                json={"name": bucket, "format": "ICEBERG", "tags": {"purpose": "integration-table-catalog"}, "idempotency_key": "create-table-bucket"},
            ),
            "create S3 Tables bucket",
        )
        require_confirmed(created_bucket, "create S3 Tables bucket")
        table_bucket_created = True
        response = result_response(created_bucket)
        bucket_arn = response.get("arn") or response.get("bucket_arn") or response.get("tableBucketARN")
        if not isinstance(bucket_arn, str) or bucket not in bucket_arn:
            raise AssertionError(f"S3 Tables bucket ARN is not bound to owned bucket name: {created_bucket}")
        evidence["steps"].append({"step": "create_table_bucket", "operation_id": created_bucket["operation_id"], "bucket_arn": bucket_arn})

        created_namespace = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/namespaces",
                headers=csrf_headers(csrf, idem="create-namespace"),
                json={"bucket_arn": bucket_arn, "name": namespace, "idempotency_key": "create-namespace"},
            ),
            "create S3 Tables namespace",
        )
        require_confirmed(created_namespace, "create S3 Tables namespace")
        namespace_created = True
        evidence["steps"].append({"step": "create_namespace", "operation_id": created_namespace["operation_id"]})

        fixtures, metadata = fixture_objects(bucket, namespace, table_name)
        upload_method = "s3_put_object"
        s3_upload_error = None
        for key, (body, content_type) in fixtures.items():
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
            except Exception as exc:
                s3_upload_error = str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))
                if not args.filer_url:
                    raise
                upload_method = "filer_http_put_after_s3_failure"
                put_filer_object(args.filer_url, bucket, key, body, content_type)
        s3_readback = []
        for key, (body, _content_type) in fixtures.items():
            head = s3.head_object(Bucket=bucket, Key=key)
            data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            if data != body:
                raise AssertionError(f"S3 readback mismatch for fixture key {key}")
            s3_readback.append({"key": key, "bytes": len(data), "etag": head.get("ETag")})
        evidence["steps"].append({
            "step": "fixtures_uploaded",
            "method": upload_method,
            "s3_upload_error": s3_upload_error,
            "keys": sorted(fixtures),
            "metadata_location": metadata_location,
            "s3_readback": s3_readback,
        })

        registered_table = s3tables_target(
            args.s3_endpoint,
            s3_secret,
            "RegisterTable",
            {
                "tableBucketARN": bucket_arn,
                "namespace": [namespace],
                "name": table_name,
                "metadataLocation": metadata_location,
            },
        )
        table_created = True
        table_arn = registered_table.get("tableARN")
        if not isinstance(table_arn, str) or bucket not in table_arn:
            raise AssertionError(f"RegisterTable returned a table ARN outside the owned bucket: {registered_table}")
        evidence["steps"].append({
            "step": "register_table_direct_s3tables",
            "operation": "RegisterTable",
            "table_arn": table_arn,
            "version_token": registered_table.get("versionToken"),
            "metadataLocation": registered_table.get("metadataLocation"),
        })

        details = assert_ok(
            client.get(
                f"/api/v1/management/{management_id}/modules/s3-tables/table-details",
                params={"bucket_arn": bucket_arn, "namespace": namespace, "name": table_name},
            ),
            "table details",
        )
        if details.get("status") != "supported":
            raise AssertionError(f"real table-details did not support owned table catalog: {details}")
        catalog = details.get("details", {}).get("catalog", {})
        if catalog.get("metadataLocation") != metadata_location:
            raise AssertionError(f"table-details metadataLocation is not the owned fixture path: {details}")
        warehouse_location = catalog.get("warehouseLocation")
        if warehouse_location is not None and warehouse_location not in {f"s3://{bucket}/{table_root}/", f"s3://{bucket}/{table_root}"}:
            raise AssertionError(f"table-details warehouseLocation is outside the owned fixture root: {details}")
        evidence["steps"].append({
            "step": "table_details_real_get_table",
            "source": details.get("source"),
            "status": details.get("status"),
            "metadataLocation": catalog.get("metadataLocation"),
            "warehouseLocation": catalog.get("warehouseLocation"),
            "versionToken": catalog.get("versionToken"),
        })

        preview = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/table-preview",
                headers=csrf_headers(csrf),
                json={"scope_id": scope["id"], "bucket_arn": bucket_arn, "namespace": namespace, "name": table_name, "limit": 2},
            ),
            "table preview",
        )
        if preview.get("status") != "supported" or preview.get("rows") != [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]:
            raise AssertionError(f"real table-preview did not decode expected rows: {preview}")
        if preview.get("preview_kind") != "raw_file_sample" or preview.get("deletes_applied") is not False or preview.get("total_rows") is not None:
            raise AssertionError(f"table-preview did not report raw-file sample semantics: {preview}")
        evidence_items = preview.get("source_evidence") or []
        if len(evidence_items) != 4 or not all(item.get("etag") and "bytes" in item for item in evidence_items):
            raise AssertionError(f"table-preview did not include real S3 evidence for all fixture files: {preview}")
        evidence["steps"].append({
            "step": "table_preview_real_catalog_real_s3",
            "rows": preview["rows"],
            "snapshot_id": preview.get("snapshot_id"),
            "source_evidence": evidence_items,
        })

        operations = assert_ok(client.get(f"/api/v1/management/{management_id}/operations?limit=100"), "read management journal")
        assert_no_secret_leaks(operations, secret_values)
        evidence["journal"] = {"operation_count": len(operations.get("items", [])), "secret_leak_check": "passed"}
        assert_no_secret_leaks(evidence, secret_values)
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"TABLE_CATALOG_PASS bucket={bucket} evidence={evidence_path}")
        return 0
    except Exception as exc:
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"TABLE_CATALOG_FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"evidence={evidence_path}")
        return 1
    finally:
        try:
            if management_id and bucket_arn:
                from urllib.parse import quote

                encoded_arn = quote(bucket_arn, safe="")
                if table_created:
                    s3tables_rest(args.s3_endpoint, s3_secret, "DeleteTable", "DELETE", f"/tables/{encoded_arn}/{quote(namespace, safe='')}/{quote(table_name, safe='')}")
                    evidence["cleanup"]["delete_table_rest"] = "ok"
                if namespace_created:
                    deleted_keys = cleanup_bucket_objects(s3, bucket)
                    evidence["cleanup"]["deleted_object_count"] = len(deleted_keys)
                if namespace_created:
                    s3tables_rest(args.s3_endpoint, s3_secret, "DeleteNamespace", "DELETE", f"/namespaces/{encoded_arn}/{quote(namespace, safe='')}")
                    evidence["cleanup"]["delete_namespace_rest"] = "ok"
                if table_bucket_created:
                    s3tables_rest(args.s3_endpoint, s3_secret, "DeleteTableBucket", "DELETE", f"/buckets/{encoded_arn}")
                    evidence["cleanup"]["delete_table_bucket_rest"] = "ok"
                    readback = s3tables_rest(args.s3_endpoint, s3_secret, "ListTableBuckets", "GET", "/buckets")
                    buckets = readback.get("buckets") or readback.get("tableBuckets") or readback.get("items") or []
                    evidence["cleanup"]["table_bucket_still_listed"] = any(bucket in json.dumps(item, ensure_ascii=False) for item in buckets)
            if table_bucket_created:
                evidence["cleanup"]["bucket_head_after_delete"] = bucket_head_state(s3, bucket)
                evidence["cleanup"]["ordinary_s3_delete_bucket_skipped"] = "table bucket deletion uses S3Tables/Admin API, not ordinary S3 DeleteBucket"
            else:
                evidence["cleanup"]["bucket_head_after_delete"] = cleanup_bucket(s3, bucket)
            evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as cleanup_exc:
            print(f"WARN cleanup failed for owned table catalog bucket {bucket}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if not args.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
