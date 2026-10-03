"""Provision ADUFARMS role accounts.

Safe by default:
  * passwords come from environment variables, or are generated randomly
    (and shown once) - there are no well-known default passwords;
  * existing accounts are left untouched unless --reset is passed.

    python create_admin.py            # create any missing accounts
    python create_admin.py --reset    # also reset passwords of existing accounts

Set ADUFARMS_ADMIN_PASSWORD, ADUFARMS_MANAGER_PASSWORD, ... to choose passwords yourself.
Honours DATABASE_PATH / ADUFARMS_DB_PATH like the main application.
"""
import os
import secrets
import sqlite3
import string
import sys
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATABASE_PATH") or os.environ.get("ADUFARMS_DB_PATH") or BASE_DIR / "adufarms.db")

ACCOUNTS = [
    ("admin", "Business Administrator", "ADMIN", "ADUFARMS_ADMIN_PASSWORD"),
    ("manager", "Operations Manager", "MANAGER", "ADUFARMS_MANAGER_PASSWORD"),
    ("sales", "Sales Officer", "SALES_OFFICER", "ADUFARMS_SALES_PASSWORD"),
    ("inventory", "Inventory Officer", "INVENTORY_OFFICER", "ADUFARMS_INVENTORY_PASSWORD"),
    ("accounts", "Accountant", "ACCOUNTANT", "ADUFARMS_ACCOUNTANT_PASSWORD"),
]


def random_password(length=16):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in candidate) and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate) and any(c in "!@#$%^&*" for c in candidate)):
            return candidate


def main(reset=False):
    conn = sqlite3.connect(DB_PATH)
    try:
        for username, full_name, role, env_var in ACCOUNTS:
            exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if exists and not reset:
                print(f"{username}: already exists - left unchanged")
                continue
            supplied = os.environ.get(env_var, "").strip()
            password = supplied or random_password()
            conn.execute(
                """
                INSERT INTO users (username, full_name, password_hash, role, active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(username) DO UPDATE SET
                    full_name = excluded.full_name,
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    active = 1,
                    failed_login_attempts = 0,
                    locked_until = NULL
                """,
                (username, full_name, generate_password_hash(password), role,
                 datetime.now().isoformat(timespec="seconds")),
            )
            shown = "(from " + env_var + ")" if supplied else password
            print(f"{username} [{role}]: {shown}")
        conn.commit()
    finally:
        conn.close()
    print("\nStore any generated passwords now - they are not saved anywhere.")


if __name__ == "__main__":
    main(reset="--reset" in sys.argv)
