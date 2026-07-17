#requires -Version 5.1

[CmdletBinding()]
param()

$script:MinimumGpt56ClientVersion = [version]'0.144.0'

function ConvertTo-CodexNumericVersion {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    if ($Value -match '(\d+)\.(\d+)\.(\d+)') {
        return [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
    }

    return $null
}

function Get-CodexEvidenceProperty {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Evidence,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $property = $Evidence.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }

    return $property.Value
}

function Get-CodexDesktopLogEvidence {
    [CmdletBinding()]
    param()

    $result = [ordered]@{
        LatestLogPath          = $null
        DesktopExecutablePath = $null
        DesktopCurrentVersion = $null
        DesktopCliPathSource  = $null
        LogReadError          = $null
    }

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $result.LogReadError = 'LOCALAPPDATA is not available.'
        return [pscustomobject]$result
    }

    $logRoot = Join-Path $env:LOCALAPPDATA 'Codex\Logs'
    if (-not (Test-Path -LiteralPath $logRoot)) {
        $result.LogReadError = "Desktop log directory was not found: $logRoot"
        return [pscustomobject]$result
    }

    try {
        $latestLog = Get-ChildItem -LiteralPath $logRoot -Recurse -File -Filter '*.log' -ErrorAction Stop |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1

        if ($null -eq $latestLog) {
            $result.LogReadError = "No desktop log files were found under: $logRoot"
            return [pscustomobject]$result
        }

        $result.LatestLogPath = $latestLog.FullName
        $lines = Get-Content -LiteralPath $latestLog.FullName -ErrorAction Stop

        foreach ($line in $lines) {
            if ($line -match 'Current reported app-server version:\s+currentVersion=([^\s]+)') {
                $result.DesktopCurrentVersion = $Matches[1]
            }

            if ($line -match 'codexCliPathSource=([^\s]+)') {
                $result.DesktopCliPathSource = $Matches[1]
            }

            if ($line -match 'stdio_transport_spawned.*?executablePath="([^"]+)"') {
                $result.DesktopExecutablePath = $Matches[1]
            }
            elseif ($line -match 'stdio_transport_spawned.*?executablePath=([^\s]+)') {
                $result.DesktopExecutablePath = $Matches[1]
            }
        }
    }
    catch {
        $result.LogReadError = $_.Exception.Message
    }

    return [pscustomobject]$result
}

function Get-CodexIndependentCliEvidence {
    [CmdletBinding()]
    param()

    $result = [ordered]@{
        IndependentCliPath    = $null
        IndependentCliVersion = $null
        IndependentCliError   = $null
    }

    try {
        $command = Get-Command 'codex' -ErrorAction Stop | Select-Object -First 1
        $result.IndependentCliPath = $command.Source

        $versionOutput = & $command.Source --version 2>&1
        $result.IndependentCliVersion = (($versionOutput | Out-String).Trim())
    }
    catch {
        $result.IndependentCliError = $_.Exception.Message
    }

    return [pscustomobject]$result
}

function Get-CodexDesktopProcessPaths {
    [CmdletBinding()]
    param()

    $paths = @()

    foreach ($process in @(Get-Process -Name 'codex' -ErrorAction SilentlyContinue)) {
        try {
            $path = $process.Path
            if (
                $path -like 'C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe' -or
                $path -like "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe"
            ) {
                $paths += $path
            }
        }
        catch {
            # Process paths can be inaccessible across integrity levels.
        }
    }

    return @($paths | Sort-Object -Unique)
}

function Get-CodexModelCacheEvidence {
    [CmdletBinding()]
    param()

    $result = [ordered]@{
        ModelCachePath          = $null
        ModelCacheClientVersion = $null
        HasGpt56Sol             = $false
        ModelSlugs              = @()
        ModelCacheReadError     = $null
    }

    $cachePath = Join-Path $HOME '.codex\models_cache.json'
    $result.ModelCachePath = $cachePath

    if (-not (Test-Path -LiteralPath $cachePath)) {
        $result.ModelCacheReadError = "Model cache was not found: $cachePath"
        return [pscustomobject]$result
    }

    try {
        $cache = Get-Content -LiteralPath $cachePath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $result.ModelCacheClientVersion = $cache.client_version
        $result.ModelSlugs = @($cache.models | ForEach-Object { $_.slug })
        $result.HasGpt56Sol = $result.ModelSlugs -contains 'gpt-5.6-sol'
    }
    catch {
        $result.ModelCacheReadError = $_.Exception.Message
    }

    return [pscustomobject]$result
}

function Get-CodexAppPackageEvidence {
    [CmdletBinding()]
    param()

    $result = [ordered]@{
        AppPackageVersion         = $null
        AppPackageInstallLocation = $null
        AppPackageSource          = $null
        AppPackageError           = $null
    }

    try {
        $package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop |
            Sort-Object Version -Descending |
            Select-Object -First 1

        if ($null -ne $package) {
            $result.AppPackageVersion = $package.Version.ToString()
            $result.AppPackageInstallLocation = $package.InstallLocation
            $result.AppPackageSource = 'current-powershell'
            return [pscustomobject]$result
        }
    }
    catch {
        $result.AppPackageError = $_.Exception.Message
    }

    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $windowsPowerShell)) {
        return [pscustomobject]$result
    }

    try {
        $query = '$package = Get-AppxPackage -Name ''OpenAI.Codex'' -ErrorAction Stop | Sort-Object Version -Descending | Select-Object -First 1; if ($null -ne $package) { [pscustomobject]@{ Version = $package.Version.ToString(); InstallLocation = $package.InstallLocation } | ConvertTo-Json -Compress }'
        $output = & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -Command $query 2>&1
        $exitCode = $LASTEXITCODE
        $outputText = (($output | Out-String).Trim())

        if ($exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($outputText)) {
            $fallbackPackage = $outputText | ConvertFrom-Json -ErrorAction Stop
            $result.AppPackageVersion = $fallbackPackage.Version
            $result.AppPackageInstallLocation = $fallbackPackage.InstallLocation
            $result.AppPackageSource = 'windows-powershell-5.1'
            $result.AppPackageError = $null
        }
        elseif ($exitCode -ne 0) {
            $result.AppPackageError = $outputText
        }
    }
    catch {
        $result.AppPackageError = $_.Exception.Message
    }

    return [pscustomobject]$result
}

function Get-CodexDesktopEvidence {
    [CmdletBinding()]
    param()

    $userOverride = [Environment]::GetEnvironmentVariable('CODEX_CLI_PATH', 'User')
    $machineOverride = [Environment]::GetEnvironmentVariable('CODEX_CLI_PATH', 'Machine')

    $userOverrideExists = $null
    if (-not [string]::IsNullOrWhiteSpace($userOverride)) {
        $userOverrideExists = Test-Path -LiteralPath $userOverride
    }

    $machineOverrideExists = $null
    if (-not [string]::IsNullOrWhiteSpace($machineOverride)) {
        $machineOverrideExists = Test-Path -LiteralPath $machineOverride
    }

    $package = Get-CodexAppPackageEvidence
    $log = Get-CodexDesktopLogEvidence
    $cli = Get-CodexIndependentCliEvidence
    $cache = Get-CodexModelCacheEvidence

    return [pscustomobject][ordered]@{
        CollectedAtUtc           = [DateTime]::UtcNow.ToString('o')
        MinimumGpt56Version      = $script:MinimumGpt56ClientVersion.ToString()
        AppPackageVersion        = $package.AppPackageVersion
        AppPackageInstallLocation = $package.AppPackageInstallLocation
        AppPackageSource         = $package.AppPackageSource
        AppPackageError          = $package.AppPackageError
        IndependentCliPath       = $cli.IndependentCliPath
        IndependentCliVersion    = $cli.IndependentCliVersion
        IndependentCliError      = $cli.IndependentCliError
        UserOverride             = $userOverride
        UserOverrideExists       = $userOverrideExists
        MachineOverride          = $machineOverride
        MachineOverrideExists    = $machineOverrideExists
        DesktopProcessPaths      = @(Get-CodexDesktopProcessPaths)
        LatestLogPath            = $log.LatestLogPath
        DesktopExecutablePath    = $log.DesktopExecutablePath
        DesktopCurrentVersion    = $log.DesktopCurrentVersion
        DesktopCliPathSource     = $log.DesktopCliPathSource
        LogReadError             = $log.LogReadError
        ModelCachePath           = $cache.ModelCachePath
        ModelCacheClientVersion  = $cache.ModelCacheClientVersion
        HasGpt56Sol              = $cache.HasGpt56Sol
        ModelSlugs               = @($cache.ModelSlugs)
        ModelCacheReadError      = $cache.ModelCacheReadError
    }
}

function Get-CodexDesktopVerdict {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Evidence
    )

    $minimumVersionValue = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'MinimumGpt56Version'
    $minimumVersion = ConvertTo-CodexNumericVersion $minimumVersionValue
    if ($null -eq $minimumVersion) {
        $minimumVersion = $script:MinimumGpt56ClientVersion
    }

    $userOverride = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'UserOverride'
    $machineOverride = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'MachineOverride'
    $userOverrideExists = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'UserOverrideExists'
    $machineOverrideExists = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'MachineOverrideExists'

    $effectiveOverride = $null
    $effectiveOverrideExists = $null
    $effectiveOverrideScope = $null

    if (-not [string]::IsNullOrWhiteSpace($userOverride)) {
        $effectiveOverride = $userOverride
        $effectiveOverrideExists = $userOverrideExists
        $effectiveOverrideScope = 'User'
    }
    elseif (-not [string]::IsNullOrWhiteSpace($machineOverride)) {
        $effectiveOverride = $machineOverride
        $effectiveOverrideExists = $machineOverrideExists
        $effectiveOverrideScope = 'Machine'
    }

    $desktopVersionText = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'DesktopCurrentVersion'
    $desktopVersion = ConvertTo-CodexNumericVersion $desktopVersionText
    $desktopBelowMinimum = $null -ne $desktopVersion -and $desktopVersion -lt $minimumVersion

    $pathSource = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'DesktopCliPathSource'
    $sourceIsOverride = $pathSource -eq 'env-override'
    $effectivePathBroken = $null -ne $effectiveOverrideExists -and -not [bool]$effectiveOverrideExists
    $hasSol = [bool](Get-CodexEvidenceProperty -Evidence $Evidence -Name 'HasGpt56Sol')
    $appPackageVersion = Get-CodexEvidenceProperty -Evidence $Evidence -Name 'AppPackageVersion'

    $notes = New-Object System.Collections.Generic.List[string]
    $nextSteps = New-Object System.Collections.Generic.List[string]

    if ($null -ne $effectiveOverride) {
        $notes.Add("Effective CODEX_CLI_PATH scope: $effectiveOverrideScope")
        $notes.Add("Effective CODEX_CLI_PATH value: $effectiveOverride")
    }

    if ($sourceIsOverride) {
        $notes.Add('The desktop log reports codexCliPathSource=env-override.')
    }

    if ($desktopBelowMinimum) {
        $notes.Add("Desktop app-server $desktopVersionText is below GPT-5.6 minimum $minimumVersion.")
    }

    if ($effectivePathBroken) {
        $notes.Add('The effective CODEX_CLI_PATH does not exist.')
    }

    if ($hasSol) {
        $notes.Add('The model cache contains gpt-5.6-sol. The cache is supporting evidence, not proof by itself.')
    }

    if (
        $effectivePathBroken -or
        ($sourceIsOverride -and $desktopBelowMinimum) -or
        ($null -ne $effectiveOverride -and $desktopBelowMinimum)
    ) {
        $verdict = 'LIKELY_OVERRIDE_ISSUE'
        $exitCode = 1
        $summary = 'A CODEX_CLI_PATH override is missing or is forcing a desktop runtime below the GPT-5.6 minimum.'
        $nextSteps.Add('Fully exit the ChatGPT/Codex desktop app.')
        $nextSteps.Add('Review and remove stale User and Machine CODEX_CLI_PATH values.')
        $nextSteps.Add('Restart Windows, open ChatGPT Codex mode, and run this check again.')
    }
    elseif ($null -ne $desktopVersion -and $desktopVersion -ge $minimumVersion -and $hasSol) {
        $verdict = 'READY_FOR_GPT56'
        $exitCode = 0
        $summary = 'The observed desktop runtime meets the GPT-5.6 minimum and the model cache contains gpt-5.6-sol.'

        if ($sourceIsOverride) {
            $nextSteps.Add('GPT-5.6 appears ready, but consider removing the override so the desktop app can track its bundled runtime.')
        }
        else {
            $nextSteps.Add('Open ChatGPT Codex mode and retry with GPT-5.6 Sol.')
        }
    }
    else {
        $verdict = 'INCONCLUSIVE'
        $exitCode = 2
        $summary = 'The available evidence does not prove a stale override or a GPT-5.6-ready desktop runtime.'

        if ([string]::IsNullOrWhiteSpace($appPackageVersion)) {
            $nextSteps.Add('Install or update the official ChatGPT/Codex desktop app.')
        }

        if ($null -eq $desktopVersion) {
            $nextSteps.Add('Open the desktop app once, wait for it to initialize, then run this check again.')
        }
        elseif ($desktopBelowMinimum) {
            $nextSteps.Add('Update the desktop app because the observed app-server is below 0.144.0.')
        }

        if (-not $hasSol) {
            $nextSteps.Add('Verify ChatGPT Codex mode, the approved account, and the approved Codex workspace.')
        }
    }

    return [pscustomobject][ordered]@{
        Verdict   = $verdict
        ExitCode  = $exitCode
        Summary   = $summary
        Notes     = @($notes)
        NextSteps = @($nextSteps)
    }
}

function Format-CodexDisplayValue {
    [CmdletBinding()]
    param(
        [AllowNull()]
        $Value,

        [string]$EmptyText = '[not set]'
    )

    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return $EmptyText
    }

    return [string]$Value
}

function Write-CodexDesktopReport {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Evidence,

        [Parameter(Mandatory = $true)]
        [psobject]$Diagnosis
    )

    Write-Host ''
    Write-Host 'Codex Desktop Runtime Check'
    Write-Host '==========================='
    Write-Host ("Collected (UTC)       : {0}" -f $Evidence.CollectedAtUtc)
    Write-Host ("GPT-5.6 minimum       : {0}" -f $Evidence.MinimumGpt56Version)
    Write-Host ''

    Write-Host 'Installed clients'
    Write-Host ("  Desktop package     : {0}" -f (Format-CodexDisplayValue $Evidence.AppPackageVersion '[not found]'))
    Write-Host ("  Independent CLI     : {0}" -f (Format-CodexDisplayValue $Evidence.IndependentCliVersion '[not found]'))
    Write-Host ("  Independent CLI path: {0}" -f (Format-CodexDisplayValue $Evidence.IndependentCliPath '[not found]'))
    Write-Host ''

    Write-Host 'CODEX_CLI_PATH overrides'
    Write-Host ("  User value          : {0}" -f (Format-CodexDisplayValue $Evidence.UserOverride))
    Write-Host ("  User path exists    : {0}" -f (Format-CodexDisplayValue $Evidence.UserOverrideExists '[n/a]'))
    Write-Host ("  Machine value       : {0}" -f (Format-CodexDisplayValue $Evidence.MachineOverride))
    Write-Host ("  Machine path exists : {0}" -f (Format-CodexDisplayValue $Evidence.MachineOverrideExists '[n/a]'))
    Write-Host ''

    Write-Host 'Desktop runtime evidence'
    Write-Host ("  Current version     : {0}" -f (Format-CodexDisplayValue $Evidence.DesktopCurrentVersion '[not observed]'))
    Write-Host ("  CLI path source     : {0}" -f (Format-CodexDisplayValue $Evidence.DesktopCliPathSource '[not observed]'))
    Write-Host ("  Executable path     : {0}" -f (Format-CodexDisplayValue $Evidence.DesktopExecutablePath '[not observed]'))
    Write-Host ("  Latest log          : {0}" -f (Format-CodexDisplayValue $Evidence.LatestLogPath '[not found]'))

    foreach ($path in @($Evidence.DesktopProcessPaths)) {
        Write-Host ("  Running process     : {0}" -f $path)
    }

    Write-Host ''
    Write-Host 'Model cache evidence'
    Write-Host ("  Cache client version: {0}" -f (Format-CodexDisplayValue $Evidence.ModelCacheClientVersion '[not found]'))
    Write-Host ("  Contains GPT-5.6 Sol: {0}" -f $Evidence.HasGpt56Sol)
    Write-Host ("  Models              : {0}" -f (@($Evidence.ModelSlugs) -join ', '))
    Write-Host ''

    Write-Host ("Verdict: {0} (exit code {1})" -f $Diagnosis.Verdict, $Diagnosis.ExitCode)
    Write-Host $Diagnosis.Summary

    if (@($Diagnosis.Notes).Count -gt 0) {
        Write-Host ''
        Write-Host 'Evidence notes:'
        foreach ($note in @($Diagnosis.Notes)) {
            Write-Host ("  - {0}" -f $note)
        }
    }

    if (@($Diagnosis.NextSteps).Count -gt 0) {
        Write-Host ''
        Write-Host 'Next steps:'
        foreach ($step in @($Diagnosis.NextSteps)) {
            Write-Host ("  - {0}" -f $step)
        }
    }

    Write-Host ''
}

function Invoke-CodexDesktopRuntimeCheck {
    [CmdletBinding()]
    param()

    $evidence = Get-CodexDesktopEvidence
    $diagnosis = Get-CodexDesktopVerdict -Evidence $evidence
    Write-CodexDesktopReport -Evidence $evidence -Diagnosis $diagnosis
    return $diagnosis.ExitCode
}

if ($MyInvocation.InvocationName -ne '.') {
    $resultCode = Invoke-CodexDesktopRuntimeCheck
    exit $resultCode
}
