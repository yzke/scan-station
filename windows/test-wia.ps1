# Deterministic mocks only: no WIA COM objects, request queue, or images.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scanner-wia.ps1')
$checks = 0
function Assert-Equal($Actual, $Expected, [string]$Message) {
    $script:checks++
    if (($Actual | ConvertTo-Json -Compress) -ne ($Expected | ConvertTo-Json -Compress)) {
        throw ($Message + ': actual=' + ($Actual | ConvertTo-Json -Compress))
    }
}
function Assert-Rejected([scriptblock]$Action, [string]$Message) {
    $script:checks++
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw $Message }
}
function New-Property([int]$Id, [int]$Value, [int]$Subtype, $Values = @(), [int]$Minimum = 0, [int]$Maximum = 5000, [int]$Step = 1) {
    return [PSCustomObject]@{ PropertyID = $Id; Value = $Value; SubType = $Subtype; SubTypeValues = $Values; SubTypeMin = $Minimum; SubTypeMax = $Maximum; SubTypeStep = $Step; IsReadOnly = $false }
}
$x = New-Property 6147 150 2 @(150, 200, 300)
$y = New-Property 6148 150 2 @(150, 200, 300)
$width = New-Property 6151 1240 1
$height = New-Property 6152 1754 1
$mode = New-Property 3088 33 3 @(1, 2, 4, 8, 16, 32, 64)
$caps = New-Property 3086 5 0
$device = [PSCustomObject]@{ Properties = @($caps, $mode) }
$item = [PSCustomObject]@{ Properties = @($x, $y, $width, $height) }

Assert-Equal @(Get-WiaDpiSupport $item) @(150, 200, 300) 'List DPI support'
$y.SubTypeValues = @(150, 300)
Assert-Equal @(Get-WiaDpiSupport $item) @(150, 300) 'Require support on both axes'
$y.SubTypeValues = @(150, 200, 300)
$range = New-Property 6147 200 1 @() 100 600 100
Assert-Equal (Test-WiaPropertyValue $range 150) $false 'Respect resolution increments'
Assert-Equal (Test-WiaPropertyValue $range 300) $true 'Accept valid range value'
Assert-Equal (Get-WiaCapabilities $device $item).duplex_supported $true 'Expose known duplex support'
Assert-Equal ((Get-WiaCapabilities $null $item).ContainsKey('duplex_supported')) $false 'Missing duplex data remains unknown'
$caps.Value = 1
Assert-Equal (Get-WiaCapabilities $device $item).duplex_supported $false 'Expose explicitly unsupported duplex'
Assert-Rejected { Set-ScanOptions $device $item (Read-ScanOptions '{"duplex":true}') } 'Reject unsupported duplex before transfer'
$caps.Value = 5

Set-ScanOptions $device $item (Read-ScanOptions '{"src":"scan-station","dpi":300,"duplex":true}')
Assert-Equal $mode.Value 5 'Duplex preserves feeder and removes front-only'
Assert-Equal @($x.Value, $y.Value, $width.Value, $height.Value) @(300, 300, 2480, 3508) 'A4 at 300 DPI'
Set-ScanOptions $device $item (Read-ScanOptions '{"dpi":200,"duplex":false}')
Assert-Equal $mode.Value 33 'Simplex clears duplex and sets front-only'
Assert-Equal @($x.Value, $y.Value, $width.Value, $height.Value) @(200, 200, 1654, 2339) 'A4 at 200 DPI'

$legacy = Read-ScanOptions '{"src":"scan-station"}'
Assert-Equal @($legacy.has_dpi, $legacy.has_duplex) @($false, $false) 'Legacy request keeps driver choices'
foreach ($json in @('{"dpi":600}', '{"dpi":"300"}', '{"duplex":"false"}', 'true', 'null', '[]', '150')) {
    Assert-Rejected { Read-ScanOptions $json } ('Reject invalid options ' + $json)
}
$y.SubTypeValues = @(150)
Assert-Rejected { Set-ScanOptions $device $item (Read-ScanOptions '{"dpi":300}') } 'Reject unavailable DPI'
$exception = New-Object Runtime.InteropServices.COMException('offline', -2145320955)
Assert-Equal (Get-WiaErrorCode $exception) 2149646341 'Identify WIA offline HRESULT'
Assert-Equal (Read-ScanOptions '{"src":"old"}').max_pages 0 'Old requests remain unlimited'
$single = Read-ScanOptions '{"max_pages":1,"duplex":true}'
Assert-Equal @($single.max_pages, $single.duplex, $single.has_duplex) @(1, $false, $true) 'One-page request forces simplex'
$mode.Value = 5
Set-ScanOptions $device $item $single
Assert-Equal $mode.Value 33 'One-page settings clear duplex before transfer'
foreach ($json in @('{"max_pages":0}', '{"max_pages":2}', '{"max_pages":-1}', '{"max_pages":true}', '{"max_pages":null}', '{"max_pages":"1"}', '{"max_pages":1.0}')) {
    Assert-Rejected { Read-ScanOptions $json } ('Reject unsafe page bound ' + $json)
}

$originX = New-Property 6149 20 1
$originY = New-Property 6150 30 1
$pageLimit = New-Property 3096 0 1
$item.Properties += @($originX, $originY, $pageLimit)
$x.Value = 300; $y.Value = 300
$y.SubTypeValues = @(150, 200, 300)
$mode.Value = 5
$resetOptions = Read-ScanOptions '{"src":"old-client"}'
Set-ScanOptions $device $item $resetOptions -ResetBatchSettings
Assert-Equal @($originX.Value, $originY.Value, $pageLimit.Value, $x.Value, $y.Value, $mode.Value) @(0, 0, 1, 150, 150, 33) 'Reset partial native configuration before any compatibility Transfer'
Assert-Equal @($resetOptions.has_dpi, $resetOptions.has_duplex) @($false, $false) 'Compatibility reset does not mutate the parsed request'
$withoutLimit = [PSCustomObject]@{ Properties = @($x, $y, $width, $height, $originX, $originY) }
Assert-Rejected { Set-ScanOptions $device $withoutLimit $resetOptions -ResetBatchSettings } 'Refuse fallback when native all-page limit cannot be reset'
$pageLimit.Value = 2
$pageLimit.IsReadOnly = $true
try {
    Set-ScanOptions $device $item (Read-ScanOptions '{"duplex":true}')
    throw 'Expected unsafe duplex page limit to be rejected'
} catch {
    Assert-Equal $_.Exception.Message '驱动仅支持逐页兼容，无法安全获取双面，请改用单面。' 'Explain unsafe compatibility duplex without silently losing a side'
}
$pageLimit.IsReadOnly = $false

# Parse the actual agent; guard against applying compatibility page limits on
# the native path. Parsing does not execute the agent or instantiate a device.
$parseErrors = $null
$agentAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'scan-agent.ps1'), [ref]$null, [ref]$parseErrors)
Assert-Equal @($parseErrors).Count 0 'The complete agent parses without execution'
$configurationCalls = @($agentAst.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Set-ScanOptions' }, $true))
Assert-Equal $configurationCalls.Count 1 'Compatibility settings are not applied before native preparation'
$branch = $configurationCalls[0].Parent
while ($null -ne $branch -and $branch -isnot [Management.Automation.Language.IfStatementAst]) { $branch = $branch.Parent }
Assert-Equal ($null -ne $branch.ElseClause -and $configurationCalls[0].Extent.StartOffset -ge $branch.ElseClause.Extent.StartOffset -and $configurationCalls[0].Extent.EndOffset -le $branch.ElseClause.Extent.EndOffset) $true 'Compatibility settings execute only in the explicit fallback branch'

# File-only recovery test in a disposable directory beside this test script.
$testBase = Join-Path $PSScriptRoot ('.wia-test-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path "$testBase\req", "$testBase\out" -Force | Out-Null
    $requestPath = Join-Path $testBase 'req\test-batch.json'
    [IO.File]::WriteAllText($requestPath, '{"src":"fake-test"}')
    New-ScanClaim $testBase 'test-batch'
    Assert-Rejected { New-ScanClaim $testBase 'test-batch' } 'Never overwrite a prior scan claim'
    Assert-Equal (Complete-InterruptedBatch $testBase 'test-batch' $requestPath) $true 'A started batch cannot be replayed'
    Assert-Equal (Test-Path -LiteralPath $requestPath) $false 'Retire interrupted request'
    Assert-Equal ((Get-Content "$testBase\out\test-batch.status" -Raw).StartsWith('error pages=0:')) $true 'Publish counted interruption error'
    $legacyRequest = Join-Path $testBase 'req\legacy-batch.json'
    $legacyPage = Join-Path $testBase 'out\legacy-batch-p1.jpg'
    [IO.File]::WriteAllText($legacyRequest, '{"src":"fake-test"}')
    [IO.File]::WriteAllText($legacyPage, 'saved-page-must-survive')
    Assert-Equal (Complete-InterruptedBatch $testBase 'legacy-batch' $legacyRequest) $true 'Protect partial batches from the old agent too'
    Assert-Equal ([IO.File]::ReadAllText($legacyPage)) 'saved-page-must-survive' 'Never delete a recovered page'
    Assert-Equal ((Get-Content "$testBase\out\legacy-batch.status" -Raw).StartsWith('error pages=1:')) $true 'Interrupted batch reports already published page count'
    Assert-Equal (Complete-InterruptedBatch $testBase 'fresh-batch' 'unused') $false 'Fresh request is not an interruption'
    function Test-FakeCapture([int]$MaxPages, [int]$FailureCode, [bool]$FailPublish = $false, [int]$Available = 2) {
        $fakeState = @{ calls = 0; published = 0 }
        $result = Invoke-PageCapture @{ max_pages = $MaxPages } {
            param($completed)
            $fakeState.calls++
            if ($fakeState.calls -gt $Available) {
                throw (New-Object Runtime.InteropServices.COMException('simulated transfer failure', $FailureCode))
            }
            return [PSCustomObject]@{ Page = $fakeState.calls }
        } {
            param($image, $number)
            if ($FailPublish -and $number -eq 2) { throw 'simulated JPEG publication failure' }
            [IO.File]::WriteAllText((Join-Path $testBase ('out\fake-p' + $number + '.jpg')), 'fake image')
            $fakeState.published++
        }
        return @{ result = $result; calls = $fakeState.calls; published = $fakeState.published }
    }
    $one = Test-FakeCapture 1 -2145320957
    Assert-Equal @($one.calls, $one.published, $one.result.pages, $one.result.terminal) @(1, 1, 1, 'ok:1') 'Bounded capture stops before second Transfer'
    $normal = Test-FakeCapture 0 -2145320957
    Assert-Equal @($normal.calls, $normal.result.pages, $normal.result.terminal) @(3, 2, 'ok:2') 'Only paper empty completes a normal batch'
    foreach ($errorCode in @(-2145320958, -2145320940, -2145320950, -1)) {
        $failed = Test-FakeCapture 0 $errorCode
        Assert-Equal @($failed.result.pages, $failed.published) @(2, 2) 'Transfer failure retains published pages'
        Assert-Equal ($failed.result.terminal.StartsWith('error pages=2:')) $true 'Jam, multi-feed and other errors cannot report success'
    }
    $saveFailure = Test-FakeCapture 0 -2145320957 $true
    Assert-Equal @($saveFailure.calls, $saveFailure.result.pages) @(2, 1) 'Failed JPEG publication cannot increase completed count'
    Assert-Equal ($saveFailure.result.terminal.StartsWith('error pages=1:')) $true 'Publication failure is visible'
    $empty = Test-FakeCapture 1 -2145320957 $false 0
    Assert-Equal ($empty.result.terminal.StartsWith('error pages=0:')) $true 'An empty one-page rescan is a failure'
    Write-ScannerHeartbeat $testBase @{ state = 'online'; supported_dpi = @(150) }
    $heartbeat = Get-Content (Join-Path $testBase 'scanner-status.json') -Raw | ConvertFrom-Json
    Assert-Equal @($heartbeat.agent_version, $heartbeat.supports_page_rescan) @(5, $true) 'Advertise v5 and the backward compatible one-page protocol'
} finally {
    Remove-Item -LiteralPath $testBase -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output ("PASS: $checks mock WIA assertions; no scanner contacted.")
