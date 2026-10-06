from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


RAW_KEYS = (
    "raw/normal.jpg",
    "raw/space key/asset 01.jpg",
    "raw/literal%percent.png",
    "raw/literal_under_score_.webp",
    "raw/literal..dot.jpg",
    "raw/duplicates/a.png",
    "raw/duplicates/b.png",
)


@dataclass(frozen=True)
class SmokeConfig:
    endpoint: str
    secrets_file: Path
    secret_ref: str
    bucket_prefix: str
    writable_scope: bool
    cleanup: bool
    keep_tmp: bool


def parse_args(argv: Sequence[str]) -> SmokeConfig:
    parser = argparse.ArgumentParser(description="Run an isolated SeaweedFS Console integration smoke test.")
    parser.add_argument("--endpoint", required=True, help="SeaweedFS S3 endpoint, for example http://10.34.158.137:8333")
    parser.add_argument("--secrets-file", default="output/server-secrets.json", help="Local secret registry JSON.")
    parser.add_argument("--secret-ref", default="server-test", help="Secret registry key to use for the test connection.")
    parser.add_argument("--bucket-prefix", default="swc-integration", help="Dedicated temporary bucket prefix.")
    parser.add_argument("--writable-scope", action="store_true", help="Required to create and delete the dedicated bucket.")
    parser.add_argument("--cleanup", action="store_true", help="Delete only the bucket created by this run.")
    parser.add_argument("--keep-tmp", action="store_true", help="Keep the temporary app data directory for debugging.")
    args = parser.parse_args(argv)
    return SmokeConfig(
        endpoint=args.endpoint.rstrip("/"),
        secrets_file=Path(args.secrets_file).resolve(),
        secret_ref=args.secret_ref,
        bucket_prefix=args.bucket_prefix,
        writable_scope=args.writable_scope,
        cleanup=args.cleanup,
        keep_tmp=args.keep_tmp,
    )


def _safe_bucket_name(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}".lower()


def _load_secret(path: Path, ref: str) -> dict[str, str | None]:
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    registry = raw.get("secrets", raw) if isinstance(raw, dict) else {}
    if not isinstance(registry, dict) or ref not in registry:
        raise AssertionError(f"secret_ref {ref!r} is missing from registry")
    entry = registry[ref]
    if not isinstance(entry, dict):
        raise AssertionError(f"secret_ref {ref!r} must be a JSON object")
    access = entry.get("access_key_id") or entry.get("aws_access_key_id") or entry.get("access_key")
    secret = entry.get("secret_access_key") or entry.get("aws_secret_access_key") or entry.get("secret_key")
    token = entry.get("session_token") or entry.get("aws_session_token")
    if not access or not secret:
        raise AssertionError(f"secret_ref {ref!r} is missing required S3 credential fields")
    return {"aws_access_key_id": access, "aws_secret_access_key": secret, "aws_session_token": token}


def _make_fixture_bytes() -> dict[str, tuple[bytes, str]]:
    from PIL import Image

    def image(fmt: str, mode: str = "RGB", color: tuple[int, ...] = (16, 96, 176)) -> bytes:
        buffer = io.BytesIO()
        Image.new(mode, (16, 12), color).save(buffer, format=fmt)
        return buffer.getvalue()

    jpeg = image("JPEG")
    png = image("PNG", "RGBA", (12, 200, 80, 128))
    webp = image("WEBP")
    duplicate = image("PNG", "RGBA", (222, 10, 10, 255))
    return {
        "raw/normal.jpg": (jpeg, "image/jpeg"),
        "raw/space key/asset 01.jpg": (jpeg, "image/jpeg"),
        "raw/literal%percent.png": (png, "image/png"),
        "raw/literal_under_score_.webp": (webp, "image/webp"),
        "raw/literal..dot.jpg": (jpeg, "image/jpeg"),
        "raw/duplicates/a.png": (duplicate, "image/png"),
        "raw/duplicates/b.png": (duplicate, "image/png"),
    }


def _s3_client(endpoint: str, secret: dict[str, str | None]):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        **{key: value for key, value in secret.items() if value},
    )


def _s3_object_identity(endpoint: str, secrets_file: Path, secret_ref: str, bucket: str, key: str) -> dict[str, Any]:
    s3 = _s3_client(endpoint, _load_secret(secrets_file, secret_ref))
    head = s3.head_object(Bucket=bucket, Key=key)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        data = body.read()
    finally:
        body.close()
    return {
        "etag": head.get("ETag"),
        "content_length": head.get("ContentLength"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _s3_object_payload(endpoint: str, secrets_file: Path, secret_ref: str, bucket: str, key: str) -> dict[str, Any]:
    s3 = _s3_client(endpoint, _load_secret(secrets_file, secret_ref))
    head = s3.head_object(Bucket=bucket, Key=key)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        data = body.read()
    finally:
        body.close()
    return {
        "body": data,
        "content_type": head.get("ContentType") or "application/octet-stream",
        "metadata": head.get("Metadata") or {},
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _s3_put_payload(endpoint: str, secrets_file: Path, secret_ref: str, bucket: str, key: str, payload: dict[str, Any]) -> None:
    s3 = _s3_client(endpoint, _load_secret(secrets_file, secret_ref))
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload["body"],
        ContentType=payload.get("content_type") or "application/octet-stream",
        Metadata={str(k): str(v) for k, v in dict(payload.get("metadata") or {}).items()},
    )


def _s3_head(endpoint: str, secrets_file: Path, secret_ref: str, bucket: str, key: str) -> dict[str, Any]:
    s3 = _s3_client(endpoint, _load_secret(secrets_file, secret_ref))
    return s3.head_object(Bucket=bucket, Key=key)


def _delete_all_bucket_versions(s3: Any, bucket: str) -> None:
    while True:
        try:
            response = s3.list_object_versions(Bucket=bucket, Prefix="")
        except Exception:
            response = {"Versions": [], "DeleteMarkers": []}
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in [*response.get("Versions", []), *response.get("DeleteMarkers", [])]
            if item.get("Key") and item.get("VersionId")
        ]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        if not response.get("IsTruncated"):
            break

    while True:
        response = s3.list_objects_v2(Bucket=bucket, Prefix="")
        objects = [{"Key": item["Key"]} for item in response.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        if not response.get("IsTruncated"):
            break


def _write_manifest(bucket: str, endpoint: str, keys: Sequence[str], cleanup: bool, tmp_dir: Path | None = None) -> Path:
    output = Path("output")
    output.mkdir(exist_ok=True)
    path = output / f"integration-smoke-{bucket}.json"
    path.write_text(
        json.dumps(
            {
                "bucket": bucket,
                "endpoint": endpoint,
                "keys": list(keys),
                "owned_prefixes": ["raw/", "derived/"],
                "cleanup_requested": cleanup,
                "tmp_dir": str(tmp_dir) if tmp_dir else None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _assert_ok(response, context: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AssertionError(f"{context} failed: HTTP {response.status_code} {response.text[:500]}")
    try:
        return response.json()
    except Exception:
        return {}


def _login(client) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123"},
        headers={"Origin": "http://testserver"},
    )
    data = _assert_ok(response, "login")
    if not data.get("csrf_token"):
        raise AssertionError("login did not return csrf_token")
    return data["csrf_token"]


def _post(client, url: str, csrf: str, payload: dict[str, Any] | None = None):
    return client.post(url, json=payload or {}, headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})


def _post_idem(client, url: str, csrf: str, idempotency_key: str, payload: dict[str, Any] | None = None):
    return client.post(url, json=payload or {}, headers={"X-CSRF-Token": csrf, "Origin": "http://testserver", "Idempotency-Key": idempotency_key})


def _put(client, url: str, csrf: str, payload: dict[str, Any] | None = None):
    return client.put(url, json=payload or {}, headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})


def _assert_error(response, context: str, status: int | tuple[int, ...], code: str | tuple[str, ...]) -> dict[str, Any]:
    statuses = (status,) if isinstance(status, int) else status
    codes = (code,) if isinstance(code, str) else code
    if response.status_code not in statuses:
        raise AssertionError(f"{context} expected HTTP {statuses}, got {response.status_code}: {response.text[:500]}")
    data = response.json()
    observed = data.get("error", {}).get("code")
    if observed not in codes:
        raise AssertionError(f"{context} expected error {codes}, got {observed}: {data}")
    return data


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_bucket_config(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key != "ResponseMetadata"}


def _run_app_flow(config: SmokeConfig, bucket: str, tmp_dir: Path) -> None:
    backend_path = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend_path))
    os.environ["PYTHONPATH"] = str(backend_path)
    os.environ["SEAWEEDFS_CONSOLE_DB"] = str(tmp_dir / "console.db")
    os.environ["SEAWEEDFS_CONSOLE_ADMIN_PASSWORD"] = "admin123"
    os.environ["SEAWEEDFS_CONSOLE_CSRF_SECRET"] = "integration-csrf"
    os.environ["CONSOLE_DATABASE_PATH"] = str(tmp_dir / "console-core.db")
    os.environ["CONSOLE_DATA_DIR"] = str(tmp_dir)
    os.environ["CONSOLE_ADMIN_PASSWORD"] = "admin123"
    os.environ["CONSOLE_SECRETS_FILE"] = str(config.secrets_file)
    try:
        from fastapi.testclient import TestClient
        from console.config import Settings
        from console.main import create_app
    except Exception as exc:
        raise AssertionError(f"application import failed: {type(exc).__name__}: {exc}") from exc

    settings = Settings(
        data_dir=tmp_dir,
        database_path=tmp_dir / "console.db",
        admin_password="admin123",
        allowed_origins=["http://testserver"],
        secrets_file=config.secrets_file,
        dev_insecure_cookie=True,
        cookie_secure=False,
        cursor_signing_key="integration-cursor",
        csrf_signing_key="integration-csrf",
        s3_connect_timeout_seconds=3,
        s3_read_timeout_seconds=10,
    )
    client = TestClient(create_app(settings))
    csrf = _login(client)

    conn = _assert_ok(
        _post(
            client,
            "/api/v1/connections",
            csrf,
            {
                "name": "integration-seaweedfs",
                "endpoint_url": config.endpoint,
                "region": "us-east-1",
                "addressing_style": "path",
                "tls_verify": False,
                "secret_ref": config.secret_ref,
            },
        ),
        "create connection",
    )
    probe = _assert_ok(_post(client, f"/api/v1/connections/{conn['id']}/probe", csrf, {"bucket": bucket, "prefix": "raw/"}), "probe connection")
    if probe.get("capabilities", {}).get("list_objects_v2") != "supported":
        raise AssertionError(f"connection probe could not list the test bucket: {probe}")
    project = _assert_ok(
        _post(client, "/api/v1/projects", csrf, {"project_key": f"smoke-{bucket[-10:]}", "display_name": "Integration Smoke"}),
        "create project",
    )
    scope = _assert_ok(
        _post(
            client,
            f"/api/v1/projects/{project['id']}/scopes",
            csrf,
            {
                "connection_id": conn["id"],
                "display_name": "Integration Raw",
                "bucket": bucket,
                "prefix": "raw/",
                "role": "source",
                "writable": True,
                "manage_bucket": True,
                "allow_overlap": False,
                "scope_policy": {"allow_preview": True, "allow_original_download": True},
            },
        ),
        "create scope",
    )
    output_scope = _assert_ok(
        _post(
            client,
            f"/api/v1/projects/{project['id']}/scopes",
            csrf,
            {
                "connection_id": conn["id"],
                "display_name": "Integration Derived",
                "bucket": bucket,
                "prefix": "derived/",
                "scope_kind": "derived",
                "writable": True,
                "manage_bucket": True,
                "overlap_ack": True,
                "scope_policy": {"allow_preview": True, "allow_original_download": True},
            },
        ),
        "create output scope",
    )
    admin_scope = _assert_ok(
        _post(
            client,
            f"/api/v1/projects/{project['id']}/scopes",
            csrf,
            {
                "connection_id": conn["id"],
                "display_name": "Integration Bucket Admin",
                "bucket": bucket,
                "prefix": "",
                "scope_kind": "admin",
                "writable": False,
                "manage_bucket": True,
                "overlap_ack": True,
                "scope_policy": {"allow_preview": False, "allow_original_download": False},
            },
        ),
        "create bucket admin scope",
    )

    def scan_scope(context: str) -> list[dict[str, Any]]:
        created = _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/scans", csrf, {"max_objects": 100, "objects_per_second": 100}), context)
        created_job_id = created.get("job_id") or created.get("id")
        _run_workers(settings)
        if created_job_id:
            created_job = _assert_ok(client.get(f"/api/v1/jobs/{created_job_id}"), f"get {context} job")
            if created_job.get("state") != "succeeded":
                raise AssertionError(f"{context} job did not succeed: {created_job}")
        listed = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/assets?limit=200"), f"list assets after {context}")
        return list(listed.get("items", []))

    scan = _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/scans", csrf, {"max_objects": 100, "objects_per_second": 100}), "create scan")
    job_id = scan.get("job_id") or scan.get("id")
    _run_workers(settings)
    if job_id:
        job = _assert_ok(client.get(f"/api/v1/jobs/{job_id}"), "get scan job")
        if job.get("state") != "succeeded":
            raise AssertionError(f"scan job did not succeed: {job}")
        if job.get("processed") != len(RAW_KEYS):
            raise AssertionError(f"scan processed {job.get('processed')} objects, expected {len(RAW_KEYS)}")
    assets = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/assets?limit=3"), "list assets")
    if assets.get("next_cursor") is None:
        raise AssertionError("asset listing did not paginate at limit=3")
    all_items = list(assets["items"])
    cursor = assets["next_cursor"]
    while cursor:
        page = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/assets?limit=3&cursor={cursor}"), "list next assets")
        all_items.extend(page["items"])
        cursor = page.get("next_cursor")
    keys = {item.get("object_key") or item.get("key") for item in all_items}
    if set(RAW_KEYS) - keys:
        raise AssertionError(f"missing scanned keys: {sorted(set(RAW_KEYS) - keys)}")
    first = all_items[0]
    object_id = first["id"]
    preview_batch = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/preview-batches", csrf, {"object_ids": [object_id], "preset": "thumb-webp"}),
        "preview batch",
    )
    preview_job_id = preview_batch.get("id") or preview_batch.get("job_id")
    _run_workers(settings)
    if preview_job_id:
        preview_job = _assert_ok(client.get(f"/api/v1/jobs/{preview_job_id}"), "get preview job")
        if preview_job.get("state") != "succeeded":
            raise AssertionError(f"preview job did not succeed: {preview_job}")
    preview = client.get(f"/api/v1/scopes/{scope['id']}/objects/{object_id}/preview")
    if preview.status_code >= 400 or not preview.headers.get("content-type", "").startswith("image/"):
        raise AssertionError(f"preview fetch failed: HTTP {preview.status_code} {preview.text[:300]}")
    checksum = _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/checksum-scans", csrf, {"max_total_bytes": 1048576}), "checksum scan")
    checksum_job_id = checksum.get("id") or checksum.get("job_id")
    _run_workers(settings)
    if checksum_job_id:
        checksum_job = _assert_ok(client.get(f"/api/v1/jobs/{checksum_job_id}"), "get checksum job")
        if checksum_job.get("state") != "succeeded":
            raise AssertionError(f"checksum job did not succeed: {checksum_job}")
    _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/diagnostics/access", csrf, {"object_id": object_id}), "access diagnostic")
    _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/diagnostics/cors-read", csrf, {"origin": "http://testserver"}), "cors diagnostic")
    duplicates = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/duplicates"), "duplicates report")
    if not duplicates.get("groups"):
        raise AssertionError(f"duplicates report did not find the duplicate fixture: {duplicates}")
    capacity = _assert_ok(client.get(f"/api/v1/projects/{project['id']}/capacity"), "capacity report")
    if capacity.get("object_count") != len(RAW_KEYS):
        raise AssertionError(f"capacity object_count mismatch: {capacity}")
    export = client.get(f"/api/v1/scopes/{scope['id']}/reports/export")
    if export.status_code >= 400 or "raw/normal.jpg" not in export.text:
        raise AssertionError(f"export failed: HTTP {export.status_code} {export.text[:300]}")
    print(f"BASIC_PASS bucket={bucket} objects={len(all_items)} duplicate_groups={len(duplicates.get('groups', []))}")
    preset1_params = {
        "mode": "fit",
        "width": 200,
        "height": 200,
        "quality": 82,
        "format": "webp",
        "alpha_policy": "preserve",
        "orientation": "auto",
        "color_policy": "srgb",
        "metadata_policy": "strip",
    }
    preset2_params = dict(preset1_params, width=320, height=320)
    preset1 = _assert_ok(
        _post(client, f"/api/v1/projects/{project['id']}/presets", csrf, {"name": "thumb", "params": preset1_params}),
        "create preset v1",
    )
    preset2 = _assert_ok(
        _post(client, f"/api/v1/projects/{project['id']}/presets", csrf, {"name": "thumb", "params": preset2_params}),
        "create preset v2",
    )
    if preset2.get("version", 0) <= preset1.get("version", 0):
        raise AssertionError("preset version did not increase")
    presets = _assert_ok(client.get(f"/api/v1/projects/{project['id']}/presets"), "list presets")
    if len(presets.get("items", [])) < 2:
        raise AssertionError(f"expected immutable preset versions in list: {presets}")
    normal = next(item for item in all_items if (item.get("key") or item.get("object_key")) == "raw/normal.jpg")
    second_source = next(item for item in all_items if (item.get("key") or item.get("object_key")) == "raw/literal..dot.jpg")
    group = _assert_ok(
        _post(client, f"/api/v1/projects/{project['id']}/groups", csrf, {"name": "smoke-manual-group", "source": "manual"}),
        "create manual group",
    )
    _assert_ok(
        _post(client, f"/api/v1/projects/{project['id']}/groups/{group['id']}/members", csrf, {"object_id": normal["id"], "source": "manual"}),
        "add group member",
    )
    groups = _assert_ok(client.get(f"/api/v1/projects/{project['id']}/groups"), "list groups")
    if not any(item.get("id") == group["id"] for item in groups.get("items", [])):
        raise AssertionError(f"created group missing from list: {groups}")
    members = _assert_ok(client.get(f"/api/v1/projects/{project['id']}/groups/{group['id']}/members?scope={scope['id']}"), "list group members")
    if not any(item.get("id") == normal["id"] and item.get("relation_source") == "manual" for item in members.get("items", [])):
        raise AssertionError(f"group member listing did not expose the added object lineage: {members}")
    asset_tags = _assert_ok(
        _put(client, f"/api/v1/scopes/{scope['id']}/assets/{normal['asset_id']}/tags", csrf, {"tags": [" smoke ", "lineage"]}),
        "put asset tags",
    )
    if set(asset_tags.get("tags", [])) != {"lineage", "smoke"}:
        raise AssertionError(f"asset tags were not normalized and persisted: {asset_tags}")
    tagged_assets = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/assets?tag=smoke"), "filter assets by tag")
    if not any(item.get("asset_id") == normal["asset_id"] for item in tagged_assets.get("items", [])):
        raise AssertionError(f"asset tag filter did not return the tagged asset: {tagged_assets}")
    asset_detail = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/assets/{normal['asset_id']}"), "asset detail")
    if not asset_detail.get("visible_objects"):
        raise AssertionError(f"asset detail did not expose visible object lineage: {asset_detail}")
    derived_body = {
        "object_id": normal["id"],
        "preset_id": preset1["id"],
        "output_scope_id": output_scope["id"],
        "output_key": "derived/thumb-normal.webp",
        "idempotency_key": "derive-normal-thumb-v1",
    }
    derived = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/derived-variants", csrf, derived_body),
        "submit derived variant",
    )
    _run_workers(settings)
    derived_job = _assert_ok(client.get(f"/api/v1/jobs/{derived['job']['id']}"), "get derived job")
    if derived_job.get("state") != "succeeded":
        raise AssertionError(f"derived job did not succeed: {derived_job}")
    variants = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/derived-variants"), "list derived variants")
    variant = next(item for item in variants.get("items", []) if item["id"] == derived["variant"]["id"])
    if variant.get("status") != "succeeded" or variant.get("output_mime") != "image/webp":
        raise AssertionError(f"derived variant did not record webp success: {variant}")
    variant_identity = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, variant["output_key"])
    if variant_identity["content_length"] <= 0:
        raise AssertionError(f"derived object readback was empty: {variant_identity}")
    target_head = client.app.state.settings
    del target_head
    idem = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/derived-variants", csrf, derived_body),
        "resubmit idempotent derived variant",
    )
    if idem["variant"]["id"] != derived["variant"]["id"] or idem["job"]["id"] != derived["job"]["id"]:
        raise AssertionError("idempotent derived submission created duplicate variant or job")
    derived_batch_body = {
        "object_ids": [normal["id"], second_source["id"]],
        "preset_ids": [preset1["id"], preset2["id"]],
        "output_scope_id": output_scope["id"],
        "output_prefix": "derived/batch",
        "idempotency_key": "derived-batch-two-by-two-v1",
    }
    derived_batch = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/derived-variant-batches", csrf, derived_batch_body),
        "submit derived variant batch",
    )
    if derived_batch.get("planned_count") != 4 or derived_batch.get("known_count") != 4:
        raise AssertionError(f"derived batch did not plan 2x2 targets: {derived_batch}")
    first_batch_variant_ids = {item.get("variant_id") for item in derived_batch.get("items", [])}
    _run_workers(settings)
    derived_batch_read = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/derived-variant-batches/{derived_batch['id']}"), "read derived variant batch")
    if derived_batch_read.get("counts", {}).get("succeeded") != 4:
        raise AssertionError(f"derived batch did not produce four succeeded variants: {derived_batch_read}")
    derived_batch_repeat = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/derived-variant-batches", csrf, derived_batch_body),
        "repeat derived variant batch",
    )
    repeat_batch_variant_ids = {item.get("variant_id") for item in derived_batch_repeat.get("items", [])}
    if derived_batch_repeat["id"] != derived_batch["id"] or repeat_batch_variant_ids != first_batch_variant_ids:
        raise AssertionError(f"derived batch idempotency created new targets: {derived_batch_repeat}")
    latest_batch_variant = next(
        item["variant"]
        for item in derived_batch_read["items"]
        if item.get("preset_id") == preset2["id"] and item.get("object_id") == second_source["id"]
    )

    upload = _assert_ok(
        _post(client, f"/api/v1/scopes/{output_scope['id']}/uploads/multipart", csrf, {"key": "derived/upload.bin", "metadata": {"purpose": "smoke"}}),
        "start multipart upload",
    )
    part = client.post(
        f"/api/v1/scopes/{output_scope['id']}/uploads/multipart/{upload['id']}/parts?part_number=1",
        content=b"multipart-smoke",
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )
    part_data = _assert_ok(part, "upload multipart part")
    complete = _assert_ok(
        _post(client, f"/api/v1/scopes/{output_scope['id']}/uploads/multipart/{upload['id']}/complete", csrf, {"parts": [part_data]}),
        "complete multipart upload",
    )
    if complete.get("status") != "completed":
        raise AssertionError(f"multipart complete did not finish: {complete}")
    abort_upload = _assert_ok(
        _post(client, f"/api/v1/scopes/{output_scope['id']}/uploads/multipart", csrf, {"key": "derived/abort.bin"}),
        "start abort multipart upload",
    )
    aborted = _assert_ok(_post(client, f"/api/v1/scopes/{output_scope['id']}/uploads/multipart/{abort_upload['id']}/abort", csrf), "abort multipart upload")
    if aborted.get("status") != "aborted":
        raise AssertionError(f"multipart abort did not record aborted: {aborted}")

    copy1 = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/objects/copy", csrf, {"object_id": normal["id"], "target_key": "raw/copy-normal.jpg"}),
        "copy object",
    )
    if copy1.get("status") != "succeeded":
        raise AssertionError(f"copy did not succeed: {copy1}")
    copy_source_identity = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/normal.jpg")
    copy_target_before = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/copy-normal.jpg")
    if copy_target_before["sha256"] != copy_source_identity["sha256"]:
        raise AssertionError(f"copy target sha256 did not match source: source={copy_source_identity} target={copy_target_before}")
    _assert_error(
        _post(client, f"/api/v1/scopes/{scope['id']}/objects/copy", csrf, {"object_id": normal["id"], "target_key": "raw/copy-normal.jpg"}),
        "copy no overwrite",
        (409, 412),
        ("TARGET_EXISTS", "STORAGE_PRECONDITION_FAILED"),
    )
    copy_target_after = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/copy-normal.jpg")
    if copy_target_after != copy_target_before:
        raise AssertionError("copy no-overwrite changed target object bytes or identity")
    copy_batch_body = {
        "items": [
            {"object_id": normal["id"], "target_scope_id": scope["id"], "target_key": "raw/copy-batch-normal.jpg"},
        ]
    }
    copy_batch = _assert_ok(
        _post_idem(client, f"/api/v1/scopes/{scope['id']}/objects/copy-batches", csrf, "copy-batch-normal-v1", copy_batch_body),
        "submit copy batch",
    )
    if copy_batch.get("state") != "queued" or len(copy_batch.get("items", [])) != 1:
        raise AssertionError(f"copy batch was not queued with one item: {copy_batch}")
    _run_workers(settings)
    copy_batch_read = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/objects/copy-batches/{copy_batch['id']}"), "read copy batch")
    if copy_batch_read.get("state") != "succeeded" or copy_batch_read.get("counts", {}).get("confirmed") != 1:
        raise AssertionError(f"copy batch did not confirm one target: {copy_batch_read}")
    copy_batch_target = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/copy-batch-normal.jpg")
    if copy_batch_target["sha256"] != copy_source_identity["sha256"]:
        raise AssertionError(f"copy batch target sha256 did not match source: source={copy_source_identity} target={copy_batch_target}")
    copy_batch_repeat = _assert_ok(
        _post_idem(client, f"/api/v1/scopes/{scope['id']}/objects/copy-batches", csrf, "copy-batch-normal-v1", copy_batch_body),
        "repeat copy batch idempotency",
    )
    if copy_batch_repeat["id"] != copy_batch["id"] or copy_batch_repeat.get("counts", {}).get("confirmed") != 1:
        raise AssertionError(f"copy batch idempotency did not reuse completed batch: {copy_batch_repeat}")
    source_before_metadata = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/normal.jpg")
    metadata_before = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/metadata"), "get metadata")
    if metadata_before.get("original_unchanged") is not True:
        raise AssertionError(f"metadata read did not confirm source state: {metadata_before}")
    metadata_copy = _assert_ok(
        _put(
            client,
            f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/metadata",
            csrf,
            {"target_key": "raw/metadata-copy-normal.jpg", "metadata": {"edited": "true", "smoke": "metadata"}},
        ),
        "metadata copy",
    )
    if metadata_copy.get("original_unchanged") is not True:
        raise AssertionError(f"metadata copy did not preserve original state: {metadata_copy}")
    metadata_head = _s3_head(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/metadata-copy-normal.jpg")
    if metadata_head.get("Metadata", {}).get("edited") != "true":
        raise AssertionError(f"metadata copy readback missing edited metadata: {metadata_head}")
    source_after_metadata = _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/normal.jpg")
    if source_after_metadata != source_before_metadata:
        raise AssertionError("metadata copy changed the source object's bytes or identity")
    _assert_error(
        _post(client, f"/api/v1/scopes/{scope['id']}/objects/move", csrf, {"object_id": normal["id"], "target_key": "raw/moved.jpg"}),
        "protected move",
        423,
        "PROTECTED_BY_REFERENCE",
    )

    versions = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/versions"), "read versions")
    if "versions" not in versions:
        raise AssertionError(f"versions response missing versions list: {versions}")
    tag_put = _assert_ok(_put(client, f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/tags", csrf, {"tags": {"smoke": "true"}}), "put tags")
    if tag_put.get("tags", {}).get("smoke") != "true":
        raise AssertionError(f"tag write did not read back: {tag_put}")
    tag_get = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/tags"), "get tags")
    if tag_get.get("tags", {}).get("smoke") != "true":
        raise AssertionError(f"tag readback did not persist: {tag_get}")
    _assert_error(
        _put(client, f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/retention", csrf, {"retention": None}),
        "retention write without version",
        501,
        "UNSUPPORTED_CAPABILITY",
    )
    trash_preview = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/owned-objects/trash-preview"), "owned trash preview")
    if trash_preview.get("physical_delete_enabled") is not False or not any(item.get("id") == variant["id"] for item in trash_preview.get("items", [])):
        raise AssertionError(f"owned trash preview did not expose logical-only eligible variant: {trash_preview}")
    trashed = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/owned-objects/{variant['id']}/trash", csrf, {"reason": "smoke logical trash"}),
        "owned logical trash",
    )
    if trashed.get("status") != "trashed" or trashed.get("physical_delete_enabled") is not False:
        raise AssertionError(f"owned trash did not stay logical-only: {trashed}")
    _s3_object_identity(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/normal.jpg")
    restored = _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/owned-objects/{variant['id']}/restore", csrf), "owned trash restore")
    if restored.get("status") != "restored":
        raise AssertionError(f"owned trash restore failed: {restored}")
    s3_for_health = _s3_client(config.endpoint, _load_secret(config.secrets_file, config.secret_ref))

    def health_check_for(variant_id: str) -> dict[str, Any]:
        health = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/derived-variants/health"), "derived health")
        if health.get("checked_count", 0) > 20 or health.get("publication_state") != "not_connected":
            raise AssertionError(f"derived health did not preserve bounded local-only publication contract: {health}")
        for check in health.get("checks", []):
            if check.get("variant_id") == variant_id:
                return check
        raise AssertionError(f"derived health did not include variant {variant_id}: {health}")

    healthy_check = health_check_for(latest_batch_variant["id"])
    if healthy_check.get("state") != "healthy":
        raise AssertionError(f"latest derived batch variant was not healthy before mutation: {healthy_check}")
    old_preset_check = health_check_for(variant["id"])
    if old_preset_check.get("state") != "outdated_preset":
        raise AssertionError(f"old preset variant was not classified as outdated_preset: {old_preset_check}")
    variant_payload = _s3_object_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, latest_batch_variant["output_key"])
    s3_for_health.delete_object(Bucket=bucket, Key=latest_batch_variant["output_key"])
    missing_check = health_check_for(latest_batch_variant["id"])
    if missing_check.get("state") != "missing_output":
        raise AssertionError(f"deleted derived output was not classified as missing_output: {missing_check}")
    _s3_put_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, latest_batch_variant["output_key"], variant_payload)
    corrupt_payload = dict(variant_payload)
    corrupt_payload["body"] = b"not-the-derived-webp-bytes"
    _s3_put_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, latest_batch_variant["output_key"], corrupt_payload)
    corrupt_check = health_check_for(latest_batch_variant["id"])
    if corrupt_check.get("state") != "corrupt_output":
        raise AssertionError(f"corrupt derived output was not classified as corrupt_output: {corrupt_check}")
    _s3_put_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, latest_batch_variant["output_key"], variant_payload)
    restored_health = health_check_for(latest_batch_variant["id"])
    if restored_health.get("state") != "healthy":
        raise AssertionError(f"restored derived output did not return to healthy: {restored_health}")
    source_payload = _s3_object_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/literal..dot.jpg")
    from PIL import Image

    changed_source_buffer = io.BytesIO()
    Image.new("RGB", (16, 12), (250, 180, 20)).save(changed_source_buffer, format="JPEG")
    _s3_put_payload(
        config.endpoint,
        config.secrets_file,
        config.secret_ref,
        bucket,
        "raw/literal..dot.jpg",
        {"body": changed_source_buffer.getvalue(), "content_type": "image/jpeg", "metadata": {"fault": "source-overwrite-health"}},
    )
    scan_scope("rescan source overwrite")
    source_changed_check = health_check_for(latest_batch_variant["id"])
    if source_changed_check.get("state") != "outdated_source":
        raise AssertionError(f"overwritten source was not classified as outdated_source: {source_changed_check}")
    if not source_changed_check.get("stored_input_revision") or not source_changed_check.get("current_indexed_revision") or not source_changed_check.get("current_observed_revision"):
        raise AssertionError(f"outdated_source response did not expose stored/current revision proof: {source_changed_check}")
    if source_changed_check["stored_input_revision"] == source_changed_check["current_observed_revision"]:
        raise AssertionError(f"outdated_source observed revision did not differ from stored input revision: {source_changed_check}")
    _s3_put_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/literal..dot.jpg", source_payload)
    scan_scope("rescan source restore")
    restored_source = _s3_object_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/literal..dot.jpg")
    if restored_source["sha256"] != source_payload["sha256"]:
        raise AssertionError("source restore did not recover the original bytes")
    s3_for_health.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    old_version_buffer = io.BytesIO()
    Image.new("RGB", (16, 12), (10, 20, 220)).save(old_version_buffer, format="JPEG")
    old_version_bytes = old_version_buffer.getvalue()
    new_version_buffer = io.BytesIO()
    Image.new("RGB", (16, 12), (220, 20, 10)).save(new_version_buffer, format="JPEG")
    new_version_bytes = new_version_buffer.getvalue()
    old_version_put = s3_for_health.put_object(Bucket=bucket, Key="raw/versioned-download.jpg", Body=old_version_bytes, ContentType="image/jpeg")
    new_version_put = s3_for_health.put_object(Bucket=bucket, Key="raw/versioned-download.jpg", Body=new_version_bytes, ContentType="image/jpeg")
    old_version_id = old_version_put.get("VersionId")
    new_version_id = new_version_put.get("VersionId")
    if not old_version_id or not new_version_id or old_version_id == new_version_id:
        raise AssertionError(f"owned versioning fixture did not produce two distinct version ids: old={old_version_put} new={new_version_put}")
    versioned_items = scan_scope("rescan versioned download fixture")
    versioned_object = next((item for item in versioned_items if (item.get("key") or item.get("object_key")) == "raw/versioned-download.jpg"), None)
    if not versioned_object or versioned_object.get("version_id") != new_version_id:
        raise AssertionError(f"versioned fixture did not index the latest version id: object={versioned_object} expected={new_version_id}")
    old_download = client.get(f"/api/v1/scopes/{scope['id']}/objects/{versioned_object['id']}/download?version_id={old_version_id}")
    if old_download.status_code != 200 or hashlib.sha256(old_download.content).hexdigest() != hashlib.sha256(old_version_bytes).hexdigest():
        raise AssertionError(f"old version download did not return old bytes: HTTP {old_download.status_code} len={len(old_download.content)}")
    current_download = client.get(f"/api/v1/scopes/{scope['id']}/objects/{versioned_object['id']}/download")
    if current_download.status_code != 200 or hashlib.sha256(current_download.content).hexdigest() != hashlib.sha256(new_version_bytes).hexdigest():
        raise AssertionError(f"current version download did not return newest bytes: HTTP {current_download.status_code} len={len(current_download.content)}")
    null_download = client.get(f"/api/v1/scopes/{scope['id']}/objects/{normal['id']}/download?version_id=null")
    normal_payload = _s3_object_payload(config.endpoint, config.secrets_file, config.secret_ref, bucket, "raw/normal.jpg")
    if null_download.status_code != 200 or hashlib.sha256(null_download.content).hexdigest() != normal_payload["sha256"]:
        raise AssertionError(f"literal null version download did not return pre-versioning normal object bytes: HTTP {null_download.status_code} len={len(null_download.content)}")

    cors_config = {
        "CORSRules": [
            {
                "AllowedMethods": ["GET"],
                "AllowedOrigins": ["http://testserver"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 60,
            }
        ]
    }
    initial_cors = _assert_ok(client.get(f"/api/v1/scopes/{admin_scope['id']}/bucket/cors"), "read initial bucket cors")
    if initial_cors.get("cas_supported") is not False or not initial_cors.get("current_hash"):
        raise AssertionError(f"bucket cors read did not expose cooperative hash contract: {initial_cors}")
    initial_cors_after_rejected_ack = _assert_ok(client.get(f"/api/v1/scopes/{admin_scope['id']}/bucket/cors"), "read bucket cors before ack rejection")
    _assert_error(
        _post(
            client,
            f"/api/v1/scopes/{admin_scope['id']}/bucket/cors",
            csrf,
            {"config": cors_config, "expected_current_hash": initial_cors["current_hash"]},
        ),
        "bucket cors missing exclusive writer ack",
        422,
        "VALIDATION_ERROR",
    )
    initial_cors_after_missing_ack = _assert_ok(client.get(f"/api/v1/scopes/{admin_scope['id']}/bucket/cors"), "read bucket cors after ack rejection")
    if _canonical_json(_stable_bucket_config(initial_cors_after_missing_ack["config"])) != _canonical_json(_stable_bucket_config(initial_cors_after_rejected_ack["config"])):
        raise AssertionError("bucket config missing-ack rejection changed remote config")
    _assert_error(
        _post(
            client,
            f"/api/v1/scopes/{admin_scope['id']}/bucket/cors",
            csrf,
            {"config": cors_config, "expected_current_hash": "stale-hash", "exclusive_writer_ack": True},
        ),
        "bucket cors stale hash",
        409,
        "CONFLICT",
    )
    stale_reject_readback = _assert_ok(client.get(f"/api/v1/scopes/{admin_scope['id']}/bucket/cors"), "read bucket cors after stale rejection")
    if _canonical_json(_stable_bucket_config(stale_reject_readback["config"])) != _canonical_json(_stable_bucket_config(initial_cors["config"])):
        raise AssertionError("bucket config stale-hash rejection changed remote config")
    bucket_config = _assert_ok(
        _post(
            client,
            f"/api/v1/scopes/{admin_scope['id']}/bucket/cors",
            csrf,
            {"config": cors_config, "expected_current_hash": initial_cors["current_hash"], "exclusive_writer_ack": True},
        ),
        "save bucket cors",
    )
    if bucket_config.get("status") not in {"succeeded", "succeeded_with_warning"}:
        raise AssertionError(f"bucket cors save did not succeed: {bucket_config}")
    cors_read = _assert_ok(client.get(f"/api/v1/scopes/{admin_scope['id']}/bucket/cors"), "read bucket cors")
    if not cors_read.get("config", {}).get("CORSRules") or cors_read["current_hash"] != bucket_config["readback_hash"]:
        raise AssertionError(f"bucket cors readback missing rules: {cors_read}")
    if bucket_config.get("status") == "succeeded_with_warning" and "no remote CAS" not in bucket_config.get("warning", ""):
        raise AssertionError(f"bucket config warning did not explicitly describe non-CAS limitation: {bucket_config}")
    rolled_cors = _assert_ok(
        _post(
            client,
            f"/api/v1/scopes/{admin_scope['id']}/bucket/cors/rollback",
            csrf,
            {"snapshot_id": bucket_config["before_snapshot_id"], "expected_current_hash": cors_read["current_hash"], "exclusive_writer_ack": True},
        ),
        "rollback bucket cors",
    )
    if rolled_cors.get("status") not in {"succeeded", "succeeded_with_warning"} or _canonical_json(_stable_bucket_config(rolled_cors.get("config", {}))) != _canonical_json(_stable_bucket_config(initial_cors["config"])):
        raise AssertionError(f"bucket cors rollback did not restore the before-write snapshot: {rolled_cors}")
    manifest = _assert_ok(
        _post(
            client,
            f"/api/v1/scopes/{scope['id']}/reference-manifests",
            csrf,
            {"schema_version": 1, "coverage": {"scope_id": scope["id"]}, "declared_count": 0, "entries": [], "finalize": True},
        ),
        "empty reference manifest",
    )
    if manifest.get("status") not in {"complete", "partial"}:
        raise AssertionError(f"reference manifest did not close cleanly: {manifest}")
    snapshot = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/capacity-snapshots", csrf, {"source_bytes": 999999999, "unknown_bytes": 999999999, "sample_complete": False}),
        "capacity snapshot",
    )
    if snapshot.get("source") != "administrator_declared" or snapshot.get("source_bytes") == 999999999:
        raise AssertionError(f"capacity snapshot did not compute index bytes independently of client input: {snapshot}")
    if snapshot.get("version_bytes") is not None or snapshot.get("physical_disk_bytes") is not None:
        raise AssertionError(f"manual capacity snapshot should preserve unknown physical/version bytes as null: {snapshot}")
    trends = _assert_ok(client.get(f"/api/v1/scopes/{scope['id']}/capacity-trends"), "capacity trends")
    if not trends.get("auto_samples") or trends["auto_samples"][0].get("source") != "auto_scan":
        raise AssertionError(f"capacity trends missing automatic scan sample: {trends}")
    if not trends.get("manual_snapshots") or trends["manual_snapshots"][0].get("id") != snapshot["id"]:
        raise AssertionError(f"capacity trends missing manual snapshot separately from scan sample: {trends}")
    if trends["auto_samples"][0].get("object_count", 0) < len(RAW_KEYS) + 1 or trends["auto_samples"][0].get("version_bytes") is not None:
        raise AssertionError(f"capacity auto sample did not preserve scan metrics and unknown version bytes: {trends}")
    thresholds = _assert_ok(_post(client, f"/api/v1/scopes/{scope['id']}/capacity-thresholds", csrf, {"thresholds": {"unknown_bytes": 1}}), "capacity thresholds")
    if (thresholds.get("status") != "unknown" or thresholds.get("alerts") != []
            or "unknown_bytes" not in thresholds.get("unknown_fields", [])):
        raise AssertionError(f"unmeasured capacity must stay unknown without a false zero or alert: {thresholds}")
    measured_thresholds = _assert_ok(
        _post(client, f"/api/v1/scopes/{scope['id']}/capacity-thresholds", csrf,
              {"thresholds": {"source_bytes": 1, "unknown_bytes": 1}}),
        "measured capacity thresholds",
    )
    if (measured_thresholds.get("status") != "alerting"
            or not any(alert.get("field") == "source_bytes" for alert in measured_thresholds.get("alerts", []))
            or "unknown_bytes" not in measured_thresholds.get("unknown_fields", [])):
        raise AssertionError(f"measured excess must alert while retaining unknown fields: {measured_thresholds}")
    print(f"CAPABILITY_LIMIT retention=UNSUPPORTED_CAPABILITY version_count={len(versions.get('versions', []))}")
    print(f"ENHANCED_PASS derived={variant['id']} multipart={upload['id']} snapshot={snapshot['id']}")


def _run_workers(settings: Any) -> None:
    from console import jobs

    ran = 0
    while jobs.run_once(settings):
        ran += 1
        if ran > 20:
            raise AssertionError("worker did not quiesce after 20 jobs")


def run(config: SmokeConfig) -> int:
    if not config.writable_scope:
        print("FAIL --writable-scope is required so the script may create one isolated test bucket.")
        return 1
    if not config.secrets_file.is_file():
        print(f"FAIL secrets file missing: {config.secrets_file}")
        return 1
    bucket = _safe_bucket_name(config.bucket_prefix)
    tmp_dir = Path(tempfile.mkdtemp(prefix="swc-smoke-"))
    manifest_path: Path | None = None
    created_bucket = False
    try:
        secret = _load_secret(config.secrets_file, config.secret_ref)
        print(
            "secret_ref="
            f"{config.secret_ref} fields_present={{'access_key_id': True, 'secret_access_key': True, "
            f"'session_token': {bool(secret.get('aws_session_token'))}}}"
        )
        s3 = _s3_client(config.endpoint, secret)
        s3.create_bucket(Bucket=bucket)
        created_bucket = True
        bucket_head = s3.head_bucket(Bucket=bucket)
        server_header = bucket_head.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("server", "")
        if "SeaweedFS" not in server_header or "4.48" not in server_header:
            raise AssertionError(f"expected SeaweedFS 4.48 test endpoint, got server header {server_header!r}")
        s3.list_buckets()
        s3.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedMethods": ["GET"],
                        "AllowedOrigins": ["http://initial.test"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 30,
                    }
                ]
            },
        )
        fixtures = _make_fixture_bytes()
        for key, (data, content_type) in fixtures.items():
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type, Metadata={"sha256": hashlib.sha256(data).hexdigest()})
        listed = s3.list_objects_v2(Bucket=bucket, Prefix="raw/", MaxKeys=20)
        observed_keys = {item["Key"] for item in listed.get("Contents", [])}
        if set(RAW_KEYS) - observed_keys:
            raise AssertionError(f"S3 fixture provisioning missing keys: {sorted(set(RAW_KEYS) - observed_keys)}")
        s3.head_object(Bucket=bucket, Key="raw/normal.jpg")
        body = s3.get_object(Bucket=bucket, Key="raw/normal.jpg")["Body"]
        try:
            if not body.read(1):
                raise AssertionError("S3 GET returned an empty normal fixture")
        finally:
            body.close()
        print(f"PROBE_SOURCE server={server_header} head_get=supported list_buckets=checked")
        manifest_path = _write_manifest(bucket, config.endpoint, RAW_KEYS, config.cleanup, tmp_dir if config.keep_tmp else None)
        _run_app_flow(config, bucket, tmp_dir)
    except Exception as exc:
        if manifest_path is None:
            manifest_path = _write_manifest(bucket, config.endpoint, RAW_KEYS, config.cleanup, tmp_dir if config.keep_tmp else None)
        print(f"FAIL {type(exc).__name__}: {exc}")
        print(f"bucket={bucket}")
        print(f"manifest={manifest_path}")
        if created_bucket and not config.cleanup:
            print("cleanup=not_requested; created bucket left for evidence")
        return 1
    finally:
        if created_bucket and config.cleanup:
            try:
                s3 = _s3_client(config.endpoint, _load_secret(config.secrets_file, config.secret_ref))
                _delete_all_bucket_versions(s3, bucket)
                s3.delete_bucket(Bucket=bucket)
            except Exception as exc:
                print(f"WARN cleanup failed for created bucket {bucket}: {type(exc).__name__}: {exc}")
        if not config.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"PASS bucket={bucket}")
    print(f"manifest={manifest_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
