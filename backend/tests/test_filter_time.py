from pathlib import Path

import pytest

from console import catalog, core, db
from console.config import Settings
from console.security import AppError


@pytest.fixture
def records(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "index.db", admin_password="test-only")
    db.initialize(settings)
    with db.connect(settings) as conn:
        connection = core.create_connection(conn, {"display_name": "unit", "endpoint_url": "http://s3", "secret_ref": "unit"})
        project = core.create_project(conn, {"project_key": "time", "display_name": "Time"})
        scope = core.create_scope(conn, project["id"], {"connection_id": connection["id"], "display_name": "Time range", "bucket": "images", "prefix": ""})
        core.upsert_object(conn, scope["id"], {"key": "earlier.jpg", "size": 10, "etag": '"a"', "last_modified": "2026-10-04T00:59:00.000000Z"})
        core.upsert_object(conn, scope["id"], {"key": "later.jpg", "size": 10, "etag": '"b"', "last_modified": "2026-10-04T01:01:00.000000Z"})
    return settings, scope


def test_offset_time_filter_compares_canonical_utc(records):
    settings, scope = records
    with db.connect(settings) as conn:
        page = catalog.query_catalog(conn, settings, scope, {"after": "2026-10-04T10:00:00+09:00"})
        assert [obj["key"] for obj in page["items"]] == ["later.jpg"]
        page = catalog.query_catalog(conn, settings, scope, {"before": "2026-10-04T10:00:00+09:00"})
        assert [obj["key"] for obj in page["items"]] == ["earlier.jpg"]


@pytest.mark.parametrize("value", ["2026-10-04T01:00:00", "not-a-date", "2026-15-01T00:00:00Z"])
def test_invalid_or_naive_time_filter_rejected(records, value):
    settings, scope = records
    with db.connect(settings) as conn, pytest.raises(AppError) as error:
        catalog.query_catalog(conn, settings, scope, {"after": value})
    assert error.value.code == "INVALID_FILTER"
    assert error.value.status == 422
