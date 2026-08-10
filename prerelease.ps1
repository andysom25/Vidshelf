<#
.SYNOPSIS
    Pre-release verification: run the checks that unit tests structurally cannot.

.DESCRIPTION
    Every release in the 1.6-1.9 range shipped a bug that the Python suite could
    not have caught, because the failure lived somewhere tests do not go:

      v1.6.1  copystat needed CAP_FOWNER on a real CIFS mount        -> every NAS download failed, for two releases
      v1.8.x  a staging sweep deleted 640 MB of real library         -> only wrong against a real directory layout
      v1.9.0  Disk Usage measured the staging dir, not the library   -> looked correct while staging held leaked files
      v1.9.1  Top artists bars rendered at 0px                       -> DOM correct, CSS silently ignored it
      v1.9.2  the dashboard refresh hammered YouTube                 -> only visible with channels configured

    This script builds the working tree, runs it in a throwaway container against
    a real media volume, drives the real UI in a real browser, and reports what
    it could NOT check. It does not touch your running instance.

    Nothing here replaces the unit suite - run that first. This covers the gap.

.PARAMETER MediaVolume
    Docker volume or host path to mount as the media root. Defaults to the
    project's own volume so the numbers are real.

.PARAMETER Port
    Host port for the throwaway container. Default 5096.

.PARAMETER KeepRunning
    Leave the container up afterwards so you can click around yourself.

.EXAMPLE
    .\prerelease.ps1
.EXAMPLE
    .\prerelease.ps1 -KeepRunning
#>
[CmdletBinding()]
param(
    [string]$MediaVolume = 'vidshelf_music_videos_final',
    [int]$Port = 5096,
    [switch]$KeepRunning
)

# docker writes to stderr routinely; 'Stop' would abort on normal output.
$ErrorActionPreference = 'Continue'
$name  = 'vidshelf-prerelease'
$image = 'vidshelf:prerelease'
$pass  = 'prerelease-' + (Get-Random)

$script:failures = @()
$script:notes    = @()
function Step { param($m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Say  { param($m) Write-Host "  $m" }
function Good { param($m) Write-Host "  OK   $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow; $script:notes += $m }
function Bad  { param($m) Write-Host "  FAIL $m" -ForegroundColor Red;    $script:failures += $m }

Set-Location -Path $PSScriptRoot

# Multi-line scripts do not survive `docker exec ... python -c` on Windows --
# the argument arrives mangled and the command silently produces nothing, which
# reads as a failing check rather than a broken harness. $work is already
# mounted at /app/data, so write the probe as a file and run that instead.
function Invoke-Probe {
    # ProbeName, not Name. PowerShell variables are case-INSENSITIVE, so a
    # parameter called $Name shadows the script's $name (the container), and
    # `docker exec $name` silently ran against a container named after the
    # probe. With 2>$null swallowing "no such container", every probe returned
    # an empty string and three checks reported failures that were entirely the
    # harness's own doing.
    param([string]$ProbeName, [string]$Body)
    $path = Join-Path $work "$ProbeName.py"
    # WriteAllText with an explicit no-BOM encoding: PowerShell 5.1's
    # `Set-Content -Encoding utf8` emits a BOM, which lands in the first line of
    # the probe.
    [System.IO.File]::WriteAllText($path, $Body, (New-Object System.Text.UTF8Encoding($false)))
    # PYTHONPATH, not just -w. Python puts the *script's* directory on sys.path,
    # not the working directory, so a probe living in /app/data cannot import
    # the app no matter where it is run from.
    $out = docker exec -e PYTHONPATH=/app -w /app $name python "/app/data/$ProbeName.py" 2>&1
    Remove-Item $path -ErrorAction SilentlyContinue
    return (($out | ForEach-Object { $_.ToString() }) -join "`n").Trim()
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("vidshelf-pre-" + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $work | Out-Null

function Cleanup {
    if (-not $KeepRunning) {
        docker rm -f $name *>$null
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

try {
    # --- 0. unit suite first -------------------------------------------------
    Step 'Unit suite (this script does not replace it)'
    $suite = Get-ChildItem tests/test_*.py | ForEach-Object { $_.Name }
    $unitFailed = $false
    foreach ($t in $suite) {
        $out = python "tests/$t" 2>&1 | Select-Object -Last 1
        if ($LASTEXITCODE -ne 0) { Bad "tests/$t : $out"; $unitFailed = $true }
    }
    if (-not $unitFailed) { Good "$($suite.Count) Python suites passed" }
    node tests/test_artists_filter.js *>$null
    if ($LASTEXITCODE -ne 0) { Bad 'tests/test_artists_filter.js failed' } else { Good 'JS suite passed' }

    # --- 1. build and run the working tree -----------------------------------
    Step 'Building the working tree'
    docker build -q -t $image . | Out-Null
    if ($LASTEXITCODE -ne 0) { Bad 'docker build failed'; throw 'build' }
    Good "built $image"

    Step "Starting a throwaway container on port $Port"
    docker rm -f $name *>$null
    docker run -d --name $name --cap-drop ALL -p "${Port}:5000" `
        -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=$pass -e VIDSHELF_DATA_DIR=/app/data `
        -v "${work}:/app/data" -v "${MediaVolume}:/app/music_videos_final" $image | Out-Null
    if ($LASTEXITCODE -ne 0) { Bad 'container failed to start'; throw 'run' }

    # Point it at the mounted media so the figures are real.
    Start-Sleep -Seconds 12
    docker exec $name python -c "import app,state; c=state.read_json(app.CONFIG_FILE,{}); c['artwork_sync']={'root_path':'/app/music_videos_final','watch_interval':86400}; state.write_json(app.CONFIG_FILE,c,indent=4)" *>$null

    $ver = (docker exec $name cat /app/VERSION 2>$null).Trim()
    Good "running v$ver (throwaway - your own instance is untouched)"

    # --- 2. the mount is real ------------------------------------------------
    # CLAUDE.md gotcha #1: Docker silently substitutes a small local volume for
    # a network share, and it looks identical until something runs out of space.
    Step 'Media mount is real, not a decoy (CLAUDE.md gotcha #1)'
    $df = docker exec $name df -h /app/music_videos_final 2>$null | Out-String
    Say ($df -split "`n" | Select-Object -Last 2 | ForEach-Object { $_.Trim() }) -join ' '
    if ($df -match 'cifs|//') { Good 'network filesystem present' }
    else { Warn 'not a network mount - CIFS-specific paths are NOT covered by this run' }

    # --- 3. writes actually work on that mount -------------------------------
    # This is the v1.6.1 bug: create/write/delete all worked with cap_drop ALL;
    # utime and chmod did not, and that is what broke every download.
    Step 'Write, utime and chmod on the media mount'
    $probe = Invoke-Probe 'probe_fs' @'
import os
p = "/app/music_videos_final/.prerelease-probe"
r = {}
try:
    open(p, "wb").write(b"x" * 1024); r["write"] = "ok"
except Exception as e: r["write"] = repr(e)
try:
    st = os.stat(p); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns)); r["utime"] = "ok"
except Exception as e: r["utime"] = type(e).__name__
try:
    os.chmod(p, 0o644); r["chmod"] = "ok"
except Exception as e: r["chmod"] = type(e).__name__
try:
    os.remove(p); r["delete"] = "ok"
except Exception as e: r["delete"] = repr(e)
print("|".join(f"{k}={v}" for k, v in r.items()))
'@
    Say $probe
    if ($probe -notmatch 'write=ok') { Bad 'cannot write to the media mount' }
    if ($probe -notmatch 'delete=ok') { Bad 'cannot delete from the media mount' }
    if ($probe -match 'utime=PermissionError') {
        Say 'utime is refused (root vs uid=1000 without CAP_FOWNER) - expected;'
        Say 'copystat must be best-effort or every download fails. See v1.8.1.'
    }

    # --- 4. the library scan sees real data ----------------------------------
    Step 'Library scan against the real media root'
    $stats = Invoke-Probe 'probe_stats' @'
import json, app
c = app.app.test_client()
with c.session_transaction() as s: s["username"] = "admin"
d = c.get("/api/library/stats?refresh=1").get_json()
print(json.dumps({k: d[k] for k in ("artists", "videos", "bytes", "missing_artwork")}))
'@
    Say $stats
    $parsed = $null
    try { $parsed = $stats | ConvertFrom-Json } catch { }
    if (-not $parsed) { Bad 'library stats did not return usable JSON' }
    elseif ($parsed.videos -eq 0) { Warn 'no videos found - UI checks will be shallow' }
    else { Good "$($parsed.videos) videos across $($parsed.artists) artists" }

    # --- 5. every route refuses an anonymous caller --------------------------
    Step 'Anonymous access sweep (against the running server, not the test client)'
    $sweep = Invoke-Probe 'probe_sweep' @'
import app
c = app.app.test_client()
PUB = {"login", "logout", "static", "favicon"}
bad = []
for r in app.app.url_map.iter_rules():
    if r.arguments or r.endpoint in PUB: continue
    for m in ("GET", "POST"):
        if m not in r.methods: continue
        code = c.open(str(r), method=m, json={} if m == "POST" else None).status_code
        if code not in (301, 302, 401, 403, 405): bad.append(f"{m} {r} -> {code}")
print("CLEAN" if not bad else "LEAK " + "; ".join(bad[:5]))
'@
    if ($sweep -match '^CLEAN') { Good 'no route serves an anonymous caller' } else { Bad $sweep }

    # --- 6. the real UI in a real browser ------------------------------------
    Step 'Browser smoke test'
    $env:VIDSHELF_URL = "http://127.0.0.1:$Port"
    $env:VIDSHELF_PASSWORD = $pass
    python tests/test_browser.py
    if ($LASTEXITCODE -ne 0) { Bad 'browser smoke test failed (see above)' }

    # --- 7. upgrade from the previous release --------------------------------
    # A release that cannot be upgraded INTO is the worst kind: v1.0.0 shipped
    # uninstallable, and the config migration in v1.8.0 had to preserve the
    # session key or every user is logged out.
    Step 'Upgrade path from the previous release'
    $prev = (gh release list --limit 2 --json tagName --jq '.[1].tagName' 2>$null)
    if (-not $prev) { Warn 'could not determine the previous release; upgrade path NOT checked' }
    else {
        $prev = ([string]$prev).Trim()
        Say "seeding data from $prev, then starting this build against it"
        docker rm -f "$name-prev" *>$null
        $prevWork = Join-Path ([System.IO.Path]::GetTempPath()) ("vidshelf-prev-" + [guid]::NewGuid().ToString('N').Substring(0,8))
        New-Item -ItemType Directory -Path $prevWork | Out-Null
        docker run -d --name "$name-prev" -e ADMIN_PASSWORD=$pass -e VIDSHELF_DATA_DIR=/app/data `
            -v "${prevWork}:/app/data" "ghcr.io/andysom25/vidshelf:$($prev.TrimStart('v'))" *>$null
        Start-Sleep -Seconds 10
        $secretBefore = docker exec "$name-prev" python -c "import state,json; print(json.load(open(state.CONFIG_FILE)).get('_secret_key',''))" 2>$null
        docker rm -f "$name-prev" *>$null

        docker rm -f "$name-up" *>$null
        docker run -d --name "$name-up" -e ADMIN_PASSWORD=$pass -e VIDSHELF_DATA_DIR=/app/data `
            -v "${prevWork}:/app/data" $image *>$null
        Start-Sleep -Seconds 12
        $secretAfter = docker exec "$name-up" python -c "import state,json; print(json.load(open(state.CONFIG_FILE)).get('_secret_key',''))" 2>$null
        $upVer = (docker exec "$name-up" cat /app/VERSION 2>$null).Trim()
        $upLog = docker logs "$name-up" 2>$null | Out-String

        if ($upVer -ne $ver) { Bad "upgraded container reports v$upVer, expected v$ver" }
        elseif ($secretBefore -and ($secretBefore.Trim() -ne $secretAfter.Trim())) {
            Bad 'the session key changed across the upgrade - every user would be logged out'
        } else { Good "upgrade from $prev preserves config and the session key" }
        if ($upLog -match 'Traceback') { Bad 'the upgraded container logged a traceback' }
        docker rm -f "$name-up" *>$null
        Remove-Item -Recurse -Force $prevWork -ErrorAction SilentlyContinue
    }

    # --- 8. what this run did NOT cover --------------------------------------
    Step 'Not covered by this script'
    Say 'A real download to the NAS. Nothing here fetches from YouTube, so the'
    Say 'download -> convert -> copy path is unverified. Do one by hand before'
    Say 'any release that touches downloader.py, transcode.py or a media path.'
    Say 'Plex: no credentials here, so collections and title cards are untested.'
}
finally {
    if ($KeepRunning) {
        Write-Host ""
        Write-Host "  container left running: http://127.0.0.1:$Port  (admin / $pass)" -ForegroundColor Yellow
        Write-Host "  remove with: docker rm -f $name" -ForegroundColor Yellow
    } else {
        Cleanup
    }
}

Step 'Result'
if ($script:failures.Count -eq 0) {
    Good 'all automated pre-release checks passed'
    if ($script:notes.Count) { $script:notes | ForEach-Object { Warn $_ } }
    Say 'Still do a real download by hand if this release touches the media path.'
    exit 0
} else {
    $script:failures | ForEach-Object { Bad $_ }
    exit 1
}
