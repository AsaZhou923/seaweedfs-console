"""Run an isolated Console against approved live S3; preserve existing data.

This helper is opt-in. It owns one random bucket and one fresh local database.
It never enables management writes or changes the server's global configuration.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from console import core, db, jobs, management, storage
from console.config import Settings, load_secret_registry
from console.main import create_app


def write_record(path, record):
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def settings(record):
    directory = Path(record["data_dir"])
    return Settings(data_dir=directory, database_path=directory / "console.db",
                    admin_password=record["admin_password"],
                    secrets_file=Path(record["registry"]), dev_insecure_cookie=True,
                    allowed_origins=[record["url"]])


def s3_client(record):
    cfg = settings(record)
    # Recheck the approved endpoint on every prepare/cleanup invocation.
    return storage.client({"id": record.get("connection_id", ""),
                           "endpoint_url": record["endpoint"],
                           "secret_ref": record["secret_ref"], "region": "us-east-1",
                           "addressing_style": "path", "verify_tls": True}, cfg)


def prepare(args, path):
    from PIL import Image, ImageDraw

    if path.exists() or (args.directory / "data" / "console.db").exists():
        raise RuntimeError("Use a fresh artifact directory; existing fixture is preserved.")
    args.directory.mkdir(parents=True, exist_ok=True)
    registry = args.registry.resolve()
    cfg = Settings(data_dir=args.directory / "data", database_path=args.directory / "data" / "console.db",
                   secrets_file=registry)
    entries = load_secret_registry(cfg)
    entry = entries[args.secret_ref]
    endpoint = entry.get("allowed_endpoint_url")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise RuntimeError("The S3 credential must explicitly approve an endpoint.")
    record = {"run_id": secrets.token_hex(8), "bucket": "swc-integration-e2e-" + secrets.token_hex(6),
              "bucket_created": False, "endpoint": endpoint, "registry": str(registry),
              "secret_ref": args.secret_ref, "data_dir": str(args.directory / "data"),
              "admin_password": secrets.token_urlsafe(30), "url": f"http://127.0.0.1:{args.port}"}
    write_record(path, record)
    client = s3_client(record)
    client.create_bucket(Bucket=record["bucket"])
    record["bucket_created"] = True
    write_record(path, record)
    samples = []
    for index, color in enumerate(("#416c62", "#cd9565", "#727e90", "#ac6b5d", "#617f8e", "#a99b80")):
        image = Image.new("RGB", (640 + index * 80, 480), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 65, 360, 365), fill="#eee9dd")
        draw.ellipse((210, 135, 580, 440), fill=color)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88)
        key = f"gallery/sample-{index + 1:02}.jpg"
        samples.append(key)
        client.put_object(Bucket=record["bucket"], Key=key, Body=buffer.getvalue(), ContentType="image/jpeg")
        if index == 0:
            for key in ("gallery/duplicate.jpg", "gallery/space key/literal%_image.jpg"):
                samples.append(key)
                client.put_object(Bucket=record["bucket"], Key=key, Body=buffer.getvalue(), ContentType="image/jpeg")
    client.put_object(Bucket=record["bucket"], Key="gallery/corrupt.jpg", Body=b"not a valid image", ContentType="image/jpeg")
    samples.append("gallery/corrupt.jpg")
    cfg = settings(record)
    create_app(cfg)
    with db.connect(cfg) as conn:
        connection = core.create_connection(conn, {"display_name": "Live E2E S3", "endpoint_url": endpoint,
                        "region": "us-east-1", "secret_ref": args.secret_ref})
        record["connection_id"] = connection["id"]
        project = core.create_project(conn, {"project_key": "e2e-" + record["run_id"], "display_name": "E2E image library"})
        source = core.create_scope(conn, project["id"], {"connection_id": connection["id"],
                    "display_name": "Test images", "bucket": record["bucket"], "prefix": "gallery/",
                    "writable": True, "scope_policy": {"allow_preview": True, "allow_original_download": True}})
        target = core.create_scope(conn, project["id"], {"connection_id": connection["id"],
                    "display_name": "Derived outputs", "bucket": record["bucket"], "prefix": "derived/",
                    "scope_kind": "derived", "writable": True,
                    "scope_policy": {"allow_preview": True, "allow_original_download": True}})
        readonly = core.create_scope(conn, project["id"], {"connection_id": connection["id"],
                    "display_name": "Restricted images", "bucket": record["bucket"], "prefix": "gallery/space key/",
                    "overlap_ack": True, "scope_policy": {}})
        admin = entries.get(args.admin_secret_ref, {})
        if admin:
            control = management.create_connection(conn, cfg, {"name": "Live official OSS 4.48",
                       "admin_url": admin["allowed_endpoint_url"], "admin_secret_ref": args.admin_secret_ref,
                       "s3_connection_id": connection["id"], "endpoints": admin.get("allowed_endpoints", {}),
                       "permissions": {"file.read_roots": ["/"]}})
            record["management_id"] = control["id"]
            record["official_admin_url"] = admin["allowed_endpoint_url"]
        record.update(project_id=project["id"], scope_id=source["id"], output_scope_id=target["id"],
                      restricted_scope_id=readonly["id"], source_keys=samples)
        for scope in (source, readonly):
            jobs.submit_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"],
                            "max_objects": 100, "objects_per_second": 100})
    write_record(path, record)
    while jobs.run_once(cfg):
        pass
    with db.connect(cfg) as conn:
        objects = [row["id"] for row in conn.execute("SELECT id FROM objects WHERE scope_id=? AND is_current=1", (source["id"],))]
        record["object_ids"] = objects
        jobs.submit_job(conn, source["id"], "checksum", {"authz_epoch": source["authz_epoch"], "object_ids": objects})
        jobs.submit_job(conn, source["id"], "preview", {"authz_epoch": source["authz_epoch"], "object_ids": objects})
    while jobs.run_once(cfg):
        pass
    write_record(path, record)
    print(f"Prepared {len(samples)} real S3 images in this run's owned bucket; private credentials stay in {path}.")


def serve(record):
    import os
    import uvicorn

    cfg = settings(record)
    environment = dict(os.environ, CONSOLE_DATA_DIR=str(cfg.data_dir), CONSOLE_DATABASE_PATH=str(cfg.database_path),
                       CONSOLE_ADMIN_PASSWORD=record["admin_password"], CONSOLE_SECRETS_FILE=record["registry"],
                       PYTHONPATH=str(ROOT / "backend"))
    worker = subprocess.Popen([sys.executable, "-m", "console.jobs"], cwd=ROOT, env=environment)
    # Keep the original process handle; never discover or terminate by name/port.
    try:
        uvicorn.run(create_app(cfg), host="127.0.0.1", port=int(record["url"].rsplit(":", 1)[1]),
                    access_log=False, log_level="warning")
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)


def cleanup(record, path):
    bucket = record["bucket"]
    if not record.get("bucket_created") or not bucket.startswith("swc-integration-e2e-") or not record.get("run_id"):
        raise RuntimeError("No owned bucket recorded; refusing cleanup.")
    client = s3_client(record)
    deleted = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            result = client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            if result.get("Errors"):
                raise RuntimeError("Owned object cleanup failed; ownership record retained.")
            deleted += len(objects)
    client.delete_bucket(Bucket=bucket)
    record.update(bucket_created=False, cleanup={"deleted_objects": deleted, "bucket_deleted": True})
    write_record(path, record)
    print(f"Cleaned this run's bucket ({deleted} objects); local evidence preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "serve", "cleanup"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=ROOT / "output/server-secrets.json")
    parser.add_argument("--secret-ref", default="server-test")
    parser.add_argument("--admin-secret-ref", default="server-admin")
    parser.add_argument("--port", type=int, default=18775)
    args = parser.parse_args()
    args.directory = args.directory.resolve()
    path = args.directory / "live-fixture.json"
    if args.action == "prepare":
        prepare(args, path)
    else:
        record = json.loads(path.read_text(encoding="utf-8"))
        if args.action == "serve":
            serve(record)
        else:
            cleanup(record, path)


if __name__ == "__main__":
    main()
