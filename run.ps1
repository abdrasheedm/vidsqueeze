<#
    Start the VidSqueeze server and open the UI in the browser.
    Usage:  powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Pick up tools installed by setup.ps1 in this session.
$machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
$user    = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = ($machine, $user | Where-Object { $_ }) -join ";"

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "No .venv yet — running setup.ps1 first..." -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup.ps1")
    if (-not (Test-Path $venvPy)) { throw "setup.ps1 did not produce a venv" }
}

$url = "http://127.0.0.1:8765"
Start-Job -ScriptBlock { Start-Sleep -Seconds 2; Start-Process $using:url } | Out-Null

& $venvPy -m uvicorn app.server:app --host 127.0.0.1 --port 8765 --no-access-log
