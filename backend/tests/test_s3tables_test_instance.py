from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import s3tables_test_instance as helper


def owned_state(**overrides):
    state = {
        "name": "swc-test-catalog-1234abcd",
        "dir": "/tmp/swc-test-catalog-1234abcd-data",
        "ssh_host": "10.34.158.137",
        "ssh_pid": 4242,
        "ssh_tunnel_argv": ["ssh", "-N", "-L", "127.0.0.1:1000:127.0.0.1:2000", "10.34.158.137"],
        "ssh_tunnel_fingerprint": {
            "pid": 4242,
            "creation_date": "created-a",
            "command_line": "ssh -N -L 127.0.0.1:1000:127.0.0.1:2000 10.34.158.137",
        },
    }
    state.update(overrides)
    return state


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def test_validate_owned_state_rejects_prefix_injection():
    with pytest.raises(SystemExit):
        helper.validate_owned_state(owned_state(name="swc-test-catalog-1234abcd-extra", dir="/tmp/swc-test-catalog-1234abcd-extra-data"))


def test_validate_owned_state_rejects_unsafe_ssh_host():
    with pytest.raises(SystemExit):
        helper.validate_owned_state(owned_state(ssh_host="-oProxyCommand=sh"))


def test_cleanup_does_not_kill_reused_pid(tmp_path, monkeypatch, capsys):
    state = owned_state()
    state_file = tmp_path / "swc-test-catalog-1234abcd-state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    killed: list[list[str]] = []

    monkeypatch.setattr(helper, "process_fingerprint", lambda pid: {"pid": pid, "creation_date": "new-process", "command_line": "ssh -N -L 127.0.0.1:1000:127.0.0.1:2000 10.34.158.137"})
    monkeypatch.setattr(helper.os, "name", "nt")

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "taskkill":
            killed.append(cmd)
            return completed(cmd)
        if cmd[:3] == ["ssh", "10.34.158.137", "docker"] and "ps" in cmd:
            return completed(cmd, stdout="")
        if cmd[:3] == ["ssh", "10.34.158.137", "docker"]:
            return completed(cmd, stdout="swc-test-catalog-1234abcd\n")
        if cmd[:3] == ["ssh", "10.34.158.137", "rm"]:
            return completed(cmd)
        if cmd[:3] == ["ssh", "10.34.158.137", "test"]:
            return completed(cmd)
        raise AssertionError(cmd)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.cleanup(type("Args", (), {"state_file": state_file, "keep_state": True})()) == 1
    output = json.loads(capsys.readouterr().out)
    assert killed == []
    assert output["status"] == "needs_review"
    assert output["ssh_tunnel_stop"]["needs_review"] is True
    assert output["ssh_tunnel_stop"]["match_status"] == "creation_date_mismatch"


def test_cleanup_does_not_report_container_absent_when_docker_ps_fails(tmp_path, monkeypatch, capsys):
    state = owned_state()
    state_file = tmp_path / "swc-test-catalog-1234abcd-state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(helper, "process_fingerprint", lambda pid: None)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["ssh", "10.34.158.137", "docker"] and "ps" in cmd:
            return completed(cmd, returncode=255, stdout="", stderr="permission denied")
        if cmd[:3] == ["ssh", "10.34.158.137", "docker"]:
            return completed(cmd, stdout="swc-test-catalog-1234abcd\n")
        if cmd[:3] == ["ssh", "10.34.158.137", "rm"]:
            return completed(cmd)
        if cmd[:3] == ["ssh", "10.34.158.137", "test"]:
            return completed(cmd)
        raise AssertionError(cmd)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.cleanup(type("Args", (), {"state_file": state_file, "keep_state": True})()) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "needs_review"
    assert output["container_absent"] is False
    assert output["docker_ps_returncode"] == 255
