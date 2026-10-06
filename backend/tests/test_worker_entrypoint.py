from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from console import core, db, enhancements, jobs
from console.config import Settings


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "console.db",
        admin_password="admin-pass",
        allowed_origins=["http://testserver"],
        cursor_signing_key="cursor-test-key",
        csrf_signing_key="csrf-test-key",
        dev_insecure_cookie=True,
        cookie_secure=False,
    )


def _seed_missing_object_variant_job(settings: Settings) -> str:
    db.initialize(settings)
    with db.connect(settings) as conn:
        jobs.initialize(conn)
        enhancements.initialize(conn)
        core.bootstrap_admin(conn, settings)
        stamp = jobs.now()
        conn.execute(
            "INSERT INTO storage_connections(id,display_name,endpoint_url,region,secret_ref,addressing_style,verify_tls,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("con", "fixture", "http://127.0.0.1:8333", "us-east-1", "ref", "path", 0, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO projects(id,project_key,display_name,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("project", "worker-entrypoint", "Worker entrypoint", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO scopes(id,project_id,connection_id,display_name,bucket,prefix,writable,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("scope", "project", "con", "Source", "bucket", "", 1, stamp, stamp),
        )
        conn.execute(
            """
            INSERT INTO enhancement_presets
              (id,project_id,name,version,params_json,params_hash,created_at,created_by)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            ("preset", "project", "thumb", 1, "{}", "hash", stamp, "admin"),
        )
        conn.execute(
            """
            INSERT INTO derived_variants(
              id,project_id,scope_id,source_object_id,source_revision,preset_id,
              output_scope_id,output_bucket,output_key,input_binding_hash,status,
              manifest_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "variant",
                "project",
                "scope",
                "missing-object",
                "missing-revision",
                "preset",
                "scope",
                "bucket",
                "out.webp",
                "binding",
                "planned",
                "{}",
                stamp,
                stamp,
            ),
        )
        job = jobs.submit_job(
            conn,
            "scope",
            "derived_variant",
            {"variant_id": "variant"},
            actor="admin",
            effect_class="remote_mutating",
        )
        conn.commit()
    return job["id"]


def test_module_entrypoint_uses_registered_non_core_job_handlers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    job_id = _seed_missing_object_variant_job(settings)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(BACKEND),
            "CONSOLE_DATA_DIR": str(settings.data_dir),
            "CONSOLE_DATABASE_PATH": str(settings.database_path),
            "CONSOLE_ADMIN_PASSWORD": "admin-pass",
            "CONSOLE_ALLOWED_ORIGINS": "http://testserver",
            "CONSOLE_CSRF_SIGNING_KEY": "csrf-test-key",
            "CONSOLE_CURSOR_SIGNING_KEY": "cursor-test-key",
            "CONSOLE_DEV_INSECURE_COOKIE": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "console.jobs"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        row = None
        while time.monotonic() < deadline:
            with db.connect(settings) as conn:
                row = conn.execute(
                    "SELECT state,error_code,attempt_count FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            if row and row["state"] == "needs_review":
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(f"worker exited early: {process.returncode}\n{stdout}\n{stderr}")
            time.sleep(0.05)
        assert row is not None
        assert dict(row) == {"state": "needs_review", "error_code": "OBJECT_NOT_FOUND", "attempt_count": 1}
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
