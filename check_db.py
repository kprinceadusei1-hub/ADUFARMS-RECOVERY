import os
import sqlite3

conn = sqlite3.connect(os.environ.get("DATABASE_PATH", "adufarms.db"))

tables = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()

print("DATABASE TABLES:")
for table in tables:
    print("-", table[0])

conn.close()