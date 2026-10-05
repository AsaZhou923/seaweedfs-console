param([int]$Port = 18765, [switch]$Install)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if ($Install) {
    if (-not (Test-Path -LiteralPath $taskPython)) {
        python -m venv (Join-Path $taskRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $taskPython -m pip install -r (Join-Path $taskRoot 'backend\requirements-lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    Push-Location (Join-Path $taskRoot 'frontend')
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run scripts/start.ps1 -Install first.' }
if (-not $env:CONSOLE_ADMIN_PASSWORD) { throw 'Set CONSOLE_ADMIN_PASSWORD before startup.' }
if (-not $env:CONSOLE_DATA_DIR) { $env:CONSOLE_DATA_DIR = Join-Path $taskRoot 'data' }
if (-not $env:CONSOLE_ALLOWED_ORIGINS) { $env:CONSOLE_ALLOWED_ORIGINS = "http://127.0.0.1:$Port" }
if (-not $env:CONSOLE_SECRETS_FILE) { $env:CONSOLE_SECRETS_FILE = Join-Path $taskRoot 'secrets.json' }
$env:CONSOLE_DATA_DIR = [System.IO.Path]::GetFullPath($env:CONSOLE_DATA_DIR, $taskRoot)
$env:CONSOLE_SECRETS_FILE = [System.IO.Path]::GetFullPath($env:CONSOLE_SECRETS_FILE, $taskRoot)
if (-not $env:CONSOLE_DEV_INSECURE_COOKIE) { $env:CONSOLE_DEV_INSECURE_COOKIE = '1' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port already has a listener; stop the recorded console before restarting." }
$taskOutput = Join-Path $taskRoot 'output'
New-Item -ItemType Directory -Path $taskOutput -Force | Out-Null

function ConvertTo-TaskUtcString {
    param([object]$Value)
    if (-not $Value) { return $null }
    if ($Value -is [datetime]) { return $Value.ToUniversalTime().ToString('o') }
    try {
        return ([System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToUniversalTime().ToString('o')
    } catch {
        return [string]$Value
    }
}

function Get-TaskConsoleChildren {
    param([int]$ParentProcessId)
    @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentProcessId" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'console\.main:create_app|console\.jobs' } | ForEach-Object {
        [pscustomobject]@{
            ProcessId = [int]$_.ProcessId
            ParentProcessId = [int]$_.ParentProcessId
            CommandLine = [string]$_.CommandLine
            ExecutablePath = if ($_.ExecutablePath) { [string]$_.ExecutablePath } else { $null }
            CreationDate = ConvertTo-TaskUtcString $_.CreationDate
        }
    })
}

function New-TaskProcessRecord {
    param([object]$TaskProcess, [string]$TaskPythonPath)
    [pscustomobject]@{
        Process = $TaskProcess
        Id = [int]$TaskProcess.Id
        ExpectedPath = $TaskPythonPath
        Path = $null
        StartTime = $null
        Children = @()
    }
}

function Add-TaskProcessMetadata {
    param([object]$TaskRecord)
    try {
        if ($TaskRecord.Process.Path) { $TaskRecord.Path = [string]$TaskRecord.Process.Path }
        $TaskRecord.StartTime = ConvertTo-TaskUtcString $TaskRecord.Process.StartTime
    } catch {
        Write-Warning "Failed to capture console PID $($TaskRecord.Id) metadata: $($_.Exception.Message)"
    }
    return $TaskRecord
}

function Add-TaskProcessChildren {
    param([object]$TaskRecord)
    $TaskRecord.Children = @(Get-TaskConsoleChildren -ParentProcessId $TaskRecord.Id)
    return $TaskRecord
}

function Test-TaskParentMatches {
    param([object]$TaskRecord)
    $taskProcess = $TaskRecord.Process
    if (-not $taskProcess -or $taskProcess.HasExited) { return $false }
    try {
        $taskProcessPath = [string]$taskProcess.Path
        if ($TaskRecord.Path -and $taskProcessPath -ne [string]$TaskRecord.Path) { return $false }
        if ((-not $TaskRecord.Path) -and $TaskRecord.ExpectedPath -and $taskProcessPath -ne [string]$TaskRecord.ExpectedPath) { return $false }
    } catch {
        if ($TaskRecord.Path) { return $false }
    }
    if (-not $TaskRecord.StartTime) { return $true }
    $taskCurrentStart = ConvertTo-TaskUtcString $taskProcess.StartTime
    if ($taskCurrentStart -ne $TaskRecord.StartTime) { return $false }
    return $true
}

function Test-TaskParentVerifiedForChildRefresh {
    param([object]$TaskRecord)
    if (-not $TaskRecord.Path -or -not $TaskRecord.StartTime) { return $false }
    if (-not (Test-TaskParentMatches -TaskRecord $TaskRecord)) { return $false }
    return $true
}

function Test-TaskChildMatches {
    param([object]$Expected, [object]$Current)
    if (-not $Current) { return $false }
    if ([int]$Current.ProcessId -ne [int]$Expected.ProcessId) { return $false }
    if ([int]$Current.ParentProcessId -ne [int]$Expected.ParentProcessId) { return $false }
    if ([string]$Current.CommandLine -ne [string]$Expected.CommandLine) { return $false }
    if ($Expected.ExecutablePath -and [string]$Current.ExecutablePath -ne [string]$Expected.ExecutablePath) { return $false }
    $taskCurrentCreated = ConvertTo-TaskUtcString $Current.CreationDate
    if ($Expected.CreationDate -and $taskCurrentCreated -ne $Expected.CreationDate) { return $false }
    return $true
}

function Stop-TaskChild {
    param([object]$TaskChild)
    try {
        $taskCurrent = Get-CimInstance Win32_Process -Filter "ProcessId=$($TaskChild.ProcessId)" -ErrorAction SilentlyContinue
        if (-not (Test-TaskChildMatches -Expected $TaskChild -Current $taskCurrent)) { return }
        $taskChildProcess = Get-Process -Id $TaskChild.ProcessId -ErrorAction SilentlyContinue
        if ($taskChildProcess -and $taskChildProcess.ProcessName -eq 'python') {
            Stop-Process -InputObject $taskChildProcess -ErrorAction Stop
        }
    } catch {
        Write-Warning "Failed to stop console child PID $($TaskChild.ProcessId): $($_.Exception.Message)"
    }
}

function Stop-TaskParent {
    param([object]$TaskRecord)
    $taskProcess = $TaskRecord.Process
    if (-not $taskProcess -or $taskProcess.HasExited) { return }
    if ($TaskRecord.Path -and $TaskRecord.StartTime) {
        try {
            if ([string]$taskProcess.Path -ne [string]$TaskRecord.Path) { return }
            $taskCurrentStart = ConvertTo-TaskUtcString $taskProcess.StartTime
            if ($taskCurrentStart -ne $TaskRecord.StartTime) { return }
        } catch {
            return
        }
    }
    try {
        Stop-Process -InputObject $TaskRecord.Process -ErrorAction Stop
        return
    } catch {
        if (-not $TaskRecord.Path -or -not $TaskRecord.StartTime) {
            Write-Warning "Failed to stop console PID $($TaskRecord.Id): $($_.Exception.Message)"
            return
        }
    }
    try {
        $taskCurrent = Get-Process -Id $TaskRecord.Id -ErrorAction SilentlyContinue
        if (-not $taskCurrent) { return }
        if ([string]$taskCurrent.Path -ne [string]$TaskRecord.Path) { return }
        $taskCurrentStart = ConvertTo-TaskUtcString $taskCurrent.StartTime
        if ($taskCurrentStart -ne $TaskRecord.StartTime) { return }
        Stop-Process -InputObject $taskCurrent -ErrorAction Stop
    } catch {
        Write-Warning "Failed to stop console PID $($TaskRecord.Id): $($_.Exception.Message)"
    }
}

function Stop-TaskProcessTree {
    param([object]$TaskRecord, [string]$TaskPythonPath)
    if (-not $TaskRecord) { return }
    try {
        if (Test-TaskParentVerifiedForChildRefresh -TaskRecord $TaskRecord) {
            try {
                $TaskRecord.Children = @($TaskRecord.Children) + @(Get-TaskConsoleChildren -ParentProcessId $TaskRecord.Id)
            } catch {
                Write-Warning "Failed to inspect console children for PID $($TaskRecord.Id): $($_.Exception.Message)"
            }
        }
        foreach ($taskChild in @($TaskRecord.Children | Sort-Object ProcessId -Unique -Descending)) {
            Stop-TaskChild -TaskChild $taskChild
        }
        if (-not $TaskRecord.Path -or $TaskRecord.Path -eq $TaskPythonPath) {
            Stop-TaskParent -TaskRecord $TaskRecord
        }
    } catch {
        Write-Warning "Failed to stop console PID $($TaskRecord.Id): $($_.Exception.Message)"
    }
}

function Stop-TaskStartupProcesses {
    param([object[]]$TaskRecords, [string]$TaskPythonPath)
    foreach ($taskRecord in @($TaskRecords | Sort-Object Id -Descending)) {
        Stop-TaskProcessTree -TaskRecord $taskRecord -TaskPythonPath $TaskPythonPath
    }
}

$taskStartedProcesses = @()
try {
    $taskApi = Start-Process -FilePath $taskPython -ArgumentList @('-m','uvicorn','console.main:create_app','--factory','--host','127.0.0.1','--port',"$Port") -WorkingDirectory (Join-Path $taskRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskOutput 'api.log') -RedirectStandardError (Join-Path $taskOutput 'api-error.log')
    $taskApiRecord = New-TaskProcessRecord -TaskProcess $taskApi -TaskPythonPath $taskPython
    $taskStartedProcesses += $taskApiRecord
    Add-TaskProcessMetadata -TaskRecord $taskApiRecord | Out-Null
    if (Test-TaskParentVerifiedForChildRefresh -TaskRecord $taskApiRecord) {
        Add-TaskProcessChildren -TaskRecord $taskApiRecord | Out-Null
    }
    $taskWorker = Start-Process -FilePath $taskPython -ArgumentList @('-m','console.jobs') -WorkingDirectory (Join-Path $taskRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskOutput 'worker.log') -RedirectStandardError (Join-Path $taskOutput 'worker-error.log')
    $taskWorkerRecord = New-TaskProcessRecord -TaskProcess $taskWorker -TaskPythonPath $taskPython
    $taskStartedProcesses += $taskWorkerRecord
    Add-TaskProcessMetadata -TaskRecord $taskWorkerRecord | Out-Null
    if (Test-TaskParentVerifiedForChildRefresh -TaskRecord $taskWorkerRecord) {
        Add-TaskProcessChildren -TaskRecord $taskWorkerRecord | Out-Null
    }
    Start-Sleep -Milliseconds 700
    if ($taskApi.HasExited -or $taskWorker.HasExited) { throw 'Console startup failed; inspect output/*-error.log.' }
    $taskProcesses = @{api=@{pid=$taskApi.Id;start_time=$taskApi.StartTime.ToUniversalTime().ToString('o')};worker=@{pid=$taskWorker.Id;start_time=$taskWorker.StartTime.ToUniversalTime().ToString('o')}}
    $taskProcesses | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskOutput 'processes.json') -Encoding utf8
    Write-Output "API http://127.0.0.1:$Port | API PID $($taskApi.Id) | worker PID $($taskWorker.Id)"
} catch {
    Stop-TaskStartupProcesses -TaskRecords $taskStartedProcesses -TaskPythonPath $taskPython
    throw
}
