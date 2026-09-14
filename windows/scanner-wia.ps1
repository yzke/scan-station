# WIA metadata/settings helpers. Loading this file never contacts a device.
# Constants: Microsoft WiaDef.h; 6147/6148 resolution, 3086 capabilities,
# 3088 document handling, DUP/DUPLEX=4, FRONT_ONLY=32.
function Release-WiaObject($Object) {
    if ($null -ne $Object -and [Runtime.InteropServices.Marshal]::IsComObject($Object)) {
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Object) } catch {}
    }
}

function Get-WiaProperty($Owner, [int]$Id) {
    if ($null -eq $Owner) { return $null }
    foreach ($property in $Owner.Properties) {
        if ([int]$property.PropertyID -eq $Id) { return $property }
    }
    return $null
}

function Test-WiaPropertyValue($Property, [int]$Value) {
    if ($null -eq $Property) { return $false }
    if ($Property.IsReadOnly) { return ([int]$Property.Value -eq $Value) }
    switch ([int]$Property.SubType) {
        1 {
            $min = [int]$Property.SubTypeMin
            $max = [int]$Property.SubTypeMax
            $step = [int]$Property.SubTypeStep
            if ($Value -lt $min -or $Value -gt $max) { return $false }
            return ($step -le 0 -or (($Value - $min) % $step) -eq 0)
        }
        2 { return (@($Property.SubTypeValues) -contains $Value) }
        default { return ([int]$Property.Value -eq $Value) }
    }
}

function Get-WiaDpiSupport($Item) {
    $x = Get-WiaProperty $Item 6147
    $y = Get-WiaProperty $Item 6148
    foreach ($dpi in @(150, 200, 300)) {
        if ((Test-WiaPropertyValue $x $dpi) -and (Test-WiaPropertyValue $y $dpi)) { $dpi }
    }
}

function Get-WiaFlagMask($Property) {
    if ($null -eq $Property -or [int]$Property.SubType -ne 3) { return $null }
    $mask = 0
    foreach ($value in $Property.SubTypeValues) { $mask = $mask -bor [int]$value }
    return $mask
}

function Get-WiaCapabilities($Device, $Item) {
    $result = @{ supported_dpi = @(Get-WiaDpiSupport $Item) }
    $caps = Get-WiaProperty $Device 3086
    if ($null -eq $caps) { $caps = Get-WiaProperty $Item 3086 }
    $select = Get-WiaProperty $Item 3088
    if ($null -eq $select) { $select = Get-WiaProperty $Device 3088 }
    $mask = Get-WiaFlagMask $select
    if ($null -ne $caps -and (([int]$caps.Value -band 4) -eq 0)) {
        $result.duplex_supported = $false
    } elseif ($null -ne $select) {
        if ($select.IsReadOnly) {
            $result.duplex_supported = (([int]$select.Value -band 4) -ne 0)
        } elseif ($null -ne $mask) {
            $result.duplex_supported = (($mask -band 4) -ne 0)
        } elseif ($null -ne $caps) {
            $result.duplex_supported = (([int]$caps.Value -band 4) -ne 0)
        }
    }
    return $result
}

function Get-WiaErrorCode($Exception) {
    $code = 0
    while ($null -ne $Exception) {
        $candidate = ([int64]$Exception.HResult -band 4294967295)
        if (($candidate -band 4294901760) -eq 2149646336) { $code = $candidate }
        $Exception = $Exception.InnerException
    }
    return $code
}

function Get-ScannerSnapshot {
    $dm = $null; $deviceInfo = $null; $device = $null; $item = $null
    $result = @{ state = 'unknown'; supported_dpi = @() }
    try {
        $dm = New-Object -ComObject WIA.DeviceManager
        foreach ($info in $dm.DeviceInfos) {
            if ($info.Type -eq 1) { $deviceInfo = $info; break }
            Release-WiaObject $info
        }
        if ($null -eq $deviceInfo) {
            $result.state = 'offline'
        } else {
            $name = Get-WiaProperty $deviceInfo 7
            if ($null -ne $name) { $result.name = [string]$name.Value }
            $device = $deviceInfo.Connect()
            $item = $device.Items.Item(1)
            $result.state = 'online'
            $capabilities = Get-WiaCapabilities $device $item
            foreach ($key in $capabilities.Keys) { $result[$key] = $capabilities[$key] }
        }
    } catch {
        $code = Get-WiaErrorCode $_.Exception
        $result.state = 'unknown'
        if ($code -eq 2149646341) { $result.state = 'offline' } # WIA_ERROR_OFFLINE
        if ($code -eq 2149646342 -or $code -eq 2149646349) { $result.state = 'scanning' }
        $result.detail = $_.Exception.Message
    } finally {
        foreach ($object in @($item, $device, $deviceInfo, $dm)) { Release-WiaObject $object }
    }
    $result.checked_at = [DateTime]::UtcNow.ToString('o')
    $result.agent_version = 5
    $result.capture_mode = 'wia2-preferred'
    $result.supports_page_rescan = $true
    return $result
}

function Read-ScanOptions([string]$Json) {
    if (-not $Json.TrimStart().StartsWith('{')) { throw 'Request must be a JSON object.' }
    $request = $Json | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $request -or $request.GetType().FullName -ne 'System.Management.Automation.PSCustomObject') {
        throw 'Request must be a JSON object.'
    }
    $names = @($request.PSObject.Properties.Name)
    $options = @{ dpi = 150; duplex = $false; has_dpi = $false; has_duplex = $false; max_pages = 0 }
    if ($names -contains 'dpi') {
        if (($request.dpi -isnot [int] -and $request.dpi -isnot [long]) -or @(150, 200, 300) -notcontains $request.dpi) {
            throw 'DPI must be 150, 200, or 300.'
        }
        $options.dpi = [int]$request.dpi
        $options.has_dpi = $true
    }
    if ($names -contains 'duplex') {
        if ($request.duplex -isnot [bool]) { throw 'Duplex must be a JSON boolean.' }
        $options.duplex = [bool]$request.duplex
        $options.has_duplex = $true
    }
    if ($names -contains 'max_pages') {
        if (($request.max_pages -isnot [int] -and $request.max_pages -isnot [long]) -or $request.max_pages -ne 1) {
            throw 'max_pages must be the integer 1 when supplied.'
        }
        $options.max_pages = 1
        $options.duplex = $false
        $options.has_duplex = $true
    }
    return $options
}

function Set-WiaValue($Property, [int]$Value) {
    if ($null -eq $Property) { throw 'Required scanner setting is unavailable.' }
    if ([int]$Property.Value -eq $Value) { return }
    if ($Property.IsReadOnly) { throw 'Scanner setting is read-only.' }
    $Property.Value = $Value
    if ([int]$Property.Value -ne $Value) { throw 'Scanner did not accept the requested setting.' }
}

function Set-ScanOptions($Device, $Item, $Options, [switch]$ResetBatchSettings) {
    if ($ResetBatchSettings) {
        # Native initialization may have partially changed driver state. The
        # Automation compatibility loop must explicitly restore every setting.
        $copy = @{}
        foreach ($key in $Options.Keys) { $copy[$key] = $Options[$key] }
        $Options = $copy
        $Options.has_dpi = $true
        $Options.has_duplex = $true
    }
    if ($Options.has_duplex) {
        $capabilities = Get-WiaCapabilities $Device $Item
        if ($Options.duplex -and $capabilities.duplex_supported -ne $true) {
            throw 'Driver has not confirmed duplex support.'
        }
        $select = Get-WiaProperty $Item 3088
        if ($null -eq $select) { $select = Get-WiaProperty $Device 3088 }
        if ($null -ne $select) {
            # Clear duplex/order/front/back flags, retaining acquisition source.
            $value = ([int]$select.Value -band (-bnot 1148))
            if ($Options.duplex) { $value = $value -bor 4 }
            else {
                $mask = Get-WiaFlagMask $select
                if ($null -ne $mask -and ($mask -band 32) -ne 0) { $value = $value -bor 32 }
            }
            Set-WiaValue $select $value
        } elseif ($Options.duplex) {
            throw 'Driver does not expose a duplex setting.'
        }
    }
    foreach ($origin in @(6149, 6150)) {
        $property = Get-WiaProperty $Item $origin
        if ($null -ne $property) { Set-WiaValue $property 0 }
        elseif ($ResetBatchSettings) { throw 'Cannot reset the native scan region for compatibility capture.' }
    }
    if ($Options.has_dpi) {
        if (@(Get-WiaDpiSupport $Item) -notcontains $Options.dpi) {
            throw ('Driver does not support requested DPI: ' + $Options.dpi)
        }
        Set-WiaValue (Get-WiaProperty $Item 6147) $Options.dpi
        Set-WiaValue (Get-WiaProperty $Item 6148) $Options.dpi
    }
    # Preserve the established A4 region; scale its pixels with explicit DPI.
    $width = [int][Math]::Round(210.0 / 25.4 * $Options.dpi)
    $height = [int][Math]::Round(297.0 / 25.4 * $Options.dpi)
    foreach ($setting in @(@(6151, $width), @(6152, $height))) {
        $property = Get-WiaProperty $Item $setting[0]
        if ($null -eq $property) { continue }
        $value = [int]$setting[1]
        if ([int]$property.SubType -eq 1 -and [int]$property.SubTypeMax -gt 0) {
            $value = [Math]::Min($value, [int]$property.SubTypeMax)
        }
        if ($Options.has_dpi) { Set-WiaValue $property $value }
        else { try { Set-WiaValue $property $value } catch {} }
    }
    $pages = Get-WiaProperty $Item 3096
    if ($null -eq $pages) { $pages = Get-WiaProperty $Device 3096 }
    if ($null -ne $pages) {
        try { Set-WiaValue $pages 1 }
        catch {
            if ($Options.duplex) { throw '驱动仅支持逐页兼容，无法安全获取双面，请改用单面。' }
            throw
        }
    }
    elseif ($ResetBatchSettings) { throw 'Cannot reset batch page count before compatibility capture.' }
}

function Write-ScannerHeartbeat([string]$Base, $Snapshot) {
    $Snapshot.checked_at = [DateTime]::UtcNow.ToString('o')
    $Snapshot.agent_version = 5
    if (-not $Snapshot.ContainsKey('capture_mode')) { $Snapshot.capture_mode = 'wia2-preferred' }
    $Snapshot.supports_page_rescan = $true
    $temporary = Join-Path $Base 'scanner-status.json.tmp'
    $target = Join-Path $Base 'scanner-status.json'
    $json = $Snapshot | ConvertTo-Json -Depth 5 -Compress
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $target -Force
}

function Write-BatchTerminal([string]$Base, [string]$Id, [string]$Terminal) {
    $target = Join-Path $Base ('out\' + $Id + '.status')
    [IO.File]::WriteAllText($target + '.tmp', $Terminal, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath ($target + '.tmp') -Destination $target -Force
}

function Complete-InterruptedBatch([string]$Base, [string]$Id, [string]$RequestPath) {
    $claim = Join-Path $Base ('out\' + $Id + '.started')
    $existingPages = @(Get-ChildItem "$Base\out\$Id-p*.jpg" -ErrorAction SilentlyContinue)
    if ((Test-Path -LiteralPath $claim) -or $existingPages.Count -gt 0) {
        Write-BatchTerminal $Base $Id ('error pages=' + $existingPages.Count + ': Previous agent stopped during this batch. Saved pages were retained; continue with a new batch.')
        Remove-Item -LiteralPath $RequestPath -Force -ErrorAction SilentlyContinue
        return $true
    }
    return $false
}

function New-ScanClaim([string]$Base, [string]$Id) {
    $path = Join-Path $Base ('out\' + $Id + '.started')
    # CreateNew cannot overwrite a prior claim. Flush before any transfer starts.
    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([DateTime]::UtcNow.ToString('o'))
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function Invoke-PageCapture($Options, [scriptblock]$Transfer, [scriptblock]$Publish, [scriptblock]$Heartbeat = {}) {
    # Callbacks make the production loop testable without creating WIA objects.
    $pages = 0
    $failure = ''
    while ($Options.max_pages -eq 0 -or $pages -lt $Options.max_pages) {
        $image = $null
        try {
            & $Heartbeat | Out-Null
            try { $image = & $Transfer $pages }
            catch {
                $code = Get-WiaErrorCode $_.Exception
                # Microsoft WiaDef.h: paper empty = 0x80210003. Jam (2),
                # multi-feed (20 decimal), communication, and other errors fail.
                if ($code -eq 2149646339 -and $pages -gt 0 -and $Options.max_pages -eq 0) { break }
                $failure = ('Image transfer failed (WIA 0x{0:X8}): {1}' -f $code, $_.Exception.Message)
                break
            }
            & $Publish $image ($pages + 1) | Out-Null
            # Count only JPEGs whose final publication has completed.
            $pages++
            & $Heartbeat | Out-Null
        } catch {
            $failure = 'Page publication or heartbeat failed: ' + $_.Exception.Message
            break
        } finally {
            Release-WiaObject $image
        }
    }
    $terminal = if ($failure) { 'error pages=' + $pages + ': ' + $failure } else { 'ok:' + $pages }
    return @{ pages = $pages; terminal = $terminal }
}
