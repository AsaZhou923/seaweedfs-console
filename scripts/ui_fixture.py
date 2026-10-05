"""Create a dedicated real-server UI fixture and isolated local console data."""
import io
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from PIL import Image, ImageDraw
import boto3
from botocore.config import Config
from console import core, db, jobs
from console.config import Settings
from console.main import create_app


def main():
    registry = ROOT / "output" / "server-secrets.json"
    credentials = json.loads(registry.read_text())["server-test"]
    endpoint = "http://10.34.158.137:8333"
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id=credentials["access_key_id"], aws_secret_access_key=credentials["secret_access_key"],
                      config=Config(s3={"addressing_style": "path"}))
    bucket = "swc-integration-ui-" + secrets.token_hex(4)
    s3.create_bucket(Bucket=bucket)
    for index, color in enumerate(("#243d35", "#eeaa55", "#929caa", "#cd754f", "#4a696e", "#c5b299")):
        image = Image.new("RGB", (640, 480), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((70 + index * 12, 80, 430, 400), fill="#f1eadc")
        draw.ellipse((210, 160, 520, 440), fill=color)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        s3.put_object(Bucket=bucket, Key=f"gallery/sample-{index + 1:02}.jpg", Body=buffer.getvalue(), ContentType="image/jpeg")
    directory = ROOT / "output" / "ui-data"
    password = secrets.token_urlsafe(24)
    settings = Settings(data_dir=directory, database_path=directory / "console.db", admin_password=password,
                        secrets_file=registry, allowed_origins=["http://127.0.0.1:18765"], dev_insecure_cookie=True)
    create_app(settings)
    with db.connect(settings) as conn:
        connection = core.create_connection(conn, {"display_name": "OptiPlex · SeaweedFS 4.48", "endpoint_url": endpoint,
                                                   "region": "us-east-1", "secret_ref": "server-test"})
        project = core.create_project(conn, {"project_key": "server-ui-test", "display_name": "服务器测试图库"})
        scope = core.create_scope(conn, project["id"], {"connection_id": connection["id"], "display_name": "专用测试样本",
                "bucket": bucket, "prefix": "gallery/", "scope_policy": {"allow_preview": True, "allow_original_download": True}})
        jobs.submit_job(conn, scope["id"], "scan", {"authz_epoch": scope["authz_epoch"], "max_objects": 100, "objects_per_second": 100})
    while jobs.run_once(settings):
        pass
    with db.connect(settings) as conn:
        identifiers = [row["id"] for row in conn.execute("SELECT id FROM objects WHERE scope_id=? AND is_current=1", (scope["id"],))]
        jobs.submit_job(conn, scope["id"], "preview", {"object_ids": identifiers, "authz_epoch": scope["authz_epoch"]})
    while jobs.run_once(settings):
        pass
    record = {"bucket": bucket, "endpoint": endpoint, "project_id": project["id"], "scope_id": scope["id"],
              "admin_password": password, "data_dir": str(directory), "secrets_file": str(registry)}
    (ROOT / "output" / "ui-fixture.json").write_text(json.dumps(record), encoding="utf-8")
    print("Isolated UI fixture prepared; server bucket=" + bucket + "; credentials kept in ignored output.")


if __name__ == "__main__":
    main()
