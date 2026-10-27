# scripts/sync-config.ps1
#
# Copy the repo's opencode/ config files to the global config directory.
# The global ~/.config/opencode is an independent copy (not a symlink), so the
# repo is the source of truth: edits must be synced here to take effect.
#
# Usage:
#   .\scripts\sync-config.ps1                           # default: repo's opencode/ dir
#   .\scripts\sync-config.ps1 -Src "D:\path\to\opencode"
#   .\scripts\sync-config.ps1 -Destination "D:\path\to\config"  # override target dir (for testing)
#   .\scripts\sync-config.ps1 -WhatIf                   # preview the plan: no files written, no deletions performed

param(
    [string]$Src = "",
    [string]$Destination = "",
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($Src)) {
    $Src = Join-Path (Split-Path -Parent $PSScriptRoot) "opencode"
}
$Src = $Src.TrimEnd('\')

if ([string]::IsNullOrEmpty($Destination)) {
    $dst = Join-Path $env:USERPROFILE ".config\opencode"
} else {
    $dst = $Destination
}
$dst = $dst.TrimEnd('\')

if (-not (Test-Path -LiteralPath $Src -PathType Container)) {
    Write-Error -Message "Source directory not found: $Src" -ErrorAction Continue
    exit 1
}

# Resolve $Src to its canonical full path. $_.FullName is always absolute, so
# the substring arithmetic below breaks if $Src was passed as a relative path
# (or an 8.3 short name).
$Src = (Resolve-Path -LiteralPath $Src).Path

# Copy every file except node_modules and package manifests: plugin
# dependencies and lockfiles belong to the global install, not the repo.
$excludedFromSync = '(^|[\\/])node_modules([\\/]|$)|(^|[\\/])\.git([\\/]|$)|(package(-lock)?\.json|bun\.lockb?|pnpm-lock\.yaml)$'
$filesToCopy = @(Get-ChildItem -Recurse -File -LiteralPath $Src | Where-Object {
    $rel = $_.FullName.Substring($Src.Length + 1)
    $rel -notmatch $excludedFromSync
})

if ($WhatIf) {
    Write-Host "WhatIf: no files written -- would copy $($filesToCopy.Count) file(s) $Src -> $dst"
    $filesToCopy | ForEach-Object { Write-Host ("  " + $_.FullName.Substring($Src.Length + 1)) }
} else {
    foreach ($file in $filesToCopy) {
        $rel = $file.FullName.Substring($Src.Length + 1)
        $target = Join-Path $dst $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }
}

# Resolve $dst to its canonical full path. Get-ChildItem reports the resolved
# long path (e.g. C:\Users\Administrator\...), while a caller may pass an 8.3
# short name (e.g. C:\Users\ADMINI~1\...) as -Destination; without this the
# substring arithmetic below would miscompute relative paths. A real run has
# already created $dst by this point; under -WhatIf it may still be absent, and
# the deletion scan below is then skipped as a clean no-op.
if (Test-Path -LiteralPath $dst -PathType Container) {
    $dst = (Get-Item -LiteralPath $dst).FullName
} else {
    # -WhatIf leaves $dst absent; normalize it as a pure string so the final
    # message is absolute. No disk access, no directory creation.
    $dst = [System.IO.Path]::GetFullPath($dst)
}

# Delete reconciliation: remove target files the repo used to manage but no
# longer does (e.g. skills deleted from the repo). The whole target tree is
# scanned except node_modules and package manifests -- those belong to the
# global install; anything the repo never tracked is left untouched.
#
# $managed = union of (currently tracked paths) and (every path that ever
# appeared in a commit on HEAD). The history log is deliberately unfiltered by
# diff status: with `--diff-filter=D` a deletion that is staged but not yet
# committed is in no commit's diff, so the stale copy would survive the sync
# (that is how a removed dcp.jsonc could linger in the global config dir).
# Every path ever added is "managed", and the delete check below is purely
# "source lacks it".
#
# The log walks HEAD only (no `--all`): files that exist solely on another
# branch are not part of the synced tree, so counting them as managed would
# delete the live copies in the destination.
#
# Paths are relative to the repo's opencode/ dir, with separators normalized
# to `/`.
$repoRoot = Split-Path -Parent $PSScriptRoot
# File copies already happened, so a git failure here must not abort the run:
# downgrade to Continue for this block only. A failed git yields an empty
# $managed, which means no stale file is deleted, and the script exits normally.
$managed = @()
$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $managed = @(
        git -C $repoRoot ls-files -- opencode 2>$null
        git -C $repoRoot log --name-only --pretty=format: -- opencode 2>$null
    ) | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' } |
        ForEach-Object { ($_ -replace '^opencode/', '') -replace '\\', '/' } |
        Sort-Object -Unique
} catch {
    $managed = @()
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
}

$toDelete = @()
if (Test-Path -LiteralPath $dst -PathType Container) {
    Get-ChildItem -Recurse -File -Force -LiteralPath $dst | ForEach-Object {
        $rel = ($_.FullName.Substring($dst.Length + 1)) -replace '\\', '/'
        if ($rel -match $excludedFromSync) { return }
        if ($managed -notcontains $rel) { return }
        $srcPath = Join-Path $Src ($rel -replace '/', '\')
        if (-not (Test-Path -LiteralPath $srcPath -PathType Leaf)) {
            $toDelete += $rel
        }
    }
}

if ($toDelete.Count -eq 0) {
    Write-Host "No stale files to remove."
} elseif ($WhatIf) {
    Write-Host "WhatIf: no deletions performed -- would remove $($toDelete.Count) stale file(s):"
    $toDelete | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "Stale files to remove:"
    $toDelete | ForEach-Object { Write-Host "  $_" }
    foreach ($rel in $toDelete) {
        Remove-Item -LiteralPath (Join-Path $dst ($rel -replace '/', '\')) -Force
        Write-Host "Removed stale: $rel"
    }
}

if ($WhatIf) {
    Write-Host "WhatIf: no files written, no deletions performed. $Src -> $dst"
} else {
    Write-Host "Synced $Src -> $dst"
}
