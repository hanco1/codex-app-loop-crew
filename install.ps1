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
    [string]$SkillsDir = (Join-Path $env:USERPROFILE '.codex\skills')
)

$ErrorActionPreference = 'Stop'
$SkillName = 'codex-agent-loop-orchestrator'

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

# Remove any previous install so stale files never linger, then copy fresh.
if (Test-Path -LiteralPath $Target) {
    Remove-Item -LiteralPath $Target -Recurse -Force
}
New-Item -ItemType Directory -Path $Target -Force | Out-Null
Copy-Item -Path (Join-Path $Source '*') -Destination $Target -Recurse -Force

Write-Host "Installed $SkillName -> $Target"
exit 0
