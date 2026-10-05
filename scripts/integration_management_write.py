"""Actual management write integration for one isolated test bucket.

Credentials are loaded from the local registry and never printed.
The script creates only a random swc-management-<hex> bucket and cleans only
that bucket, including versions and multipart uploads.
"""

from __future__ import annotations

import argparse
import hashlib
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

from console.config import Settings
from console.main import create_app


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated SeaweedFS management write smoke test.")
    parser.add_argument("--admin-url", default="http://10.34.158.137:23646")
    parser.add_argument("--s3-endpoint", default="http://10.34.158.137:8333")
    parser.add_argument("--secrets-file", default=str(ROOT / "output" / "server-secrets.json"))
    parser.add_argument("--admin-secret-ref", default="server-admin")
    parser.add_argument("--s3-secret-ref", default="server-test")
    parser.add_argument("--bucket-prefix", default="swc-management")
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
            raise AssertionError("journal/evidence contains a raw secret value")


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


def assert_ok(response, context: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AssertionError(f"{context} failed: HTTP {response.status_code} {response.text[:800]}")
    try:
        return response.json()
    except Exception:
        return {}


def assert_error(response, context: str, status: int, code: str) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(f"{context} expected HTTP {status}, got {response.status_code}: {response.text[:800]}")
    data = response.json()
    observed = data.get("error", {}).get("code")
    if observed != code:
        raise AssertionError(f"{context} expected {code}, got {observed}: {data}")
    return data


def assert_confirmed(response, context: str) -> dict[str, Any]:
    data = assert_ok(response, context)
    if data.get("status") != "confirmed" or data.get("result", {}).get("confirmed") is not True:
        raise AssertionError(f"{context} did not confirm by readback: {data}")
    return data


def assert_status_with_readback(response, context: str, statuses: set[str]) -> dict[str, Any]:
    data = assert_ok(response, context)
    if data.get("status") not in statuses:
        raise AssertionError(f"{context} returned unexpected operation status: {data}")
    if "result" not in data or "readback" not in data["result"]:
        raise AssertionError(f"{context} did not include readback evidence: {data}")
    return data


def csrf_headers(token: str, *, idem: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token}
    if idem:
        headers["Idempotency-Key"] = idem
    return headers


def delete_owned_bucket_contents(s3, bucket: str) -> None:
    try:
        uploads = s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
        for upload in uploads:
            s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
    except Exception:
        pass

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


def assert_advanced_bucket_sdk_state(s3, bucket: str) -> dict[str, Any]:
    versioning = s3.get_bucket_versioning(Bucket=bucket)
    if versioning.get("Status") != "Enabled":
        raise AssertionError(f"advanced bucket versioning was not enabled: {versioning}")
    lock = s3.get_object_lock_configuration(Bucket=bucket).get("ObjectLockConfiguration") or {}
    retention = lock.get("Rule", {}).get("DefaultRetention", {})
    if lock.get("ObjectLockEnabled") != "Enabled" or retention.get("Mode") != "GOVERNANCE" or int(retention.get("Days") or 0) != 1:
        raise AssertionError(f"advanced bucket object-lock default rule mismatch: {lock}")
    return {"versioning_status": versioning.get("Status"), "object_lock_configuration": lock}


def run(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    secrets_file = Path(args.secrets_file).resolve()
    admin_secret = load_secret(secrets_file, args.admin_secret_ref)
    s3_secret = load_secret(secrets_file, args.s3_secret_ref)
    secret_values = raw_secret_values(admin_secret, s3_secret)
    bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    advanced_bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-mgmt-write-"))
    output_dir = ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    evidence_path = output_dir / f"management-write-{bucket}.json"
    evidence: dict[str, Any] = {
        "bucket": bucket,
        "advanced_bucket": advanced_bucket,
        "admin_url": args.admin_url,
        "s3_endpoint": args.s3_endpoint,
        "steps": [],
        "cleanup": {},
    }
    s3 = s3_client(args.s3_endpoint, s3_secret)
    management_id: str | None = None
    try:
        settings = Settings(
            data_dir=tmp_dir,
            database_path=tmp_dir / "console.db",
            admin_password="integration-only-password",
            secrets_file=secrets_file,
            dev_insecure_cookie=True,
            cookie_secure=False,
            allowed_origins=["http://testserver"],
        )
        client = TestClient(create_app(settings))
        login = client.post("/api/v1/auth/login", headers={"Origin": "http://testserver"}, json={"username": "admin", "password": settings.admin_password})
        csrf = assert_ok(login, "login")["csrf_token"]

        s3_connection = assert_ok(
            client.post(
                "/api/v1/connections",
                headers=csrf_headers(csrf),
                json={
                    "name": "Management write associated S3",
                    "endpoint_url": args.s3_endpoint,
                    "region": "us-east-1",
                    "addressing_style": "path",
                    "tls_verify": False,
                    "secret_ref": args.s3_secret_ref,
                },
            ),
            "create associated S3 connection",
        )
        evidence["steps"].append({"step": "s3_connection", "id": s3_connection["id"]})

        created = assert_ok(
            client.post(
                "/api/v1/management/connections",
                headers=csrf_headers(csrf),
                json={
                    "name": "OptiPlex management write",
                    "admin_url": args.admin_url,
                    "admin_secret_ref": args.admin_secret_ref,
                    "s3_connection_id": s3_connection["id"],
                    "endpoints": {},
                    "permissions": {"bucket.manage": False, "bucket_write_prefixes": [bucket, advanced_bucket]},
                },
            ),
            "create management connection",
        )
        management_id = created["id"]
        evidence["management_id"] = management_id
        evidence["steps"].append({"step": "management_connection", "id": management_id, "write_enabled": created["management_write_enabled"]})

        access = assert_ok(
            client.put(
                f"/api/v1/management/connections/{management_id}/access-policy",
                headers=csrf_headers(csrf),
                json={
                    "management_write_enabled": True,
                    "acknowledge_management_write": True,
                    "permissions": {"bucket.manage": True, "bucket_write_prefixes": [bucket, advanced_bucket]},
                    "reason": "isolated integration write test for one random bucket",
                },
            ),
            "enable management write policy",
        )
        if access.get("management_write_enabled") is not True or access.get("permissions", {}).get("bucket_write_prefixes") != [bucket, advanced_bucket]:
            raise AssertionError(f"management write policy did not apply exact bucket prefix: {access}")
        evidence["steps"].append({"step": "access_policy", "write_enabled": True, "bucket_write_prefixes": [bucket, advanced_bucket]})

        created_bucket = assert_confirmed(
            client.post(
                f"/api/v1/management/{management_id}/buckets",
                headers=csrf_headers(csrf, idem="create-bucket"),
                json={"name": bucket, "region": "us-east-1"},
            ),
            "create bucket",
        )
        evidence["steps"].append({"step": "create_bucket", "operation_id": created_bucket["operation_id"], "status": created_bucket["status"]})

        listed = assert_ok(client.get(f"/api/v1/management/{management_id}/buckets"), "list buckets")
        if not any(item.get("name") == bucket for item in listed.get("items", [])):
            raise AssertionError(f"created bucket not found in bucket list: {listed}")
        detail = assert_ok(client.get(f"/api/v1/management/{management_id}/buckets/{bucket}"), "bucket detail")
        if detail.get("bucket", {}).get("name") != bucket:
            raise AssertionError(f"bucket detail did not read back created bucket: {detail}")
        evidence["steps"].append({"step": "list_detail", "listed": True, "detail_name": bucket})

        quota = assert_status_with_readback(
            client.put(
                f"/api/v1/management/{management_id}/buckets/{bucket}/quota",
                headers=csrf_headers(csrf, idem="quota"),
                json={"quota_size": 1, "quota_unit": "GB", "quota_enabled": True},
            ),
            "set quota",
            {"confirmed", "needs_review"},
        )
        quota_bucket = quota.get("result", {}).get("readback", {}).get("bucket", {})
        if quota_bucket.get("quota") != 1073741824 or quota_bucket.get("quota_enabled") is not True:
            raise AssertionError(f"quota readback did not prove the requested quota state: {quota}")
        evidence["steps"].append({
            "step": "quota",
            "operation_id": quota["operation_id"],
            "status": quota["status"],
            "route_confirmed": quota.get("result", {}).get("confirmed"),
            "readback_quota": quota_bucket.get("quota"),
            "readback_quota_enabled": quota_bucket.get("quota_enabled"),
        })

        lifecycle = {"rules": [{"id": "expire-test-prefix", "status": "Enabled", "prefix": "tmp/", "expiration_days": 30}]}
        lifecycle_write = assert_confirmed(
            client.put(
                f"/api/v1/management/{management_id}/buckets/{bucket}/lifecycle",
                headers=csrf_headers(csrf, idem="lifecycle"),
                json={"lifecycle": lifecycle},
            ),
            "set lifecycle",
        )
        lifecycle_read = assert_ok(client.get(f"/api/v1/management/{management_id}/buckets/{bucket}/lifecycle"), "read lifecycle")
        if not lifecycle_read:
            raise AssertionError("lifecycle readback is empty")
        evidence["steps"].append({"step": "lifecycle", "operation_id": lifecycle_write["operation_id"], "readback_present": True})

        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket}/public/*",
                }
            ],
        }
        policy_write = assert_confirmed(
            client.put(
                f"/api/v1/management/{management_id}/buckets/{bucket}/policy",
                headers=csrf_headers(csrf, idem="policy"),
                json={"policy": policy},
            ),
            "set policy",
        )
        policy_read = assert_ok(client.get(f"/api/v1/management/{management_id}/buckets/{bucket}/policy"), "read policy")
        if not policy_read:
            raise AssertionError("policy readback is empty")
        evidence["steps"].append({"step": "policy", "operation_id": policy_write["operation_id"], "readback_present": True})

        owner = assert_confirmed(
            client.put(
                f"/api/v1/management/{management_id}/buckets/{bucket}/owner",
                headers=csrf_headers(csrf, idem="owner"),
                json={"owner": "integration-owner"},
            ),
            "set owner",
        )
        evidence["steps"].append({"step": "owner", "operation_id": owner["operation_id"], "status": owner["status"]})

        versioning = assert_confirmed(
            client.put(
                f"/api/v1/management/{management_id}/buckets/{bucket}/versioning",
                headers=csrf_headers(csrf, idem="versioning"),
                json={"status": "Enabled"},
            ),
            "enable versioning",
        )
        read_versioning = assert_ok(client.get(f"/api/v1/management/{management_id}/buckets/{bucket}/versioning"), "read versioning")
        if read_versioning.get("status") != "Enabled":
            raise AssertionError(f"versioning did not read back Enabled: {read_versioning}")
        evidence["steps"].append({"step": "versioning", "operation_id": versioning["operation_id"], "status": read_versioning["status"]})

        s3.put_object(Bucket=bucket, Key="non-empty.txt", Body=b"management write non-empty guard")
        rejected = assert_error(client.delete(f"/api/v1/management/{management_id}/buckets/{bucket}", headers=csrf_headers(csrf, idem="delete-non-empty")), "delete non-empty bucket", 409, "BUCKET_NOT_EMPTY")
        evidence["steps"].append({"step": "delete_non_empty_rejected", "code": rejected["error"]["code"]})

        delete_owned_bucket_contents(s3, bucket)
        deleted = assert_confirmed(
            client.delete(f"/api/v1/management/{management_id}/buckets/{bucket}", headers=csrf_headers(csrf, idem="delete-empty")),
            "delete empty bucket",
        )
        evidence["steps"].append({"step": "delete_empty_bucket", "operation_id": deleted["operation_id"], "status": deleted["status"]})

        head_after = bucket_head_state(s3, bucket)
        if head_after not in {"404", "NoSuchBucket", "NotFound"}:
            raise AssertionError(f"bucket still exists after delete: {head_after}")
        evidence["cleanup"]["head_after_delete"] = head_after

        advanced_created = assert_confirmed(
            client.post(
                f"/api/v1/management/{management_id}/buckets",
                headers=csrf_headers(csrf, idem="create-advanced-empty-bucket"),
                json={
                    "name": advanced_bucket,
                    "region": "us-east-1",
                    "quota_size": 1,
                    "quota_unit": "MB",
                    "quota_enabled": True,
                    "owner": "integration-owner",
                    "versioning_enabled": True,
                    "object_lock_enabled": True,
                    "object_lock_mode": "GOVERNANCE",
                    "set_default_retention": True,
                    "object_lock_duration": 1,
                },
            ),
            "create advanced empty bucket",
        )
        advanced_sdk = assert_advanced_bucket_sdk_state(s3, advanced_bucket)
        advanced_detail = assert_ok(client.get(f"/api/v1/management/{management_id}/buckets/{advanced_bucket}"), "advanced bucket detail")
        if advanced_detail.get("bucket", {}).get("name") != advanced_bucket:
            raise AssertionError(f"advanced bucket detail did not read back the bucket name: {advanced_detail}")
        evidence["steps"].append({
            "step": "create_advanced_empty_bucket",
            "operation_id": advanced_created["operation_id"],
            "status": advanced_created["status"],
            "sdk": advanced_sdk,
            "detail_bucket": advanced_detail.get("bucket", {}).get("name"),
        })
        advanced_deleted = assert_confirmed(
            client.delete(f"/api/v1/management/{management_id}/buckets/{advanced_bucket}", headers=csrf_headers(csrf, idem="delete-advanced-empty-bucket")),
            "delete advanced empty bucket",
        )
        advanced_head_after = bucket_head_state(s3, advanced_bucket)
        if advanced_head_after not in {"404", "NoSuchBucket", "NotFound"}:
            raise AssertionError(f"advanced bucket still exists after delete: {advanced_head_after}")
        evidence["steps"].append({"step": "delete_advanced_empty_bucket", "operation_id": advanced_deleted["operation_id"], "status": advanced_deleted["status"]})
        evidence["cleanup"]["advanced_head_after_delete"] = advanced_head_after

        operations = assert_ok(client.get(f"/api/v1/management/{management_id}/operations?limit=100"), "read management journal")
        if not operations.get("items"):
            raise AssertionError("management journal is empty after write operations")
        assert_no_secret_leaks(operations, secret_values)
        evidence["journal"] = {"operation_count": len(operations["items"]), "secret_leak_check": "passed"}
        assert_no_secret_leaks(evidence, secret_values)
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"MANAGEMENT_WRITE_PASS bucket={bucket} evidence={evidence_path}")
        return 0
    except Exception as exc:
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"MANAGEMENT_WRITE_FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"evidence={evidence_path}")
        return 1
    finally:
        try:
            delete_owned_bucket_contents(s3, bucket)
            if bucket_head_state(s3, bucket) == "exists":
                s3.delete_bucket(Bucket=bucket)
            delete_owned_bucket_contents(s3, advanced_bucket)
            if bucket_head_state(s3, advanced_bucket) == "exists":
                s3.delete_bucket(Bucket=advanced_bucket)
        except Exception as exc:
            print(f"WARN cleanup failed for owned buckets {bucket}/{advanced_bucket}: {type(exc).__name__}: {exc}")
        if not args.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
