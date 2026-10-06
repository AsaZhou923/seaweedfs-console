from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_browser_regression_runner_exits_promptly_when_browser_exits(tmp_path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the frontend browser regression runner")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><main>fixture</main>", encoding="utf-8")
    env = os.environ.copy()
    env["CHROME_PATH"] = sys.executable
    env["SWC_REGRESSION_DIST"] = str(dist)
    result = subprocess.run(
        [node, str(ROOT / "scripts" / "verify_frontend_review_regressions.cjs")],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode != 0
    assert "Browser exited before exposing DevTools endpoint" in result.stderr
    assert "unknown option" in result.stderr.lower() or "usage:" in result.stderr.lower()
