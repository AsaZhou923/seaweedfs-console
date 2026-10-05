$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRecord = Join-Path $taskRoot 'output\processes.json'
if (-not (Test-Path -LiteralPath $taskRecord)) { Write-Output 'No recorded console processes.'; return }
$taskRecords = Get-Content -LiteralPath $taskRecord -Raw | ConvertFrom-Json -DateKind String
foreach ($taskEntry in @($taskRecords.api, $taskRecords.worker)) {
    $taskProcess = Get-Process -Id $taskEntry.pid -ErrorAction SilentlyContinue
    if ($taskProcess -and $taskProcess.Path -eq (Join-Path $taskRoot '.venv\Scripts\python.exe') -and $taskProcess.StartTime.ToUniversalTime().ToString('o') -eq $taskEntry.start_time) {
        # Windows venv launchers create a base Python child. Stop that owned
        # console module too, otherwise its listener survives the launcher.
        $taskChildren = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($taskProcess.Id)" | Where-Object { $_.CommandLine -match 'console\.main:create_app|console\.jobs' }
        foreach ($taskChild in $taskChildren) {
            $taskChildProcess = Get-Process -Id $taskChild.ProcessId -ErrorAction SilentlyContinue
            if ($taskChildProcess -and $taskChildProcess.ProcessName -eq 'python') { Stop-Process -InputObject $taskChildProcess }
        }
        Stop-Process -InputObject $taskProcess
        Write-Output "Stopped console PID $($taskEntry.pid)"
    }
}
