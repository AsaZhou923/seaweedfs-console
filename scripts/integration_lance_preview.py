"""Actual S3Tables LANCE preview smoke against an isolated SeaweedFS instance.

This script uses the project API with TestClient. It creates only one random
S3Tables bucket and expects LANCE preview to report a worker dependency limit,
not an empty successful result.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from console import core, db, management
from console.config import Settings
from console.main import create_app
from scripts.integration_table_catalog import assert_no_secret_leaks, assert_ok, csrf_headers, load_secret, raw_secret_values, result_response, safe_json, s3tables_rest


def parse_args(argv: Sequence[str]):
    import argparse

    parser = argparse.ArgumentParser(description="Run real LANCE table-preview dependency smoke.")
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--s3-endpoint", required=True)
    parser.add_argument("--secrets-file", required=True)
    parser.add_argument("--admin-secret-ref", default="catalog-admin")
    parser.add_argument("--s3-secret-ref", default="catalog-s3")
    parser.add_argument("--bucket-prefix", default="swc-management-lance")
    parser.add_argument("--keep-tmp", action="store_true")
    return parser.parse_args(argv)


def require_confirmed(data: dict[str, Any], context: str) -> None:
    if data.get("status") != "confirmed" or data.get("result", {}).get("confirmed") is not True:
        raise AssertionError(f"{context} was not confirmed by readback: {data}")


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    namespace = "qa"
    table_name = "events"
    secrets_file = Path(args.secrets_file).resolve()
    admin_secret = load_secret(secrets_file, args.admin_secret_ref)
    s3_secret = load_secret(secrets_file, args.s3_secret_ref)
    secret_values = raw_secret_values(admin_secret, s3_secret)
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-lance-preview-"))
    evidence_path = ROOT / "output" / f"lance-preview-{bucket}.json"
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
    management_id = None
    bucket_arn = None
    table_created = False
    namespace_created = False
    table_bucket_created = False
    try:
        settings = Settings(
            data_dir=tmp_dir,
            database_path=tmp_dir / "console.db",
            admin_password="integration-lance-preview-password",
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
                "display_name": "LANCE preview S3",
                "endpoint_url": args.s3_endpoint,
                "region": "us-east-1",
                "secret_ref": args.s3_secret_ref,
                "addressing_style": "path",
                "verify_tls": False,
            })
            project = core.create_project(conn, {"project_key": f"lance-preview-{bucket[-10:]}", "display_name": "LANCE Preview"})
            scope = core.create_scope(conn, project["id"], {
                "connection_id": s3_connection["id"],
                "display_name": "LANCE owned scope",
                "bucket": bucket,
                "prefix": f"{namespace}/{table_name}/",
                "scope_policy": {"allow_original_download": True},
            })
            manager = management.create_connection(conn, settings, {
                "name": "LANCE preview management",
                "admin_url": args.admin_url,
                "admin_secret_ref": args.admin_secret_ref,
                "s3_connection_id": s3_connection["id"],
                "endpoints": {"s3": args.s3_endpoint},
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
                "reason": "isolated LANCE preview dependency smoke for one random bucket",
            },
        ), "enable table management policy")
        evidence["steps"].append({"step": "policy_enabled", "table_manage": policy.get("permissions", {}).get("table.manage")})

        created_bucket = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/buckets",
                headers=csrf_headers(csrf, idem="create-lance-bucket"),
                json={"name": bucket, "format": "LANCE", "tags": {"purpose": "integration-lance-preview"}, "idempotency_key": "create-lance-bucket"},
            ),
            "create LANCE table bucket",
        )
        require_confirmed(created_bucket, "create LANCE table bucket")
        table_bucket_created = True
        bucket_arn = result_response(created_bucket).get("arn")
        evidence["steps"].append({"step": "create_lance_bucket", "bucket_arn": bucket_arn})

        created_namespace = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/namespaces",
                headers=csrf_headers(csrf, idem="create-lance-namespace"),
                json={"bucket_arn": bucket_arn, "name": namespace, "idempotency_key": "create-lance-namespace"},
            ),
            "create LANCE namespace",
        )
        require_confirmed(created_namespace, "create LANCE namespace")
        namespace_created = True
        evidence["steps"].append({"step": "create_namespace"})

        created_table = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/tables",
                headers=csrf_headers(csrf, idem="create-lance-table"),
                json={"bucket_arn": bucket_arn, "namespace": namespace, "name": table_name, "format": "LANCE", "idempotency_key": "create-lance-table"},
            ),
            "create LANCE table",
        )
        require_confirmed(created_table, "create LANCE table")
        table_created = True
        evidence["steps"].append({"step": "create_lance_table", "table_arn": result_response(created_table).get("table_arn")})

        details = assert_ok(
            client.get(
                f"/api/v1/management/{management_id}/modules/s3-tables/table-details",
                params={"bucket_arn": bucket_arn, "namespace": namespace, "name": table_name},
            ),
            "LANCE table details",
        )
        catalog = details.get("details", {}).get("catalog", {})
        if details.get("status") != "supported" or str(catalog.get("format", "")).upper() != "LANCE":
            raise AssertionError(f"LANCE table-details did not return a LANCE catalog: {details}")
        evidence["steps"].append({
            "step": "table_details_lance",
            "source": details.get("source"),
            "metadata_supported": details.get("details", {}).get("metadata_supported"),
            "metadataLocation": catalog.get("metadataLocation"),
            "warehouseLocation": catalog.get("warehouseLocation"),
        })

        preview = assert_ok(
            client.post(
                f"/api/v1/management/{management_id}/modules/s3-tables/table-preview",
                headers=csrf_headers(csrf),
                json={"scope_id": scope["id"], "bucket_arn": bucket_arn, "namespace": namespace, "name": table_name, "limit": 2},
            ),
            "LANCE table preview",
        )
        if preview.get("status") != "dependency_unavailable" or preview.get("rows") is not None or preview.get("total_rows") is not None:
            raise AssertionError(f"LANCE preview did not report dependency_unavailable with rows=None: {preview}")
        evidence["steps"].append({
            "step": "lance_preview_dependency_unavailable",
            "error_code": preview.get("error_code"),
            "rows": preview.get("rows"),
            "total_rows": preview.get("total_rows"),
            "deletes_applied": preview.get("deletes_applied"),
        })

        operations = assert_ok(client.get(f"/api/v1/management/{management_id}/operations?limit=100"), "read management journal")
        evidence["journal"] = {"operation_count": len(operations.get("items", []))}
        assert_no_secret_leaks(operations, secret_values)
        assert_no_secret_leaks(evidence, secret_values)
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"LANCE_PREVIEW_PASS bucket={bucket} evidence={evidence_path}")
        return 0
    except Exception as exc:
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"LANCE_PREVIEW_FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"evidence={evidence_path}")
        return 1
    finally:
        try:
            if bucket_arn:
                encoded_arn = quote(str(bucket_arn), safe="")
                if table_created:
                    s3tables_rest(args.s3_endpoint, s3_secret, "DeleteTable", "DELETE", f"/tables/{encoded_arn}/{quote(namespace, safe='')}/{quote(table_name, safe='')}")
                    evidence["cleanup"]["delete_table_rest"] = "ok"
                if namespace_created:
                    s3tables_rest(args.s3_endpoint, s3_secret, "DeleteNamespace", "DELETE", f"/namespaces/{encoded_arn}/{quote(namespace, safe='')}")
                    evidence["cleanup"]["delete_namespace_rest"] = "ok"
                if table_bucket_created:
                    s3tables_rest(args.s3_endpoint, s3_secret, "DeleteTableBucket", "DELETE", f"/buckets/{encoded_arn}")
                    evidence["cleanup"]["delete_table_bucket_rest"] = "ok"
                    readback = s3tables_rest(args.s3_endpoint, s3_secret, "ListTableBuckets", "GET", "/buckets")
                    buckets = readback.get("buckets") or readback.get("tableBuckets") or readback.get("items") or []
                    evidence["cleanup"]["table_bucket_still_listed"] = any(bucket in json.dumps(item, ensure_ascii=False) for item in buckets)
                evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as cleanup_exc:
            print(f"WARN cleanup failed for owned LANCE bucket {bucket}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if not args.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
