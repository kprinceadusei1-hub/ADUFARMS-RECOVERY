# ADUFARMS – Maize Distribution Management System

A Flask + SQLite web application for maize trading/distribution.

## Features
- Admin/staff login and role-based access
- Purchase entry with automatic purchase IDs
- Customer sales with automatic transaction IDs
- Stock control; sales cannot exceed available stock
- Multiple payments per customer transaction
- Automatic PAID / PART PAYMENT / UNPAID status
- Printable A4 invoice and PDF export
- Customer/transaction search
- Customer ledger with statements, opening balances and derived outstanding balances
- Dashboard: stock, sales, payments, expenses and estimated profit
- Verified database assistant for stock, activity, balances and top-customer questions
- Official branding path: `static/images/branding/adufarms-logo.jpg`
- SQLite database
- Filterable audit log with CSV export
- Stock-movement CSV export for warehouse reconciliation
- Responsive business interface; Bootstrap, icons, Chart.js and fonts are bundled in `static/vendor` so the app works offline

## Quick start in VS Code (Windows)

```powershell
git fetch origin
git checkout claude/brave-johnson-ti1ay2
code .
.\run_dev.ps1 -Demo     # first run: sets up .venv, creates the database and admin, loads demo figures
```

Then open http://127.0.0.1:5000. On a new database the admin password is printed once in the terminal. Use `.\run_dev.ps1` (without `-Demo`) on a database that already holds real records; it never overwrites existing users. In VS Code you can also press F5 (`ADUFARMS: run app`) once `.venv` exists and the interpreter is selected.

## Run on Windows PowerShell

```powershell
cd ADUFARMS
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Production on Windows

Set a stable secret before starting the production server. The application refuses to start in production without it.

```powershell
$env:ADUFARMS_ENV = "production"
$env:ADUFARMS_SECRET_KEY = "generate-a-long-random-secret"
$env:ADUFARMS_COOKIE_SECURE = "1" # Use only when served over HTTPS
$env:ADUFARMS_HOST = "127.0.0.1"
$env:ADUFARMS_PORT = "5000"
 .\.venv\Scripts\Activate.ps1
python run_production.py
```

Use a reverse proxy with HTTPS when exposing the service beyond the local machine. Keep `ADUFARMS_COOKIE_SECURE=0` for plain local HTTP development.

Run the automated checks with:

```powershell
python -m pytest -q
```

## First login
Use an administrator account already provisioned for your environment. The application does not display or create a known default password.

Staff can create purchases, customers, sales and payments. Financial-record edits, reversals, permanent deletion, user management, backups and audit logs are restricted to administrators.

The AI Assistant is local and database-backed. It does not call an external provider or invent financial figures; unsupported questions receive a clear limitation message. External AI or notification integrations can be added later using environment variables.

## Database
The SQLite database `adufarms.db` is created automatically on first run.

Administrator backup and restore actions create integrity-checked SQLite snapshots. Restore first creates a safety backup, verifies a temporary restore image, and replaces the database atomically. Restores must be performed when no other application session is actively using the database; if Windows reports the database is locked, the restore is cancelled without changing live data. Keep the `backups` directory on a separate protected drive for disaster recovery.

For automatic protection, configure a second drive or synchronized protected folder and run the scheduled backup command daily:

```powershell
$env:ADUFARMS_OFFSITE_BACKUP_DIR = "D:\ADUFARMS-Backups"
$env:ADUFARMS_BACKUP_KEEP_COUNT = "90"
python run_backup.py
```

`run_backup.py` creates a consistent SQLite snapshot, writes a SHA-256 manifest, verifies the snapshot, and copies both files to the off-site folder. Schedule it with Windows Task Scheduler after the application account has access to that folder. A backup is only truly protected when the off-site folder is on a different disk, machine, or managed cloud-synchronization service.

## Notes
This version is designed as a solid local business system. For multi-computer/network deployment, use a production WSGI server and a shared database such as PostgreSQL.
