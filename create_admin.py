import os
import secrets
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

BASE_USERS = {
    "admin": {
        "full_name": os.environ.get("ADUFARMS_ADMIN_FULL_NAME", "Business Administrator"),
        "password": os.environ.get("ADUFARMS_ADMIN_PASSWORD", ""),
        "role": "ADMIN",
    },
    "manager": {
        "full_name": "Operations Manager",
        "password": os.environ.get("ADUFARMS_MANAGER_PASSWORD", ""),
        "role": "MANAGER",
    },
    "sales": {
        "full_name": "Sales Officer",
        "password": os.environ.get("ADUFARMS_SALES_PASSWORD", ""),
        "role": "SALES_OFFICER",
    },
    "inventory": {
        "full_name": "Inventory Officer",
        "password": os.environ.get("ADUFARMS_INVENTORY_PASSWORD", ""),
        "role": "INVENTORY_OFFICER",
    },
    "accounts": {
        "full_name": "Accountant",
        "password": os.environ.get("ADUFARMS_ACCOUNTANT_PASSWORD", ""),
        "role": "ACCOUNTANT",
    },
}

DB_PATH = os.environ.get("DATABASE_PATH") or os.environ.get("ADUFARMS_DB_PATH") or "adufarms.db"
conn = sqlite3.connect(DB_PATH)
for username, config in BASE_USERS.items():
    password = str(config["password"]).strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    conn.execute(
        """
        INSERT INTO users
        (username, full_name, password_hash, role, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            full_name = excluded.full_name,
            password_hash = excluded.password_hash,
            role = excluded.role,
            active = excluded.active,
            last_login = NULL,
            failed_login_attempts = 0,
            locked_until = NULL
        """,
        (
            username,
            config["full_name"].strip() or username.title(),
            generate_password_hash(password),
            config["role"],
            1,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    print(f"{username} ({config['role']}): {password if generated else '(password from environment)'}")

conn.commit()
conn.close()
print("Business accounts ready.")