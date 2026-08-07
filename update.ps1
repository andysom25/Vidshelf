<#
.SYNOPSIS
    Update the Vidshelf container to the latest published release, and verify it.

.DESCRIPTION
    Wraps the pull/recreate/verify steps into one run and checks the things that
    have actually gone wrong in this project before, rather than just reporting
    exit codes:

      * the image really changed (a local build tagged ghcr.io/...:latest will
        shadow the registry copy, and a plain `up -d` would keep running it)
      * the container is serving, not crash-looping
      * the reported VERSION matches the release that was pulled
      * the media mounts are real CIFS, not Docker's silent decoy volume
        (CLAUDE.md gotcha #1 - this has cost two debugging sessions)

.PARAMETER Recreate
    Full down/up instead of `up -d`. Required when docker-compose.yml changes
    capabilities, volumes or ports - a plain `up -d` will not re-apply those.

.PARAMETER Version
    Pin a specific release, e.g. -Version 1.8.0 to roll back. Defaults to latest.

.EXAMPLE
    .\update.ps1
.EXAMPLE
    .\update.ps1 -Recreate
.EXAMPLE
    .\update.ps1 -Version 1.8.0
#>
[CmdletBinding()]
param(
    [switch]$Recreate,
    [string]$Version
)

# Native commands (docker) write to stderr routinely -- Python's logging does,
# and `docker logs` replays it. In PowerShell 5.1 that becomes a NativeCommandError,
# so 'Stop' would abort on perfectly normal output. Exit codes are checked
# explicitly via $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$service   = 'vidshelf'
$imageRepo = 'ghcr.io/andysom25/vidshelf'

function Say    { param($m) Write-Host "  $m" }
function Step   { param($m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Good   { param($m) Write-Host "  OK   $m" -ForegroundColor Green }
function Warn   { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow }
function Fail   { param($m) Write-Host "  FAIL $m" -ForegroundColor Red }

function Get-ContainerVersion {
    try { (docker exec $service cat /app/VERSION 2>$null).Trim() } catch { $null }
}

# --- preflight -------------------------------------------------------------
Step 'Preflight'
Set-Location -Path $PSScriptRoot
if (-not (Test-Path 'docker-compose.yml')) {
    Fail "no docker-compose.yml in $PSScriptRoot"; exit 1
}
docker info *>$null
if ($LASTEXITCODE -ne 0) { Fail 'Docker is not responding - is Docker Desktop running?'; exit 1 }
Good "compose project found in $PSScriptRoot"

$beforeVersion = Get-ContainerVersion
$beforeImage   = docker inspect $service --format '{{.Image}}' 2>$null
if ($beforeVersion) { Say "currently running: v$beforeVersion" }
else { Say 'no running container (fresh start)' }

# --- pick the target ------------------------------------------------------
Step 'Resolving target release'
if ($Version) {
    $target = $Version.TrimStart('v')
    Say "pinned to v$target"
} else {
    $target = $null
    try {
        $tag = (gh release list --limit 1 --json tagName --jq '.[0].tagName' 2>$null)
        if ($tag) { $target = ([string]$tag).Trim().TrimStart('v'); Say "latest release is v$target" }
    } catch { }
    if (-not $target) { Warn 'could not query GitHub; using the :latest tag' }
}

# The compose override may pin :latest. Pull the explicit tag too when we know
# it, so a stale local build tagged :latest cannot be mistaken for the release.
Step 'Pulling image'
if ($target) {
    docker pull "${imageRepo}:$target"
    if ($LASTEXITCODE -ne 0) { Fail "could not pull ${imageRepo}:$target"; exit 1 }
    docker tag "${imageRepo}:$target" "${imageRepo}:latest"
    Good "pulled v$target and retagged :latest to match"
} else {
    docker compose pull
    if ($LASTEXITCODE -ne 0) { Fail 'docker compose pull failed'; exit 1 }
    Good 'pulled :latest'
}

# --- restart --------------------------------------------------------------
if ($Recreate) {
    Step 'Recreating container (full down/up)'
    docker compose down
    docker compose up -d --pull never
} else {
    Step 'Starting container'
    docker compose up -d --pull never
}
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up failed'; exit 1 }

# --- wait for it to actually serve ----------------------------------------
Step 'Waiting for the app to serve'
$ready = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 2
    $state = docker inspect $service --format '{{.State.Status}}' 2>$null
    if ($state -ne 'running') { continue }
    $log = docker logs $service --tail 40 2>$null | Out-String
    if ($log -match 'listening on http') { $ready = $true; break }
}
if (-not $ready) {
    Fail 'container did not report "listening" within 60s'
    docker logs $service --tail 40 2>$null
    exit 1
}
$restarts = docker inspect $service --format '{{.RestartCount}}' 2>$null
Good "serving (restarts: $restarts)"

# --- verify ---------------------------------------------------------------
Step 'Verifying'
$afterVersion = Get-ContainerVersion
$afterImage   = docker inspect $service --format '{{.Image}}' 2>$null

if (-not $afterVersion) { Fail 'could not read /app/VERSION'; exit 1 }
Say "version in container: v$afterVersion"

if ($target -and $afterVersion -ne $target) {
    Fail "expected v$target but the container reports v$afterVersion"
    Say "a local image tagged :latest can shadow the registry copy - check: docker images $imageRepo"
    exit 1
}
Good "version matches the target"

if ($beforeImage -and $afterImage -eq $beforeImage) {
    if ($beforeVersion -eq $afterVersion) {
        Warn 'image did not change - already up to date'
    }
} else {
    Good 'image changed'
}

# Gotcha #1: a bind-mounted network share silently becomes a small local volume.
Step 'Checking mounts are real (CLAUDE.md gotcha #1)'
$df = docker exec $service df -h 2>$null | Out-String
$mediaLines = $df -split "`n" | Where-Object { $_ -match 'music_videos_final|/app/data' }
foreach ($line in $mediaLines) { Say $line.Trim() }
if ($df -match 'cifs|//') { Good 'a network mount is present' }
else { Warn 'no cifs mount visible - if a media path errors, run: docker exec vidshelf df -h' }

# What the startup housekeeping did (v1.8.1+).
Step 'Startup housekeeping'
$house = docker logs $service 2>$null | Select-String -Pattern '\[downloads\]|\[state\] Migration'
if ($house) { $house | ForEach-Object { Say $_.Line.Trim() } }
else { Say 'nothing to reconcile or sweep' }

Step 'Done'
if ($beforeVersion -and $beforeVersion -ne $afterVersion) {
    Good "v$beforeVersion -> v$afterVersion"
} else {
    Good "running v$afterVersion"
}
Say "Release notes: gh release view v$afterVersion"
exit 0
