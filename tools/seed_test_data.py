"""Create a SEPARATE test database filled with realistic transactions (customers, purchases, sales, payments).

Your real database is never touched: the script always writes to test_data/adufarms_test.db
(or the path in ADUFARMS_TEST_DB). Run the app on it with:  run_test.ps1

    python tools/seed_test_data.py            # build it (asks nothing, refuses to overwrite)
    python tools/seed_test_data.py --reset    # delete the test database and rebuild it

Sign in with  admin / Admin@2026!  (test database only).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_DB = Path(os.environ.get("ADUFARMS_TEST_DB") or ROOT / "test_data" / "adufarms_test.db").resolve()
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
if "--reset" in sys.argv:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
os.environ["DATABASE_PATH"] = str(TEST_DB)
os.environ["ADUFARMS_DB_PATH"] = str(TEST_DB)
import random
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

import app as adu
import stock_service as stock_svc

CUSTOMERS = [
    ("Kofi Mensah Traders", "0244123456", "Kumasi", "WHOLESALE"),
    ("Ama Serwaa Poultry Farm", "0207654321", "Techiman", "FARM"),
    ("Yaw Boateng Feeds", "0551239876", "Sunyani", "WHOLESALE"),
    ("Abena Owusu", "0269988776", "Ejura", "RETAIL"),
    ("Nana Kwame Millers", "0501112233", "Kintampo", "WHOLESALE"),
    ("Esi Appiah Kitchen", "0233344556", "Accra", "RETAIL"),
]
SUPPLIERS = [("Agent Fuseini", "0244000111", "Ejura"), ("Agent Adjei", "0208000222", "Techiman"),
             ("Agent Salifu", "0553000333", "Tamale")]
METHODS = ["CASH", "MOBILE MONEY", "BANK TRANSFER"]


def main(force=False):
    adu.init_db()
    conn = adu.db()
    if conn.execute("SELECT 1 FROM sales LIMIT 1").fetchone() and not force:
        conn.close()
        sys.exit("Test database already contains sales. Use --reset to rebuild it.")

    rng = random.Random(2026)
    today = date.today()
    ts = adu.now()

    conn.execute(
        "INSERT OR IGNORE INTO users(username,full_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)",
        ("admin", "Business Administrator", generate_password_hash("Admin@2026!"), "ADMIN", 1, ts))
    conn.commit()

    customer_ids = []
    for name, phone, location, ctype in CUSTOMERS:
        cur = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)", (name, phone, ts))
        customer_ids.append(cur.lastrowid)
        conn.execute("UPDATE customers SET location=?,customer_type=? WHERE id=?", (location, ctype, cur.lastrowid))
    conn.commit()

    for week in range(10, 0, -1):
        dt = today - timedelta(days=week * 8)
        d = dt.isoformat()
        agent, phone, loc = rng.choice(SUPPLIERS)
        qty, price = rng.randint(3000, 6000), round(rng.uniform(3.2, 4.4), 2)
        transport, other = rng.randint(150, 400), rng.randint(0, 120)
        pid = adu.next_daily_id("ADU-PUR", "purchases", "purchase_id", conn, dt)
        conn.execute("""INSERT INTO purchases(purchase_id,purchase_date,local_agent,agent_phone,location,quantity_kg,
            price_per_kg,total_purchase_cost,transport_cost,other_expenses,total_cost,quantity_received_kg,staff_user,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, d, agent, phone, loc, qty, price, qty * price, transport, other,
             qty * price + transport + other, qty, "admin", ts))
        stock_svc.apply_purchase_create(conn, pid, qty, d, "admin", ts)
    conn.commit()

    for day in range(75, -1, -2):
        dt = today - timedelta(days=day)
        d = dt.isoformat()
        for _ in range(rng.randint(1, 2)):
            qty, price = rng.randint(80, 600), round(rng.uniform(4.6, 5.8), 2)
            if stock_svc.available_stock(conn) < qty:
                continue
            total = round(qty * price, 2)
            sid = adu.next_daily_id("ADU-SAL", "sales", "sales_id", conn, dt)
            tid = sid.replace("ADU-SAL", "ADU")
            inv = sid.replace("ADU-SAL", "ADU-INV")
            customer_id = rng.choice(customer_ids)
            conn.execute("""INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,
                selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tid, sid, inv, d, customer_id, qty, price, total, "admin", ts))
            conn.execute("""INSERT INTO invoices(invoice_number,transaction_id,sales_id,invoice_date,generated_by,generated_at)
                VALUES(?,?,?,?,?,?)""", (inv, tid, sid, d, "admin", ts))
            stock_svc.apply_sale_create(conn, tid, qty, d, "admin", ts)
            outcome = rng.random()
            paid = total if outcome < 0.55 else round(total * rng.uniform(0.3, 0.7), 2) if outcome < 0.85 else 0
            if paid:
                pay_id = adu.next_daily_id("ADU-PAY", "payments", "payment_id", conn, dt)
                conn.execute("""INSERT INTO payments(payment_id,transaction_id,sales_id,payment_date,amount,payment_method,
                    payment_reference,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (pay_id, tid, sid, d, paid, rng.choice(METHODS), "", "admin", ts))
    conn.commit()
    conn.close()
    print(f"Test data ready in {TEST_DB}")
    print("Sign in as admin / Admin@2026!  (test database only). Start it with run_test.ps1")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
