"""Actual S3-backed object-version delete smoke.

Creates isolated random buckets only. The app runs in a temporary TestClient
database while all object versions and object-lock state are read from the real
SeaweedFS S3 endpoint.
"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description="Run real S3 object version delete integration smoke.")
    parser.add_argument("--admin-url", default="http://10.34.158.137:23646")
    parser.add_argument("--s3-endpoint", default="http://10.34.158.137:8333")
    parser.add_argument("--secrets-file", default=str(ROOT / "output" / "server-secrets.json"))
    parser.add_argument("--admin-secret-ref", default="server-admin")
    parser.add_argument("--s3-secret-ref", default="server-test")
    parser.add_argument("--bucket-prefix", default="swc-management-versiondelete")
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
    result = {"aws_access_key_id": str(access), "aws_secret_access_key": str(key)}
    if token:
        result["aws_session_token"] = str(token)
    return result


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
        raise AssertionError(f"{context} failed: HTTP {response.status_code} {response.text[:800]}")
    return response.json()


def assert_error(response, context: str, status: int, code: str | None = None) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(f"{context} expected HTTP {status}, got {response.status_code}: {response.text[:800]}")
    data = response.json()
    observed = data.get("error", {}).get("code")
    if code is not None and observed != code:
        raise AssertionError(f"{context} expected {code}, got {observed}: {data}")
    return data


def csrf_headers(token: str, *, idem: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token}
    if idem:
        headers["Idempotency-Key"] = idem
    return headers


def assert_confirmed(data: dict[str, Any], context: str) -> None:
    if data.get("status") != "confirmed" or data.get("result", {}).get("confirmed") is not True:
        raise AssertionError(f"{context} was not confirmed by readback: {data}")


def bucket_head_state(s3, bucket: str) -> str:
    try:
        s3.head_bucket(Bucket=bucket)
        return "exists"
    except Exception as exc:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))


def object_head_state(s3, bucket: str, key: str, version_id: str | None = None) -> str:
    try:
        kwargs = {"Bucket": bucket, "Key": key}
        if version_id is not None:
            kwargs["VersionId"] = version_id
        s3.head_object(**kwargs)
        return "exists"
    except Exception as exc:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))


def cleanup_bucket(s3, bucket: str, legal_hold_versions: Sequence[tuple[str, str]] = ()) -> str:
    for key, version_id in legal_hold_versions:
        try:
            s3.put_object_legal_hold(Bucket=bucket, Key=key, VersionId=version_id, LegalHold={"Status": "OFF"})
        except Exception:
            pass
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
        if not listed.get("IsTruncated"):
            break
    if bucket_head_state(s3, bucket) == "exists":
        s3.delete_bucket(Bucket=bucket)
    return bucket_head_state(s3, bucket)


def run(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    lock_bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    key = "raw/versioned-object.txt"
    lock_key = "raw/legal-hold-object.txt"
    secrets_file = Path(args.secrets_file).resolve()
    admin_secret = load_secret(secrets_file, args.admin_secret_ref)
    s3_secret = load_secret(secrets_file, args.s3_secret_ref)
    secret_values = raw_secret_values(admin_secret, s3_secret)
    s3 = s3_client(args.s3_endpoint, s3_secret)
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-version-delete-"))
    output_dir = ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    evidence_path = output_dir / f"object-delete-{bucket}.json"
    evidence: dict[str, Any] = {
        "bucket": bucket,
        "lock_bucket": lock_bucket,
        "admin_url": args.admin_url,
        "s3_endpoint": args.s3_endpoint,
        "steps": [],
        "cleanup": {},
    }
    legal_hold_versions: list[tuple[str, str]] = []
    try:
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        first = s3.put_object(Bucket=bucket, Key=key, Body=b"old-version")
        second = s3.put_object(Bucket=bucket, Key=key, Body=b"current-version")
        old_version = first["VersionId"]
        current_version = second["VersionId"]
        current_head = s3.head_object(Bucket=bucket, Key=key, VersionId=current_version)
        old_head = s3.head_object(Bucket=bucket, Key=key, VersionId=old_version)
        current_etag = current_head["ETag"]
        old_etag = old_head["ETag"]
        evidence["steps"].append({
            "step": "fixture_versions_created",
            "key": key,
            "old_version": old_version,
            "current_version": current_version,
            "old_etag": old_etag,
            "current_etag": current_etag,
        })

        settings = Settings(
            data_dir=tmp_dir,
            database_path=tmp_dir / "console.db",
            admin_password="integration-object-delete-password",
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
                "display_name": "Object delete S3",
                "endpoint_url": args.s3_endpoint,
                "region": "us-east-1",
                "secret_ref": args.s3_secret_ref,
                "addressing_style": "path",
                "verify_tls": False,
            })
            project = core.create_project(conn, {"project_key": f"object-delete-{bucket[-10:]}", "display_name": "Object Delete"})
            writable_scope = core.create_scope(conn, project["id"], {
                "connection_id": s3_connection["id"],
                "display_name": "Writable owned bucket",
                "bucket": bucket,
                "prefix": "raw/",
                "writable": True,
                "scope_policy": {"allow_original_download": True},
            })
            manager = management.create_connection(conn, settings, {
                "name": "Object delete management",
                "admin_url": args.admin_url,
                "admin_secret_ref": args.admin_secret_ref,
                "s3_connection_id": s3_connection["id"],
            })
            conn.commit()
        policy = assert_ok(client.put(
            f"/api/v1/management/connections/{manager['id']}/access-policy",
            headers=csrf_headers(csrf),
            json={
                "management_write_enabled": True,
                "acknowledge_management_write": True,
                "permissions": {"object.manage": True},
                "reason": "isolated version delete integration smoke",
            },
        ), "enable object management policy")
        if policy.get("management_write_enabled") is not True or policy.get("permissions", {}).get("object.manage") is not True:
            raise AssertionError(f"object.manage policy did not apply: {policy}")
        evidence["steps"].append({"step": "policy_enabled", "object_manage": True})

        version_info_url = f"/api/v1/management/{manager['id']}/objects/version-info"
        old_info = assert_ok(client.get(version_info_url, params={"scope_id": writable_scope["id"], "key": key, "version_id": old_version}), "old version info")
        if old_info.get("identity_strength") != "strong" or old_info.get("etag") != old_etag:
            raise AssertionError(f"old version info did not prove strong identity: {old_info}")
        evidence["steps"].append({"step": "version_info_old", "identity_strength": old_info["identity_strength"], "etag": old_info["etag"]})

        delete_body = {
            "scope_id": writable_scope["id"],
            "key": key,
            "version_id": old_version,
            "expected_etag": old_etag,
            "confirm_version_delete": True,
            "acknowledge_unknown_references": True,
        }
        delete_url = f"/api/v1/management/{manager['id']}/objects/delete-version"
        deleted = assert_ok(client.post(delete_url, headers=csrf_headers(csrf, idem="delete-old-version"), json=delete_body), "delete old version")
        assert_confirmed(deleted, "delete old version")
        old_after = object_head_state(s3, bucket, key, old_version)
        current_body = s3.get_object(Bucket=bucket, Key=key, VersionId=current_version)["Body"].read()
        latest_body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if old_after not in {"404", "NoSuchVersion", "NotFound"}:
            raise AssertionError(f"old version still exists after delete: {old_after}")
        if current_body != b"current-version" or latest_body != b"current-version":
            raise AssertionError("current version bytes changed after deleting old version")
        replay = assert_ok(client.post(delete_url, headers=csrf_headers(csrf, idem="delete-old-version"), json=delete_body), "replay delete old version")
        if replay.get("replayed") is not True or replay.get("operation_id") != deleted.get("operation_id"):
            raise AssertionError(f"idempotent replay did not return same operation: {replay}")
        evidence["steps"].append({
            "step": "delete_old_version_confirmed",
            "operation_id": deleted["operation_id"],
            "status": deleted["status"],
            "old_head_after": old_after,
            "current_bytes_unchanged": True,
            "replay_same_operation": True,
        })

        wrong_etag = assert_error(
            client.post(delete_url, headers=csrf_headers(csrf, idem="wrong-etag"), json={**delete_body, "version_id": current_version, "expected_etag": '"wrong"'}),
            "wrong etag rejected",
            409,
            "OBJECT_VERSION_CHANGED",
        )
        evidence["steps"].append({"step": "wrong_etag_rejected", "code": wrong_etag["error"]["code"]})
        with db.connect(settings) as conn:
            conn.execute("UPDATE scopes SET writable=0 WHERE id=?", (writable_scope["id"],))
            conn.commit()
        readonly = assert_error(
            client.post(delete_url, headers=csrf_headers(csrf, idem="readonly"), json={**delete_body, "version_id": current_version, "expected_etag": current_etag}),
            "readonly scope rejected",
            403,
            "FORBIDDEN_SCOPE",
        )
        evidence["steps"].append({"step": "readonly_scope_rejected", "code": readonly["error"]["code"]})
        with db.connect(settings) as conn:
            conn.execute("UPDATE scopes SET writable=1 WHERE id=?", (writable_scope["id"],))
            conn.commit()
        null_version = assert_error(
            client.post(delete_url, headers=csrf_headers(csrf, idem="null-version"), json={**delete_body, "version_id": "null", "expected_etag": current_etag}),
            "null version rejected",
            404,
            "S3_OBJECT_REQUEST_FAILED",
        )
        evidence["steps"].append({"step": "nonexistent_null_version_rejected", "code": null_version["error"]["code"]})

        s3.create_bucket(Bucket=lock_bucket, ObjectLockEnabledForBucket=True)
        s3.put_bucket_versioning(Bucket=lock_bucket, VersioningConfiguration={"Status": "Enabled"})
        locked_put = s3.put_object(Bucket=lock_bucket, Key=lock_key, Body=b"locked-version")
        locked_version = locked_put["VersionId"]
        legal_hold_versions.append((lock_key, locked_version))
        locked_head = s3.head_object(Bucket=lock_bucket, Key=lock_key, VersionId=locked_version)
        s3.put_object_legal_hold(Bucket=lock_bucket, Key=lock_key, VersionId=locked_version, LegalHold={"Status": "ON"})
        with db.connect(settings) as conn:
            lock_scope = core.create_scope(conn, project["id"], {
                "connection_id": s3_connection["id"],
                "display_name": "Writable legal hold bucket",
                "bucket": lock_bucket,
                "prefix": "raw/",
                "writable": True,
                "scope_policy": {"allow_original_download": True},
            })
            conn.commit()
        held = assert_error(
            client.post(delete_url, headers=csrf_headers(csrf, idem="legal-hold"), json={
                "scope_id": lock_scope["id"],
                "key": lock_key,
                "version_id": locked_version,
                "expected_etag": locked_head["ETag"],
                "confirm_version_delete": True,
                "acknowledge_unknown_references": True,
            }),
            "legal hold rejected",
            409,
            "OBJECT_LEGAL_HOLD",
        )
        latest_locked = s3.get_object(Bucket=lock_bucket, Key=lock_key, VersionId=locked_version)["Body"].read()
        if latest_locked != b"locked-version":
            raise AssertionError("legal hold rejection changed the locked object")
        evidence["steps"].append({"step": "legal_hold_protection", "status": "verified", "code": held["error"]["code"], "version_id": locked_version})

        operations = assert_ok(client.get(f"/api/v1/management/{manager['id']}/operations?limit=100"), "read management journal")
        if not operations.get("items"):
            raise AssertionError("management journal is empty")
        assert_no_secret_leaks(operations, secret_values)
        evidence["journal"] = {"operation_count": len(operations["items"]), "secret_leak_check": "passed"}
        assert_no_secret_leaks(evidence, secret_values)
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OBJECT_DELETE_PASS bucket={bucket} evidence={evidence_path}")
        return 0
    except Exception as exc:
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OBJECT_DELETE_FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"evidence={evidence_path}")
        return 1
    finally:
        try:
            evidence["cleanup"]["bucket_head_after_delete"] = cleanup_bucket(s3, bucket)
            evidence["cleanup"]["lock_bucket_head_after_delete"] = cleanup_bucket(s3, lock_bucket, legal_hold_versions)
            evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as cleanup_exc:
            print(f"WARN cleanup failed for owned buckets {bucket}/{lock_bucket}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if not args.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
