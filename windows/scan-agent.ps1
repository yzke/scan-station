# scan-agent.ps1 v5: one WIA 2 Download per batch, with explicit compatibility mode.
[CmdletBinding()]
param(
    [string]$Base = 'C:\Users\Public\scan-agent',
    [switch]$StatusOnly,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scanner-wia.ps1')
Add-Type -Path (Join-Path $PSScriptRoot 'wia-batch.cs') -ReferencedAssemblies System.Drawing

if ($ValidateOnly) {
    # Parsing the agent and loading helpers never enumerate hardware or requests.
    'Scan agent scripts loaded successfully; no scanner was contacted.'
    return
}
if ($StatusOnly) {
    # Deliberately separate from the request loop: this path cannot transfer.
    if (@(Get-ChildItem "$Base\req\*.json" -ErrorAction SilentlyContinue).Count) {
        throw 'Scan request pending; read-only status query deferred.'
    }
    $snapshot = Get-ScannerSnapshot
    Write-ScannerHeartbeat $Base $snapshot
    $snapshot | ConvertTo-Json -Depth 5 -Compress
    return
}

New-Item -ItemType Directory -Force -Path "$Base\req", "$Base\out" | Out-Null
Add-Type -AssemblyName System.Drawing
$lastProbe = [DateTime]::MinValue
$captureMode = 'wia2-preferred'
$captureDetail = ''
$snapshot = @{ state = 'unknown'; supported_dpi = @() }
while ($true) {
    try {
        foreach ($requestFile in @(Get-ChildItem "$Base\req\*.json" -ErrorAction SilentlyContinue)) {
            $id = $requestFile.BaseName
            if ($id -notmatch '^[A-Za-z0-9_-]{1,120}$') { continue }
            $statusPath = "$Base\out\$id.status"
            if (Test-Path -LiteralPath $statusPath) {
                Remove-Item -LiteralPath $requestFile.FullName -Force -ErrorAction SilentlyContinue
                continue
            }
            if (Complete-InterruptedBatch $Base $id $requestFile.FullName) { continue }
            $terminal = 'error pages=0: Capture did not start.'
            $capture = $null
            $native = $null
            $resetBatchSettings = $false
            $dm = $null; $deviceInfo = $null; $device = $null; $item = $null; $image = $null; $bitmap = $null
            try {
                $options = Read-ScanOptions (Get-Content -LiteralPath $requestFile.FullName -Raw)
                $snapshot.state = 'scanning'
                Write-ScannerHeartbeat $Base $snapshot
                $dm = New-Object -ComObject WIA.DeviceManager
                foreach ($info in $dm.DeviceInfos) {
                    if ($info.Type -eq 1) { $deviceInfo = $info; break }
                    Release-WiaObject $info
                }
                if ($null -eq $deviceInfo) { throw 'No WIA scanner is present.' }
                $device = $deviceInfo.Connect()
                $item = $device.Items.Item(1)
                $capabilities = Get-WiaCapabilities $device $item
                foreach ($key in $capabilities.Keys) { $snapshot[$key] = $capabilities[$key] }
                $name = Get-WiaProperty $deviceInfo 7
                if ($null -ne $name) { $snapshot.name = [string]$name.Value }
                $deviceId = [string]$deviceInfo.DeviceID
                # Close Automation's connection before preparing the native feeder.
                Release-WiaObject $item; $item = $null
                Release-WiaObject $device; $device = $null
                try {
                    $native = [ScanStation.Wia2.NativeSession]::Prepare($deviceId, $options.dpi, $options.duplex, $options.max_pages)
                    $captureMode = 'wia2-batch'
                    $captureDetail = ''
                } catch {
                    $unavailable = $false
                    $cause = $_.Exception
                    while ($null -ne $cause) {
                        if ($cause -is [ScanStation.Wia2.BatchUnavailableException]) { $unavailable = $true; break }
                        $cause = $cause.InnerException
                    }
                    if (-not $unavailable) { throw }
                    # Prepare never calls Download. This is the only fallback gate.
                    $captureMode = 'wia-automation-compat'
                    $captureDetail = '驱动未提供可用的连续进纸设置'
                    $snapshot.capture_mode_error = $_.Exception.Message
                    $resetBatchSettings = $cause.ConfigurationChanged
                }
                $snapshot.capture_mode = $captureMode
                $snapshot.capture_mode_detail = $captureDetail
                Write-ScannerHeartbeat $Base $snapshot
                New-ScanClaim $Base $id
                if ($null -ne $native) {
                    # Run never falls back, including a failed Download with zero callbacks.
                    $result = [ScanStation.Wia2.BatchCapture]::Run($native, "$Base\out", $id, $options.max_pages, "$Base\scanner-status.json")
                    $terminal = $result.Terminal
                } else {
                $device = $deviceInfo.Connect()
                $item = $device.Items.Item(1)
                Set-ScanOptions $device $item $options -ResetBatchSettings:$resetBatchSettings
                $capture = @{ Item = $item; Device = $device }
                $result = Invoke-PageCapture $options {
                    param($completedPages)
                    if ($completedPages -gt 0) {
                        Release-WiaObject $capture.Item
                        $capture.Item = $capture.Device.Items.Item(1)
                    }
                    # The only physical scan call; reached exclusively for a real request.
                    $capture.Item.Transfer('{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}')
                } {
                    param($image, $nextPage)
                    $temporaryBmp = "$Base\out\$id-p$nextPage.tmp"
                    $temporaryJpg = "$Base\out\$id-p$nextPage.jpg.part"
                    Remove-Item -LiteralPath $temporaryBmp -Force -ErrorAction SilentlyContinue
                    $image.SaveFile($temporaryBmp)
                    $bitmap = [System.Drawing.Image]::FromFile($temporaryBmp)
                    try { $bitmap.Save($temporaryJpg, [System.Drawing.Imaging.ImageFormat]::Jpeg) }
                    finally { $bitmap.Dispose() }
                    Move-Item -LiteralPath $temporaryJpg -Destination "$Base\out\$id-p$nextPage.jpg" -Force
                    Remove-Item -LiteralPath $temporaryBmp -Force -ErrorAction SilentlyContinue
                } {
                    Write-ScannerHeartbeat $Base $snapshot
                }
                $terminal = $result.terminal
                }
            } catch {
                $terminal = 'error pages=0: ' + $_.Exception.Message
            } finally {
                if ($null -ne $native) { $native.Dispose() }
                if ($null -ne $capture) { Release-WiaObject $capture.Item }
                foreach ($object in @($image, $item, $device, $deviceInfo, $dm)) { Release-WiaObject $object }
            }
            Write-BatchTerminal $Base $id $terminal
            Remove-Item -LiteralPath "$Base\out\$id.started" -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $requestFile.FullName -Force -ErrorAction SilentlyContinue
            $lastProbe = [DateTime]::MinValue
        }
        # Runs in the same loop, so metadata queries never collide with a scan.
        if (([DateTime]::UtcNow - $lastProbe).TotalSeconds -ge 10) {
            $snapshot = Get-ScannerSnapshot
            $snapshot.capture_mode = $captureMode
            $snapshot.capture_mode_detail = $captureDetail
            Write-ScannerHeartbeat $Base $snapshot
            $lastProbe = [DateTime]::UtcNow
        }
    } catch {
        # A transport/status error must not terminate the existing request loop.
    }
    Start-Sleep -Seconds 3
}
