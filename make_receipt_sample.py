import os
import sqlite3
from datetime import date

DB_PATH = os.environ.get("DATABASE_PATH", "adufarms.db")

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    customer = conn.execute("SELECT id FROM customers ORDER BY id DESC LIMIT 1").fetchone()
    if customer is None:
        conn.execute(
            "INSERT INTO customers(name, phone, created_at) VALUES (?, ?, ?)",
            ("Demo Customer", "0550000000", today),
        )
        customer_id = conn.execute("SELECT id FROM customers ORDER BY id DESC LIMIT 1").fetchone()["id"]
    else:
        customer_id = customer["id"]

    sale = conn.execute(
        "SELECT transaction_id, sales_id, total_sale FROM sales WHERE deleted=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if sale is None:
        tx = f"ADU-TRX-{today.replace('-', '')}-001"
        sid = f"ADU-SAL-{today.replace('-', '')}-001"
        conn.execute(
            "INSERT INTO sales(transaction_id, sales_id, invoice_number, sale_date, customer_id, quantity_kg, selling_price_kg, total_sale, staff_user, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tx, sid, f"ADU-INV-{today.replace('-', '')}-001", today, customer_id, 100, 2.5, 250.0, "admin", today),
        )
        sale = conn.execute(
            "SELECT transaction_id, sales_id, total_sale FROM sales WHERE transaction_id=?",
            (tx,),
        ).fetchone()

    tx = sale["transaction_id"]
    sid = sale["sales_id"]
    if conn.execute("SELECT 1 FROM payments WHERE transaction_id=? AND deleted=0 LIMIT 1", (tx,)).fetchone() is not None:
        print(f"PAYMENT_EXISTS {tx} {sid}")
        conn.close()
        return

    amount = float(sale["total_sale"]) if sale["total_sale"] else 250.0
    payment_id = f"ADU-PAY-{today.replace('-', '')}-001"
    conn.execute(
        "INSERT INTO payments(payment_id, transaction_id, sales_id, payment_date, amount, payment_method, payment_reference, notes, staff_user, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (payment_id, tx, sid, today, amount, "Mobile Money", "TEST-RECEIPT-1001", "Sample receipt preview", "admin", today),
    )
    conn.commit()
    print(f"CREATED {payment_id} {tx} {sid} {amount}")
    conn.close()


if __name__ == "__main__":
    main()
