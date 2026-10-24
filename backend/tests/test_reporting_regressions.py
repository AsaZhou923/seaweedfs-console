from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from console import catalog, core, db, jobs, management
from console.config import Settings
from console.security import AppError


class FakeAdminClient:
    checked_at = "2026-10-24T00:00:00.000000Z"

    def __init__(self, data=None, error: AppError | None = None):
        self.data = data
        self.error = error

    def get_json(self, path, params=None):
        if self.error:
            raise self.error
        return self.data


MGMT = {"id": "mgmt_1", "protocol_baseline": "seaweedfs-4.48"}


# --- #41: a configured endpoint's 404 is "not found", never "not configured" ---


def test_health_maps_configured_endpoint_404_to_not_found():
    assert management._health_status_from_error(AppError("NATIVE_NOT_FOUND", "x", 404)) == "not_found"
    status = management._health_status("not_found", source="server_registry_endpoint")
    assert status["endpoint_configured"] is True


def test_missing_endpoint_still_reports_not_configured():
    status = management._probe_http_service({}, "filer", ("/status",))
    assert status["status"] == "not_configured"
    assert status["endpoint_configured"] is False


def test_s3_health_404_is_not_found():
    class NotFound(Exception):
        response = {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    assert management._s3_exception_status(NotFound()) == "not_found"


def test_admin_optional_fetch_404_is_not_found():
    result = management._fetch_optional(FakeAdminClient(error=AppError("ADMIN_ENDPOINT_NOT_FOUND", "x", 404)), "/api/volumes/export")
    assert result["status"] == "not_found"


@pytest.mark.parametrize(
    ("raw", "normalized", "port"),
    [
        ("http://[::1]:23646/", "http://[::1]:23646", 23646),
        ("HTTPS://[FD00::5]/admin/", "https://[fd00::5]/admin", None),
        ("http://Admin.Example:23646", "http://admin.example:23646", 23646),
    ],
)
def test_normalize_endpoint_keeps_ipv6_brackets_and_port(raw, normalized, port):
    value = management._normalize_endpoint(raw)
    assert value == normalized
    assert httpx.URL(value).port == port  # httpx can parse what we built


def test_normalize_endpoint_rejects_invalid_port_as_422():
    with pytest.raises(AppError) as error:
        management._normalize_endpoint("http://[::1]:notaport")
    assert (error.value.code, error.value.status) == ("INVALID_ENDPOINT", 422)


# --- #42: capacity aggregation ---


def _volume_export(*volumes):
    return {"data_centers": [{"id": "dc1", "racks": [{"id": "r1", "nodes": [{"id": "node-a", "disks": [{"type": "hdd", "volumes": list(volumes)}]}]}]}]}


def test_collection_size_counts_each_logical_volume_once():
    # Replication 001: every logical volume has two physical replicas.
    data = _volume_export(
        {"Id": 1, "Collection": "photos", "Size": 100},
        {"Id": 1, "Collection": "photos", "Size": 100},
        {"Id": 2, "Collection": "photos", "Size": 40},
        {"Id": 2, "Collection": "photos", "Size": 42},
    )

    item = management._collections(FakeAdminClient(data), MGMT)["items"][0]

    assert item["logical_volumes"] == 2
    assert item["physical_replicas"] == 4
    assert item["size_bytes"] == 142  # largest replica per logical volume
    assert item["physical_replica_bytes"] == 282


def test_collection_size_is_unknown_when_a_sized_replica_has_no_volume_id():
    data = _volume_export({"Id": 1, "Collection": "photos", "Size": 100}, {"Collection": "photos", "Size": 50})

    item = management._collections(FakeAdminClient(data), MGMT)["items"][0]

    assert item["size_bytes"] is None
    assert item["physical_replica_bytes"] == 150


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, database_path=tmp_path / "console.db", admin_password="admin-pass", cursor_signing_key="cursor-key", csrf_signing_key="csrf-key", cookie_secure=False, dev_insecure_cookie=True)


def test_capacity_by_bucket_keeps_same_named_buckets_on_different_connections_apart(tmp_path: Path):
    settings = _settings(tmp_path)
    db.initialize(settings)
    with db.connect(settings) as conn:
        jobs.initialize(conn)
        catalog.initialize(conn)
        project = core.create_project(conn, {"project_key": "p", "display_name": "P"})
        sizes = {}
        for name, size in (("east", 10), ("west", 7)):
            connection = core.create_connection(conn, {"display_name": name, "endpoint_url": f"http://{name}.example:8333", "secret_ref": f"{name}-secret"})
            scope = core.create_scope(conn, project["id"], {"connection_id": connection["id"], "display_name": name, "bucket": "images", "prefix": ""})
            core.upsert_object(conn, scope["id"], {"key": "a.jpg", "size": size, "etag": f'"{name}"'})
            sizes[f"{connection['id']}/images"] = size
        conn.commit()

        result = catalog._capacity_result_for_project(conn, project["id"])

    assert result["by_bucket"] == sizes
    assert result["current_logical_bytes"] == 17
