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
#   .\scripts\sync-config.ps1 -WhatIf                   # preview stale-file deletions without removing

param(
    [string]$Src = "",
    [string]$Destination = "",
    [switch]$WhatIf
)

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
    Write-Error "Source directory not found: $Src"
    exit 1
}

# Resolve $Src to its canonical full path. $_.FullName is always absolute, so
# the substring arithmetic below breaks if $Src was passed as a relative path
# (or an 8.3 short name).
$Src = (Resolve-Path -LiteralPath $Src).Path

# Copy every file except node_modules and package manifests: plugin
# dependencies and lockfiles belong to the global install, not the repo.
$excludedFromSync = '(^|[\\/])node_modules([\\/]|$)|(^|[\\/])\.git([\\/]|$)|(package(-lock)?\.json|bun\.lockb?|pnpm-lock\.yaml)$'
Get-ChildItem -Recurse -File -LiteralPath $Src | Where-Object {
    $rel = $_.FullName.Substring($Src.Length + 1)
    $rel -notmatch $excludedFromSync
} | ForEach-Object {
    $rel = $_.FullName.Substring($Src.Length + 1)
    $target = Join-Path $dst $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
}

# Resolve $dst to its canonical full path. Get-ChildItem reports the resolved
# long path (e.g. C:\Users\Administrator\...), while a caller may pass an 8.3
# short name (e.g. C:\Users\ADMINI~1\...) as -Destination; without this the
# substring arithmetic below would miscompute relative paths. The copy step has
# already created $dst by this point.
if (Test-Path -LiteralPath $dst -PathType Container) {
    $dst = (Get-Item -LiteralPath $dst).FullName
}

# Delete reconciliation: remove target files the repo used to manage but no
# longer does (e.g. skills deleted from the repo). The whole target tree is
# scanned except node_modules and package manifests -- those belong to the
# global install; anything the repo never tracked is left untouched.
#
# $managed = union of (currently tracked paths) and (every path that ever
# appeared in a commit). The history log is deliberately unfiltered: with
# `--diff-filter=D` a deletion that is staged but not yet committed is in no
# commit's diff, so the stale copy would survive the sync (that is how a
# removed dcp.jsonc could linger in the global config dir). Every path ever
# added is "managed", and the delete check below is purely "source lacks it".
#
# Paths are relative to the repo's opencode/ dir, with separators normalized
# to `/`.
$repoRoot = Split-Path -Parent $PSScriptRoot
$managed = @(
    git -C $repoRoot ls-files -- opencode
    git -C $repoRoot log --all --name-only --pretty=format: -- opencode
) | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' } |
    ForEach-Object { ($_ -replace '^opencode/', '') -replace '\\', '/' } |
    Sort-Object -Unique

$toDelete = @()
Get-ChildItem -Recurse -File -Force -LiteralPath $dst | ForEach-Object {
    $rel = ($_.FullName.Substring($dst.Length + 1)) -replace '\\', '/'
    if ($rel -match $excludedFromSync) { return }
    if ($managed -notcontains $rel) { return }
    $srcPath = Join-Path $Src ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $srcPath -PathType Leaf)) {
        $toDelete += $rel
    }
}

if ($toDelete.Count -eq 0) {
    Write-Host "No stale files to remove."
} else {
    Write-Host "Stale files to remove:"
    $toDelete | ForEach-Object { Write-Host "  $_" }
    foreach ($rel in $toDelete) {
        if ($WhatIf) {
            Write-Host "WhatIf: would remove stale: $rel"
        } else {
            Remove-Item -LiteralPath (Join-Path $dst ($rel -replace '/', '\')) -Force
            Write-Host "Removed stale: $rel"
        }
    }
}

Write-Host "Synced $Src -> $dst"
