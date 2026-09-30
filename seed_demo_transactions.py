import os
from datetime import date, timedelta
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", "adufarms.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    today = date.today()

    customer = conn.execute("SELECT id FROM customers WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    if customer is None:
        conn.execute(
            "INSERT INTO customers(name, phone, created_at, customer_type, opening_balance, active) VALUES(?,?,?,?,?,?)",
            ("Demo Customer", "0550000000", today.isoformat(), "RETAIL", 0, 1),
        )
        customer = conn.execute("SELECT id FROM customers ORDER BY id DESC LIMIT 1").fetchone()
    customer_id = customer["id"]

    if conn.execute("SELECT id FROM sales WHERE deleted=0 ORDER BY id DESC LIMIT 1").fetchone() is None:
        prev_sale_date = (today - timedelta(days=3)).isoformat()
        new_sale_date = today.isoformat()
        rows = [
            ("ADU-" + prev_sale_date.replace('-', '') + "-001", "ADU-SAL-" + prev_sale_date.replace('-', '') + "-001", "ADU-INV-" + prev_sale_date.replace('-', '') + "-001", prev_sale_date, 48.0, 7.25, 348.00),
            ("ADU-" + new_sale_date.replace('-', '') + "-001", "ADU-SAL-" + new_sale_date.replace('-', '') + "-001", "ADU-INV-" + new_sale_date.replace('-', '') + "-001", new_sale_date, 72.0, 6.80, 489.60),
        ]
        for tid, sid, inv, d, qty, price, total in rows:
            conn.execute(
                "INSERT INTO sales(transaction_id, sales_id, invoice_number, sale_date, customer_id, quantity_kg, selling_price_kg, total_sale, staff_user, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tid, sid, inv, d, customer_id, qty, price, total, "admin", d),
            )
            conn.execute(
                "INSERT INTO invoices(invoice_number, transaction_id, sales_id, invoice_date, generated_by, generated_at) VALUES(?,?,?,?,?,?)",
                (inv, tid, sid, d, "admin", d),
            )
            pay_id = "ADU-PAY-" + d.replace('-', '') + "-001"
            amount = total * 0.7
            conn.execute(
                "INSERT INTO payments(payment_id, transaction_id, sales_id, payment_date, amount, payment_method, payment_reference, notes, staff_user, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pay_id, tid, sid, d, amount, "Mobile Money", "TEST-" + sid, "Sample test transaction", "admin", d),
            )
        print("created_demo_sales_and_payments")
    else:
        print("sales_already_present")

    if conn.execute("SELECT id FROM purchases WHERE deleted=0 ORDER BY id DESC LIMIT 1").fetchone() is None:
        prev_pur_date = (today - timedelta(days=5)).isoformat()
        new_pur_date = today.isoformat()
        for pid, agent, d, qty, price, total in [
            ("ADU-PUR-" + prev_pur_date.replace('-', '') + "-001", "Abaoma Agent", prev_pur_date, 120.0, 4.30, 516.0),
            ("ADU-PUR-" + new_pur_date.replace('-', '') + "-001", "Kumasi Supplier", new_pur_date, 90.0, 4.60, 414.0),
        ]:
            conn.execute(
                "INSERT INTO purchases(purchase_id, purchase_date, local_agent, agent_phone, location, quantity_kg, price_per_kg, total_purchase_cost, transport_cost, other_expenses, total_cost, quantity_received_kg, staff_user, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, d, agent, "0551234567", "Kumasi", qty, price, total, 18.0, 12.0, total + 30.0, qty, "admin", d),
            )
            conn.execute(
                "INSERT INTO stock_movements(movement_type, reference, quantity_kg, movement_date, created_by, notes, created_at) VALUES(?,?,?,?,?,?,?)",
                ("PURCHASE", pid, qty, d, "admin", "Demo purchase for testing", d),
            )
        print("created_demo_purchases")
    else:
        print("purchases_already_present")

    conn.commit()
    print("sales_count", conn.execute("SELECT COUNT(*) FROM sales WHERE deleted=0").fetchone()[0])
    print("payment_count", conn.execute("SELECT COUNT(*) FROM payments WHERE deleted=0").fetchone()[0])
    print("purchase_count", conn.execute("SELECT COUNT(*) FROM purchases WHERE deleted=0").fetchone()[0])
    print("latest_sales", conn.execute("SELECT sales_id, sale_date, total_sale FROM sales WHERE deleted=0 ORDER BY id DESC LIMIT 3").fetchall())
    print("latest_purchases", conn.execute("SELECT purchase_id, purchase_date, total_cost FROM purchases WHERE deleted=0 ORDER BY id DESC LIMIT 3").fetchall())
    conn.close()


if __name__ == "__main__":
    main()
