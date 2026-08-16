<#
    One-time Windows setup: system tools + Python environment.

    Installs ffmpeg, exiftool and adb (via winget, falling back to scoop), then
    creates the venv and verifies that hardware encoding actually works on this
    machine — which is not the same question as whether ffmpeg was built with
    an NVENC/QSV/AMF encoder.

    Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>
[CmdletBinding()]
param(
    # ffmpeg version to install. "latest" tracks winget; a version number pins it.
    # Pinning matters: builds newer than your GPU driver's NVENC API refuse to
    # start, and setup falls back to a pinned build automatically if that happens.
    [string]$FfmpegVersion = "latest",
    [switch]$SkipTools
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# ffmpeg builds known to work with older NVENC drivers, newest first.
$NvencFallbackVersions = @("8.0", "7.1.1")

function Info  { param($m) Write-Host "  $m" }
function Ok    { param($m) Write-Host "  [ok] $m"   -ForegroundColor Green }
function Warn  { param($m) Write-Host "  [!!] $m"   -ForegroundColor Yellow }
function Fail  { param($m) Write-Host "  [xx] $m"   -ForegroundColor Red }
function Step  { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

function Sync-Path {
    # winget edits the registry PATH; refresh this session so the new tools
    # are visible without restarting the shell.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user | Where-Object { $_ }) -join ";"
}

function Test-Tool { param($Name) [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Install-WithWinget {
    param($Id, $Label, $Version)
    if (-not (Test-Tool winget)) { return $false }
    $wgArgs = @("install", "--id", $Id, "-e", "--accept-source-agreements",
                "--accept-package-agreements", "--disable-interactivity")
    if ($Version -and $Version -ne "latest") { $wgArgs += @("--version", $Version, "--force") }
    Info "installing $Label via winget..."
    & winget @wgArgs | Out-Null
    Sync-Path
    return $true
}

Step "1. System tools"

if ($SkipTools) {
    Info "skipped (-SkipTools)"
} else {
    Sync-Path

    if ((Test-Tool ffmpeg) -and $FfmpegVersion -eq "latest") {
        Ok "ffmpeg already installed"
    } else {
        if (-not (Install-WithWinget "Gyan.FFmpeg" "ffmpeg" $FfmpegVersion)) {
            if (Test-Tool scoop) { scoop install ffmpeg | Out-Null; Sync-Path }
            else { Fail "install winget or scoop, then re-run"; exit 1 }
        }
        if (Test-Tool ffmpeg) { Ok "ffmpeg installed" } else { Fail "ffmpeg install failed"; exit 1 }
    }

    if (Test-Tool exiftool) {
        Ok "exiftool already installed"
    } else {
        if (-not (Install-WithWinget "OliverBetz.ExifTool" "exiftool" $null)) {
            if (Test-Tool scoop) { scoop install exiftool | Out-Null; Sync-Path }
        }
        if (Test-Tool exiftool) { Ok "exiftool installed" } else { Warn "exiftool not found — metadata copying will fail" }
    }

    if (Test-Tool adb) {
        Ok "adb already installed"
    } else {
        if (-not (Install-WithWinget "Google.PlatformTools" "adb (Android platform-tools)" $null)) {
            if (Test-Tool scoop) { scoop install adb | Out-Null; Sync-Path }
        }
        if (Test-Tool adb) { Ok "adb installed" } else { Warn "adb not found — phone mode unavailable, folder mode still works" }
    }
}

Step "2. Python environment"

$python = $null
foreach ($c in @("python", "python3", "py")) {
    if (Test-Tool $c) { $python = $c; break }
}
if (-not $python) { Fail "Python 3 not found — install it from https://python.org or 'winget install Python.Python.3.12'"; exit 1 }

if (-not (Test-Path ".venv")) {
    Info "creating .venv..."
    & $python -m venv .venv
}
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "venv creation failed"; exit 1 }
& $venvPy -m pip install --quiet --upgrade pip fastapi uvicorn
Ok "venv ready with fastapi + uvicorn"

Step "3. Hardware encoder check"

# ffmpeg lists every encoder it was built with, so the only honest test is to
# run one. app.hwaccel does exactly that.
$env:PYTHONPATH = $PSScriptRoot
$detect = & $venvPy -c "from app import hwaccel; import json; print(json.dumps(hwaccel.summary()))" 2>&1
try { $hw = $detect | ConvertFrom-Json } catch { $hw = $null }

if ($hw -and $hw.available) {
    Ok "$($hw.detail)"
    if (-not $hw.ten_bit) { Warn "10-bit HEVC not supported — HDR clips will use libx265 instead" }
} else {
    Warn "no hardware encoder available"
    if ($hw) { Info $hw.detail }

    # The common cause on NVIDIA laptops: the ffmpeg build wants a newer NVENC
    # API than the installed driver provides. An older ffmpeg fixes it.
    $needsOlder = $hw -and ($hw.detail -match "NVENC API")
    if ($needsOlder -and -not $SkipTools -and $FfmpegVersion -eq "latest") {
        foreach ($v in $NvencFallbackVersions) {
            Info "retrying with ffmpeg $v..."
            Install-WithWinget "Gyan.FFmpeg" "ffmpeg $v" $v | Out-Null
            $detect = & $venvPy -c "from app import hwaccel; import json; print(json.dumps(hwaccel.summary(force=True)))" 2>&1
            try { $hw = $detect | ConvertFrom-Json } catch { $hw = $null }
            if ($hw -and $hw.available) { Ok "$($hw.detail) (ffmpeg pinned to $v)"; break }
        }
    }
    if (-not ($hw -and $hw.available)) {
        Warn "the 'Fast' profile will fall back to libx265 — correct, just slower"
    }
}

Write-Host "`nSetup complete. Start the app with .\run.ps1" -ForegroundColor Green
