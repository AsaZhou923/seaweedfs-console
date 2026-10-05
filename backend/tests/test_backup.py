from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.py"


def test_backup_restore_and_verify_preserve_table_counts(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent(id))")
        conn.executemany("INSERT INTO parent(id, name) VALUES(?, ?)", [(1, "one"), (2, "two")])
        conn.executemany("INSERT INTO child(id, parent_id) VALUES(?, ?)", [(10, 1), (20, 2)])

    backup_result = subprocess.run(
        [sys.executable, str(BACKUP), "backup", str(source), str(backup)],
        check=True,
        capture_output=True,
        text=True,
    )
    backup_payload = json.loads(backup_result.stdout)

    verify_result = subprocess.run(
        [sys.executable, str(BACKUP), "verify", str(backup)],
        check=True,
        capture_output=True,
        text=True,
    )
    verify_payload = json.loads(verify_result.stdout)

    restore_result = subprocess.run(
        [sys.executable, str(BACKUP), "restore", str(backup), str(restored)],
        check=True,
        capture_output=True,
        text=True,
    )
    restore_payload = json.loads(restore_result.stdout)
    manifest = json.loads((restored.with_suffix(restored.suffix + ".manifest.json")).read_text(encoding="utf-8"))

    assert backup_payload["integrity"] == "ok"
    assert verify_payload["tables"]["parent"] == 2
    assert verify_payload["tables"]["child"] == 2
    assert restore_payload["tables"] == verify_payload["tables"]
    assert manifest["sha256"] == restore_payload["sha256"]
    assert "S3 objects excluded" in manifest["scope"]


def test_restore_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE item(id INTEGER PRIMARY KEY)")
    destination.write_bytes(b"already here")

    result = subprocess.run(
        [sys.executable, str(BACKUP), "restore", str(source), str(destination)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Destination must be a new isolated path" in result.stderr
