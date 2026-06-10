<#
.SYNOPSIS
    Package the skill into <skill-name>.zip (claude.ai upload format).

.DESCRIPTION
    SHARED SKELETON FILE — generic on purpose; it auto-discovers the single
    SKILL.md-bearing directory, so it needs no per-skill edits and skeleton
    updates merge cleanly.

    1. Discovers the skill dir (exactly one top-level dir containing SKILL.md).
    2. Validates SKILL.md frontmatter `name:` equals the folder name.
    3. Validates frontmatter `version:` matches SKILL_VERSION in
       scripts/skill_schema.py (or scripts/*.py fallback).
    4. Stages a copy with an EMPTY data/ (no cache, sessions, credentials),
       strips __pycache__, zips with the top-level skill folder included.
#>
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

Add-Type -AssemblyName System.IO.Compression.FileSystem
$dest = (Join-Path (Resolve-Path .).Path $outName)
[System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $dest)
Remove-Item $tmp -Recurse -Force

$size = (Get-Item $outName).Length
Write-Host "Built: $outName v$mdVer ($size bytes)"
