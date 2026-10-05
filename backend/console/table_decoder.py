"""Bounded, isolated Avro and Parquet decoding for table previews."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Literal

Kind = Literal["avro", "parquet", "json"]

MAX_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ROWS = 100
MAX_AVRO_RECORDS = 10_000
MAX_PARQUET_COLUMNS = 100
MAX_NESTED_ITEMS = 20
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 10_000
MAX_SAFE_JSON_INTEGER = 2**53 - 1


def decode(
    data: bytes,
    kind: Kind,
    *,
    limit: int = MAX_ROWS,
    max_bytes: int = MAX_BYTES,
    timeout: float = 15.0,
) -> dict[str, Any]:
    if kind not in {"avro", "parquet", "json"}:
        return {"status": "unsupported", "reason": "format", "truncated": False}
    if kind == "json" and len(data) > MAX_JSON_BYTES:
        return {"status": "resource_limited", "reason": "file_bytes", "truncated": False}
    if len(data) > max_bytes:
        return {"status": "resource_limited", "reason": "file_bytes", "truncated": False}
    row_limit = _row_limit(limit)
    with tempfile.TemporaryDirectory(prefix="swc-table-") as directory:
        root = Path(directory)
        source, result = root / "input", root / "result.json"
        source.write_bytes(data)
        options = json.dumps({"kind": kind, "limit": row_limit})
        command = [sys.executable, str(Path(__file__).resolve()), str(source), str(result), options]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            return {"status": "resource_limited", "reason": "decode_timeout", "truncated": False}
        if completed.returncode != 0 or not result.exists():
            return {"status": "resource_limited", "reason": "decoder_process", "truncated": False}
        try:
            return json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "resource_limited", "reason": "decoder_process", "truncated": False}


def _row_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return MAX_ROWS
    return max(1, min(parsed, MAX_ROWS))


def _apply_child_limits() -> Any:
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
        return None
    if os.name == "nt":
        backend_root = Path(__file__).resolve().parents[1]
        if str(backend_root) not in sys.path:
            sys.path.insert(0, str(backend_root))
        from console.imaging import _windows_memory_limit

        return _windows_memory_limit()
    return None


def _child(source: str, result: str, options: dict[str, Any]) -> None:
    _memory_job_handle = _apply_child_limits()
    try:
        data = Path(source).read_bytes()
        kind = options.get("kind")
        limit = _row_limit(options.get("limit", MAX_ROWS))
        if kind == "avro":
            payload = _decode_avro(data, limit)
        elif kind == "parquet":
            payload = _decode_parquet(data, limit)
        elif kind == "json":
            payload = _decode_json(data)
        else:
            payload = {"status": "unsupported", "reason": "format", "truncated": False}
    except MemoryError:
        payload = {"status": "resource_limited", "reason": "memory", "truncated": False}
    except (OSError, ValueError, EOFError, json.JSONDecodeError):
        payload = {"status": "corrupt", "reason": "decode_failed", "truncated": False}
    except Exception as exc:
        if "Memory" in type(exc).__name__ or "Capacity" in type(exc).__name__:
            payload = {"status": "resource_limited", "reason": "memory", "truncated": False}
        else:
            payload = {"status": "corrupt", "reason": "decode_failed", "truncated": False}
    _write_result(Path(result), payload)


def _decode_avro(data: bytes, limit: int) -> dict[str, Any]:
    import io

    from fastavro import reader

    rows: list[dict[str, Any]] = []
    scanned = 0
    with io.BytesIO(data) as buffer:
        avro_reader = reader(buffer)
        schema = _sanitize_avro_schema(avro_reader.writer_schema)
        for record in avro_reader:
            scanned += 1
            if scanned > MAX_AVRO_RECORDS:
                return {
                    "status": "resource_limited",
                    "reason": "record_count",
                    "records": rows,
                    "schema": schema,
                    "truncated": True,
                }
            if len(rows) < limit:
                rows.append(_json_value(record))
    return {
        "status": "decoded",
        "records": rows,
        "schema": schema,
        "record_count": scanned,
        "truncated": scanned > len(rows),
    }


def _decode_parquet(data: bytes, limit: int) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    source = pa.BufferReader(data)
    parquet_file = pq.ParquetFile(
        source,
        pre_buffer=False,
        thrift_string_size_limit=1024 * 1024,
        thrift_container_size_limit=100_000,
        filesystem=None,
        arrow_extensions_enabled=False,
    )
    arrow_schema = parquet_file.schema_arrow
    columns = [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in arrow_schema
    ]
    if len(columns) > MAX_PARQUET_COLUMNS:
        return {"status": "resource_limited", "reason": "column_count", "columns": columns[:MAX_PARQUET_COLUMNS], "truncated": False}
    rows: list[dict[str, Any]] = []
    for batch in parquet_file.iter_batches(batch_size=limit, use_threads=False):
        for row in batch.to_pylist():
            rows.append(_json_value(row))
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    row_count = parquet_file.metadata.num_rows if parquet_file.metadata is not None else len(rows)
    return {
        "status": "decoded",
        "rows": rows,
        "columns": columns,
        "row_count": row_count,
        "truncated": row_count > len(rows),
    }


def _decode_json(data: bytes) -> dict[str, Any]:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        return {"status": "corrupt", "reason": "root_type", "truncated": False}
    node_count = _json_node_count(payload)
    if node_count > MAX_JSON_NODES:
        return {"status": "resource_limited", "reason": "structure_nodes", "truncated": False}
    if _json_depth(payload) > MAX_JSON_DEPTH:
        return {"status": "resource_limited", "reason": "structure_depth", "truncated": False}
    return {"status": "decoded", "payload": payload, "truncated": False}


def _json_node_count(value: Any) -> int:
    stack = [value]
    count = 0
    while stack:
        count += 1
        if count > MAX_JSON_NODES:
            return count
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return count


def _json_depth(value: Any) -> int:
    stack = [(value, 1)]
    max_depth = 0
    while stack:
        current, depth = stack.pop()
        max_depth = max(max_depth, depth)
        if max_depth > MAX_JSON_DEPTH:
            return max_depth
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return max_depth


def _sanitize_avro_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        allowed = {"type", "name", "namespace", "fields", "items", "values", "symbols", "doc", "logicalType"}
        return {str(key): _sanitize_avro_schema(value) for key, value in schema.items() if key in allowed}
    if isinstance(schema, list):
        return [_sanitize_avro_schema(item) for item in schema[:MAX_NESTED_ITEMS]]
    if isinstance(schema, (str, int, float, bool)) or schema is None:
        return schema
    return str(schema)[:200]


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return {"truncated": True}
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        encoded = base64.b64encode(value[:64]).decode("ascii")
        return {"type": "bytes", "base64": encoded, "length": len(value), "truncated": len(value) > 64}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dict):
        items = list(value.items())
        result = {str(key): _json_value(item, depth=depth + 1) for key, item in items[:MAX_NESTED_ITEMS]}
        if len(items) > MAX_NESTED_ITEMS:
            result["__truncated__"] = True
        return result
    if isinstance(value, (list, tuple)):
        items = [_json_value(item, depth=depth + 1) for item in value[:MAX_NESTED_ITEMS]]
        if len(value) > MAX_NESTED_ITEMS:
            items.append({"truncated": True, "remaining": len(value) - MAX_NESTED_ITEMS})
        return items
    return str(value)[:200]


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        payload = {"status": "corrupt", "reason": "decode_failed", "truncated": False}
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        payload = {"status": "resource_limited", "reason": "result_bytes", "truncated": True}
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    path.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
