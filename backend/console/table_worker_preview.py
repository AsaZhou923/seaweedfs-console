from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import quote

from . import core, db, management, management_native, table_preview
from .security import AppError, utc_now

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 20
MAX_ROWS = 200
MAX_COLUMNS = 100
MAX_CELL_CHARS = 2000


def _catalog_identity(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: catalog.get(field)
        for field in ("versionToken", "metadataLocation", "metadataVersion", "warehouseLocation", "format")
    }


def _bucket_from_arn(bucket_arn: str) -> str:
    marker = ":bucket/"
    if marker in bucket_arn:
        return bucket_arn.rsplit(marker, 1)[-1].split("/", 1)[0]
    if "/bucket/" in bucket_arn:
        return bucket_arn.rsplit("/bucket/", 1)[-1].split("/", 1)[0]
    if bucket_arn.startswith("arn:"):
        raise AppError("TABLE_BUCKET_ARN_UNSUPPORTED", "cannot verify the bucket name from this Table Bucket ARN", 422)
    return bucket_arn.split("/", 1)[0]


def _namespace_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return ".".join(value)
    return None


def _sanitize_text(value: str, limit: int = 300) -> str:
    value = re.sub(
        r"https?://\S*(?i:signature|credential|token|secret|password)=\S*",
        "[redacted-url]",
        value or "",
    )
    value = re.sub(r"(?i)(signature|credential|token|secret|password)=([^\s&]+)", r"\1=[redacted]", value)
    return " ".join(value.split())[:limit]


def _cell_text(value: str) -> str:
    value = re.sub(
        r"https?://\S*(?i:signature|credential|token|secret|password)=\S*",
        "[redacted-url]",
        value or "",
    )
    value = re.sub(r"(?i)(signature|credential|token|secret|password)=([^\s&]+)", r"\1=[redacted]", value)
    return value.strip()[:MAX_CELL_CHARS]


class _SampleRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sample_seen = False
        self.tables_seen_after_sample = 0
        self.in_table = False
        self.in_cell: str | None = None
        self.current_cell: list[str] = []
        self.current_row: list[str] = []
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self.alert_depth = 0
        self.alert_kind: str | None = None
        self.alert_parts: list[str] = []
        self.error_alert = ""
        self.info_notes: list[str] = []
        self.card_header_depth = 0
        self.no_rows_marker = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key: value or "" for key, value in attrs}
        classes = attrs_map.get("class", "")
        if tag == "div" and "alert" in classes.split():
            self.alert_depth += 1
            if "alert-warning" in classes.split() or "alert-danger" in classes.split():
                self.alert_kind = "error"
            elif "alert-info" in classes.split():
                self.alert_kind = "info"
            else:
                self.alert_kind = "other"
        if tag == "div" and "card-header" in classes.split():
            self.card_header_depth += 1
        if self.sample_seen and tag == "table":
            self.tables_seen_after_sample += 1
            if self.tables_seen_after_sample == 1:
                self.in_table = True
        if self.in_table and tag == "tr":
            self.current_row = []
        if self.in_table and tag in {"th", "td"}:
            self.in_cell = tag
            self.current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell == tag:
            self.current_row.append(_cell_text("".join(self.current_cell)))
            self.current_cell = []
            self.in_cell = None
        if self.in_table and tag == "tr" and self.current_row:
            if not self.headers and self.current_row[0] == "#":
                self.headers = self.current_row[1:]
            else:
                self.rows.append(self.current_row[1:] if self.current_row and self.current_row[0].isdigit() else list(self.current_row))
            self.current_row = []
        if self.in_table and tag == "table":
            self.in_table = False
        if tag == "div" and self.alert_depth:
            if self.alert_depth == 1:
                text = _sanitize_text(" ".join(self.alert_parts))
                if text:
                    if self.alert_kind == "info":
                        self.info_notes.append(text)
                    elif self.alert_kind == "error":
                        self.error_alert = text
                self.alert_parts = []
                self.alert_kind = None
            self.alert_depth -= 1
        if tag == "div" and self.card_header_depth:
            self.card_header_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.card_header_depth and "Sample Rows" in data:
            self.sample_seen = True
        if self.sample_seen and "No rows to preview." in data:
            self.no_rows_marker = True
        if self.alert_depth:
            self.alert_parts.append(data)
        if self.in_cell:
            self.current_cell.append(data)

    @property
    def alert(self) -> str:
        return self.error_alert


def _parse_sample_rows(html: str) -> tuple[list[str], list[list[str]], str | None, list[str]]:
    parser = _SampleRowsParser()
    parser.feed(html)
    if parser.alert:
        return [], [], parser.alert, parser.info_notes
    if not parser.sample_seen:
        raise AppError("TABLE_WORKER_PREVIEW_UNKNOWN", "Admin worker preview page did not include the Sample Rows marker", 502)
    if parser.tables_seen_after_sample == 0:
        if parser.no_rows_marker:
            return [], [], None, parser.info_notes
        raise AppError("TABLE_WORKER_PREVIEW_UNKNOWN", "Admin worker preview page did not include a Sample Rows table or the official empty marker", 502)
    if parser.tables_seen_after_sample > 1:
        raise AppError("TABLE_WORKER_PREVIEW_UNKNOWN", "Admin worker preview page contained multiple Sample Rows tables", 502)
    if not parser.headers or any(not header for header in parser.headers) or len(set(parser.headers)) != len(parser.headers):
        raise AppError("TABLE_WORKER_PREVIEW_UNKNOWN", "Admin worker preview table headers were empty or duplicated", 502)
    if len(parser.headers) > MAX_COLUMNS:
        raise AppError("TABLE_WORKER_PREVIEW_LIMIT", "Worker preview returned too many columns", 413)
    if len(parser.rows) > MAX_ROWS:
        raise AppError("TABLE_WORKER_PREVIEW_LIMIT", "Worker preview returned too many rows", 413)
    for row in parser.rows:
        if len(row) != len(parser.headers):
            raise AppError("TABLE_WORKER_PREVIEW_UNKNOWN", "Worker preview row shape did not match the header", 502)
    return parser.headers, parser.rows, None, parser.info_notes


def _sample_truncated(notes: list[str]) -> bool | None:
    for note in notes:
        match = re.search(r"Showing\s+(\d+)\s+of\s+(\d+)\s+rows", note, flags=re.IGNORECASE)
        if match:
            return int(match.group(1)) < int(match.group(2))
    return None


def _clip_rows(rows: list[list[str]], limit: int, notes: list[str]) -> tuple[list[list[str]], list[str], bool | None]:
    if len(rows) <= limit:
        return rows, notes, _sample_truncated(notes)
    clipped_notes = list(notes)
    clipped_notes.append(f"Console clipped worker preview to the requested limit of {limit} rows.")
    return rows[:limit], clipped_notes, True


def _recheck_scope(settings: Any, scope: Mapping[str, Any]) -> None:
    with db.connect(settings) as conn:
        current = core.ensure_scope_permission(conn, scope["id"], "object:download_original")
        if current["authz_epoch"] != scope["authz_epoch"]:
            raise AppError("TABLE_SCOPE_CHANGED", "scope authorization changed", 403)


def _fetch_worker_html(settings: Any, mgmt: Mapping[str, Any], bucket: str, namespace: str, name: str, limit: int, deadline: float) -> str:
    path = "/object-store/s3tables/buckets/{}/namespaces/{}/tables/{}/data".format(
        quote(bucket, safe=""), quote(namespace, safe=""), quote(name, safe="")
    )
    with db.connect(settings) as conn:
        client = management.admin_client(settings, conn, dict(mgmt))
    try:
        setattr(client, "_deadline_monotonic", deadline)
        if getattr(client, "_csrf", None) is None and hasattr(client, "_login"):
            client._login()
        remaining = max(0.1, min(MAX_SECONDS, deadline - time.monotonic()))
        request_kwargs = {"headers": {"Accept": "text/html"}, "params": {"limit": limit},
                          "_max_body_bytes": MAX_HTML_BYTES, "_deadline_monotonic": deadline, "timeout": remaining}
        if hasattr(client, "_request"):
            response = client._request("GET", path, **request_kwargs)
        elif hasattr(client, "get_html"):
            response = client.get_html(path, params={"limit": limit})
        else:
            raise AppError("TABLE_WORKER_PREVIEW_DEPENDENCY_UNAVAILABLE", "Admin client cannot fetch worker preview HTML", 501)
        if 300 <= getattr(response, "status_code", 200) < 400:
            raise AppError("TABLE_WORKER_PREVIEW_DEPENDENCY_UNAVAILABLE", "Admin worker preview redirected instead of returning HTML", 502)
        content = getattr(response, "content", None)
        if content is None:
            text = getattr(response, "text", "")
            content = text.encode("utf-8")
        if len(content) > MAX_HTML_BYTES:
            raise AppError("TABLE_WORKER_PREVIEW_LIMIT", "Worker preview HTML exceeded the size limit", 413)
        content_type = getattr(response, "headers", {}).get("content-type", "")
        if content_type and "html" not in content_type.lower():
            raise AppError("TABLE_WORKER_PREVIEW_UNKNOWN", "Admin worker preview did not return HTML", 502)
        return content.decode("utf-8", errors="replace")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _validate_catalog_scope(
    scope: Mapping[str, Any],
    catalog: Mapping[str, Any],
    bucket_arn: str,
    namespace: str,
    name: str,
) -> tuple[str, str]:
    bucket = _bucket_from_arn(bucket_arn)
    if bucket != scope.get("bucket"):
        raise AppError("TABLE_BUCKET_SCOPE_MISMATCH", "Table Bucket ARN does not match the authorized scope bucket", 403)
    namespace_parts = management_native._s3_tables_namespace_parts(namespace)
    table_name = management_native._segment(name.strip(), "name")
    if table_name != name:
        raise AppError("INVALID_REQUEST", "table name contains unsupported whitespace.", 422, field="name")
    catalog_name = catalog.get("name")
    if isinstance(catalog_name, str) and catalog_name != table_name:
        raise AppError("TABLE_CATALOG_MISMATCH", "table catalog name does not match the requested table", 409)
    catalog_namespace = _namespace_text(catalog.get("namespace"))
    if catalog_namespace is not None and catalog_namespace != namespace:
        raise AppError("TABLE_CATALOG_MISMATCH", "table catalog namespace does not match the requested namespace", 409)
    catalog_bucket_arn = catalog.get("bucket_arn")
    if isinstance(catalog_bucket_arn, str) and catalog_bucket_arn != bucket_arn:
        raise AppError("TABLE_BUCKET_SCOPE_MISMATCH", "table catalog bucket does not match the requested Table Bucket ARN", 403)
    table_arn = catalog.get("tableARN")
    if isinstance(table_arn, str) and table_arn and not table_arn.startswith(bucket_arn.rstrip("/") + "/table/"):
        raise AppError("TABLE_BUCKET_SCOPE_MISMATCH", "table ARN does not belong to the requested Table Bucket", 403)
    warehouse = catalog.get("warehouseLocation")
    metadata = catalog.get("metadataLocation")
    authorization_source = "official_lance_default_location"
    if isinstance(warehouse, str) and warehouse:
        location = warehouse
        authorization_source = "catalog_warehouse_location"
    elif isinstance(metadata, str) and metadata:
        location = metadata
        authorization_source = "catalog_metadata_location"
    else:
        location = f"s3://{bucket}/{'.'.join(namespace_parts)}/{table_name}/"
    root_location = location if location.endswith("/") else location + "/"
    root_key = table_preview.location_key(root_location, dict(scope))
    core.assert_key_in_scope(dict(scope), root_key)
    if isinstance(metadata, str) and metadata:
        table_preview.location_key(metadata, dict(scope))
    return bucket, authorization_source


def _dependency_response(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "dependency_unavailable",
        "error_code": code,
        "message": _sanitize_text(message),
        "preview_kind": "worker_sample",
        "worker_source": "official_admin_worker_preview_html",
        "columns": None,
        "rows": None,
        "total_rows": None,
        "deletes_applied": None,
        "notes": [],
        "checked_at": utc_now(),
    }


def _unknown_response(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "error_code": code,
        "message": _sanitize_text(message),
        "preview_kind": "worker_sample",
        "worker_source": "official_admin_worker_preview_html",
        "columns": None,
        "rows": None,
        "total_rows": None,
        "deletes_applied": None,
        "notes": [],
        "checked_at": utc_now(),
    }


def preview(
    request: Any,
    mgmt: Mapping[str, Any],
    scope: Mapping[str, Any],
    catalog: Mapping[str, Any],
    bucket_arn: str,
    namespace: str,
    name: str,
    limit: int,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + MAX_SECONDS
    if str(catalog.get("format", "")).upper() != "LANCE":
        raise AppError("TABLE_WORKER_PREVIEW_UNSUPPORTED", "Worker sample preview is only enabled for verified LANCE tables", 501)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise AppError("INVALID_REQUEST", "Worker sample preview limit must be 1-200", 422)
    bucket, authorization_source = _validate_catalog_scope(scope, catalog, bucket_arn, namespace, name)
    settings = request.app.state.settings
    _recheck_scope(settings, scope)
    try:
        html = _fetch_worker_html(settings, mgmt, bucket, namespace, name, limit, deadline)
        columns_raw, rows_raw, alert, notes = _parse_sample_rows(html)
    except AppError as exc:
        if exc.code in {"ADMIN_ENDPOINT_NOT_FOUND", "ADMIN_ENDPOINT_UNSUPPORTED", "TABLE_WORKER_PREVIEW_DEPENDENCY_UNAVAILABLE"}:
            _recheck_scope(settings, scope)
            return _dependency_response(exc.code, str(exc))
        if exc.code == "TABLE_WORKER_PREVIEW_UNKNOWN":
            _recheck_scope(settings, scope)
            return _unknown_response(exc.code, str(exc))
        raise
    if time.monotonic() - started > MAX_SECONDS:
        raise AppError("TABLE_WORKER_PREVIEW_TIMEOUT", "Worker sample preview exceeded the time budget", 413)
    if alert:
        _recheck_scope(settings, scope)
        return _dependency_response("TABLE_WORKER_PREVIEW_ALERT", alert)
    latest = management_native.fetch_table_details(request, str(mgmt.get("id") or ""), bucket_arn, namespace, name)
    if latest.get("status") != "supported" or _catalog_identity(latest.get("details", {}).get("catalog", {})) != _catalog_identity(catalog):
        raise AppError("TABLE_CATALOG_CHANGED", "table catalog changed while reading worker preview", 409)
    _recheck_scope(settings, scope)
    rows_raw, notes, sample_truncated = _clip_rows(rows_raw, limit, notes)
    columns = [{"name": column, "type": "unknown", "nullable": None} for column in columns_raw]
    rows = [dict(zip(columns_raw, row)) for row in rows_raw]
    return {
        "status": "supported",
        "source": "scope_authorized_lance_worker_preview",
        "preview_kind": "worker_sample",
        "worker_source": "official_admin_worker_preview_html",
        "scope_id": scope.get("id"),
        "bucket_arn": bucket_arn,
        "namespace": namespace,
        "name": name,
        "authorization_source": authorization_source,
        "columns": columns,
        "rows": rows,
        "sample_truncated": sample_truncated,
        "notes": notes,
        "total_rows": None,
        "deletes_applied": None,
        "checked_at": utc_now(),
    }
