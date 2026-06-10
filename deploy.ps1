<#
.SYNOPSIS
    Build and deploy the monarch-money skill into Claude Desktop's local
    skills-plugin directory.

.DESCRIPTION
    Trigger phrase for Claude Code: "build and deploy this skill".

    1. Runs build.ps1 to produce monarch-money.zip (with an empty data/).
    2. Extracts the zip and validates SKILL.md (frontmatter `name:` field).
    3. Locates the Claude Desktop skills-plugin target — auto-detects the nested
       <outer-guid>\<inner-guid>\skills\<skill-name>\ layout used by Claude
       Desktop; falls back to
       %APPDATA%\Claude\local-agent-mode-sessions\skills-plugin\<skill-name>\.
    4. Backs up the existing deployed skill, keeping the last 5 backups.
    5. Mirrors source -> target with robocopy /MIR (stale files removed).
    6. Optionally restarts Claude Desktop.

.PARAMETER DryRun
    Show what would happen without writing anything.

.PARAMETER RestartClaude
    Kill Claude Desktop and relaunch it after a successful deploy.

.PARAMETER SkipBackup
    Skip the pre-deploy backup of the currently-deployed skill.

.EXAMPLE
    .\deploy.ps1 -DryRun

.EXAMPLE
    .\deploy.ps1 -RestartClaude
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$RestartClaude,
    [switch]$SkipBackup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:StartTime = Get-Date
$script:ProjectRoot = $PSScriptRoot
$script:TempDirs = New-Object System.Collections.Generic.List[string]

function Write-Step    { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Info    { param([string]$m) Write-Host "    $m" -ForegroundColor Gray }
function Write-Ok      { param([string]$m) Write-Host "    $m" -ForegroundColor Green }
function Write-Warn    { param([string]$m) Write-Host "    $m" -ForegroundColor Yellow }

function Cleanup-TempDirs {
    foreach ($d in $script:TempDirs) {
        if ($d -and (Test-Path -LiteralPath $d)) {
            try { Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction Stop }
            catch { Write-Warn "Could not remove temp dir: $d ($($_.Exception.Message))" }
        }
    }
}

# ---------------------------------------------------------------------------
# 1. Run the build
# ---------------------------------------------------------------------------
function Invoke-ProjectBuild {
    $buildPs1 = Join-Path $script:ProjectRoot 'build.ps1'
    if (-not (Test-Path -LiteralPath $buildPs1)) {
        throw "build.ps1 not found at $buildPs1"
    }
    Write-Step "Running build.ps1"
    if ($DryRun) {
        Write-Info "[dry-run] would invoke: pwsh $buildPs1"
    } else {
        $global:LASTEXITCODE = 0
        & $buildPs1
        $rc = $global:LASTEXITCODE
        if ($rc -and $rc -ne 0) { throw "build.ps1 failed with exit code $rc" }
    }
}

# ---------------------------------------------------------------------------
# 2. Resolve the build output: extract the zip; the skill source is the
#    monarch-money/ folder inside it (build.ps1 wraps a top-level folder).
# ---------------------------------------------------------------------------
function Resolve-BuildOutput {
    $zip = Join-Path $script:ProjectRoot 'monarch-money.zip'
    if (-not (Test-Path -LiteralPath $zip)) {
        if ($DryRun) {
            # Fall back to the in-tree skill dir so dry-run is meaningful.
            $inTree = Join-Path $script:ProjectRoot 'monarch-money'
            Write-Info "[dry-run] zip absent; using $inTree as proxy source"
            return $inTree
        }
        throw "monarch-money.zip not found after build."
    }

    $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("monarch-skill-deploy-" + [Guid]::NewGuid().ToString('N'))
    $script:TempDirs.Add($temp)
    if ($DryRun) {
        Write-Info "[dry-run] would extract $zip to $temp"
        return (Join-Path $script:ProjectRoot 'monarch-money')
    }
    New-Item -ItemType Directory -Path $temp -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $temp)

    # SKILL.md at extraction root, or inside a single skill folder.
    if (Test-Path -LiteralPath (Join-Path $temp 'SKILL.md')) { return $temp }
    $skillDirs = @(Get-ChildItem -LiteralPath $temp -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') })
    if ($skillDirs.Count -eq 1) { return $skillDirs[0].FullName }
    throw "Could not locate SKILL.md inside the extracted zip."
}

# ---------------------------------------------------------------------------
# 3. Validate SKILL.md and read the skill name from frontmatter
# ---------------------------------------------------------------------------
function Get-SkillName {
    param([string]$SourceDir)

    $skillMd = Join-Path $SourceDir 'SKILL.md'
    if (-not (Test-Path -LiteralPath $skillMd)) {
        throw "SKILL.md not found at source root: $skillMd"
    }

    $content = Get-Content -LiteralPath $skillMd -Raw
    if ($content -notmatch '(?s)\A---\r?\n(.*?)\r?\n---') {
        throw "SKILL.md has no YAML frontmatter block: $skillMd"
    }
    $frontmatter = $Matches[1]

    $name = $null
    foreach ($line in $frontmatter -split "`n") {
        if ($line -match '^\s*name\s*:\s*(.+?)\s*$') {
            $raw = $Matches[1].Trim()
            if (($raw.StartsWith('"') -and $raw.EndsWith('"')) -or
                ($raw.StartsWith("'") -and $raw.EndsWith("'"))) {
                $raw = $raw.Substring(1, $raw.Length - 2)
            }
            $name = $raw
            break
        }
    }
    if ([string]::IsNullOrWhiteSpace($name)) {
        throw "SKILL.md frontmatter does not contain a non-empty 'name:' field."
    }
    if ($name -notmatch '^[A-Za-z0-9._\-]+$') {
        throw "Skill name '$name' contains characters unsafe for a directory name."
    }
    return $name
}

# ---------------------------------------------------------------------------
# 4. Locate the Claude Desktop skills-plugin target directory
# ---------------------------------------------------------------------------
function Resolve-DeployTarget {
    param([string]$SkillName)

    $appdata = $env:APPDATA
    if ([string]::IsNullOrWhiteSpace($appdata)) {
        throw "APPDATA environment variable is not set."
    }
    $base = Join-Path $appdata 'Claude\local-agent-mode-sessions\skills-plugin'

    $existingHit = $null
    $anyManifestHit = $null
    if (Test-Path -LiteralPath $base -PathType Container) {
        $outers = @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne '.backups' })
        foreach ($outer in $outers) {
            $inners = @(Get-ChildItem -LiteralPath $outer.FullName -Directory -ErrorAction SilentlyContinue)
            foreach ($inner in $inners) {
                $skillsDir = Join-Path $inner.FullName 'skills'
                if (-not (Test-Path -LiteralPath $skillsDir -PathType Container)) { continue }
                if (-not $anyManifestHit) { $anyManifestHit = $skillsDir }
                $candidate = Join-Path $skillsDir $SkillName
                if (Test-Path -LiteralPath $candidate -PathType Container) {
                    $existingHit = $candidate
                    break
                }
            }
            if ($existingHit) { break }
        }
    }

    if ($existingHit) {
        Write-Info "Detected existing deployment: $existingHit"
        return [pscustomobject]@{ Path = $existingHit; Layout = 'nested-existing' }
    }
    if ($anyManifestHit) {
        $target = Join-Path $anyManifestHit $SkillName
        Write-Info "Detected nested skills-plugin layout; new skill dir: $target"
        return [pscustomobject]@{ Path = $target; Layout = 'nested-new' }
    }

    $fallback = Join-Path $base $SkillName
    Write-Warn "No nested GUID layout found under skills-plugin. Falling back to literal path: $fallback"
    Write-Warn "Claude Desktop may not pick this up automatically. Inspect the directory after deploy."
    return [pscustomobject]@{ Path = $fallback; Layout = 'literal-fallback' }
}

# ---------------------------------------------------------------------------
# 5. Backup existing deployment and prune to the last 5
# ---------------------------------------------------------------------------
function Backup-ExistingDeployment {
    param([string]$Target, [string]$SkillName)

    if ($SkipBackup) {
        Write-Info "Backup skipped (-SkipBackup)."
        return $null
    }
    if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
        Write-Info "Nothing to back up (target does not exist yet)."
        return $null
    }

    $backupRoot = Join-Path $env:APPDATA 'Claude\local-agent-mode-sessions\skills-plugin\.backups'
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = Join-Path $backupRoot ("{0}_{1}" -f $SkillName, $stamp)

    if ($DryRun) {
        Write-Info "[dry-run] would back up $Target -> $backupDir"
    } else {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        $rcArgs = @($Target, $backupDir, '/E', '/COPY:DAT', '/R:1', '/W:1', '/NFL', '/NDL', '/NP', '/NJH', '/NJS')
        & robocopy @rcArgs | Out-Null
        $rc = $LASTEXITCODE
        $global:LASTEXITCODE = 0
        if ($rc -ge 8) { throw "Backup robocopy failed with exit code $rc" }
        Write-Ok "Backup created: $backupDir"
    }

    # Prune older backups, keep last 5 for THIS skill.
    if (-not $DryRun -and (Test-Path -LiteralPath $backupRoot -PathType Container)) {
        $pattern = "$SkillName" + "_*"
        $old = @(Get-ChildItem -LiteralPath $backupRoot -Directory -Filter $pattern -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 5)
        foreach ($o in $old) {
            try {
                Remove-Item -LiteralPath $o.FullName -Recurse -Force -ErrorAction Stop
                Write-Info "Pruned old backup: $($o.Name)"
            } catch {
                Write-Warn "Could not prune backup $($o.FullName): $($_.Exception.Message)"
            }
        }
    }

    return $backupDir
}

# ---------------------------------------------------------------------------
# 6. Mirror source -> target with robocopy /MIR
# ---------------------------------------------------------------------------
function Invoke-Mirror {
    param([string]$Source, [string]$Target)

    if (-not $DryRun) {
        if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
            New-Item -ItemType Directory -Path $Target -Force | Out-Null
        }
    }

    # /XD data: never clobber the deployed skill's runtime data (session
    # pickle, SQLite cache, credentials) with the build's empty data/ dir.
    $rcArgs = @(
        $Source, $Target,
        '/MIR',
        '/XD', 'data',
        '/COPY:DAT',
        '/R:2', '/W:2',
        '/NFL', '/NDL', '/NP'
    )
    if ($DryRun) { $rcArgs += '/L' }

    Write-Step ("robocopy " + ($rcArgs -join ' '))
    $output = & robocopy @rcArgs
    $rc = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    if ($rc -ge 8) {
        Write-Host ($output -join "`n") -ForegroundColor Red
        throw "robocopy failed with exit code $rc (>=8 = failure)"
    }

    # Ensure data/ exists at the target even on first deploy (it is excluded
    # from the mirror above).
    $targetData = Join-Path $Target 'data'
    if (-not $DryRun -and -not (Test-Path -LiteralPath $targetData)) {
        New-Item -ItemType Directory -Path $targetData -Force | Out-Null
    }

    $copied = 0; $removed = 0
    foreach ($line in $output) {
        if ($line -match '^\s*Files\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$') {
            $copied  = [int]$Matches[2]
            $removed = [int]$Matches[6]
        }
    }

    return [pscustomobject]@{ ExitCode = $rc; Copied = $copied; Removed = $removed }
}

# ---------------------------------------------------------------------------
# 7. Optional: restart Claude Desktop
# ---------------------------------------------------------------------------
function Restart-ClaudeDesktop {
    Write-Step "Restarting Claude Desktop"
    $procs = @(Get-Process -Name 'Claude' -ErrorAction SilentlyContinue)
    if ($DryRun) {
        if ($procs.Count -gt 0) { Write-Info "[dry-run] would kill PID(s): $($procs.Id -join ', ')" }
        else                    { Write-Info "[dry-run] no Claude process running" }
        Write-Info "[dry-run] would relaunch Claude Desktop"
        return
    }
    if ($procs.Count -gt 0) {
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 750
        Write-Ok "Stopped Claude Desktop ($($procs.Count) process(es))"
    } else {
        Write-Info "Claude Desktop was not running."
    }

    $exe = Join-Path $env:LOCALAPPDATA 'AnthropicClaude\Claude.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        $exe = Join-Path $env:LOCALAPPDATA 'Programs\Claude\Claude.exe'
    }
    if (Test-Path -LiteralPath $exe) {
        Start-Process -FilePath $exe | Out-Null
        Write-Ok "Launched: $exe"
    } else {
        Write-Warn "Could not find Claude.exe under %LOCALAPPDATA%. Launch manually."
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    Write-Host ""
    Write-Host "=== Monarch Money Skill Deploy ===" -ForegroundColor Magenta
    if ($DryRun) { Write-Host "    (DRY RUN — no changes will be written)" -ForegroundColor Yellow }
    Write-Host ""

    Invoke-ProjectBuild
    $sourceDir = Resolve-BuildOutput
    $skillName = Get-SkillName -SourceDir $sourceDir
    Write-Info "Skill name: $skillName"

    $targetInfo = Resolve-DeployTarget -SkillName $skillName
    $targetDir  = $targetInfo.Path

    $backupPath = Backup-ExistingDeployment -Target $targetDir -SkillName $skillName

    $mirror = Invoke-Mirror -Source $sourceDir -Target $targetDir

    if ($RestartClaude) {
        Restart-ClaudeDesktop
    }

    $elapsed = (Get-Date) - $script:StartTime

    Write-Host ""
    Write-Host "=== Deploy Summary ===" -ForegroundColor Magenta
    Write-Host ("  Skill name     : {0}" -f $skillName)
    Write-Host ("  Source         : {0}" -f $sourceDir)
    Write-Host ("  Target         : {0}" -f $targetDir)
    Write-Host ("  Target layout  : {0}" -f $targetInfo.Layout)
    Write-Host ("  Files copied   : {0}  (robocopy 'Copied' — adds+modifies combined)" -f $mirror.Copied)
    Write-Host ("  Files removed  : {0}  (stale extras removed from target by /MIR)" -f $mirror.Removed)
    Write-Host ("  Backup         : {0}" -f $(if ($backupPath) { $backupPath } else { '(none)' }))
    Write-Host ("  Restart Claude : {0}" -f $(if ($RestartClaude) { 'yes' } else { 'no' }))
    Write-Host ("  Dry run        : {0}" -f $(if ($DryRun) { 'yes' } else { 'no' }))
    Write-Host ("  Elapsed        : {0:N1}s" -f $elapsed.TotalSeconds)
    Write-Host ""
    if ($DryRun) {
        Write-Host "Dry run complete. Re-run without -DryRun to apply." -ForegroundColor Yellow
    } else {
        Write-Host "Deploy complete. Restart Claude Desktop to pick up changes." -ForegroundColor Green
    }
}
catch {
    Write-Host ""
    Write-Host "DEPLOY FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Cleanup-TempDirs
    exit 1
}
finally {
    Cleanup-TempDirs
}
