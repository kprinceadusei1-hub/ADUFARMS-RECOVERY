import sqlite3

conn = sqlite3.connect("adufarms.db")

print("USERS TABLE STRUCTURE:")
columns = conn.execute("PRAGMA table_info(users)").fetchall()

for column in columns:
    print(column)

print("\nUSER ACCOUNTS:")
users = conn.execute("SELECT id, username, full_name, role, active, email, email_verified, last_login, created_at FROM users").fetchall()  # never print password hashes

for user in users:
    print(user)

conn.close()