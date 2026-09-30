# Run the app on the SEPARATE test database (realistic sample transactions). Your real data is not used.
# First time (or to start over):  python tools\seed_test_data.py --reset
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$db = Join-Path $PSScriptRoot 'test_datadufarms_test.db'
if (-not (Test-Path $db)) { & $python tools\seed_test_data.py }
$env:DATABASE_PATH = $db
$env:ADUFARMS_DB_PATH = $db
$env:ADUFARMS_DEBUG = '1'
Write-Host "Running on TEST database: $db  (sign in: admin / Admin@2026!)" -ForegroundColor Yellow
& $python app.py
