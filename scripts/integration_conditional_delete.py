"""Actual S3-backed conditional delete smoke for mutable null versions.

This script creates one isolated random swc-management-* bucket, probes whether
the storage endpoint honors DeleteObject IfMatch, then verifies manual deletion
of unversioned and versioning-suspended null-version objects through the
management API. It never writes global IAM, workers, tables, or production data.
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
    parser = argparse.ArgumentParser(description="Run real S3 conditional-delete integration smoke.")
    parser.add_argument("--admin-url", default="http://10.34.158.137:23646")
    parser.add_argument("--s3-endpoint", default="http://10.34.158.137:8333")
    parser.add_argument("--secrets-file", default=str(ROOT / "output" / "server-secrets.json"))
    parser.add_argument("--admin-secret-ref", default="server-admin")
    parser.add_argument("--s3-secret-ref", default="server-test")
    parser.add_argument("--bucket-prefix", default="swc-management-conditionaldelete")
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


def cleanup_bucket(s3, bucket: str) -> str:
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


def require_supported_probe(data: dict[str, Any]) -> None:
    if data.get("status") not in {"confirmed", "needs_review"}:
        raise AssertionError(f"conditional delete probe returned unexpected status: {data}")
    if data.get("capability_status") != "supported" or data.get("status") != "confirmed":
        raise AssertionError(f"conditional delete is not proven supported; refusing to claim mutable delete success: {data}")
    if data.get("capability") != "conditional_delete_if_match":
        raise AssertionError(f"conditional delete probe did not identify expected capability: {data}")


def require_delete_confirmed(data: dict[str, Any], context: str) -> None:
    if data.get("status") != "confirmed" or data.get("result", {}).get("confirmed") is not True:
        raise AssertionError(f"{context} was not confirmed by readback: {data}")


def mutable_delete_body(scope_id: str, key: str, etag: str, version_id: str | None = "null") -> dict[str, Any]:
    return {
        "scope_id": scope_id,
        "key": key,
        "version_id": version_id,
        "expected_etag": etag,
        "confirm_version_delete": True,
        "acknowledge_unknown_references": True,
    }


def run(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    bucket = f"{args.bucket_prefix}-{secrets.token_hex(5)}".lower()
    secrets_file = Path(args.secrets_file).resolve()
    admin_secret = load_secret(secrets_file, args.admin_secret_ref)
    s3_secret = load_secret(secrets_file, args.s3_secret_ref)
    secret_values = raw_secret_values(admin_secret, s3_secret)
    s3 = s3_client(args.s3_endpoint, s3_secret)
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-conditional-delete-"))
    output_dir = ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    evidence_path = output_dir / f"conditional-delete-{bucket}.json"
    evidence: dict[str, Any] = {
        "bucket": bucket,
        "admin_url": args.admin_url,
        "s3_endpoint": args.s3_endpoint,
        "steps": [],
        "cleanup": {},
    }
    try:
        s3.create_bucket(Bucket=bucket)
        settings = Settings(
            data_dir=tmp_dir,
            database_path=tmp_dir / "console.db",
            admin_password="integration-conditional-delete-password",
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
                "display_name": "Conditional delete S3",
                "endpoint_url": args.s3_endpoint,
                "region": "us-east-1",
                "secret_ref": args.s3_secret_ref,
                "addressing_style": "path",
                "verify_tls": False,
            })
            project = core.create_project(conn, {"project_key": f"conditional-delete-{bucket[-10:]}", "display_name": "Conditional Delete"})
            scope = core.create_scope(conn, project["id"], {
                "connection_id": s3_connection["id"],
                "display_name": "Owned mutable delete scope",
                "bucket": bucket,
                "prefix": "raw/",
                "writable": True,
                "scope_policy": {"allow_original_download": True},
            })
            manager = management.create_connection(conn, settings, {
                "name": "Conditional delete management",
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
                "reason": "isolated conditional delete integration smoke",
            },
        ), "enable object management policy")
        if policy.get("management_write_enabled") is not True or policy.get("permissions", {}).get("object.manage") is not True:
            raise AssertionError(f"object.manage policy did not apply: {policy}")
        evidence["steps"].append({"step": "policy_enabled", "object_manage": True})

        check_url = f"/api/v1/management/{manager['id']}/objects/check-conditional-delete"
        probe = assert_ok(
            client.post(check_url, headers=csrf_headers(csrf, idem="fresh-conditional-delete-probe"), json={"scope_id": scope["id"]}),
            "fresh conditional delete probe",
        )
        evidence["steps"].append({
            "step": "conditional_delete_probe",
            "status": probe.get("status"),
            "capability_status": probe.get("capability_status"),
            "capability": probe.get("capability"),
            "operation_id": probe.get("operation_id"),
            "error_code": probe.get("error_code"),
        })
        require_supported_probe(probe)

        delete_url = f"/api/v1/management/{manager['id']}/objects/delete-version"
        info_url = f"/api/v1/management/{manager['id']}/objects/version-info"

        unversioned_key = "raw/unversioned-null.txt"
        s3.put_object(Bucket=bucket, Key=unversioned_key, Body=b"unversioned-null-body")
        unversioned_head = s3.head_object(Bucket=bucket, Key=unversioned_key)
        unversioned_etag = unversioned_head["ETag"]
        unversioned_info = assert_ok(client.get(info_url, params={"scope_id": scope["id"], "key": unversioned_key}), "unversioned version-info")
        if unversioned_info.get("etag") != unversioned_etag or unversioned_info.get("identity_strength") not in {"mutable", "conditional"}:
            raise AssertionError(f"unversioned version-info did not expose mutable identity: {unversioned_info}")
        deleted_unversioned = assert_ok(
            client.post(delete_url, headers=csrf_headers(csrf, idem="delete-unversioned-null"), json=mutable_delete_body(scope["id"], unversioned_key, unversioned_etag, unversioned_head.get("VersionId"))),
            "delete unversioned null object",
        )
        require_delete_confirmed(deleted_unversioned, "delete unversioned null object")
        if object_head_state(s3, bucket, unversioned_key) not in {"404", "NoSuchKey", "NotFound"}:
            raise AssertionError("unversioned null object still exists after delete")
        replay_unversioned = assert_ok(
            client.post(delete_url, headers=csrf_headers(csrf, idem="delete-unversioned-null"), json=mutable_delete_body(scope["id"], unversioned_key, unversioned_etag, unversioned_head.get("VersionId"))),
            "replay delete unversioned null object",
        )
        if replay_unversioned.get("replayed") is not True or replay_unversioned.get("operation_id") != deleted_unversioned.get("operation_id"):
            raise AssertionError(f"unversioned replay did not return same operation: {replay_unversioned}")
        evidence["steps"].append({
            "step": "delete_unversioned_null_confirmed",
            "operation_id": deleted_unversioned.get("operation_id"),
            "etag": unversioned_etag,
            "head_after": object_head_state(s3, bucket, unversioned_key),
            "replay_same_operation": True,
        })

        guard_key = "raw/wrong-etag-guard.txt"
        s3.put_object(Bucket=bucket, Key=guard_key, Body=b"wrong-etag-guard")
        guard_head = s3.head_object(Bucket=bucket, Key=guard_key)
        wrong = assert_error(
            client.post(delete_url, headers=csrf_headers(csrf, idem="wrong-etag-null"), json=mutable_delete_body(scope["id"], guard_key, '"swc-wrong-etag"', guard_head.get("VersionId"))),
            "wrong ETag null delete rejected",
            409,
            "OBJECT_VERSION_CHANGED",
        )
        guard_body = s3.get_object(Bucket=bucket, Key=guard_key)["Body"].read()
        if guard_head["ETag"] != s3.head_object(Bucket=bucket, Key=guard_key)["ETag"] or guard_body != b"wrong-etag-guard":
            raise AssertionError("wrong ETag rejection did not protect the object")
        evidence["steps"].append({"step": "wrong_etag_null_rejected", "code": wrong["error"]["code"], "object_unchanged": True})

        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Suspended"})
        suspended_key = "raw/suspended-null.txt"
        suspended_put = s3.put_object(Bucket=bucket, Key=suspended_key, Body=b"suspended-null-body")
        suspended_head = s3.head_object(Bucket=bucket, Key=suspended_key)
        suspended_etag = suspended_head["ETag"]
        suspended_version_id = suspended_put.get("VersionId") or suspended_head.get("VersionId") or "null"
        if suspended_version_id not in {None, "", "null"}:
            raise AssertionError(f"expected a suspended null version, got {suspended_version_id!r}")
        suspended_info = assert_ok(client.get(info_url, params={"scope_id": scope["id"], "key": suspended_key}), "suspended null version-info")
        if suspended_info.get("etag") != suspended_etag or suspended_info.get("identity_strength") not in {"mutable", "conditional"}:
            raise AssertionError(f"suspended version-info did not expose mutable identity: {suspended_info}")
        deleted_suspended = assert_ok(
            client.post(delete_url, headers=csrf_headers(csrf, idem="delete-suspended-null"), json=mutable_delete_body(scope["id"], suspended_key, suspended_etag)),
            "delete suspended null object",
        )
        require_delete_confirmed(deleted_suspended, "delete suspended null object")
        if object_head_state(s3, bucket, suspended_key) not in {"404", "NoSuchKey", "NotFound"}:
            raise AssertionError("suspended null object still exists after delete")
        replay_suspended = assert_ok(
            client.post(delete_url, headers=csrf_headers(csrf, idem="delete-suspended-null"), json=mutable_delete_body(scope["id"], suspended_key, suspended_etag)),
            "replay delete suspended null object",
        )
        if replay_suspended.get("replayed") is not True or replay_suspended.get("operation_id") != deleted_suspended.get("operation_id"):
            raise AssertionError(f"suspended replay did not return same operation: {replay_suspended}")
        evidence["steps"].append({
            "step": "delete_suspended_null_confirmed",
            "operation_id": deleted_suspended.get("operation_id"),
            "etag": suspended_etag,
            "head_after": object_head_state(s3, bucket, suspended_key),
            "replay_same_operation": True,
        })

        operations = assert_ok(client.get(f"/api/v1/management/{manager['id']}/operations?limit=100"), "read management journal")
        condition_replay = assert_ok(client.post(check_url, headers=csrf_headers(csrf, idem="fresh-conditional-delete-probe"), json={"scope_id": scope["id"]}), "replay conditional delete probe")
        if condition_replay.get("replayed") is not True or condition_replay.get("operation_id") != probe.get("operation_id"):
            raise AssertionError(f"conditional probe replay did not reuse operation: {condition_replay}")
        assert_no_secret_leaks(operations, secret_values)
        evidence["journal"] = {"operation_count": len(operations.get("items", [])), "secret_leak_check": "passed"}
        evidence["steps"].append({"step": "conditional_probe_replay", "replay_same_operation": True, "operation_id": condition_replay.get("operation_id")})
        assert_no_secret_leaks(evidence, secret_values)
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CONDITIONAL_DELETE_PASS bucket={bucket} evidence={evidence_path}")
        return 0
    except Exception as exc:
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CONDITIONAL_DELETE_FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"evidence={evidence_path}")
        return 1
    finally:
        try:
            evidence["cleanup"]["bucket_head_after_delete"] = cleanup_bucket(s3, bucket)
            evidence_path.write_text(json.dumps(safe_json(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as cleanup_exc:
            print(f"WARN cleanup failed for owned bucket {bucket}: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if not args.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
