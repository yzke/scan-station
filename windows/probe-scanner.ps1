# Read-only diagnostic: no property assignments, image transfer, or queue writes.
param([string]$OutputPath = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function Publish-Result($Result) {
    $json = $Result | ConvertTo-Json -Depth 9 -Compress
    if ($OutputPath) { [IO.File]::WriteAllText($OutputPath, $json, (New-Object Text.UTF8Encoding($false))) }
    $json
}
function Read-Properties($owner, $ids) {
    $result = @()
    foreach ($p in $owner.Properties) {
        if ($ids -contains [int]$p.PropertyID) {
            $entry = [ordered]@{ id = [int]$p.PropertyID; name = [string]$p.Name; value = $p.Value; subtype = [int]$p.SubType; readonly = [bool]$p.IsReadOnly }
            if ($p.SubType -eq 1) { $entry.min = $p.SubTypeMin; $entry.max = $p.SubTypeMax; $entry.step = $p.SubTypeStep }
            if ($p.SubType -eq 2 -or $p.SubType -eq 3) { $entry.values = @($p.SubTypeValues) }
            $result += $entry
        }
    }
    return $result
}
$dm = $null; $device = $null; $item = $null
try {
    if (@(Get-ChildItem 'C:\Users\Public\scan-agent\req\*.json' -ErrorAction SilentlyContinue).Count) { throw 'Scan request pending; capability query deferred.' }
    $dm = New-Object -ComObject WIA.DeviceManager
    $devices = @()
    foreach ($di in $dm.DeviceInfos) {
        if ($di.Type -ne 1) { continue }
        $device = $di.Connect()
        $entry = [ordered]@{ device_id = $di.DeviceID; info = @(Read-Properties $di @(2, 3, 4, 7)); root = @(Read-Properties $device @(3086, 3087, 3088, 3096)); items = @() }
        foreach ($item in $device.Items) {
            $entry.items += ,@(Read-Properties $item @(3086, 3087, 3088, 3096, 4098, 4125, 6147, 6148, 6151, 6152))
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($item)
            $item = $null
        }
        $devices += $entry
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($device)
        $device = $null
    }
    Publish-Result ([ordered]@{ checked_at = [DateTime]::UtcNow.ToString('o'); devices = $devices })
} catch {
    Publish-Result ([ordered]@{ checked_at = [DateTime]::UtcNow.ToString('o'); error = $_.Exception.Message })
    exit 1
} finally {
    foreach ($obj in @($item, $device, $dm)) {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) }
    }
}
