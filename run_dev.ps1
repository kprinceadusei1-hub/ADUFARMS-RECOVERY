# One-step local setup and launch for ADUFARMS (Windows PowerShell).
# Usage:  .\run_dev.ps1            -> install, create database, start the app
#         .\run_dev.ps1 -Demo      -> also load demo sales/purchases so the dashboard has figures
param([switch]$Demo)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path .\.venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

& .\.venv\Scripts\python.exe -c "import app; app.init_db()"

# Accounts are created only for an empty database, so existing users and passwords are never overwritten.
$userCount = & .\.venv\Scripts\python.exe -c "import sqlite3, app; print(sqlite3.connect(app.DB_PATH).execute('SELECT COUNT(*) FROM users').fetchone()[0])"
$loginNote = "sign in with your existing account"
if ([int]$userCount -eq 0) {
    if (-not $env:ADUFARMS_ADMIN_PASSWORD) {
        $env:ADUFARMS_ADMIN_PASSWORD = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(10))"
    }
    & .\.venv\Scripts\python.exe create_admin.py | Out-Null
    $loginNote = "user: admin, password: $env:ADUFARMS_ADMIN_PASSWORD"
}
# -Demo adds sample sales/purchases; use it on a fresh or test database only.
if ($Demo) { & .\.venv\Scripts\python.exe seed_demo_transactions.py | Out-Null }

Write-Host "`nOpen http://127.0.0.1:5000  ($loginNote)`n"
& .\.venv\Scripts\python.exe app.py
