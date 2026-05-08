$ErrorActionPreference = 'Stop'
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
if (-not $env:TARGET_URLS) { $env:TARGET_URLS = 'https://www.kampojanechen.org/,https://www.kampojanechen.org/v2/Official/NewestSalePage' }
python crawler/universal_public_monitor.py
Write-Host "DB: $(Resolve-Path .\gampo_public_monitor.db)"
Write-Host "Summary: $(Resolve-Path .\data\latest_summary.json)"
