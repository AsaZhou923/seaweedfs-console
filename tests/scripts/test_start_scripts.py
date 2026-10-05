from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]


def stopped_set(result: dict) -> set[int]:
    stopped = result["stopped"]
    if stopped is None:
        return set()
    if isinstance(stopped, list):
        return set(stopped)
    return {stopped}


def run_start_probe(
    tmp_path: Path,
    *,
    fail_index: int = 0,
    fail_record_write: bool = False,
    throw_start_index: int = 0,
    stop_fail_ids: tuple[int, ...] = (),
    reused_child_ids: tuple[int, ...] = (),
    child_discovery_throw_count: int = 0,
    metadata_throw_indices: tuple[int, ...] = (),
) -> dict:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip("PowerShell is not available")
    root = tmp_path / "app"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "start.ps1", scripts / "start.ps1")
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (root / "backend").mkdir()
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        textwrap.dedent(
            f"""
            $ErrorActionPreference = 'Stop'
            $env:CONSOLE_ADMIN_PASSWORD = 'test-password'
            $script:started = @()
            $script:processes = @()
            $script:stopped = @()
            $script:failIndex = {fail_index}
            $script:failRecordWrite = ${str(fail_record_write).lower()}
            $script:throwStartIndex = {throw_start_index}
            $script:stopFailIds = @({",".join(str(item) for item in stop_fail_ids)})
            $script:reusedChildIds = @({",".join(str(item) for item in reused_child_ids)})
            $script:childDiscoveryThrowCount = {child_discovery_throw_count}
            $script:childDiscoveryCalls = 0
            $script:metadataThrowIndices = @({",".join(str(item) for item in metadata_throw_indices)})

            function Get-NetTCPConnection {{ return $null }}
            function Start-Sleep {{
                param($Milliseconds)
                foreach ($taskProcess in $script:processes) {{
                    if ($taskProcess.FailAfterSleep) {{ $taskProcess.HasExited = $true }}
                }}
            }}
            function Start-Process {{
                param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
                $taskNext = $script:started.Count + 1
                if ($script:throwStartIndex -eq $taskNext) {{ throw "mock start failed $taskNext" }}
                $script:started += ,$ArgumentList
                $taskId = 4100 + $script:started.Count
                $taskThrowMetadata = $script:metadataThrowIndices -contains $script:started.Count
                Add-Type -TypeDefinition @"
public class MockConsoleProcess {{
    public int Id {{ get; set; }}
    public bool HasExited {{ get; set; }}
    public bool FailAfterSleep {{ get; set; }}
    public string FilePath {{ get; set; }}
    public bool ThrowMetadata {{ get; set; }}
    public System.DateTime Created {{ get; set; }}
    public string Path {{
        get {{
            if (ThrowMetadata) throw new System.InvalidOperationException("mock metadata path failed");
            return FilePath;
        }}
    }}
    public System.DateTime StartTime {{
        get {{
            if (ThrowMetadata) throw new System.InvalidOperationException("mock metadata start failed");
            return Created;
        }}
    }}
}}
"@ -ErrorAction SilentlyContinue
                $taskProcess = [MockConsoleProcess]@{{
                    Id = $taskId
                    HasExited = $false
                    FailAfterSleep = ($script:failIndex -eq $script:started.Count)
                    FilePath = $FilePath
                    ThrowMetadata = $taskThrowMetadata
                    Created = [datetime]::UtcNow
                }}
                $script:processes += $taskProcess
                $taskProcess
            }}
            function Get-CimInstance {{
                param($ClassName, $Filter, $ErrorAction)
                if ($Filter -match 'ParentProcessId=(\\d+)') {{
                    $script:childDiscoveryCalls += 1
                    if ($script:childDiscoveryCalls -le $script:childDiscoveryThrowCount) {{ throw "mock child discovery failed $($script:childDiscoveryCalls)" }}
                    $taskParent = [int]$Matches[1]
                    [pscustomobject]@{{
                        ProcessId = ($taskParent + 100)
                        ParentProcessId = $taskParent
                        CommandLine = 'python -m console.jobs'
                        ExecutablePath = '{(root / ".venv" / "Scripts" / "python.exe").as_posix()}'
                        CreationDate = '20261005000000.000000+000'
                    }}
                }} elseif ($Filter -match 'ProcessId=(\\d+)') {{
                    $taskProcessId = [int]$Matches[1]
                    $taskCreation = if ($script:reusedChildIds -contains $taskProcessId) {{ '20261006000000.000000+000' }} else {{ '20261005000000.000000+000' }}
                    [pscustomobject]@{{
                        ProcessId = $taskProcessId
                        ParentProcessId = ($taskProcessId - 100)
                        CommandLine = 'python -m console.jobs'
                        ExecutablePath = '{(root / ".venv" / "Scripts" / "python.exe").as_posix()}'
                        CreationDate = $taskCreation
                    }}
                }}
            }}
            function Get-Process {{
                param($Id, $ErrorAction)
                [pscustomobject]@{{ Id = $Id; ProcessName = 'python' }}
            }}
            function Stop-Process {{
                param($InputObject, $ErrorAction)
                if ($script:stopFailIds -contains $InputObject.Id) {{ throw "mock stop failed $($InputObject.Id)" }}
                $script:stopped += @($InputObject.Id)
            }}
            function Set-Content {{
                param([string]$LiteralPath, [Parameter(ValueFromPipeline=$true)]$Value, [string]$Encoding)
                begin {{ $items = @() }}
                process {{ $items += $Value }}
                end {{
                    if ($script:failRecordWrite -and $LiteralPath -like '*processes.json') {{ throw 'mock pid write failed' }}
                    Microsoft.PowerShell.Management\\Set-Content -LiteralPath $LiteralPath -Value $items -Encoding $Encoding
                }}
            }}

            try {{
                . '{(scripts / "start.ps1").as_posix()}' -Port 19876 | Out-Null
                $taskError = $null
            }} catch {{
                $taskError = $_.Exception.Message
            }}
            [pscustomobject]@{{
                error = $taskError
                started = $script:started.Count
                stopped = $script:stopped
                recordExists = (Test-Path -LiteralPath '{(root / "output" / "processes.json").as_posix()}')
            }} | ConvertTo-Json -Compress
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run([pwsh, "-NoProfile", "-File", str(probe)], text=True, capture_output=True, check=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_start_failure_cleans_live_process_from_this_attempt(tmp_path: Path):
    result = run_start_probe(tmp_path, fail_index=1)

    assert result["error"] == "Console startup failed; inspect output/*-error.log."
    assert result["started"] == 2
    stopped = stopped_set(result)
    assert 4101 not in stopped
    assert {4102, 4201, 4202}.issubset(stopped)
    assert result["recordExists"] is False


def test_start_worker_failure_cleans_api_from_this_attempt(tmp_path: Path):
    result = run_start_probe(tmp_path, fail_index=2)

    assert result["error"] == "Console startup failed; inspect output/*-error.log."
    assert result["started"] == 2
    stopped = stopped_set(result)
    assert {4101, 4201, 4202}.issubset(stopped)
    assert 4102 not in stopped
    assert result["recordExists"] is False


def test_second_start_failure_cleans_first_process_tree(tmp_path: Path):
    result = run_start_probe(tmp_path, throw_start_index=2)

    assert result["error"] == "mock start failed 2"
    assert result["started"] == 1
    assert {4101, 4201}.issubset(stopped_set(result))
    assert result["recordExists"] is False


def test_start_pid_record_failure_cleans_all_processes_from_this_attempt(tmp_path: Path):
    result = run_start_probe(tmp_path, fail_record_write=True)

    assert result["error"] == "mock pid write failed"
    assert result["started"] == 2
    assert {4101, 4201, 4102, 4202}.issubset(stopped_set(result))
    assert result["recordExists"] is False


def test_start_cleanup_failure_is_best_effort_and_preserves_original_error(tmp_path: Path):
    result = run_start_probe(tmp_path, fail_record_write=True, stop_fail_ids=(4201,))

    assert result["error"] == "mock pid write failed"
    stopped = stopped_set(result)
    assert 4201 not in stopped
    assert {4101, 4102, 4202}.issubset(stopped)
    assert result["recordExists"] is False


def test_start_does_not_stop_reused_child_pid_after_parent_exit(tmp_path: Path):
    result = run_start_probe(tmp_path, fail_index=1, reused_child_ids=(4201,))

    assert result["error"] == "Console startup failed; inspect output/*-error.log."
    stopped = stopped_set(result)
    assert 4201 not in stopped
    assert {4102, 4202}.issubset(stopped)
    assert result["recordExists"] is False


def test_child_discovery_failure_after_start_still_cleans_parent(tmp_path: Path):
    result = run_start_probe(tmp_path, child_discovery_throw_count=2)

    assert result["error"] == "mock child discovery failed 1"
    assert result["started"] == 1
    assert stopped_set(result) == {4101}
    assert result["recordExists"] is False


def test_metadata_capture_failure_after_start_still_cleans_parent(tmp_path: Path):
    result = run_start_probe(tmp_path, throw_start_index=2, metadata_throw_indices=(1,))

    assert result["error"] == "mock start failed 2"
    assert result["started"] == 1
    assert stopped_set(result) == {4101}
    assert result["recordExists"] is False


def test_start_success_writes_pid_record_without_cleanup(tmp_path: Path):
    result = run_start_probe(tmp_path)

    assert result["error"] is None
    assert result["started"] == 2
    assert stopped_set(result) == set()
    assert result["recordExists"] is True
