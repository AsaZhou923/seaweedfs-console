from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from console import core, db, management, management_native
from console.config import Settings
from console.security import AppError, sign_json, unsign_json


def make_settings(tmp_path: Path) -> Settings:
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(
        json.dumps(
            {
                "server-admin": {
                    "kind": "seaweed_admin",
                    "username": "admin",
                    "password": "admin-pass",
                    "allowed_endpoint_url": "http://admin.local:23646",
                    "allowed_endpoints": {
                        "filer": "http://filer.local:8888",
                        "master": "http://master.local:9333",
                        "s3": "http://s3.local:8333",
                    },
                },
                "server-s3": {
                    "access_key_id": "s3-access",
                    "secret_access_key": "s3-secret",
                    "allowed_endpoint_url": "http://s3.local:8333",
                },
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_password="console-pass",
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        secrets_file=secret_path,
        dev_insecure_cookie=True,
        cookie_secure=False,
    )


def app_with_native(settings: Settings) -> FastAPI:
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)
        management_native.initialize(conn)
    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(management.router)
    app.include_router(management_native.router)
    return app


def login_cookie(settings: Settings) -> tuple[str, str]:
    with db.connect(settings) as conn:
        result = core.login(conn, settings, "admin", "console-pass")
    return result["session_token"], result["csrf_token"]


class NativeMock:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.volume_read_only = False
        self.volume_export_omit_read_only = False
        self.fail_readback = False
        self.fail_after_mutation = False
        self.files: dict[str, bytes] = {"/safe/file.txt": b"hello"}
        self.dirs: set[str] = {"/", "/safe", "/safe/dir"}
        self.ignore_upload = False
        self.ignore_config_update = False
        self.root_should_display_more = True
        self.plugin_config: dict = {"admin_config_values": {"enabled": {"bool_value": False}}}
        self.plugin_enabled = True
        self.job_types_response: object = {"job_types": [{"job_type": "vacuum"}]}
        self.activities: list[dict] = []
        self.jobs: list[dict] = []
        self.job_execute_response: dict = {"job_id": "job-exec", "status": "failed"}
        self.job_execute_updates_jobs = False
        self.job_details: dict[str, dict] = {"old": {"job_id": "old", "status": "running"}}
        self.job_expire_status = "failed"
        self.job_expire_response_expired = False
        self.run_adds_unrelated_job = False
        self.run_skip_job_update = False
        self.topics: dict[tuple[str, str], dict] = {}
        self.table_buckets: dict[str, dict] = {}
        self.table_namespaces: dict[str, set[str]] = {}
        self.tables: dict[tuple[str, str, str], dict] = {}
        self.bucket_policies: dict[str, str] = {}
        self.table_policies: dict[tuple[str, str, str], str] = {}
        self.tags: dict[str, dict[str, str]] = {}
        self.s3tables_available = False
        self.long_snapshot_id = 9_007_199_254_740_993
        self.s3tables_target_status = 200
        self.s3tables_target_response: dict = {
            "name": "events",
            "tableARN": "arn:aws:s3tables:us-east-1:000000000000:bucket/warehouse/table/analytics/events",
            "namespace": ["analytics"],
            "format": "ICEBERG",
            "createdAt": "2026-01-01T00:00:00Z",
            "modifiedAt": "2026-01-02T00:00:00Z",
            "ownerAccountId": "000000000000",
            "metadataLocation": "s3://warehouse/analytics/events/metadata/v1.metadata.json?X-Amz-Signature=hidden",
            "versionToken": "v1",
            "metadataVersion": 1,
            "metadata": {
                "fullMetadata": {
                    "current-schema-id": 1,
                    "schemas": [{"schema-id": 1, "fields": [{"id": 1, "name": "event_id", "type": "string", "required": True}]}],
                    "snapshots": [
                        {
                            "snapshot-id": self.long_snapshot_id,
                            "sequence-number": self.long_snapshot_id + 1,
                            "timestamp-ms": self.long_snapshot_id + 2,
                            "summary": {"total-records": self.long_snapshot_id + 3, "aws.secret.access.key": "hidden"},
                        }
                    ],
                    "current-snapshot-id": self.long_snapshot_id,
                    "snapshot-log": [{"snapshot-id": self.long_snapshot_id, "timestamp-ms": self.long_snapshot_id + 4}],
                    "partition-specs": [{"spec-id": 0, "fields": []}],
                    "properties": {"owner": "data", "aws.secret.access.key": "hidden", "service.url": "https://user:pass@example.test/path?token=secret"},
                }
            },
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "filer.local":
            return self._filer(request)
        if request.url.host == "s3.local":
            return self._s3tables_target(request)
        return self._admin(request)

    def _admin(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<input type="hidden" name="csrf_token" value="login-csrf">')
        if request.url.path == "/login" and request.method == "POST":
            form = parse_qs(request.content.decode())
            assert form["username"] == ["admin"]
            assert form["password"] == ["admin-pass"]
            return httpx.Response(303, headers={"location": "/admin", "set-cookie": "admin_sid=remote; Path=/"})
        if request.url.path == "/admin" and request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<meta name="csrf-token" content="rotated-csrf">')
        if request.url.path == "/api/plugin/status":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"enabled": self.plugin_enabled, "worker_count": 1 if self.plugin_enabled else 0})
        if request.url.path == "/api/plugin/workers":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"workers": [{"worker_id": "w1", "capabilities": ["vacuum"]}]})
        if request.url.path == "/api/plugin/jobs":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"jobs": self.jobs})
        if request.url.path == "/api/plugin/jobs/execute" and request.method == "POST":
            if self.job_execute_updates_jobs:
                self.jobs.append(dict(self.job_execute_response))
            return httpx.Response(200, headers={"content-type": "application/json"}, json=self.job_execute_response)
        if request.url.path.startswith("/api/plugin/jobs/") and request.url.path.endswith("/detail") and request.method == "GET":
            job_id = request.url.path.split("/")[4]
            return httpx.Response(200, headers={"content-type": "application/json"}, json=self.job_details.get(job_id, {"job_id": job_id, "status": "unknown", "steps": []}))
        if request.url.path.startswith("/api/plugin/jobs/") and request.method == "GET":
            job_id = request.url.path.split("/")[4]
            for job in self.jobs:
                if job.get("job_id") == job_id:
                    return httpx.Response(200, headers={"content-type": "application/json"}, json=job)
            return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": "missing"})
        if request.url.path.startswith("/api/plugin/jobs/") and request.url.path.endswith("/expire") and request.method == "POST":
            job_id = request.url.path.split("/")[4]
            self.job_details[job_id] = {"job_id": job_id, "status": self.job_expire_status}
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"job_id": job_id, "expired": self.job_expire_response_expired, "job": self.job_details[job_id]})
        if request.url.path == "/api/plugin/lanes":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"lanes": []})
        if request.url.path == "/api/plugin/job-types":
            return httpx.Response(200, headers={"content-type": "application/json"}, json=self.job_types_response)
        if request.url.path == "/api/plugin/job-types/vacuum/descriptor" and request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"job_type": "vacuum", "display_name": "Vacuum", "secret": "redacted-by-management"},
            )
        if request.url.path == "/api/plugin/job-types/vacuum/schema" and request.method == "POST":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"job_type": "vacuum", "admin_config_schema": {"properties": {"enabled": {"type": "bool"}}}, "force_refresh": request.url.params.get("force_refresh")},
            )
        if request.url.path == "/api/plugin/activities":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"activities": self.activities})
        if request.url.path == "/api/plugin/scheduler-states":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"states": []})
        if request.url.path == "/api/plugin/scheduler-status":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"state": "idle"})
        if request.url.path == "/api/plugin/job-types/vacuum/detect" and request.method == "POST":
            self.activities.append({"request_id": "detect-req-1", "job_type": "vacuum", "state": "completed"})
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"request_id": "detect-req-1", "detected": []})
        if request.url.path == "/api/plugin/job-types/vacuum/runs":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"runs": [{"job_id": "run-1", "status": "completed"}]})
        if request.url.path == "/api/plugin/job-types/vacuum/config":
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "application/json"}, json=self.plugin_config)
            if request.method == "PUT":
                if not self.ignore_config_update:
                    self.plugin_config = json.loads(request.content or b"{}")
                return httpx.Response(200, headers={"content-type": "application/json"}, json=self.plugin_config)
        if request.url.path == "/api/plugin/job-types/vacuum/run" and request.method == "POST":
            if self.run_skip_job_update:
                pass
            elif self.run_adds_unrelated_job:
                self.jobs.append({"job_id": "unrelated", "job_type": "other", "status": "completed"})
            else:
                self.jobs.append({"job_id": "job-1", "job_type": "vacuum", "status": "completed"})
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"job_type": "vacuum", "executed_count": 1, "success_count": 1, "error_count": 0, "canceled_count": 0})
        if request.url.path == "/api/volumes/export":
            if self.fail_readback or (self.fail_after_mutation and self.volume_read_only):
                return httpx.Response(500, headers={"content-type": "application/json"}, json={"error": "boom"})
            volume = {"id": 7, "collection": "photos"}
            if not self.volume_export_omit_read_only:
                volume["read_only"] = self.volume_read_only
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "data_centers": [
                        {
                            "id": "dc1",
                            "racks": [
                                {
                                    "id": "rack1",
                                    "nodes": [
                                        {
                                            "id": "node-a",
                                            "disks": [
                                                {
                                                    "type": "hdd",
                                                    "volumes": [
                                                        volume
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/api/volumes/7/node-a/read-only" and request.method == "POST":
            self.volume_read_only = bool(json.loads(request.content or b"{}").get("read_only"))
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"volume_id": 7, "server": "node-a", "read_only": self.volume_read_only})
        if request.url.path == "/api/volumes/7/node-a/vacuum" and request.method == "POST":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "started"})
        if request.url.path == "/api/admin":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message_brokers": None, "mount_clients": None})
        if request.url.path.startswith("/api/s3tables/"):
            if not self.s3tables_available:
                return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": "s3 tables unavailable"})
            return self._s3tables(request)
        if request.url.path == "/api/mq/topics/ns/topic":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"namespace": "ns", "topic": "topic", "subscribers": [], "epoch": 0})
        if request.url.path.startswith("/api/mq/"):
            return self._mq(request)
        if request.url.path == "/api/files/create-folder" and request.method == "POST":
            payload = json.loads(request.content or b"{}")
            target = str(payload["path"]).rstrip("/") + "/" + str(payload["folder_name"])
            self.dirs.add(target)
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Folder created successfully"})
        if request.url.path == "/api/files/delete" and request.method == "DELETE":
            payload = json.loads(request.content or b"{}")
            target = str(payload["path"])
            self.files.pop(target, None)
            self.dirs.discard(target)
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "File deleted successfully"})
        if request.url.path == "/api/files/upload" and request.method == "POST":
            content = request.content
            path_marker = b'name="path"'
            path_index = content.find(path_marker)
            file_index = content.find(b'filename="')
            if path_index < 0 or file_index < 0:
                return httpx.Response(400, headers={"content-type": "application/json"}, json={"error": "bad multipart"})
            path_start = content.find(b"\r\n\r\n", path_index) + 4
            path_end = content.find(b"\r\n--", path_start)
            parent = content[path_start:path_end].decode()
            filename_start = file_index + len(b'filename="')
            filename_end = content.find(b'"', filename_start)
            filename = content[filename_start:filename_end].decode()
            data_start = content.find(b"\r\n\r\n", filename_end) + 4
            data_end = content.find(b"\r\n--", data_start)
            data = content[data_start:data_end]
            target = parent.rstrip("/") + "/" + filename
            self.files[target] = (b"x" * len(data)) if self.ignore_upload else data
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"uploaded": 1, "failed": 0, "files": [{"name": filename, "size": len(data), "path": target}]},
            )
        return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": request.url.path})

    def _filer(self, request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers
        if request.method == "POST" and "mv.from" in request.url.params:
            source = request.url.params["mv.from"]
            target = request.url.path
            if source not in self.files and source not in self.dirs:
                return httpx.Response(400, headers={"content-type": "application/json"}, json={"error": "missing source"})
            if target in self.files or target in self.dirs:
                return httpx.Response(409, headers={"content-type": "application/json"}, json={"error": "target exists"})
            if source in self.files:
                self.files[target] = self.files.pop(source)
            else:
                self.dirs.add(target)
                self.dirs.discard(source)
            return httpx.Response(204)
        if request.method == "HEAD":
            if request.url.path in self.files:
                return httpx.Response(
                    200,
                    headers={
                        "content-type": "text/plain",
                        "content-length": str(len(self.files[request.url.path])),
                        "etag": '"mock-etag"',
                        "set-cookie": "leak=1",
                        "authorization": "Bearer secret",
                    },
                )
            if request.url.path in self.dirs:
                return httpx.Response(200, headers={"content-type": "application/json"})
            return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": request.url.path})
        if request.url.path == "/huge-json":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"Path":"/huge-json","Entries":[],"padding":"' + (b"x" * (8 * 1024 * 1024 + 1)) + b'"}',
            )
        if request.url.path == "/filtered":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "Path": "/filtered",
                    "Entries": [{"FullPath": "relative"}, {"FullPath": "/%2fhidden"}],
                    "LastFileName": "hidden",
                    "ShouldDisplayLoadMore": True,
                },
            )
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "Path": "/",
                    "Entries": [
                        {"FullPath": "/buckets", "Mode": 2147484153, "FileSize": 0, "Mtime": 11, "Extended": {"secret": "hide"}},
                        {"FullPath": "/etc", "Mode": 2147484153, "FileSize": 0, "Mtime": 12, "Content": "do-not-leak"},
                        {"FullPath": "/topics", "Mode": 2147484153, "FileSize": 0, "Mtime": 13, "Remote": {"key": "hide"}},
                    ],
                    "Limit": 1,
                    "LastFileName": "topics",
                    "ShouldDisplayLoadMore": self.root_should_display_more,
                },
            )
        if request.url.path in self.dirs:
            children = []
            prefix = request.url.path.rstrip("/") + "/"
            for path in sorted(self.dirs | set(self.files)):
                if path.startswith(prefix):
                    rest = path[len(prefix) :]
                    if rest and "/" not in rest:
                        children.append({"Name": rest, "IsDirectory": path in self.dirs})
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"Path": request.url.path, "Entries": children})
        if request.url.path in self.files and request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=self.files[request.url.path])
        return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": request.url.path})

    def _mq(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mq/topics/create" and request.method == "POST":
            payload = json.loads(request.content or b"{}")
            key = (payload["namespace"], payload["name"])
            self.topics[key] = {
                "namespace": payload["namespace"],
                "topic": payload["name"],
                "partition_count": payload["partition_count"],
                "retention": payload.get("retention") or {},
            }
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Topic created successfully", "topic": ".".join(key)})
        if request.url.path == "/api/mq/topics/retention/update" and request.method == "POST":
            payload = json.loads(request.content or b"{}")
            key = (payload["namespace"], payload["name"])
            self.topics.setdefault(key, {"namespace": key[0], "topic": key[1], "partition_count": 1})["retention"] = payload.get("retention") or {}
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Topic retention updated successfully", "topic": ".".join(key)})
        if request.url.path == "/api/mq/retention/purge" and request.method == "POST":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Topic retention purge triggered successfully"})
        parts = request.url.path.split("/")
        if len(parts) == 6 and parts[:4] == ["", "api", "mq", "topics"]:
            key = (parts[4], parts[5])
            if key not in self.topics:
                return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": "missing"})
            return httpx.Response(200, headers={"content-type": "application/json"}, json=self.topics[key] | {"subscribers": [], "epoch": 0})
        return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": request.url.path})

    def _s3tables(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/s3tables/buckets":
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"buckets": list(self.table_buckets.values())})
            if request.method == "POST":
                payload = json.loads(request.content or b"{}")
                arn = f"arn:seaweed:s3tables:::bucket/{payload['name']}"
                self.table_buckets[arn] = {"name": payload["name"], "arn": arn, "format": payload.get("format") or "ICEBERG", "tags": payload.get("tags") or {}}
                self.table_namespaces.setdefault(arn, set())
                self.tags[arn] = payload.get("tags") or {}
                return httpx.Response(201, headers={"content-type": "application/json"}, json={"arn": arn})
            if request.method == "DELETE":
                arn = request.url.params.get("bucket")
                self.table_buckets.pop(arn, None)
                self.table_namespaces.pop(arn, None)
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Bucket deleted"})
        if path == "/api/s3tables/namespaces":
            arn = request.url.params.get("bucket")
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"bucket_arn": arn, "namespaces": [{"name": name} for name in sorted(self.table_namespaces.get(arn, set()))]})
            if request.method == "POST":
                payload = json.loads(request.content or b"{}")
                self.table_namespaces.setdefault(payload["bucket_arn"], set()).add(payload["name"])
                return httpx.Response(201, headers={"content-type": "application/json"}, json={"namespace": payload["name"]})
            if request.method == "DELETE":
                name = request.url.params.get("name")
                self.table_namespaces.setdefault(arn, set()).discard(name)
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Namespace deleted"})
        if path == "/api/s3tables/tables":
            arn = request.url.params.get("bucket")
            namespace = request.url.params.get("namespace", "")
            if request.method == "GET":
                tables = [value for (bucket, ns, _name), value in self.tables.items() if bucket == arn and ns == namespace]
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"bucket_arn": arn, "namespace": namespace, "tables": tables})
            if request.method == "POST":
                payload = json.loads(request.content or b"{}")
                table_arn = f"{payload['bucket_arn']}/{payload['namespace']}/{payload['name']}"
                row = {"name": payload["name"], "table_arn": table_arn, "version_token": "v1", "row_count": payload.get("row_count", 0)}
                self.tables[(payload["bucket_arn"], payload["namespace"], payload["name"])] = row
                self.tags[table_arn] = payload.get("tags") or {}
                return httpx.Response(201, headers={"content-type": "application/json"}, json={"table_arn": table_arn, "version_token": "v1"})
            if request.method == "DELETE":
                self.tables.pop((arn, namespace, request.url.params.get("name")), None)
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Table deleted"})
        if path in {"/api/s3tables/bucket-policy", "/api/s3tables/table-policy"}:
            if path.endswith("bucket-policy"):
                key = request.url.params.get("bucket") if request.method != "PUT" else json.loads(request.content or b"{}")["bucket_arn"]
                store = self.bucket_policies
            else:
                if request.method == "PUT":
                    payload = json.loads(request.content or b"{}")
                    key = (payload["bucket_arn"], payload["namespace"], payload["name"])
                else:
                    key = (request.url.params.get("bucket"), request.url.params.get("namespace"), request.url.params.get("name"))
                store = self.table_policies
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"policy": store.get(key, "")})
            if request.method == "PUT":
                payload = json.loads(request.content or b"{}")
                store[key] = payload["policy"]
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Policy updated"})
            if request.method == "DELETE":
                store.pop(key, None)
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Policy deleted"})
        if path == "/api/s3tables/tags":
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"tags": self.tags.get(request.url.params.get("arn"), {})})
            payload = json.loads(request.content or b"{}")
            current = self.tags.setdefault(payload["resource_arn"], {})
            if request.method == "PUT":
                current.update(payload["tags"])
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Tags updated"})
            if request.method == "DELETE":
                for key in payload["tag_keys"]:
                    current.pop(key, None)
                return httpx.Response(200, headers={"content-type": "application/json"}, json={"message": "Tags removed"})
        return httpx.Response(404, headers={"content-type": "application/json"}, json={"error": request.url.path})

    def _s3tables_target(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/"
        assert request.headers.get("x-amz-target") == "S3Tables.GetTable"
        assert "cookie" not in request.headers
        assert "/s3tables/" in request.headers.get("authorization", "")
        if self.s3tables_target_status != 200:
            return httpx.Response(self.s3tables_target_status, headers={"content-type": "application/json"}, json={"message": "typed upstream failure"})
        payload = json.loads(request.content.decode())
        assert payload["tableBucketARN"]
        assert payload["namespace"] == ["analytics"]
        assert payload["name"] == "events"
        return httpx.Response(200, headers={"content-type": "application/json"}, json=self.s3tables_target_response)


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    native = NativeMock()
    monkeypatch.setattr(management, "_TEST_TRANSPORT", native.transport())
    app = app_with_native(settings)
    token, csrf = login_cookie(settings)
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name, token)
        yield client, settings, csrf, native
    monkeypatch.setattr(management, "_TEST_TRANSPORT", None)


def headers(csrf: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


def create_connection(client: TestClient, settings: Settings, csrf: str, *, write: bool = False, s3: bool = False) -> dict[str, str]:
    s3_connection_id = None
    if s3:
        with db.connect(settings) as conn:
            s3_connection_id = core.create_connection(
                conn,
                {
                    "display_name": "S3",
                    "endpoint_url": "http://s3.local:8333",
                    "secret_ref": "server-s3",
                    "region": "us-east-1",
                },
            )["id"]
    endpoints = {"filer": "http://filer.local:8888", "master": "http://master.local:9333"}
    payload = {
        "name": "Admin",
        "admin_url": "http://admin.local:23646",
        "admin_secret_ref": "server-admin",
        "endpoints": endpoints,
    }
    if s3_connection_id:
        endpoints["s3"] = "http://s3.local:8333"
        payload["s3_connection_id"] = s3_connection_id
    response = client.post(
        "/api/v1/management/connections",
        json=payload,
        headers=headers(csrf),
    )
    assert response.status_code == 200, response.text
    item = response.json()
    if write:
        with db.connect(settings) as conn:
            permissions = {
                "file.read_roots": ["/"],
                "file.write_roots": ["/safe"],
                "file.manage": True,
                "volume.manage": True,
                "maintenance.execute": True,
                "mq.manage": True,
                "table.manage": True,
            }
            conn.execute(
                "UPDATE management_connections SET management_write_enabled=1, permissions_json=? WHERE id=?",
                (json.dumps(permissions), item["id"]),
            )
    return item


def test_files_read_requires_auth_roots_hides_system_paths_and_uses_cookie_free_native_client(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf)

    unauth = TestClient(client.app).get(f"/api/v1/management/{conn['id']}/files")
    assert unauth.status_code == 401

    response = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/", "limit": 500})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == 200
    assert [item["name"] for item in body["items"]] == ["buckets", "etc", "topics"]
    assert [item["Name"] for item in body["items"]] == ["buckets", "etc", "topics"]
    assert [item["full_path"] for item in body["items"]] == ["/buckets", "/etc", "/topics"]
    assert [item["FullPath"] for item in body["items"]] == ["/buckets", "/etc", "/topics"]
    assert [item["FileSize"] for item in body["items"]] == [0, 0, 0]
    assert [item["Mtime"] for item in body["items"]] == [11, 12, 13]
    assert all(item["is_directory"] for item in body["items"])
    assert all(item["IsDirectory"] for item in body["items"])
    assert {item["name"]: item["protected"] for item in body["items"]} == {"buckets": False, "etc": True, "topics": True}
    assert "Content" not in response.text
    assert "Extended" not in response.text
    assert "Remote" not in response.text
    assert "do-not-leak" not in response.text
    assert body["next_cursor"]
    assert "preview_hash" not in body
    assert body["page_hash"]
    cursor = unsign_json(body["next_cursor"], settings.require_cursor_key(), "mgmt-files:v1")
    assert cursor["lastFileName"] == "topics"
    filer_requests = [req for req in native.requests if req.url.host == "filer.local"]
    assert filer_requests and "cookie" not in filer_requests[0].headers


def test_files_next_cursor_requires_upstream_more_flag_and_preserves_last_name_when_items_filtered(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf)
    native.root_should_display_more = False

    end = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/"})
    assert end.status_code == 200, end.text
    assert end.json()["raw"]["ShouldDisplayLoadMore"] is False
    assert end.json()["raw"]["LastFileName"] == "topics"
    assert end.json()["next_cursor"] is None

    filtered = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/filtered"})
    assert filtered.status_code == 200, filtered.text
    body = filtered.json()
    assert body["items"] == []
    assert body["next_cursor"]
    cursor = unsign_json(body["next_cursor"], settings.require_cursor_key(), "mgmt-files:v1")
    assert cursor["path"] == "/filtered"
    assert cursor["lastFileName"] == "hidden"


def test_file_delete_preview_walks_full_tree_and_properties_filter_headers(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.dirs.update({"/safe/tree", "/safe/tree/sub"})
    native.files["/safe/tree/root.txt"] = b"root"
    native.files["/safe/tree/sub/leaf.txt"] = b"leaf"

    properties = client.get(f"/api/v1/management/{conn['id']}/files/properties", params={"path": "/safe/file.txt"})
    assert properties.status_code == 200, properties.text
    assert properties.json()["headers"] == {"content-type": "text/plain", "content-length": "5", "etag": '"mock-etag"'}

    protected_preview = client.get(f"/api/v1/management/{conn['id']}/files/delete-preview", params={"path": "/buckets"})
    assert protected_preview.status_code == 403

    preview = client.get(f"/api/v1/management/{conn['id']}/files/delete-preview", params={"path": "/safe/tree"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["path"] == "/safe/tree"
    assert body["complete"] is True
    assert body["truncated"] is False
    assert body["cas_atomic"] is False
    assert {item["full_path"] for item in body["items"]} == {"/safe/tree/root.txt", "/safe/tree/sub", "/safe/tree/sub/leaf.txt"}
    assert body["preview_hash"]


def test_files_cursor_validation_and_native_response_limit(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf)

    bad = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/", "cursor": "not-a-signed-cursor"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_CURSOR"

    expired = sign_json(
        {"management_id": conn["id"], "path": "/", "lastFileName": "x", "expires_at": "2000-01-01T00:00:00.000000Z"},
        settings.require_cursor_key(),
        "mgmt-files:v1",
    )
    expired_response = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/", "cursor": expired})
    assert expired_response.status_code == 400
    assert expired_response.json()["error"]["code"] == "INVALID_CURSOR"

    too_large = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/huge-json"})
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "NATIVE_RESPONSE_TOO_LARGE"


def test_path_traversal_and_s3_raw_byte_guards(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf, write=True)

    traversal = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/%252e%252e/.etc"})
    assert traversal.status_code == 422
    protected = client.get(f"/api/v1/management/{conn['id']}/files", params={"path": "/.etc"})
    assert protected.status_code == 403
    s3_download = client.get(f"/api/v1/management/{conn['id']}/files/download", params={"path": "/buckets/photos/a.jpg"})
    assert s3_download.status_code == 403
    s3_delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/buckets/photos/a.jpg"},
        headers=headers(csrf),
    )
    assert s3_delete.status_code == 403


def test_native_filer_paths_preserve_reserved_filename_characters(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    literal_name = "q?frag#100%.txt"
    literal_path = "/safe/" + literal_name

    upload = client.post(
        f"/api/v1/management/{conn['id']}/files/upload",
        params={"path": "/safe", "idempotency_key": "upload-literal-path"},
        files={"file": (literal_name, b"literal", "text/plain")},
        headers=headers(csrf),
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["status"] == "confirmed"
    assert native.files[literal_path] == b"literal"

    properties = client.get(f"/api/v1/management/{conn['id']}/files/properties", params={"path": literal_path})
    assert properties.status_code == 200, properties.text
    assert properties.json()["headers"]["content-length"] == "7"

    download = client.get(f"/api/v1/management/{conn['id']}/files/download", params={"path": literal_path})
    assert download.status_code == 200, download.text
    assert download.content == b"literal"

    filer_requests = [req for req in native.requests if req.url.host == "filer.local"]
    literal_requests = [req for req in filer_requests if req.url.path == literal_path]
    assert literal_requests
    assert not [req for req in filer_requests if req.url.path == "/safe/q"]
    assert any(b"/safe/q%3Ffrag%23100%25.txt" in req.url.raw_path for req in literal_requests)


def test_file_mkdir_upload_rename_and_delete_confirm_with_real_readback(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)

    mkdir = client.post(
        f"/api/v1/management/{conn['id']}/files/mkdir",
        json={"path": "/safe", "folder_name": "new-folder", "idempotency_key": "mkdir-1"},
        headers=headers(csrf),
    )
    assert mkdir.status_code == 200, mkdir.text
    assert mkdir.json()["status"] == "confirmed"
    assert "/safe/new-folder" in native.dirs

    upload = client.post(
        f"/api/v1/management/{conn['id']}/files/upload",
        params={"path": "/safe", "idempotency_key": "upload-1"},
        files={"file": ("upload.txt", b"payload", "text/plain")},
        headers=headers(csrf),
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["status"] == "confirmed"
    assert native.files["/safe/upload.txt"] == b"payload"
    assert upload.json()["result"]["readback"]["sha256"]

    rename = client.post(
        f"/api/v1/management/{conn['id']}/files/rename",
        json={"source_path": "/safe/upload.txt", "target_path": "/safe/renamed.txt", "idempotency_key": "rename-1"},
        headers=headers(csrf),
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["status"] == "confirmed"
    assert "/safe/upload.txt" not in native.files
    assert native.files["/safe/renamed.txt"] == b"payload"
    rename_calls = [req for req in native.requests if req.url.host == "filer.local" and req.method == "POST" and req.url.path == "/safe/renamed.txt"]
    assert rename_calls[-1].url.params["mv.from"] == "/safe/upload.txt"

    delete_file = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/renamed.txt", "idempotency_key": "delete-file-1"},
        headers=headers(csrf),
    )
    assert delete_file.status_code == 200, delete_file.text
    assert delete_file.json()["status"] == "confirmed"
    assert "/safe/renamed.txt" not in native.files

    with db.connect(settings) as raw:
        rows = raw.execute("SELECT action,state FROM management_operations WHERE action LIKE 'file.%' ORDER BY created_at").fetchall()
    assert [(row["action"], row["state"]) for row in rows] == [
        ("file.mkdir", "confirmed"),
        ("file.upload", "confirmed"),
        ("file.rename", "confirmed"),
        ("file.delete", "confirmed"),
    ]


def test_file_writes_reject_existing_targets_and_directory_delete_requires_preview(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)

    existing_upload = client.post(
        f"/api/v1/management/{conn['id']}/files/upload",
        params={"path": "/safe", "idempotency_key": "existing-upload"},
        files={"file": ("file.txt", b"replacement", "text/plain")},
        headers=headers(csrf),
    )
    assert existing_upload.status_code == 409
    assert native.files["/safe/file.txt"] == b"hello"

    native.files["/safe/source.txt"] = b"source"
    existing_rename = client.post(
        f"/api/v1/management/{conn['id']}/files/rename",
        json={"source_path": "/safe/source.txt", "target_path": "/safe/file.txt", "idempotency_key": "existing-rename"},
        headers=headers(csrf),
    )
    assert existing_rename.status_code == 409
    assert native.files["/safe/source.txt"] == b"source"
    assert native.files["/safe/file.txt"] == b"hello"

    rejected_dir_delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/dir", "idempotency_key": "rejected-dir-delete"},
        headers=headers(csrf),
    )
    assert rejected_dir_delete.status_code == 409
    assert rejected_dir_delete.json()["error"]["code"] == "MANAGEMENT_RECURSIVE_DELETE_REQUIRES_CONFIRMATION"

    confirmed_dir_delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/dir", "confirm_recursive": True, "recursive_preview_path": "/safe/dir", "recursive_preview_hash": "wrong", "idempotency_key": "wrong-dir-delete"},
        headers=headers(csrf),
    )
    assert confirmed_dir_delete.status_code == 409

    preview = client.get(f"/api/v1/management/{conn['id']}/files/delete-preview", params={"path": "/safe/dir"})
    assert preview.status_code == 200, preview.text
    confirmed_dir_delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/dir", "confirm_recursive": True, "recursive_preview_path": "/safe/dir", "recursive_preview_hash": preview.json()["preview_hash"], "idempotency_key": "confirmed-dir-delete"},
        headers=headers(csrf),
    )
    assert confirmed_dir_delete.status_code == 200, confirmed_dir_delete.text
    assert confirmed_dir_delete.json()["status"] == "confirmed"
    assert "/safe/dir" not in native.dirs


def test_file_delete_rejects_truncated_recursive_preview(api, monkeypatch):
    client, settings, csrf, native = api
    monkeypatch.setattr(management_native, "MAX_DELETE_PREVIEW_ENTRIES", 1)
    conn = create_connection(client, settings, csrf, write=True)
    native.dirs.add("/safe/large")
    native.files["/safe/large/a.txt"] = b"a"
    native.files["/safe/large/b.txt"] = b"b"

    preview = client.get(f"/api/v1/management/{conn['id']}/files/delete-preview", params={"path": "/safe/large"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["truncated"] is True
    assert body["complete"] is False

    delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/large", "confirm_recursive": True, "recursive_preview_path": "/safe/large", "recursive_preview_hash": body["preview_hash"]},
        headers=headers(csrf),
    )
    assert delete.status_code == 409
    assert delete.json()["error"]["code"] == "MANAGEMENT_DELETE_PREVIEW_INCOMPLETE"
    assert "/safe/large" in native.dirs


def test_file_rename_and_delete_source_hash_preconditions(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.files["/safe/hash.txt"] = b"source"
    source_hash = "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d"

    stale_rename = client.post(
        f"/api/v1/management/{conn['id']}/files/rename",
        json={"source_path": "/safe/hash.txt", "target_path": "/safe/hash-renamed.txt", "expected_source_sha256": "0" * 64, "idempotency_key": "stale-rename"},
        headers=headers(csrf),
    )
    assert stale_rename.status_code == 409
    assert native.files["/safe/hash.txt"] == b"source"
    assert "/safe/hash-renamed.txt" not in native.files

    rename = client.post(
        f"/api/v1/management/{conn['id']}/files/rename",
        json={"source_path": "/safe/hash.txt", "target_path": "/safe/hash-renamed.txt", "expected_source_sha256": source_hash, "idempotency_key": "hash-rename"},
        headers=headers(csrf),
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["status"] == "confirmed"
    assert rename.json()["result"]["readback"]["target_sha256"] == source_hash

    stale_delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/hash-renamed.txt", "expected_source_sha256": "0" * 64, "idempotency_key": "stale-delete"},
        headers=headers(csrf),
    )
    assert stale_delete.status_code == 409
    assert native.files["/safe/hash-renamed.txt"] == b"source"

    delete = client.post(
        f"/api/v1/management/{conn['id']}/files/delete",
        json={"path": "/safe/hash-renamed.txt", "expected_source_sha256": source_hash, "idempotency_key": "hash-delete"},
        headers=headers(csrf),
    )
    assert delete.status_code == 200, delete.text
    assert delete.json()["status"] == "confirmed"
    assert "/safe/hash-renamed.txt" not in native.files


def test_upload_same_length_wrong_content_needs_review(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.ignore_upload = True

    upload = client.post(
        f"/api/v1/management/{conn['id']}/files/upload",
        params={"path": "/safe", "idempotency_key": "upload-ignored"},
        files={"file": ("upload.txt", b"payload", "text/plain")},
        headers=headers(csrf),
    )

    assert upload.status_code == 200, upload.text
    assert upload.json()["status"] == "needs_review"
    assert native.files["/safe/upload.txt"] == b"xxxxxxx"
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state FROM management_operations WHERE idempotency_key='upload-ignored'").fetchone()
    assert row["state"] == "needs_review"


def test_write_actions_require_csrf_and_persist_intent_before_call(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)

    missing_csrf = client.post(f"/api/v1/management/{conn['id']}/maintenance/actions", json={"action": "detect", "job_type": "vacuum"})
    assert missing_csrf.status_code == 403

    response = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "idempotency_key": "detect-1"},
        headers=headers(csrf),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    detect_calls = [req for req in native.requests if req.url.path == "/api/plugin/job-types/vacuum/detect"]
    assert len(detect_calls) == 1
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state,path FROM management_operations WHERE id=?", (body["operation_id"],)).fetchone()
    assert dict(row) == {"state": "confirmed", "path": "/api/plugin/job-types/vacuum/detect"}


def test_maintenance_config_update_requires_semantic_readback(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    config = {"admin_config_values": {"enabled": {"bool_value": True}}}

    response = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "config_update", "job_type": "vacuum", "config": config, "idempotency_key": "config-1"},
        headers=headers(csrf),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"

    native.ignore_config_update = True
    ignored = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "config_update", "job_type": "vacuum", "config": {"admin_config_values": {"enabled": {"bool_value": False}}}, "idempotency_key": "config-ignored"},
        headers=headers(csrf),
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["status"] == "needs_review"


def test_maintenance_config_and_params_validate_before_operation_dispatch(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf, write=True)

    missing_config = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "config_update", "job_type": "vacuum", "idempotency_key": "missing-config"},
        headers=headers(csrf),
    )
    assert missing_config.status_code == 422
    assert missing_config.json()["error"]["field"] == "config"

    bad_config = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "config_update", "job_type": "vacuum", "config": [], "idempotency_key": "bad-config"},
        headers=headers(csrf),
    )
    assert bad_config.status_code == 422
    assert bad_config.json()["error"]["field"] == "config"

    bad_params = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "params": [], "idempotency_key": "bad-params"},
        headers=headers(csrf),
    )
    assert bad_params.status_code == 422
    assert bad_params.json()["error"]["field"] == "params"

    clear_config = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "config_update", "job_type": "vacuum", "config": {}, "idempotency_key": "clear-config"},
        headers=headers(csrf),
    )
    assert clear_config.status_code == 200, clear_config.text
    assert clear_config.json()["status"] == "confirmed"

    with db.connect(settings) as raw:
        rows = raw.execute("SELECT idempotency_key,state FROM management_operations WHERE action='maintenance.config_update' OR action='maintenance.detect' ORDER BY created_at").fetchall()
    assert [(row["idempotency_key"], row["state"]) for row in rows] == [("clear-config", "confirmed")]


def test_maintenance_plugin_disabled_and_bad_registry_block_before_dispatch(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)

    native.plugin_enabled = False
    disabled = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "idempotency_key": "disabled"},
        headers=headers(csrf),
    )
    assert disabled.status_code == 409
    assert not [req for req in native.requests if req.url.path == "/api/plugin/job-types/vacuum/detect"]

    native.plugin_enabled = True
    native.job_types_response = {"unexpected": []}
    unknown = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "idempotency_key": "unknown-registry"},
        headers=headers(csrf),
    )
    assert unknown.status_code == 502

    native.job_types_response = {"job_types": [{"display_name": "missing type"}]}
    missing = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "idempotency_key": "missing-registry"},
        headers=headers(csrf),
    )
    assert missing.status_code == 502

    native.job_types_response = {"job_types": [{"display_name": "partial"}, {"job_type": "vacuum"}]}
    partial = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "idempotency_key": "partial-registry"},
        headers=headers(csrf),
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["status"] == "confirmed"

    native.job_types_response = {"items": [{"name": "other"}]}
    unsupported = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "detect", "job_type": "vacuum", "idempotency_key": "unsupported-registry"},
        headers=headers(csrf),
    )
    assert unsupported.status_code == 422

    with db.connect(settings) as raw:
        rows = raw.execute("SELECT idempotency_key,state FROM management_operations WHERE idempotency_key LIKE '%registry%' OR idempotency_key='disabled' ORDER BY created_at").fetchall()
    assert [(row["idempotency_key"], row["state"]) for row in rows] == [("partial-registry", "confirmed")]


def test_maintenance_replay_does_not_resend(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    payload = {"action": "detect", "job_type": "vacuum", "idempotency_key": "detect-replay"}

    first = client.post(f"/api/v1/management/{conn['id']}/maintenance/actions", json=payload, headers=headers(csrf))
    replay = client.post(f"/api/v1/management/{conn['id']}/maintenance/actions", json=payload, headers=headers(csrf))

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    detect_calls = [req for req in native.requests if req.url.path == "/api/plugin/job-types/vacuum/detect"]
    assert len(detect_calls) == 1


def test_maintenance_job_type_fetches_config_schema_and_descriptor(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf)

    response = client.get(f"/api/v1/management/{conn['id']}/maintenance/job-types/vacuum", params={"force_refresh": True})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_type"] == "vacuum"
    assert body["config"]["status"] == "supported"
    assert body["config"]["data"]["admin_config_values"]["enabled"]["bool_value"] is False
    assert body["schema"]["status"] == "supported"
    assert body["schema"]["data"]["force_refresh"] == "true"
    assert body["descriptor"]["status"] == "supported"
    assert body["descriptor"]["data"]["display_name"] == "Vacuum"
    assert body["runs"]["status"] == "supported"
    assert body["runs"]["data"]["runs"] == [{"job_id": "run-1", "status": "completed"}]


def test_maintenance_job_detail_fetches_job_and_detail(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf)
    native.jobs.append({"job_id": "job-1", "job_type": "vacuum", "status": "completed"})
    native.job_details["job-1"] = {"job_id": "job-1", "status": "completed", "steps": [{"name": "vacuum"}]}

    response = client.get(f"/api/v1/management/{conn['id']}/maintenance/jobs/job-1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == "job-1"
    assert body["job"]["status"] == "supported"
    assert body["job"]["data"] == {"job_id": "job-1", "job_type": "vacuum", "status": "completed"}
    assert body["detail"]["status"] == "supported"
    assert body["detail"]["data"]["steps"] == [{"name": "vacuum"}]
    paths = [req.url.path for req in native.requests]
    assert "/api/plugin/jobs/job-1" in paths or "/api/plugin/jobs" in paths
    assert "/api/plugin/jobs/job-1/detail" in paths


def test_maintenance_job_detail_rejects_bad_identifier(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf)

    response = client.get(f"/api/v1/management/{conn['id']}/maintenance/jobs/bad/job")

    assert response.status_code == 404

    response = client.get(f"/api/v1/management/{conn['id']}/maintenance/jobs/bad!job")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_maintenance_job_detail_reports_unknown_when_upstream_missing(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf)

    response = client.get(f"/api/v1/management/{conn['id']}/maintenance/jobs/missing")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["status"] in {"not_configured", "unknown"}
    assert body["detail"]["status"] == "supported"
    assert body["detail"]["data"]["job_id"] == "missing"


def test_maintenance_job_execute_failed_response_without_readback_change_needs_review(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.job_execute_response = {"job_id": "job-exec", "status": "failed"}
    native.job_execute_updates_jobs = False

    response = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "job_execute", "job": {"job_id": "job-exec"}, "idempotency_key": "job-exec-failed"},
        headers=headers(csrf),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state FROM management_operations WHERE idempotency_key='job-exec-failed'").fetchone()
    assert row["state"] == "needs_review"


def test_maintenance_run_requires_related_successful_job(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)

    native.run_adds_unrelated_job = True
    unrelated = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "run", "job_type": "vacuum", "idempotency_key": "run-unrelated"},
        headers=headers(csrf),
    )
    assert unrelated.status_code == 200, unrelated.text
    assert unrelated.json()["status"] == "needs_review"

    native.run_adds_unrelated_job = False
    related = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "run", "job_type": "vacuum", "idempotency_key": "run-related"},
        headers=headers(csrf),
    )
    assert related.status_code == 200, related.text
    assert related.json()["status"] == "confirmed"


def test_maintenance_run_existing_same_type_success_without_new_job_needs_review(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.jobs.append({"job_id": "old-success", "job_type": "vacuum", "status": "completed"})
    native.run_skip_job_update = True

    response = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "run", "job_type": "vacuum", "idempotency_key": "run-existing-same-type"},
        headers=headers(csrf),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert response.json()["result"]["readback"]["jobs"] == [{"job_id": "old-success", "job_type": "vacuum", "status": "completed"}]
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state,before_json,result_json FROM management_operations WHERE idempotency_key='run-existing-same-type'").fetchone()
    assert row["state"] == "needs_review"
    assert "old-success" in row["before_json"]
    assert "old-success" in row["result_json"]


def test_maintenance_job_execute_requires_same_job_success_state(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)

    native.job_execute_response = {"job_id": "job-exec", "status": "completed"}
    native.job_execute_updates_jobs = False
    native.jobs.append({"job_id": "other", "status": "completed"})
    unrelated = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "job_execute", "job": {"job_id": "job-exec"}, "idempotency_key": "job-exec-unrelated"},
        headers=headers(csrf),
    )
    assert unrelated.status_code == 200, unrelated.text
    assert unrelated.json()["status"] == "needs_review"

    native.job_execute_updates_jobs = True
    related = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "job_execute", "job": {"job_id": "job-exec"}, "idempotency_key": "job-exec-success"},
        headers=headers(csrf),
    )
    assert related.status_code == 200, related.text
    assert related.json()["status"] == "confirmed"


def test_maintenance_job_expire_failed_only_does_not_confirm(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.job_details["old"] = {"job_id": "old", "status": "running"}
    native.job_expire_status = "failed"

    response = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "job_expire", "job_id": "old", "idempotency_key": "job-expire-failed"},
        headers=headers(csrf),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert response.json()["result"]["readback"]["status"] == "failed"
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state FROM management_operations WHERE idempotency_key='job-expire-failed'").fetchone()
    assert row["state"] == "needs_review"


def test_maintenance_job_expire_confirms_official_expired_response_with_same_job_readback(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.job_details["old"] = {"job_id": "old", "status": "running"}
    native.job_expire_response_expired = True
    native.job_expire_status = "failed"

    response = client.post(
        f"/api/v1/management/{conn['id']}/maintenance/actions",
        json={"action": "job_expire", "job_id": "old", "params": {"reason": "stale"}, "idempotency_key": "job-expire-success"},
        headers=headers(csrf),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert response.json()["result"]["response"]["expired"] is True


def test_volume_read_only_verifies_against_export_and_vacuum_needs_review(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf, write=True)

    read_only = client.post(
        f"/api/v1/management/{conn['id']}/volumes/7/actions",
        json={"action": "read_only", "server_id": "node-a", "read_only": True, "idempotency_key": "volume-read-only"},
        headers=headers(csrf),
    )
    assert read_only.status_code == 200, read_only.text
    assert read_only.json()["status"] == "confirmed"

    vacuum = client.post(
        f"/api/v1/management/{conn['id']}/volumes/7/actions",
        json={"action": "vacuum", "server_id": "node-a", "idempotency_key": "volume-vacuum"},
        headers=headers(csrf),
    )
    assert vacuum.status_code == 200, vacuum.text
    assert vacuum.json()["status"] == "needs_review"


def test_volume_read_only_missing_export_field_cannot_confirm_false(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.volume_export_omit_read_only = True

    response = client.post(
        f"/api/v1/management/{conn['id']}/volumes/7/actions",
        json={"action": "read_only", "server_id": "node-a", "read_only": False, "idempotency_key": "ro-missing-field"},
        headers=headers(csrf),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state FROM management_operations WHERE idempotency_key='ro-missing-field'").fetchone()
    assert row["state"] == "needs_review"


def test_readback_failure_marks_operation_needs_review_without_retry(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, write=True)
    native.fail_after_mutation = True

    response = client.post(
        f"/api/v1/management/{conn['id']}/volumes/7/actions",
        json={"action": "read_only", "server_id": "node-a", "read_only": True, "idempotency_key": "ro-uncertain"},
        headers=headers(csrf),
    )

    assert response.status_code == 502
    with db.connect(settings) as raw:
        row = raw.execute("SELECT state,error_code FROM management_operations WHERE idempotency_key='ro-uncertain'").fetchone()
    assert dict(row) == {"state": "needs_review", "error_code": "ADMIN_UPSTREAM_ERROR"}
    assert len([req for req in native.requests if req.url.path == "/api/volumes/7/node-a/read-only"]) == 1


def test_modules_and_mq_do_not_fake_zero_for_unconfigured_sources(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf)

    modules = client.get(f"/api/v1/management/{conn['id']}/modules")
    assert modules.status_code == 200, modules.text
    body = modules.json()
    assert body["admin"]["data"]["message_brokers"] is None
    assert body["s3_tables"]["s3_tables_buckets"]["status"] == "not_configured"
    assert body["optional_services"]["mount_clients"] == "unknown"

    topic = client.get(f"/api/v1/management/{conn['id']}/modules/mq/topics/ns/topic")
    assert topic.status_code == 200, topic.text
    details = topic.json()["details"]
    assert "subscribers" not in details
    assert "epoch" not in details


def test_mq_native_create_retention_and_purge_verification(api):
    client, settings, csrf, _native = api
    conn = create_connection(client, settings, csrf, write=True)

    create = client.post(
        f"/api/v1/management/{conn['id']}/modules/mq/topics",
        json={
            "namespace": "ns",
            "name": "created",
            "partition_count": 2,
            "retention": {"enabled": True, "retention_seconds": 60},
            "idempotency_key": "mq-create",
        },
        headers=headers(csrf),
    )
    assert create.status_code == 200, create.text
    assert create.json()["status"] == "confirmed"

    details = client.get(f"/api/v1/management/{conn['id']}/modules/mq/topics/ns/created")
    assert details.status_code == 200, details.text
    assert details.json()["details"]["retention"]["retention_seconds"] == 60
    assert "subscribers" not in details.json()["details"]
    assert "epoch" not in details.json()["details"]

    retention = client.post(
        f"/api/v1/management/{conn['id']}/modules/mq/topics/retention",
        json={
            "namespace": "ns",
            "name": "created",
            "retention": {"enabled": True, "retention_seconds": 120},
            "idempotency_key": "mq-retention",
        },
        headers=headers(csrf),
    )
    assert retention.status_code == 200, retention.text
    assert retention.json()["status"] == "confirmed"

    purge = client.post(
        f"/api/v1/management/{conn['id']}/modules/mq/retention/purge",
        json={"idempotency_key": "mq-purge"},
        headers=headers(csrf),
    )
    assert purge.status_code == 200, purge.text
    assert purge.json()["status"] == "needs_review"


def test_s3_table_details_uses_signed_native_target_and_redacts_metadata(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, s3=True)

    response = client.get(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/table-details",
        params={
            "bucket_arn": "arn:aws:s3tables:us-east-1:000000000000:bucket/warehouse",
            "namespace": "analytics",
            "name": "events",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "native_s3tables_get_table"
    assert body["status"] == "supported"
    details = body["details"]
    assert details["tableARN"].endswith("/table/analytics/events")
    assert details["versionToken"] == "v1"
    assert details["metadataVersion"] == 1
    assert details["catalog"]["metadataLocation"].startswith("s3://warehouse/")
    assert details["catalog"]["format"] == "ICEBERG"
    assert details["metadata"]["schema"][0]["name"] == "event_id"
    assert details["metadata"]["snapshot_summary"]["aws.secret.access.key"] == "[REDACTED]"
    assert details["metadata"]["snapshot_summary"]["total-records"] == str(native.long_snapshot_id + 3)
    assert details["metadata"]["snapshots"][0]["snapshot-id"] == str(native.long_snapshot_id)
    assert details["metadata"]["snapshots"][0]["sequence-number"] == str(native.long_snapshot_id + 1)
    assert details["metadata"]["snapshots"][0]["timestamp-ms"] == str(native.long_snapshot_id + 2)
    assert details["metadata"]["history"][0]["snapshot-id"] == str(native.long_snapshot_id)
    assert details["metadata"]["history"][0]["timestamp-ms"] == str(native.long_snapshot_id + 4)
    assert details["metadata"]["properties"]["aws.secret.access.key"] == "[REDACTED]"
    assert details["metadata"]["properties"]["service.url"] == "https://[REDACTED]@example.test/path?token=%5BREDACTED%5D"
    assert "hidden" not in response.text
    assert "s3-secret" not in response.text
    s3_requests = [req for req in native.requests if req.url.host == "s3.local"]
    assert len(s3_requests) == 1
    assert s3_requests[0].url.path == "/"
    assert s3_requests[0].headers["x-amz-target"] == "S3Tables.GetTable"
    assert "cookie" not in s3_requests[0].headers


def test_s3_table_details_reports_not_configured_and_not_found_without_admin_cookie(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf)

    missing_config = client.get(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/table-details",
        params={"bucket_arn": "arn:aws:s3tables:us-east-1:000000000000:bucket/warehouse", "namespace": "analytics", "name": "events"},
    )
    assert missing_config.status_code == 200, missing_config.text
    assert missing_config.json()["status"] == "not_configured"

    conn = create_connection(client, settings, csrf, s3=True)
    native.s3tables_target_status = 404
    not_found = client.get(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/table-details",
        params={"bucket_arn": "arn:aws:s3tables:us-east-1:000000000000:bucket/warehouse", "namespace": "analytics", "name": "events"},
    )
    assert not_found.status_code == 200, not_found.text
    assert not_found.json()["status"] == "not_found"


def test_s3_table_details_lance_is_catalog_only(api):
    client, settings, csrf, native = api
    conn = create_connection(client, settings, csrf, s3=True)
    native.s3tables_target_response = {
        "name": "events",
        "tableARN": "arn:aws:s3tables:us-east-1:000000000000:bucket/warehouse/table/analytics/events",
        "namespace": ["analytics"],
        "format": "LANCE",
        "metadataLocation": "s3://warehouse/analytics/events",
        "versionToken": "v1",
        "metadataVersion": native.long_snapshot_id,
        "metadata": {"fullMetadata": {"properties": {"secret": "do-not-show"}}},
    }

    response = client.get(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/table-details",
        params={"bucket_arn": "arn:aws:s3tables:us-east-1:000000000000:bucket/warehouse", "namespace": "analytics", "name": "events"},
    )

    assert response.status_code == 200, response.text
    details = response.json()["details"]
    assert details["format"] == "LANCE"
    assert details["metadataVersion"] == str(native.long_snapshot_id)
    assert details["catalog"]["metadataVersion"] == str(native.long_snapshot_id)
    assert details["metadata"] is None
    assert details["metadata_supported"] is False
    assert "do-not-show" not in response.text


def test_s3_tables_native_crud_requires_flags_and_precise_readback(api):
    client, settings, csrf, native = api
    native.s3tables_available = True
    conn = create_connection(client, settings, csrf, write=True)

    bucket = client.post(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/buckets",
        json={"name": "warehouse", "tags": {"env": "test"}, "idempotency_key": "table-bucket"},
        headers=headers(csrf),
    )
    assert bucket.status_code == 200, bucket.text
    assert bucket.json()["status"] == "confirmed"
    bucket_arn = bucket.json()["result"]["response"]["arn"]

    namespace = client.post(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/namespaces",
        json={"bucket_arn": bucket_arn, "name": "analytics", "idempotency_key": "table-namespace"},
        headers=headers(csrf),
    )
    assert namespace.status_code == 200, namespace.text
    assert namespace.json()["status"] == "confirmed"

    table = client.post(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/tables",
        json={"bucket_arn": bucket_arn, "namespace": "analytics", "name": "events", "tags": {"team": "data"}, "idempotency_key": "table-create"},
        headers=headers(csrf),
    )
    assert table.status_code == 200, table.text
    assert table.json()["status"] == "confirmed"
    table_arn = table.json()["result"]["response"]["table_arn"]

    blocked_delete = client.request(
        "DELETE",
        f"/api/v1/management/{conn['id']}/modules/s3-tables/tables",
        json={"bucket_arn": bucket_arn, "namespace": "analytics", "name": "events"},
        headers=headers(csrf),
    )
    assert blocked_delete.status_code == 409

    policy = client.put(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/bucket-policy",
        json={"bucket_arn": bucket_arn, "policy": '{"allow":"read"}', "idempotency_key": "table-policy"},
        headers=headers(csrf),
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["status"] == "confirmed"

    tags = client.put(
        f"/api/v1/management/{conn['id']}/modules/s3-tables/tags",
        json={"resource_arn": table_arn, "tags": {"owner": "console"}, "idempotency_key": "table-tags"},
        headers=headers(csrf),
    )
    assert tags.status_code == 200, tags.text
    assert tags.json()["status"] == "confirmed"

    delete_tags = client.request(
        "DELETE",
        f"/api/v1/management/{conn['id']}/modules/s3-tables/tags",
        json={"resource_arn": table_arn, "tag_keys": ["owner"], "idempotency_key": "table-tags-delete"},
        headers=headers(csrf),
    )
    assert delete_tags.status_code == 200, delete_tags.text
    assert delete_tags.json()["status"] == "confirmed"

    delete_table = client.request(
        "DELETE",
        f"/api/v1/management/{conn['id']}/modules/s3-tables/tables",
        json={"bucket_arn": bucket_arn, "namespace": "analytics", "name": "events", "confirm_empty": True, "idempotency_key": "table-delete"},
        headers=headers(csrf),
    )
    assert delete_table.status_code == 200, delete_table.text
    assert delete_table.json()["status"] == "confirmed"

    with db.connect(settings) as raw:
        states = raw.execute("SELECT action,state FROM management_operations WHERE action LIKE 'table.%' ORDER BY created_at").fetchall()
    assert [(row["action"], row["state"]) for row in states] == [
        ("table.create_bucket", "confirmed"),
        ("table.create_namespace", "confirmed"),
        ("table.create_table", "confirmed"),
        ("table.put_bucket_policy", "confirmed"),
        ("table.tag_resource", "confirmed"),
        ("table.untag_resource", "confirmed"),
        ("table.delete_table", "confirmed"),
    ]
