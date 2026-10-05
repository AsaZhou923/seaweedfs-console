import json
import time
from types import SimpleNamespace

import httpx
import pytest

from console import core, db, management, management_native, table_worker_preview
from console.config import Settings
from console.security import AppError


def sample_html(rows=True):
    body = '<div class="card-header"><h6><i></i>Sample Rows </h6></div><div class="card-body">'
    if rows:
        body += """
        <div class="table-responsive"><table class="table table-sm table-hover table-striped">
        <thead><tr><th class="text-muted">#</th><th>id</th><th>name</th></tr></thead>
        <tbody>
          <tr><td class="text-muted">1</td><td><span class="small">1</span></td><td><span class="small">alpha</span></td></tr>
          <tr><td class="text-muted">2</td><td><span class="small">2</span></td><td><span class="small">beta</span></td></tr>
        </tbody></table></div>
        """
    else:
        body += '<div class="text-center text-muted py-4">No rows to preview.</div>'
    return "<html><body>" + body + "</div></body></html>"


class HtmlAdmin:
    def __init__(self, html):
        self.html = html
        self.calls = []
        self.closed = False

    def get_html(self, path, params=None):
        self.calls.append((path, params))
        content = self.html if isinstance(self.html, bytes) else self.html.encode("utf-8")
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=content)

    def close(self):
        self.closed = True


class RequestAdmin(HtmlAdmin):
    def __init__(self, html):
        super().__init__(html)
        self.private_kwargs = None

    def _login(self):
        self.logged_in = True

    def _request(self, method, path, **kwargs):
        self.private_kwargs = kwargs
        return self.get_html(path, params=kwargs.get("params"))


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "db", admin_password="pw")
    db.initialize(settings)
    with db.connect(settings) as conn:
        connection = core.create_connection(conn, {"display_name": "S3", "endpoint_url": "http://s3.local", "secret_ref": "s"})
        project = core.create_project(conn, {"project_key": "p", "display_name": "P"})
        scope = core.create_scope(conn, project["id"], {"connection_id": connection["id"], "display_name": "Table",
                                  "bucket": "bucket", "prefix": "table/", "scope_policy": {"allow_original_download": True}})
    catalog = {"format": "LANCE", "versionToken": "v1", "warehouseLocation": "s3://bucket/table/",
               "metadataLocation": "s3://bucket/table/metadata.json"}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    mgmt = {"id": "mgmt_1", "admin_url": "http://admin.local", "admin_secret_ref": "a"}
    admin = HtmlAdmin(sample_html())
    monkeypatch.setattr(management, "admin_client", lambda _settings, _conn, _mgmt: admin)
    monkeypatch.setattr(management_native, "fetch_table_details",
                        lambda *args: {"status": "supported", "details": {"catalog": dict(catalog)}})
    return SimpleNamespace(settings=settings, request=request, mgmt=mgmt, scope=scope, catalog=catalog, admin=admin,
                           bucket_arn="arn:aws:s3tables:us-east-1:000000000000:bucket/bucket")


def call_preview(ctx, limit=2, namespace="ns", name="events"):
    return table_worker_preview.preview(ctx.request, ctx.mgmt, ctx.scope, ctx.catalog, ctx.bucket_arn, namespace, name, limit)


def test_worker_preview_parses_official_sample_rows_html(ctx):
    result = call_preview(ctx)
    assert result["status"] == "supported"
    assert result["preview_kind"] == "worker_sample"
    assert result["worker_source"] == "official_admin_worker_preview_html"
    assert result["columns"] == [
        {"name": "id", "type": "unknown", "nullable": None},
        {"name": "name", "type": "unknown", "nullable": None},
    ]
    assert result["rows"] == [{"id": "1", "name": "alpha"}, {"id": "2", "name": "beta"}]
    assert result["sample_truncated"] is None
    assert result["notes"] == []
    assert result["authorization_source"] == "catalog_warehouse_location"
    assert result["total_rows"] is None and result["deletes_applied"] is None
    assert ctx.admin.calls == [("/object-store/s3tables/buckets/bucket/namespaces/ns/tables/events/data", {"limit": 2})]
    assert ctx.admin.closed is True


def test_worker_preview_authorizes_lance_default_location_when_catalog_location_missing(ctx):
    ctx.catalog.update({
        "name": "events",
        "namespace": "analytics.daily",
        "bucket_arn": ctx.bucket_arn,
        "tableARN": ctx.bucket_arn + "/table/analytics.daily/events",
        "warehouseLocation": None,
        "metadataLocation": None,
    })
    ctx.scope["prefix"] = "analytics.daily/events/"
    result = call_preview(ctx, namespace="analytics.daily", name="events")
    assert result["status"] == "supported"
    assert result["authorization_source"] == "official_lance_default_location"
    assert result["bucket_arn"] == ctx.bucket_arn


def test_worker_preview_rejects_external_or_partially_authorized_lance_location(ctx):
    ctx.catalog.update({"warehouseLocation": None, "metadataLocation": "s3://bucket/other/events", "name": "events", "namespace": "ns"})
    with pytest.raises(AppError) as external:
        call_preview(ctx)
    assert external.value.status == 403

    ctx.catalog["metadataLocation"] = None
    ctx.scope["prefix"] = "ns/events/data/"
    with pytest.raises(AppError) as partial:
        call_preview(ctx)
    assert partial.value.status == 403


def test_worker_preview_rejects_catalog_identity_mismatch(ctx):
    ctx.catalog["name"] = "other"
    with pytest.raises(AppError) as bad_name:
        call_preview(ctx)
    assert bad_name.value.code == "TABLE_CATALOG_MISMATCH"
    ctx.catalog["name"] = "events"
    ctx.catalog["namespace"] = "other"
    with pytest.raises(AppError) as bad_namespace:
        call_preview(ctx)
    assert bad_namespace.value.code == "TABLE_CATALOG_MISMATCH"


def test_worker_preview_preserves_cell_text_and_rejects_bad_headers(ctx):
    ctx.admin.html = """
    <div class="card-header"><h6>Sample Rows</h6></div>
    <table><thead><tr><th>#</th><th>url</th><th>note</th></tr></thead>
    <tbody><tr><td>1</td><td>https://example.test/a?plain=1</td><td>  spaced
    text  </td></tr></tbody></table>
    """
    result = call_preview(ctx)
    assert result["rows"] == [{"url": "https://example.test/a?plain=1", "note": "spaced\n    text"}]
    ctx.admin.html = """
    <div class="card-header"><h6>Sample Rows</h6></div>
    <table><thead><tr><th>#</th><th>url</th></tr></thead>
    <tbody><tr><td>1</td><td>https://example.test/a?X-Amz-Signature=secret</td></tr></tbody></table>
    """
    redacted = call_preview(ctx)
    assert redacted["rows"] == [{"url": "[redacted-url]"}]
    ctx.admin.html = '<div class="card-header"><h6>Sample Rows</h6></div><table><tr><th>#</th><th>a</th><th>a</th></tr></table>'
    bad = call_preview(ctx)
    assert bad["status"] == "unknown"


def test_worker_preview_empty_sample_rows_is_supported_not_fake_total(ctx):
    ctx.admin.html = sample_html(rows=False)
    result = call_preview(ctx)
    assert result["status"] == "supported"
    assert result["columns"] == []
    assert result["rows"] == []
    assert result["sample_truncated"] is None
    assert result["total_rows"] is None


def test_worker_preview_info_alert_is_note_not_dependency(ctx):
    ctx.admin.html = """
    <html><body>
    <div class="alert alert-info py-2 mb-2">Showing 2 of 5 rows.</div>
    <div class="card-header"><h6>Sample Rows</h6></div>
    <table><thead><tr><th>#</th><th>id</th></tr></thead>
    <tbody><tr><td>1</td><td>a</td></tr><tr><td>2</td><td>b</td></tr></tbody></table>
    </body></html>
    """
    result = call_preview(ctx)
    assert result["status"] == "supported"
    assert result["notes"] == ["Showing 2 of 5 rows."]
    assert result["sample_truncated"] is True
    assert result["rows"] == [{"id": "a"}, {"id": "b"}]


def test_worker_preview_clips_rows_to_requested_limit(ctx):
    ctx.admin.html = """
    <div class="card-header"><h6>Sample Rows</h6></div>
    <table><thead><tr><th>#</th><th>id</th></tr></thead>
    <tbody>
      <tr><td>1</td><td>a</td></tr>
      <tr><td>2</td><td>b</td></tr>
      <tr><td>3</td><td>c</td></tr>
    </tbody></table>
    """
    result = call_preview(ctx, limit=2)
    assert result["rows"] == [{"id": "a"}, {"id": "b"}]
    assert result["sample_truncated"] is True
    assert result["notes"] == ["Console clipped worker preview to the requested limit of 2 rows."]


def test_worker_preview_requires_official_empty_marker_and_card_header(ctx):
    ctx.admin.html = "<p>Sample Rows</p>"
    result = call_preview(ctx)
    assert result["status"] == "unknown"
    ctx.admin.html = '<div class="card-header"><h6>Sample Rows</h6></div><div class="card-body"></div>'
    result = call_preview(ctx)
    assert result["status"] == "unknown"


def test_worker_preview_alert_becomes_dependency_unavailable_and_redacted(ctx):
    ctx.admin.html = '<div class="alert alert-warning">Could not read LANCE: https://host/path?X-Amz-Signature=secret</div>' + sample_html()
    result = call_preview(ctx)
    assert result["status"] == "dependency_unavailable"
    assert result["error_code"] == "TABLE_WORKER_PREVIEW_ALERT"
    assert "[redacted-url]" in result["message"]
    assert "secret" not in result["message"].lower()
    assert result["rows"] is None


def test_worker_preview_rechecks_scope_before_returning_alert(ctx, monkeypatch):
    class RevokingAdmin(HtmlAdmin):
        def get_html(self, path, params=None):
            with db.connect(ctx.settings) as conn:
                conn.execute("UPDATE scopes SET allow_original_download=0 WHERE id=?", (ctx.scope["id"],))
            return super().get_html(path, params=params)

    admin = RevokingAdmin('<div class="alert alert-warning">private table worker detail</div>' + sample_html())
    monkeypatch.setattr(management, "admin_client", lambda _settings, _conn, _mgmt: admin)
    with pytest.raises(AppError) as revoked:
        call_preview(ctx)
    assert revoked.value.status == 403
    assert revoked.value.code == "FORBIDDEN_SCOPE"
    assert "private table worker detail" not in str(revoked.value)


def test_worker_preview_rejects_ambiguous_or_huge_html(ctx):
    ctx.admin.html = sample_html() + "<table><tr><td>second</td></tr></table>"
    multiple = call_preview(ctx)
    assert multiple["status"] == "unknown"
    assert multiple["error_code"] == "TABLE_WORKER_PREVIEW_UNKNOWN"
    ctx.admin.html = b"x" * (table_worker_preview.MAX_HTML_BYTES + 1)
    with pytest.raises(AppError) as huge:
        call_preview(ctx)
    assert huge.value.code == "TABLE_WORKER_PREVIEW_LIMIT"


def test_worker_preview_passes_stream_bounds_to_request_client(ctx, monkeypatch):
    admin = RequestAdmin(sample_html())
    monkeypatch.setattr(management, "admin_client", lambda _settings, _conn, _mgmt: admin)
    result = call_preview(ctx)
    assert result["status"] == "supported"
    assert admin.private_kwargs["_max_body_bytes"] == table_worker_preview.MAX_HTML_BYTES
    assert admin.private_kwargs["_deadline_monotonic"] > 0
    assert 0 < admin.private_kwargs["timeout"] <= table_worker_preview.MAX_SECONDS
    assert admin.private_kwargs["headers"]["Accept"] == "text/html"


def test_worker_preview_real_admin_client_wire_uses_login_cookie_and_html_accept(tmp_path, monkeypatch):
    secret_file = tmp_path / "secrets.json"
    secret_file.write_text(json.dumps({"admin": {"kind": "seaweed_admin", "username": "u", "password": "p",
                                                "allowed_endpoint_url": "http://admin.local"}}))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "db", admin_password="pw", secrets_file=secret_file)
    db.initialize(settings)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query)
        seen.append((request.method, request.url.path, query, request.headers.get("accept"), request.headers.get("cookie")))
        if request.method == "GET" and request.url.path == "/login":
            return httpx.Response(200, text='<input type="hidden" name="csrf_token" value="csrf">', headers={"content-type": "text/html"})
        if request.method == "POST" and request.url.path == "/login":
            return httpx.Response(302, headers={"location": "/admin", "set-cookie": "swc=ok; Path=/"})
        if request.method == "GET" and request.url.path == "/admin":
            return httpx.Response(200, text='<meta name="csrf-token" content="csrf2">', headers={"content-type": "text/html"})
        if request.method == "GET" and request.url.path.endswith("/data"):
            return httpx.Response(200, text=sample_html(), headers={"content-type": "text/html"})
        return httpx.Response(404, text="missing")

    monkeypatch.setattr(management, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    html = table_worker_preview._fetch_worker_html(settings, {"admin_url": "http://admin.local", "admin_secret_ref": "admin"},
                                                   "bucket", "ns", "events", 2, time.monotonic() + 20)
    assert "Sample Rows" in html
    data_call = [item for item in seen if item[1].endswith("/data")][0]
    assert data_call[2] == "limit=2"
    assert data_call[3] == "text/html"
    assert data_call[4] == "swc=ok"


def test_worker_preview_bounds_scope_and_catalog_identity(ctx, monkeypatch):
    ctx.catalog["warehouseLocation"] = "s3://bucket/private/"
    with pytest.raises(AppError) as outside:
        call_preview(ctx)
    assert outside.value.status == 403
    ctx.catalog["warehouseLocation"] = "s3://bucket/table/"
    monkeypatch.setattr(management_native, "fetch_table_details",
                        lambda *args: {"status": "supported", "details": {"catalog": {**ctx.catalog, "versionToken": "v2"}}})
    with pytest.raises(AppError) as changed:
        call_preview(ctx)
    assert changed.value.code == "TABLE_CATALOG_CHANGED"


def test_worker_preview_rechecks_scope_epoch(ctx):
    with db.connect(ctx.settings) as conn:
        conn.execute("UPDATE scopes SET allow_original_download=0 WHERE id=?", (ctx.scope["id"],))
    with pytest.raises(AppError) as revoked:
        call_preview(ctx)
    assert revoked.value.status == 403
