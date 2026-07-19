#requires -Version 3.0
<#
.SYNOPSIS
    Install the codex-agent-loop-orchestrator skill into the local Codex skills directory.
.DESCRIPTION
    Copies the skill folder that ships beside this script into
    %USERPROFILE%\.codex\skills\codex-agent-loop-orchestrator, overwriting any
    previous install. Idempotent: running it again refreshes the installed copy
    so it never lags the source. No third-party modules required.
#>
[CmdletBinding()]
param(
    [string]$SkillsDir
)

$ErrorActionPreference = 'Stop'
$SkillName = 'codex-agent-loop-orchestrator'

# Resolve the skills directory. Precedence: explicit -SkillsDir > CODEX_SKILLS_DIR
# > CODEX_HOME\skills > %USERPROFILE%\.codex\skills. (CODEX_SKILLS_DIR support is
# new here, added for parity with install.sh, which has honored it all along.)
if (-not $SkillsDir) {
    if ($env:CODEX_SKILLS_DIR) {
        $SkillsDir = $env:CODEX_SKILLS_DIR
    } elseif ($env:CODEX_HOME) {
        $SkillsDir = Join-Path $env:CODEX_HOME 'skills'
    } else {
        $SkillsDir = Join-Path $env:USERPROFILE '.codex\skills'
    }
}

# Probe for a Python >= 3.9 launcher (the skill's scripts require it). This is
# a warning, not a gate: the copy still proceeds so docs-only hosts install fine.
$PythonLauncher = $null
$PyCheck = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = $cand[0]
    $extra = @($cand | Select-Object -Skip 1)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        # Scope EAP to Continue: under Stop, redirected native stderr throws in PS 5.1.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $null = & $exe @extra -c $PyCheck 2>$null
        $probeOk = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $prevEAP
        if ($probeOk) {
            $PythonLauncher = ($cand -join ' ')
            break
        }
    }
}
if ($PythonLauncher) {
    Write-Host "Found Python launcher: $PythonLauncher"
} else {
    Write-Warning "No Python 3.9+ launcher found (tried: py -3, python, python3)."
    Write-Warning "The skill's scripts require Python 3.9+; installing the files anyway."
}

# Resolve the script directory (repo root) and locate the skill source folder.
# The skill ships at skills/<name>/ (plugin layout: repo root is the plugin root).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Candidates = @(
    (Join-Path $ScriptDir (Join-Path 'skills' $SkillName)),          # plugin layout (skills/ at repo root)
    (Join-Path $ScriptDir (Join-Path 'plugin\skills' $SkillName)),   # legacy nested-plugin layout
    (Join-Path $ScriptDir $SkillName)                                # flat layout
)
$Source = $null
foreach ($c in $Candidates) {
    if (Test-Path -LiteralPath $c) { $Source = $c; break }
}
if (-not $Source) {
    if (Test-Path -LiteralPath (Join-Path $ScriptDir 'SKILL.md')) {
        # Allow running the installer from inside the skill folder itself.
        $Source = $ScriptDir
    } else {
        Write-Error "Cannot find skill source folder '$SkillName' under skills/, plugin/skills/, or next to install.ps1 (looked in $ScriptDir)."
        exit 1
    }
}

$Source = (Resolve-Path -LiteralPath $Source).Path
$Target = Join-Path $SkillsDir $SkillName

# Create the skills directory if needed.
if (-not (Test-Path -LiteralPath $SkillsDir)) {
    New-Item -ItemType Directory -Path $SkillsDir -Force | Out-Null
}

# Self-delete guard: refuse when the install target resolves to (or overlaps)
# the skill source — the Remove-Item below would destroy the source itself.
$SkillsReal = (Resolve-Path -LiteralPath $SkillsDir).Path
$TargetReal = Join-Path $SkillsReal $SkillName
if (Test-Path -LiteralPath $TargetReal) {
    $TargetReal = (Resolve-Path -LiteralPath $TargetReal).Path
}
$sep = [System.IO.Path]::DirectorySeparatorChar
$srcCmp = $Source.TrimEnd('\', '/') + $sep
$tgtCmp = $TargetReal.TrimEnd('\', '/') + $sep
$overlap = $tgtCmp.StartsWith($srcCmp, [System.StringComparison]::OrdinalIgnoreCase) -or
           $srcCmp.StartsWith($tgtCmp, [System.StringComparison]::OrdinalIgnoreCase)
if ($overlap) {
    Write-Error "Refusing to install: the skills dir cannot point into the repo's own skills/ folder (target '$TargetReal' overlaps the skill source '$Source'; installing would delete the source)."
    exit 1
}

# Remove any previous install so stale files never linger, then copy fresh.
if (Test-Path -LiteralPath $Target) {
    Remove-Item -LiteralPath $Target -Recurse -Force
}
New-Item -ItemType Directory -Path $Target -Force | Out-Null
Copy-Item -Path (Join-Path $Source '*') -Destination $Target -Recurse -Force

Write-Host "Installed $SkillName -> $Target"
exit 0
