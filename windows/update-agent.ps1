# Run this on the scanner's Windows PC. This updater never submits a scan.
[CmdletBinding()]
param([string]$Base = 'C:\Users\Public\scan-agent')
$ErrorActionPreference = 'Stop'
$runtimeFiles = @('scan-agent.ps1', 'scanner-wia.ps1', 'wia-batch.cs', 'run-agent.cmd')
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$agentPath = Join-Path $Base 'scan-agent.ps1'
$backup = $null
$replaced = $false
$stopped = $false
$newProcess = $null

function Assert-QueueEmpty {
    $requestDirectory = Join-Path $Base 'req'
    if (-not (Test-Path -LiteralPath $requestDirectory -PathType Container)) {
        throw 'Existing scan request directory is unavailable; the running agent was not changed.'
    }
    if (@(Get-ChildItem -LiteralPath $requestDirectory -Filter '*.json' -ErrorAction Stop).Count) {
        throw 'A scan is queued or in progress. Wait for it to finish, then run this updater again.'
    }
}
function Find-AgentProcesses {
    $pattern = [regex]::Escape($agentPath)
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object { $_.ProcessId -ne $PID })
    if (@($candidates | Where-Object { -not $_.CommandLine }).Count) {
        throw 'A PowerShell process could not be identified. Run this updater using the account that started the current agent.'
    }
    return @($candidates | Where-Object { $_.CommandLine -match $pattern })
}
function Start-AgentProcess {
    return Start-Process -FilePath $powershellPath -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', ('"' + $agentPath + '"'), '-Base', ('"' + $Base + '"')) -WindowStyle Hidden -PassThru
}

try {
    foreach ($name in $runtimeFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name))) {
            throw ('Missing package file: ' + $name + '. Extract the complete update folder first.')
        }
    }
    & $powershellPath -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'scan-agent.ps1') -ValidateOnly
    if ($LASTEXITCODE -ne 0) { throw 'Package validation failed. Existing agent was not changed.' }
    Assert-QueueEmpty
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
    $backup = Join-Path $Base ('backups\' + $stamp)
    $stage = Join-Path $Base ('updates\' + $stamp)
    New-Item -ItemType Directory -Force -Path $backup, $stage | Out-Null
    foreach ($name in $runtimeFiles) {
        $current = Join-Path $Base $name
        if (Test-Path -LiteralPath $current) { Copy-Item -LiteralPath $current -Destination (Join-Path $backup $name) }
        $source = Join-Path $PSScriptRoot $name
        $target = Join-Path $stage $name
        Copy-Item -LiteralPath $source -Destination $target
        if ((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash) {
            throw ('Staged file verification failed: ' + $name)
        }
    }
    Assert-QueueEmpty
    foreach ($process in @(Find-AgentProcesses)) {
        Stop-Process -Id $process.ProcessId -ErrorAction Stop
        $stopped = $true
    }
    Assert-QueueEmpty
    $replaced = $true
    foreach ($name in $runtimeFiles) {
        Copy-Item -LiteralPath (Join-Path $stage $name) -Destination (Join-Path $Base $name) -Force
    }
    Assert-QueueEmpty
    $startedAt = [DateTime]::UtcNow
    $newProcess = Start-AgentProcess
    $fresh = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        if ($newProcess.HasExited) { throw 'Updated agent exited before producing a heartbeat.' }
        try {
            $status = Get-Content -LiteralPath (Join-Path $Base 'scanner-status.json') -Raw | ConvertFrom-Json
            if ($status.agent_version -eq 5 -and $status.supports_page_rescan -eq $true -and [DateTime]::Parse($status.checked_at).ToUniversalTime() -ge $startedAt.AddSeconds(-1)) {
                $fresh = $true
                break
            }
        } catch {}
    }
    Write-Output ('Update installed. Backup: ' + $backup)
    if ($fresh) {
        Write-Output ('Scanner status: ' + $status.state + '; DPI: ' + ($status.supported_dpi -join ', '))
    } else {
        Write-Output 'Agent is running; waiting for the scanner driver heartbeat.'
    }
    Write-Output 'No scan was submitted by this updater. Refresh the scan-station web page.'
} catch {
    $failure = $_.Exception.Message
    if ($null -ne $newProcess -and -not $newProcess.HasExited) {
        Stop-Process -Id $newProcess.Id -ErrorAction SilentlyContinue
    }
    if ($replaced -and $backup) {
        foreach ($name in $runtimeFiles) {
            $saved = Join-Path $backup $name
            if (Test-Path -LiteralPath $saved) {
                Copy-Item -LiteralPath $saved -Destination (Join-Path $Base $name) -Force -ErrorAction SilentlyContinue
            }
        }
    }
    if ($stopped) {
        try {
            Assert-QueueEmpty
            if (@(Find-AgentProcesses).Count -eq 0) { $null = Start-AgentProcess }
        } catch { Write-Output 'Previous agent could not be restarted automatically. Close this window and run the saved run-agent.cmd when the queue is empty.' }
    }
    Write-Output ('Update stopped: ' + $failure)
    if ($backup) { Write-Output ('Existing files are backed up at: ' + $backup) }
    exit 1
}
