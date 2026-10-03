# Start the app in debug mode from this folder. Run: .\run_debug.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$env:ADUFARMS_DEBUG = '1'
& $python app.py
