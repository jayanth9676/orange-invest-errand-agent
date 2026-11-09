# Errand Agent Enterprise PowerShell Launcher
Write-Host "===============================================================================" -ForegroundColor Cyan
Write-Host "  Launching Errand Agent Enterprise Platform (FastAPI + Tailwind SPA)..." -ForegroundColor Green
Write-Host "===============================================================================" -ForegroundColor Cyan
$env:PYTHONIOENCODING = "utf-8"
uv run python run_server.py
