from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.environ["PYTHONPATH"] = str(BACKEND)

from console import table_decoder  # noqa: E402


def _avro_bytes(*, codec: str = "null", records: list[dict] | None = None) -> bytes:
    from fastavro import parse_schema, writer

    schema = {
        "type": "record",
        "name": "ManifestRecord",
        "fields": [
            {"name": "content", "type": "int"},
            {"name": "status", "type": "int"},
            {
                "name": "data_file",
                "type": {
                    "type": "record",
                    "name": "DataFile",
                    "fields": [
                        {"name": "file_path", "type": "string"},
                        {"name": "column_sizes", "type": {"type": "map", "values": "bytes"}},
                    ],
                },
            },
        ],
    }
    payload = records or [
        {
            "content": 0,
            "status": 1,
            "data_file": {
                "file_path": "s3://bucket/table/data-1.parquet",
                "column_sizes": {"1": b"\x00\x01\x02"},
            },
        },
        {
            "content": 1,
            "status": 2,
            "data_file": {
                "file_path": "s3://bucket/table/delete-1.parquet",
                "column_sizes": {"2": b"\x03\x04"},
            },
        },
    ]
    buffer = io.BytesIO()
    writer(buffer, parse_schema(schema), payload, codec=codec)
    return buffer.getvalue()


def _avro_long_bytes(value: int) -> bytes:
    from fastavro import parse_schema, writer

    schema = {
        "type": "record",
        "name": "LongRecord",
        "fields": [
            {"name": "big", "type": "long"},
            {"name": "flag", "type": "boolean"},
        ],
    }
    buffer = io.BytesIO()
    writer(buffer, parse_schema(schema), [{"big": value, "flag": True}], codec="null")
    return buffer.getvalue()


def _parquet_bytes(rows: int = 2, columns: int = 2) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    data = {f"col_{index}": list(range(rows)) for index in range(columns)}
    table = pa.table(data)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="NONE")
    return buffer.getvalue()


def _parquet_int64_extremes_bytes() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "big": pa.array([-(2**63), 2**63 - 1], type=pa.int64()),
            "flag": pa.array([True, False], type=pa.bool_()),
        }
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="NONE")
    return buffer.getvalue()


def test_decode_avro_manifest_records_with_bytes_as_json() -> None:
    decoded = table_decoder.decode(_avro_bytes(), "avro", limit=1)

    assert decoded["status"] == "decoded"
    assert decoded["record_count"] == 2
    assert decoded["truncated"] is True
    assert len(decoded["records"]) == 1
    data_file = decoded["records"][0]["data_file"]
    assert data_file["file_path"].endswith("data-1.parquet")
    assert data_file["column_sizes"]["1"] == {"type": "bytes", "base64": "AAEC", "length": 3, "truncated": False}
    assert decoded["schema"]["name"] == "ManifestRecord"


def test_decode_deflate_avro_roundtrip() -> None:
    decoded = table_decoder.decode(_avro_bytes(codec="deflate"), "avro")

    assert decoded["status"] == "decoded"
    assert [record["content"] for record in decoded["records"]] == [0, 1]


def test_decode_avro_stops_at_manifest_record_limit() -> None:
    records = [
        {
            "content": 0,
            "status": 1,
            "data_file": {"file_path": f"s3://bucket/table/data-{index}.parquet", "column_sizes": {}},
        }
        for index in range(table_decoder.MAX_AVRO_RECORDS + 1)
    ]

    decoded = table_decoder.decode(_avro_bytes(records=records), "avro")

    assert decoded["status"] == "resource_limited"
    assert decoded["reason"] == "record_count"
    assert decoded["truncated"] is True
    assert len(decoded["records"]) == table_decoder.MAX_ROWS


def test_decode_avro_long_over_javascript_safe_range_as_string() -> None:
    decoded = table_decoder.decode(_avro_long_bytes(2**63 - 1), "avro")

    assert decoded["status"] == "decoded"
    assert decoded["records"] == [{"big": "9223372036854775807", "flag": True}]


def test_decode_parquet_rows_and_columns_without_snappy() -> None:
    decoded = table_decoder.decode(_parquet_bytes(rows=3), "parquet", limit=2)

    assert decoded["status"] == "decoded"
    assert decoded["row_count"] == 3
    assert decoded["truncated"] is True
    assert decoded["columns"] == [
        {"name": "col_0", "type": "int64", "nullable": True},
        {"name": "col_1", "type": "int64", "nullable": True},
    ]
    assert decoded["rows"] == [{"col_0": 0, "col_1": 0}, {"col_0": 1, "col_1": 1}]


def test_decode_parquet_int64_extremes_as_strings() -> None:
    decoded = table_decoder.decode(_parquet_int64_extremes_bytes(), "parquet")

    assert decoded["status"] == "decoded"
    assert decoded["rows"] == [
        {"big": "-9223372036854775808", "flag": True},
        {"big": "9223372036854775807", "flag": False},
    ]


def test_decode_rejects_too_many_parquet_columns() -> None:
    decoded = table_decoder.decode(_parquet_bytes(columns=101), "parquet")

    assert decoded["status"] == "resource_limited"
    assert decoded["reason"] == "column_count"
    assert len(decoded["columns"]) == 100


def test_decode_rejects_payloads_that_exceed_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_spawned(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("decoder subprocess should not be spawned for oversized payloads")

    monkeypatch.setattr(table_decoder.subprocess, "run", fail_if_spawned)

    decoded = table_decoder.decode(b"too large", "parquet", max_bytes=4)

    assert decoded == {"status": "resource_limited", "reason": "file_bytes", "truncated": False}


def test_decode_json_dict_roundtrip_in_subprocess() -> None:
    source = {"snapshot-id": 17, "manifests": [{"manifest_path": "s3://bucket/table/manifest.avro"}]}

    decoded = table_decoder.decode(json.dumps(source).encode("utf-8"), "json")

    assert decoded == {"status": "decoded", "payload": source, "truncated": False}


def test_decode_json_rejects_malformed_payload() -> None:
    decoded = table_decoder.decode(b"{", "json")

    assert decoded == {"status": "corrupt", "reason": "decode_failed", "truncated": False}


def test_decode_json_rejects_non_dict_root() -> None:
    decoded = table_decoder.decode(b"[1, 2, 3]", "json")

    assert decoded == {"status": "corrupt", "reason": "root_type", "truncated": False}


def test_decode_json_rejects_deep_nested_payload() -> None:
    source: dict = {"leaf": True}
    for _ in range(table_decoder.MAX_JSON_DEPTH + 1):
        source = {"nested": source}

    decoded = table_decoder.decode(json.dumps(source).encode("utf-8"), "json")

    assert decoded == {"status": "resource_limited", "reason": "structure_depth", "truncated": False}


def test_decode_json_rejects_too_many_nodes() -> None:
    source = {f"k{index}": index for index in range(table_decoder.MAX_JSON_NODES)}

    decoded = table_decoder.decode(json.dumps(source).encode("utf-8"), "json")

    assert decoded == {"status": "resource_limited", "reason": "structure_nodes", "truncated": False}


def test_decode_json_rejects_oversized_payload_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_spawned(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("decoder subprocess should not be spawned for oversized JSON")

    monkeypatch.setattr(table_decoder.subprocess, "run", fail_if_spawned)

    decoded = table_decoder.decode(b"{}" * (table_decoder.MAX_JSON_BYTES // 2 + 1), "json")

    assert decoded == {"status": "resource_limited", "reason": "file_bytes", "truncated": False}


def test_decode_reports_corrupt_without_raw_exception() -> None:
    decoded = table_decoder.decode(b"not avro", "avro")

    assert decoded == {"status": "corrupt", "reason": "decode_failed", "truncated": False}


def test_decode_reports_timeout_when_decoder_process_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=["decoder"], timeout=0.01)

    monkeypatch.setattr(table_decoder.subprocess, "run", raise_timeout)

    decoded = table_decoder.decode(_parquet_bytes(), "parquet", timeout=0.01)

    assert decoded == {"status": "resource_limited", "reason": "decode_timeout", "truncated": False}


def test_decode_subprocess_imports_console_module_when_cwd_is_not_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    decoded = table_decoder.decode(_parquet_bytes(), "parquet")

    assert decoded["status"] == "decoded"
    assert decoded["rows"][0] == {"col_0": 0, "col_1": 0}


def test_decode_unsupported_kind() -> None:
    decoded = table_decoder.decode(b"{}", "yaml")  # type: ignore[arg-type]

    assert decoded == {"status": "unsupported", "reason": "format", "truncated": False}
