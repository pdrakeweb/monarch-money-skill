<#
.SYNOPSIS
    Package the skill into <skill-name>.zip (claude.ai upload format).

.DESCRIPTION
    SHARED SKELETON FILE - generic on purpose; it auto-discovers the single
    SKILL.md-bearing directory, so it needs no per-skill edits and skeleton
    updates merge cleanly.

    1. Discovers the skill dir (exactly one top-level dir containing SKILL.md).
    2. Validates SKILL.md frontmatter `name:` equals the folder name.
    3. Validates frontmatter `version:` matches SKILL_VERSION in
       scripts/skill_schema.py (or scripts/*.py fallback).
    4. Stages a copy with an EMPTY data/ (no cache, sessions, credentials),
       strips __pycache__, zips with the top-level skill folder included.

.PARAMETER IncludeAuth
    OPT-IN: bundle auth material (session.pickle, credentials.json,
    cookie_session.json, .env) into data/bootstrap/ inside the zip, sourced
    from the persistent data dir (~\.claude-skills\<name>\) or the repo's
    data\. New sessions on ANY device bootstrap auth from these files - the
    package (including the secrets) is uploaded to claude.ai, so only use
    this when cross-device auth is worth that trade-off.
#>
param([switch]$IncludeAuth)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- discover the skill directory --------------------------------------------
$skillDirs = @(Get-ChildItem -Directory | Where-Object {
    Test-Path (Join-Path $_.FullName "SKILL.md") })
if ($skillDirs.Count -ne 1) {
    Write-Error "Expected exactly one SKILL.md-bearing directory; found $($skillDirs.Count)."
    exit 1
}
$skillDir = $skillDirs[0].Name
$outName = "$skillDir.zip"

# --- validate folder name matches SKILL.md frontmatter -----------------------
$nameMatch = Select-String -Path "$skillDir\SKILL.md" -Pattern '^name:\s+"?([^"\s]+)"?'
if (-not $nameMatch) { Write-Error "Could not read 'name:' from $skillDir\SKILL.md"; exit 1 }
$skillName = $nameMatch.Matches[0].Groups[1].Value.Trim()
if ($skillName -ne $skillDir) {
    Write-Error "SKILL.md name '$skillName' does not match folder '$skillDir'"
    exit 1
}
# Anthropic skill-name rules: <=64 chars, lowercase/digits/hyphens only, and
# no reserved words. Catch violations at build time, not at upload-rejection.
if ($skillName.Length -gt 64) { Write-Error "Skill name exceeds 64 chars."; exit 1 }
if ($skillName -cnotmatch '^[a-z0-9-]+$') {
    Write-Error "Skill name '$skillName' must be lowercase letters, digits, hyphens only."
    exit 1
}
if ($skillName -match '(?i)\b(claude|anthropic)\b') {
    Write-Error "Skill name '$skillName' uses a reserved word (claude/anthropic)."
    exit 1
}
# description must be present and <=1024 chars.
$descMatch = Select-String -Path "$skillDir\SKILL.md" -Pattern '^description:\s*(.*)$'
$descRest = ""
if ($descMatch) {
    # Folded scalar: gather indented continuation lines for the length check.
    $fmLines = (Get-Content "$skillDir\SKILL.md")
    $inDesc = $false
    foreach ($l in $fmLines) {
        if ($l -match '^description:\s*(.*)$') { $inDesc = $true; $descRest += $Matches[1]; continue }
        if ($inDesc) {
            if ($l -match '^\s+\S') { $descRest += " " + $l.Trim() } else { break }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($descRest.Replace('>', '').Trim())) {
    Write-Error "SKILL.md frontmatter description is empty."
    exit 1
}
if ($descRest.Length -gt 1024) {
    Write-Error "SKILL.md description exceeds 1024 chars ($($descRest.Length))."
    exit 1
}

# --- validate frontmatter version matches SKILL_VERSION constant -------------
$mdVerMatch = Select-String -Path "$skillDir\SKILL.md" -Pattern '^version:\s+"?([0-9]+\.[0-9]+\.[0-9]+)"?'
$pyVerMatch = Get-ChildItem "$skillDir\scripts\*.py" |
    Select-String -Pattern 'SKILL_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"' |
    Select-Object -First 1
if (-not $mdVerMatch -or -not $pyVerMatch) {
    Write-Error "Missing version: in SKILL.md frontmatter or SKILL_VERSION in scripts/*.py"
    exit 1
}
$mdVer = $mdVerMatch.Matches[0].Groups[1].Value
$pyVer = $pyVerMatch.Matches[0].Groups[1].Value
if ($mdVer -ne $pyVer) {
    Write-Error "Version mismatch: SKILL.md says $mdVer, scripts say $pyVer. Bump both (see CLAUDE.md)."
    exit 1
}

if (Test-Path $outName) { Remove-Item $outName -Force }

# --- stage to temp so data/ ships empty ---------------------------------------
$tmp = Join-Path $env:TEMP "skill_build_$(Get-Random)"
New-Item $tmp -ItemType Directory | Out-Null
$stage = Join-Path $tmp $skillDir
Copy-Item $skillDir $stage -Recurse

Get-ChildItem -Path $stage -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$dataDir = Join-Path $stage "data"
if (Test-Path $dataDir) {
    Remove-Item "$dataDir\*" -Recurse -Force -ErrorAction SilentlyContinue
} else {
    New-Item $dataDir -ItemType Directory | Out-Null
}
"This directory is created at runtime (cache, sessions). Ships empty." |
    Set-Content -Path (Join-Path $dataDir ".gitkeep")

# --- opt-in: bundle auth material for cross-device bootstrap ------------------
if ($IncludeAuth) {
    $authFiles = @("session.pickle", "credentials.json", "cookie_session.json", ".env")
    $sources = @(
        (Join-Path $env:USERPROFILE ".claude-skills\$skillName"),
        (Join-Path $PSScriptRoot "$skillDir\data")
    )
    $bootstrap = Join-Path $dataDir "bootstrap"
    New-Item $bootstrap -ItemType Directory | Out-Null
    $bundled = @()
    foreach ($f in $authFiles) {
        foreach ($src in $sources) {
            $p = Join-Path $src $f
            if (Test-Path $p) { Copy-Item $p $bootstrap; $bundled += $f; break }
        }
    }
    if ($bundled.Count -eq 0) {
        Write-Error "-IncludeAuth set but no auth files found in: $($sources -join '; ')"
        exit 1
    }
    Write-Host "! AUTH BUNDLED into the zip: $($bundled -join ', ')" -ForegroundColor Yellow
    Write-Host "  These secrets ship inside the package (uploaded to claude.ai)." -ForegroundColor Yellow
    Write-Host "  New sessions on any device will bootstrap auth from them." -ForegroundColor Yellow
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$dest = (Join-Path (Resolve-Path .).Path $outName)
[System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $dest)
Remove-Item $tmp -Recurse -Force

$size = (Get-Item $outName).Length
Write-Host "Built: $outName v$mdVer ($size bytes)"
