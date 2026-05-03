# Convenience launcher for EcoTrust dev stack (Windows PowerShell)
param([switch]$NoSim)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating venv..." -ForegroundColor Cyan
    # Prefer the standard Windows Python install; msys2 builds lack wheels.
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    & $py -m venv .venv
}
. .\.venv\Scripts\Activate.ps1

# Optional: set these before launch to enable nudge layer + auth.
# $env:ECOTRUST_NUDGE_WEBHOOK_URL = "https://hooks.slack.com/services/..."
# $env:ECOTRUST_API_KEY = "supersecret"

Write-Host "Installing deps..." -ForegroundColor Cyan
pip install -q -r requirements.txt

Write-Host "Seeding DB..." -ForegroundColor Cyan
python -m backend.seed
Write-Host "Backfilling 24h of demo decisions..." -ForegroundColor Cyan
python -m backend.demo_seed --hours 24 --step-minutes 15

Write-Host "Starting API on http://127.0.0.1:8000 ..." -ForegroundColor Green
$api = Start-Process -PassThru powershell -ArgumentList @(
    "-NoExit","-Command",". .\.venv\Scripts\Activate.ps1; uvicorn backend.main:app --reload"
)

if (-not $NoSim) {
    Start-Sleep -Seconds 3
    Write-Host "Starting sensor simulator..." -ForegroundColor Green
    Start-Process powershell -ArgumentList @(
        "-NoExit","-Command",". .\.venv\Scripts\Activate.ps1; python -m edge.sensor_simulator"
    )
}

Write-Host "`nOpen http://127.0.0.1:8000 in your browser." -ForegroundColor Yellow
Pop-Location
