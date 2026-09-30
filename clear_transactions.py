import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", "adufarms.db")

TABLES_TO_CLEAR = [
    "payments",
    "invoices",
    "sales",
    "purchases",
    "customers",
    "stock_movements",
    "audit_log",
    "reversals",
]

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = OFF")
try:
    for table in TABLES_TO_CLEAR:
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.Error as exc:
            print(f"Failed to clear {table}: {exc}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN (?, ?, ?, ?, ?, ?, ?, ?)", tuple(TABLES_TO_CLEAR))
    conn.commit()
    print("Business transaction data cleared successfully.")
    for table in TABLES_TO_CLEAR:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count}")
finally:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
