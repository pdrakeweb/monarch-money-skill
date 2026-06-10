<#
.SYNOPSIS
    Package the monarch-money skill into monarch-money.zip.

.DESCRIPTION
    1. Validates the SKILL.md frontmatter `name:` equals the skill folder name.
    2. Copies the skill to a temp dir and ensures data/ ships EMPTY (no cache,
       no session pickle, no credentials).
    3. Zips it to monarch-money.zip at the repo root.
#>
$ErrorActionPreference = "Stop"
$skillDir = "monarch-money"
$outName = "monarch-money.zip"

# --- validate folder name matches SKILL.md frontmatter -----------------------
$frontmatterMatch = Select-String -Path "$skillDir\SKILL.md" -Pattern 'name:\s+"?([^"\s]+)"?'
if (-not $frontmatterMatch) {
    Write-Error "Could not read 'name:' from $skillDir\SKILL.md"
    exit 1
}
$skillName = $frontmatterMatch.Matches[0].Groups[1].Value.Trim()
if ($skillName -ne $skillDir) {
    Write-Error "SKILL.md name '$skillName' does not match folder '$skillDir'"
    exit 1
}

if (Test-Path $outName) { Remove-Item $outName -Force }

# --- copy to temp so we can guarantee data/ is empty -------------------------
$tmp = Join-Path $env:TEMP "monarch_build_$(Get-Random)"
Copy-Item $skillDir $tmp -Recurse

# Strip caches and any local secrets that must never ship.
Get-ChildItem -Path $tmp -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$dataDir = Join-Path $tmp "data"
if (Test-Path $dataDir) {
    Remove-Item "$dataDir\*" -Recurse -Force -ErrorAction SilentlyContinue
} else {
    New-Item $dataDir -ItemType Directory | Out-Null
}
# Keep an empty data/ in the archive with a placeholder.
"This directory is created at runtime to hold the SQLite cache and session." |
    Set-Content -Path (Join-Path $dataDir ".gitkeep")

Add-Type -AssemblyName System.IO.Compression.FileSystem
$dest = (Join-Path (Resolve-Path .).Path $outName)
[System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $dest)
Remove-Item $tmp -Recurse -Force

$size = (Get-Item $outName).Length
Write-Host "Built: $outName ($size bytes)"
