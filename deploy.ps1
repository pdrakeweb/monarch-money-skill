<#
.SYNOPSIS
    Build and deploy the monarch-money skill into Claude Desktop's local
    skills-plugin directory.

.DESCRIPTION
    1. Runs build.ps1 to produce monarch-money.zip (with an empty data/).
    2. Resolves the Claude Desktop skills-plugin target — the same
       %APPDATA%\Claude\local-agent-mode-sessions\skills-plugin location used by
       ForecastSkill's deploy.ps1, auto-detecting the nested
       <outer-guid>\<inner-guid>\skills\ layout, falling back to the literal
       skills-plugin\ path.
    3. Copies monarch-money.zip into that directory.
#>
$ErrorActionPreference = "Stop"

# --- 1. build ----------------------------------------------------------------
& "$PSScriptRoot\build.ps1"
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "build.ps1 failed ($LASTEXITCODE)" }

$zip = Join-Path $PSScriptRoot "monarch-money.zip"
if (-not (Test-Path $zip)) { throw "monarch-money.zip not found after build." }

# --- 2. resolve the skills-plugin target (mirrors ForecastSkill deploy.ps1) ---
$appdata = $env:APPDATA
if ([string]::IsNullOrWhiteSpace($appdata)) { throw "APPDATA is not set." }
$base = Join-Path $appdata 'Claude\local-agent-mode-sessions\skills-plugin'

$targetDir = $null
if (Test-Path -LiteralPath $base -PathType Container) {
    $outers = @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '.backups' })
    foreach ($outer in $outers) {
        foreach ($inner in @(Get-ChildItem -LiteralPath $outer.FullName -Directory -ErrorAction SilentlyContinue)) {
            $skillsDir = Join-Path $inner.FullName 'skills'
            if (Test-Path -LiteralPath $skillsDir -PathType Container) {
                $targetDir = $skillsDir
                break
            }
        }
        if ($targetDir) { break }
    }
}
if (-not $targetDir) {
    # Fallback: literal skills-plugin directory.
    $targetDir = $base
    Write-Warning "No nested GUID skills layout found. Using literal: $targetDir"
}

if (-not (Test-Path -LiteralPath $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

# --- 3. copy the zip ---------------------------------------------------------
Copy-Item -LiteralPath $zip -Destination $targetDir -Force
Write-Host "Copied monarch-money.zip -> $targetDir"
Write-Host "Deployed. Restart Claude Desktop to pick up changes."
