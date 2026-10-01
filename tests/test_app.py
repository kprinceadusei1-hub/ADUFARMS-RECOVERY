import io
import re
import os
import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

TEST_DATABASE = Path(__file__).resolve().parent / "test-adufarms.db"
os.environ["DATABASE_PATH"] = str(TEST_DATABASE)
os.environ["ADUFARMS_SECRET_KEY"] = "test-secret-key"

import app as application
import backup_service
from stock_service import assert_stock_available, available_stock


@pytest.fixture(scope="session", autouse=True)
def initialized_database():
    if TEST_DATABASE.exists():
        TEST_DATABASE.unlink()
    application.init_db()
    yield
    if TEST_DATABASE.exists():
        TEST_DATABASE.unlink()


@pytest.fixture(scope="session", autouse=True)
def isolated_upload_dir(tmp_path_factory):
    """Uploaded dashboard images must never land in the real static folder."""
    original = application.DASHBOARD_IMAGE_DIR
    application.DASHBOARD_IMAGE_DIR = tmp_path_factory.mktemp("dashboard-uploads")
    yield
    application.DASHBOARD_IMAGE_DIR = original


@pytest.fixture
def client():
    application.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app.test_client() as test_client:
        yield test_client


def seed_admin():
    conn = application.db()
    conn.execute(
        "INSERT OR IGNORE INTO users(username,full_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)",
        ("admin", "Test Admin", generate_password_hash("StrongPassword1!"), "ADMIN", 1, application.now()),
    )
    conn.execute("UPDATE users SET role='ADMIN',active=1 WHERE username='admin'")
    conn.commit()
    conn.close()


def login_session(client):
    with client.session_transaction() as session:
        session.update(user_id=1, username="admin", full_name="Test Admin", role="ADMIN")


def test_health_endpoint_reports_system_readiness(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["database"]


def test_route_endpoints_build_without_errors():
    with application.app.test_request_context():
        for rule in application.app.url_map.iter_rules():
            if "<" not in rule.rule:
                application.url_for(rule.endpoint)


def test_invoice_status_boundaries():
    assert application.invoice_status_for(100, 0) == "UNPAID"
    assert application.invoice_status_for(100, 25) == "PART PAYMENT"
    assert application.invoice_status_for(100, 100) == "PAID"
    assert application.invoice_status_for(100, 101) == "OVERPAID"


def test_stock_service_rejects_sales_above_available_stock():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE purchases(quantity_received_kg REAL, deleted INTEGER);"
        "CREATE TABLE sales(quantity_kg REAL, deleted INTEGER);"
    )
    conn.execute("INSERT INTO purchases VALUES(100, 0)")
    conn.execute("INSERT INTO sales VALUES(40, 0)")
    assert available_stock(conn) == 60
    with pytest.raises(ValueError, match="Insufficient stock"):
        assert_stock_available(conn, 61)
    conn.close()


def test_dashboard_includes_customer_opening_balance(client):
    seed_admin()
    conn = application.db()
    conn.execute(
        "INSERT INTO customers(name,phone,opening_balance,created_at) VALUES(?,?,?,?)",
        ("Opening Balance Customer", "000", 125, application.now()),
    )
    conn.commit()
    conn.close()
    login_session(client)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"GHS 125.00" in response.data


def test_dashboard_handles_monthly_sales_data(client):
    seed_admin()
    conn = application.db()
    customer_id = conn.execute(
        "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
        ("Chart Customer", "111", application.now()),
    ).lastrowid
    conn.execute(
        "INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("ADU-CHART-1", "ADU-SAL-CHART-1", "ADU-INV-CHART-1", "2026-09-22", customer_id, 10, 5, 50, "admin", application.now()),
    )
    conn.commit()
    conn.close()
    login_session(client)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"Chart Customer" in response.data or b"Latest activity" in response.data


def test_dashboard_default_images_are_unique():
    assert len(application.DASHBOARD_IMAGE_DEFAULTS) == len(set(application.DASHBOARD_IMAGE_DEFAULTS.values()))


def test_dashboard_settings_page_is_separate_from_dashboard(client):
    seed_admin()
    login_session(client)
    response = client.get("/dashboard/settings")
    assert response.status_code == 200
    assert b"Dashboard imagery" in response.data
    assert b"dashboard-imagery" in response.data


def test_dashboard_imagery_accepts_sales_slot(client):
    seed_admin()
    login_session(client)
    with client.session_transaction() as session:
        session["csrf_token"] = "dashboard-sales-csrf"
    image_buffer = io.BytesIO()
    Image.new("RGB", (100, 60), color="green").save(image_buffer, format="PNG")
    image_buffer.seek(0)
    response = client.post(
        "/dashboard/images",
        data={
            "slot": "sales",
            "csrf_token": "dashboard-sales-csrf",
            "image": (image_buffer, "sales.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard/settings#dashboard-imagery"
    conn = application.db()
    saved = conn.execute("SELECT slot, filename FROM dashboard_images WHERE slot='sales'").fetchone()
    conn.close()
    assert saved is not None
    assert saved["slot"] == "sales"


def test_login_requires_csrf_token(client):
    response = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert response.status_code == 400


def test_session_role_is_refreshed_and_deactivated_users_are_logged_out(client):
    conn = application.db()
    conn.execute("UPDATE users SET role='ADMINISTRATOR' WHERE id=1")
    conn.commit()
    conn.close()
    login_session(client)
    assert client.get("/dashboard").status_code == 200

    conn = application.db()
    conn.execute("UPDATE users SET active=0 WHERE id=1")
    conn.commit()
    conn.close()
    assert client.get("/dashboard").status_code == 302


def test_viewer_cannot_read_customer_or_transaction_details(client):
    conn = application.db()
    conn.execute("UPDATE users SET active=1,role='VIEWER' WHERE id=1")
    customer_id = conn.execute(
        "INSERT INTO customers(name,phone,opening_balance,created_at) VALUES(?,?,?,?)",
        ("Private Customer", "111", 0, application.now()),
    ).lastrowid
    conn.commit()
    conn.close()
    with client.session_transaction() as session:
        session.update(user_id=1, username="admin", full_name="Test Admin", role="VIEWER")
    assert client.get(f"/customers/{customer_id}").status_code == 403
    assert client.get("/search?q=Private").status_code == 403


def test_invoice_opens_payment_collection_for_unpaid_sale(client):
    seed_admin()
    conn = application.db()
    conn.execute("UPDATE users SET active=1,role='ADMIN' WHERE id=1")
    customer_id = conn.execute(
        "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
        ("Payment Customer", "222", application.now()),
    ).lastrowid
    conn.execute(
        "INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("ADU-TEST-1", "ADU-SAL-TEST-1", "ADU-INV-TEST-1", "2026-09-22", customer_id, 10, 5, 50, "admin", application.now()),
    )
    conn.execute(
        "INSERT INTO invoices(invoice_number,transaction_id,sales_id,invoice_date,generated_by,generated_at) VALUES(?,?,?,?,?,?)",
        ("ADU-INV-TEST-1", "ADU-TEST-1", "ADU-SAL-TEST-1", "2026-09-22", "admin", application.now()),
    )
    conn.commit()
    conn.close()
    login_session(client)

    invoice_response = client.get("/invoice/ADU-SAL-TEST-1")
    assert invoice_response.status_code == 200
    assert b"Record payment" in invoice_response.data
    assert b"payments?customer_id=" in invoice_response.data

    payment_response = client.get(
        "/payments?customer_id=%s&sales_id=ADU-SAL-TEST-1" % customer_id
    )
    assert payment_response.status_code == 200
    assert b"ADU-SAL-TEST-1" in payment_response.data


def test_invoice_pdf_stays_on_one_a4_page(client):
    seed_admin()
    conn = application.db()
    conn.execute("UPDATE users SET active=1,role='ADMIN' WHERE id=1")
    customer_id = conn.execute(
        "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
        ("PDF Customer", "333", application.now()),
    ).lastrowid
    conn.execute(
        "INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("ADU-PDF-ONE", "ADU-SAL-PDF-ONE", "ADU-INV-PDF-ONE", "2026-09-23", customer_id, 12, 6, 72, "admin", application.now()),
    )
    conn.execute(
        "INSERT INTO invoices(invoice_number,transaction_id,sales_id,invoice_date,generated_by,generated_at) VALUES(?,?,?,?,?,?)",
        ("ADU-INV-PDF-ONE", "ADU-PDF-ONE", "ADU-SAL-PDF-ONE", "2026-09-23", "admin", application.now()),
    )
    conn.commit()
    conn.close()
    login_session(client)

    response = client.get("/invoice/ADU-SAL-PDF-ONE/pdf?inline=1")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert len(re.findall(rb"/Type /Page(?!s)", response.data)) == 1
    assert len(response.data) < 600_000, "logo should be downsampled"


def test_customer_creation_rejects_duplicate_normalized_identity(client):
    seed_admin()
    login_session(client)
    conn = application.db()
    conn.execute(
        "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
        ("Lord Sekyi", "059 905 5062", application.now()),
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    conn.close()
    with client.session_transaction() as session:
        session["csrf_token"] = "duplicate-test-csrf"
    response = client.post("/customers", data={
        "csrf_token": "duplicate-test-csrf",
        "name": "  lord   sekyi ",
        "phone": "059-905-5062",
        "opening_balance": "0",
        "customer_type": "RETAIL",
    })
    assert response.status_code == 302
    assert "/sales?customer_id=" in response.headers["Location"]
    conn = application.db()
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == before
    conn.close()


def test_purchase_sale_payment_cycle_remains_consistent(client):
    conn = application.db()
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in ("payments", "sales", "purchases", "customers", "users", "audit_log", "stock_movements"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('users','customers','purchases','sales','payments','audit_log','stock_movements')")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()

    seed_admin()
    login_session(client)
    with client.session_transaction() as session:
        session["csrf_token"] = "e2e-flow-csrf"

    customer_response = client.post(
        "/customers",
        data={
            "csrf_token": "e2e-flow-csrf",
            "name": "Ama Boateng",
            "phone": "024 456 7890",
            "location": "Kumasi",
            "address": "Adum Market",
            "opening_balance": "0",
            "customer_type": "RETAIL",
        },
        follow_redirects=False,
    )
    assert customer_response.status_code == 302

    purchase_response = client.post(
        "/purchases",
        data={
            "csrf_token": "e2e-flow-csrf",
            "purchase_date": application.now().split()[0],
            "local_agent": "Farming Group",
            "agent_phone": "020 000 0000",
            "location": "Ejura",
            "quantity_kg": "100",
            "quantity_received_kg": "100",
            "price_per_kg": "4.2",
            "transport_cost": "40",
            "other_expenses": "15",
        },
        follow_redirects=False,
    )
    assert purchase_response.status_code == 302

    conn = application.db()
    customer_id = conn.execute("SELECT id FROM customers WHERE name='Ama Boateng'").fetchone()["id"]
    sale_response = client.post(
        "/sales",
        data={
            "csrf_token": "e2e-flow-csrf",
            "sale_date": application.now().split()[0],
            "customer_id": str(customer_id),
            "customer_name": "Ama Boateng",
            "customer_phone": "024 456 7890",
            "quantity_kg": "50",
            "selling_price_kg": "6.50",
        },
        follow_redirects=False,
    )
    assert sale_response.status_code == 302
    sale = conn.execute(
        "SELECT transaction_id, total_sale, quantity_kg FROM sales WHERE customer_id=? ORDER BY id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()

    payment_response = client.post(
        "/payments",
        data={
            "csrf_token": "e2e-flow-csrf",
            "payment_date": application.now().split()[0],
            "transaction_id": sale["transaction_id"],
            "customer_id": str(customer_id),
            "amount": "150",
            "payment_method": "MOBILE MONEY",
            "payment_reference": "MM-001",
        },
        follow_redirects=False,
    )
    assert payment_response.status_code in {200, 302}
    payment_total = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE transaction_id=? AND deleted=0",
        (sale["transaction_id"],),
    ).fetchone()[0]
    stock_remaining = conn.execute(
        "SELECT COALESCE((SELECT SUM(quantity_received_kg) FROM purchases WHERE deleted=0),0) - COALESCE((SELECT SUM(quantity_kg) FROM sales WHERE deleted=0),0)"
    ).fetchone()[0]
    conn.close()
    assert sale["total_sale"] == 325.0
    assert payment_total == 150.0
    assert stock_remaining == 50.0


def test_backup_restore_verifies_and_preserves_safety_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker VALUES('original')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup_service, "BACKUP_DIR", backup_dir)

    backup = backup_service.create_backup(source)
    assert backup.with_suffix(".json").exists()
    backup_service.verify_backup(backup)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE marker SET value='changed'")
    conn.commit()
    conn.close()

    safety_backup = backup_service.safe_restore(source, backup.name)
    conn = sqlite3.connect(source)
    assert conn.execute("SELECT value FROM marker").fetchone()[0] == "original"
    conn.close()
    assert safety_backup.exists()
    backup_service.verify_database(safety_backup)


def _seed_dashboard_data():
    """One old unpaid sale (aged), one paid recent sale, one purchase."""
    from datetime import date, timedelta
    conn = application.db()
    for table in ("payments", "invoices", "sales", "purchases", "stock_movements"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM customers")
    cid = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES('Aged Debtor','1',?)", (application.now(),)).lastrowid
    old, recent = (date.today() - timedelta(days=75)).isoformat(), date.today().isoformat()
    conn.execute("INSERT INTO purchases(purchase_id,purchase_date,local_agent,quantity_kg,price_per_kg,total_purchase_cost,total_cost,quantity_received_kg,staff_user,created_at) VALUES('P1',?,'Agent',1000,2,2000,2000,1000,'admin',?)", (old, application.now()))
    application.stock_svc.apply_purchase_create(conn, "P1", 1000, old, "admin", application.now())
    for tid, day, total in (("T-OLD", old, 500.0), ("T-NEW", recent, 300.0)):
        conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (tid, "S-" + tid, "I-" + tid, day, cid, 100, total / 100, total, "admin", application.now()))
    conn.execute("INSERT INTO payments(payment_id,transaction_id,payment_date,amount,payment_method,staff_user,created_at) VALUES('PAY1','T-NEW',?,300,'CASH','admin',?)", (recent, application.now()))
    conn.commit()
    conn.close()


def test_dashboard_renders_every_period_and_survives_bad_input(client):
    seed_admin()
    login_session(client)
    for key in ("today", "7d", "30d", "mtd", "90d", "all", "nonsense"):
        response = client.get(f"/dashboard?range={key}")
        assert response.status_code == 200, key
    html = client.get("/dashboard").get_data(as_text=True)
    for fabricated in ("Maize Seeds", "Animal Feed", "Organic Fertilizer", "from last month"):
        assert fabricated not in html


def test_dashboard_service_ages_receivables_and_flags_overdue_debt():
    import dashboard_service
    from datetime import date
    _seed_dashboard_data()
    conn = application.db()
    data = dashboard_service.build_dashboard(conn, "all", date.today(), application.money, application.cogs_summary(conn), 900.0, 100.0)
    conn.close()
    assert data["totals"]["revenue"] == 800.0
    assert data["totals"]["collected"] == 300.0
    assert round(data["receivables"]["buckets"]["Over 60 days"], 2) == 500.0
    assert round(data["receivables"]["total"], 2) == 500.0
    assert [d["name"] for d in data["debtors"]] == ["Aged Debtor"] and data["debtors"][0]["oldest"] >= 75
    assert any(i["level"] == "danger" and "over 60 days" in i["title"] for i in data["insights"])
    assert data["counts"]["sales"] == 2 and data["is_empty"] is False


def test_dashboard_period_math_and_comparison_window():
    import dashboard_service
    from datetime import date
    today = date(2026, 9, 30)
    assert dashboard_service.resolve_period("7d", today)[1:3] == (date(2026, 9, 24), today)
    key, start, end, prev = dashboard_service.resolve_period("30d", today)
    assert (end - start).days == 29 and prev[1] == start.replace(day=start.day) - __import__("datetime").timedelta(days=1)
    assert dashboard_service.resolve_period("all", today)[3] is None
    assert dashboard_service.resolve_period("bogus", today)[0] == "30d"
    assert dashboard_service._delta(150, 100) == {"pct": 50.0, "direction": "up"}
    assert dashboard_service._delta(5, 0) is None and dashboard_service._delta(5, None) is None


def test_dashboard_empty_system_shows_getting_started(client):
    seed_admin()
    login_session(client)
    conn = application.db()
    for table in ("payments", "invoices", "sales", "purchases", "stock_movements"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM customers")
    conn.commit()
    conn.close()
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Get started" in html and "Record a purchase" in html


def test_dashboard_hides_finance_widgets_from_roles_without_access(client):
    seed_admin()
    _seed_dashboard_data()
    login_session(client)
    conn = application.db()
    conn.execute("UPDATE users SET role='INVENTORY_OFFICER' WHERE id=1")
    conn.commit()
    conn.close()
    try:
        html = client.get("/dashboard").get_data(as_text=True)
    finally:  # the app re-reads the role from the database on every request, so restore it
        conn = application.db()
        conn.execute("UPDATE users SET role='ADMIN' WHERE id=1")
        conn.commit()
        conn.close()
    assert "Who owes you" not in html and "Receivables ageing" not in html
    assert "Stock position" in html


def test_sales_list_shows_newest_sale_first(client):
    seed_admin()
    login_session(client)
    conn = application.db()
    customer_id = conn.execute(
        "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)", ("Order Customer", "444", application.now())
    ).lastrowid
    for tid, day in (("ADU-ORD-OLD", "2026-01-05"), ("ADU-ORD-NEW", "2026-08-20")):
        conn.execute(
            "INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, tid.replace("ADU-", "ADU-SAL-"), tid.replace("ADU-", "ADU-INV-"), day, customer_id, 1, 1, 1, "admin", application.now()),
        )
    conn.commit()
    conn.close()
    html = client.get("/sales").get_data(as_text=True)
    assert html.index("ADU-SAL-ORD-NEW") < html.index("ADU-SAL-ORD-OLD")


def test_short_date_filter_is_compact_and_safe():
    assert application.short_date("2026-09-27") == "27 Sep 2026"
    assert application.short_date("2026-09-27 14:03:00") == "27 Sep 2026"
    assert application.short_date(None) == "-"
    assert application.short_date("not a date") == "not a date"


def test_create_admin_never_uses_known_passwords_or_overwrites(tmp_path, monkeypatch):
    import importlib
    import sqlite3

    db_file = tmp_path / "accounts.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, full_name TEXT,
        password_hash TEXT, role TEXT, active INTEGER, created_at TEXT, failed_login_attempts INTEGER DEFAULT 0, locked_until TEXT)""")
    conn.execute("INSERT INTO users(username,full_name,password_hash,role,active,created_at) VALUES('admin','Keep Me','existing-hash','ADMIN',1,'x')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    for name in ("ADUFARMS_ADMIN_PASSWORD", "ADUFARMS_MANAGER_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    import create_admin
    importlib.reload(create_admin)
    create_admin.main(reset=False)

    conn = sqlite3.connect(db_file)
    rows = dict(conn.execute("SELECT username,password_hash FROM users").fetchall())
    conn.close()
    assert rows["admin"] == "existing-hash"          # untouched without --reset
    assert {"manager", "sales", "inventory", "accounts"} <= set(rows)
    from werkzeug.security import check_password_hash
    for known in ("Manager@2026!", "Sales@2026!", "Inventory@2026!", "Accounts@2026!"):
        assert not any(check_password_hash(h, known) for h in rows.values() if h != "existing-hash")


def _seed_analytics_data():
    """Two suppliers (one with transit loss), two customers, known prices and a payment lag."""
    from datetime import date, timedelta
    conn = application.db()
    for table in ("payments", "invoices", "sales", "purchases", "stock_movements"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM customers")
    big = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES('Big Buyer','1',?)", (application.now(),)).lastrowid
    small = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES('Small Buyer','2',?)", (application.now(),)).lastrowid
    d = lambda n: (date.today() - timedelta(days=n)).isoformat()
    for pid, agent, qty, recv, cost, day in (("PA", "Steady Agent", 1000, 1000, 2000.0, d(40)), ("PB", "Leaky Agent", 1000, 900, 2700.0, d(35))):
        conn.execute("INSERT INTO purchases(purchase_id,purchase_date,local_agent,quantity_kg,price_per_kg,total_purchase_cost,total_cost,quantity_received_kg,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (pid, day, agent, qty, cost / qty, cost, cost, recv, "admin", application.now()))
        application.stock_svc.apply_purchase_create(conn, pid, recv, day, "admin", application.now())
    for tid, cust, day, kg, total in (("A1", big, d(20), 500, 3000.0), ("A2", big, d(10), 300, 1800.0), ("A3", small, d(5), 100, 600.0)):
        conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (tid, "S-" + tid, "I-" + tid, day, cust, kg, total / kg, total, "admin", application.now()))
        application.stock_svc.apply_sale_create(conn, tid, kg, day, "admin", application.now())
    conn.execute("INSERT INTO payments(payment_id,transaction_id,payment_date,amount,payment_method,staff_user,created_at) VALUES('P1','A1',?,3000,'CASH','admin',?)", (d(10), application.now()))
    conn.commit()
    conn.close()


def test_analytics_numbers_are_correct():
    import analytics_service
    from datetime import date
    _seed_analytics_data()
    conn = application.db()
    a = analytics_service.build_analytics(conn, "all", date.today(), application.money, application.cogs_summary(conn))
    conn.close()
    k = a["kpis"]
    assert k["revenue"] == 5400.0 and k["kg_sold"] == 900.0 and k["orders"] == 3
    assert round(k["sell_price"], 2) == 6.0                      # 5400 / 900 KG
    assert round(k["buy_price"], 3) == round(4700 / 1900, 3)    # cost / KG actually received
    assert round(k["shrink_pct"], 4) == round(100 / 2000, 4) and k["shrink_kg"] == 100.0
    assert round(k["days_to_pay"]) == 10                         # 3000 paid 10 days after the sale
    assert round(k["net_cash"], 2) == round(3000 - 4700, 2)
    assert [c["name"] for c in a["customers"]] == ["Big Buyer", "Small Buyer"]
    assert a["customers"][0]["abc"] == "A" and abs(sum(c["share"] for c in a["customers"]) - 1) < 1e-9
    leaky = next(s for s in a["suppliers"] if s["name"] == "Leaky Agent")
    assert round(leaky["loss_pct"], 3) == 0.1 and round(leaky["cost_per_kg"], 2) == 3.0
    assert any("never arrived" in i["title"] for i in a["insights"])
    assert a["timeline"]["stock"][-1] == 1000.0                 # 1900 received - 900 sold


def test_analytics_page_and_exports(client):
    seed_admin()
    _seed_analytics_data()
    login_session(client)
    for key in ("30d", "90d", "6m", "12m", "all", "junk"):
        assert client.get(f"/analytics?range={key}").status_code == 200
    html = client.get("/analytics?range=all").get_data(as_text=True)
    assert "Customer performance" in html and "Supplier performance" in html and "<<<<<<<" not in html
    for section, needle in (("customers", "Big Buyer"), ("suppliers", "Leaky Agent"), ("timeline", "Revenue")):
        response = client.get(f"/analytics/export/{section}?range=all")
        assert response.status_code == 200 and response.mimetype == "text/csv" and needle.encode() in response.data
    assert client.get("/analytics/export/unknown").status_code == 404


def test_analytics_is_restricted_to_management_roles(client):
    seed_admin()
    login_session(client)

    def as_role(role):
        conn = application.db()
        conn.execute("UPDATE users SET role=? WHERE id=1", (role,))
        conn.commit()
        conn.close()
        page = client.get("/analytics")
        export = client.get("/analytics/export/customers")
        link = b'href="/analytics"' in client.get("/reports").data
        return page.status_code, export.status_code, link

    try:
        assert as_role("STAFF") == (403, 403, False)        # blocked, and no sidebar link
        assert as_role("SALES_OFFICER")[0] == 403
        assert as_role("MANAGER") == (200, 200, True)
        assert as_role("ACCOUNTANT") == (200, 200, True)
    finally:
        conn = application.db()
        conn.execute("UPDATE users SET role='ADMIN' WHERE id=1")
        conn.commit()
        conn.close()


def test_analytics_health_forecast_custom_range_and_at_risk():
    import analytics_service
    from datetime import date, timedelta
    _seed_analytics_data()
    conn = application.db()
    cogs = application.cogs_summary(conn)
    today = date.today()
    a = analytics_service.build_analytics(conn, "all", today, application.money, cogs)
    assert a["health"]["score"] is not None and 0 <= a["health"]["score"] <= 100
    assert a["health"]["components"] == sorted(a["health"]["components"], key=lambda c: c["score"])
    assert all(0 <= c["score"] <= 100 for c in a["health"]["components"])
    # custom range wins over the preset, is clamped to today, and has a same-length comparison window
    custom = analytics_service.build_analytics(conn, "30d", today, application.money, cogs, (today - timedelta(days=30)).isoformat(), "2099-01-01")
    assert custom["range_key"] == "custom" and custom["date_to"] == today.isoformat() and custom["compare_text"]
    junk = analytics_service.build_analytics(conn, "30d", today, application.money, cogs, "zzz", "qqq")
    assert junk["range_key"] == "30d"
    reversed_ = analytics_service.build_analytics(conn, "30d", today, application.money, cogs, today.isoformat(), (today - timedelta(days=9)).isoformat())
    assert reversed_["range_key"] == "30d"                     # from > to is ignored
    # an empty window must not crash the comparison logic (previous has data, current does not)
    quiet = analytics_service.build_analytics(conn, "custom", today, application.money, cogs, (today - timedelta(days=3)).isoformat(), today.isoformat())
    assert quiet["deltas"]["sell_price"] is None
    # forecast needs 4 active weeks; seeded data has fewer, so it must say "not ready" rather than invent a number
    assert a["forecast"]["ready"] is False and a["forecast"]["projected_total"] is None
    conn.close()


def test_analytics_forecast_follows_a_clear_uptrend_and_flags_silent_customers():
    import analytics_service
    from datetime import date, timedelta
    conn = application.db()
    for table in ("payments", "invoices", "sales", "purchases", "stock_movements"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM customers")
    cid = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES('Regular','9',?)", (application.now(),)).lastrowid
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    for i in range(8):                                         # revenue rises 100 per week for 8 weeks
        day = (monday - timedelta(weeks=8 - i)).isoformat()
        conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (f"W{i}", f"S-W{i}", f"I-W{i}", day, cid, 10, 10 * (i + 1), 100.0 * (i + 1), "admin", application.now()))
    conn.commit()
    fc = analytics_service._forecast(conn, today, None)
    assert fc["ready"] and fc["future"] == sorted(fc["future"]) and fc["future"][0] > fc["history"][-1] - 1
    assert fc["trend_pct"] is not None and fc["trend_pct"] > 0
    risk = analytics_service._at_risk(conn, today + timedelta(days=60))   # 60 days of silence vs a 7-day rhythm
    assert [r["name"] for r in risk] == ["Regular"] and risk[0]["usual_days"] == 7
    assert analytics_service._at_risk(conn, today) == []                  # recently active: not at risk
    conn.close()


def test_analytics_health_score_is_not_invented_for_an_empty_period():
    import analytics_service
    from datetime import date
    conn = application.db()
    for table in ("payments", "invoices", "sales", "purchases", "stock_movements"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM customers")
    conn.commit()
    a = analytics_service.build_analytics(conn, "all", date.today(), application.money, application.cogs_summary(conn))
    conn.close()
    assert a["health"]["score"] is None and a["health"]["grade"] == "Not enough data" and not a["health"]["components"]
    assert a["has_data"] is False


def test_purchase_analytics_real_cost_loss_and_supplier_grades():
    import purchases_service
    from datetime import date
    _seed_analytics_data()        # Steady: 1000 KG, all arrived, GHS 2,000 | Leaky: 1000 KG bought, 900 arrived, GHS 2,700
    conn = application.db()
    pa = purchases_service.build_purchase_analytics(conn, "all", date.today(), application.money, 1000.0, 100.0, 30.0)
    conn.close()
    k = pa["kpis"]
    assert k["spend"] == 4700.0 and k["received"] == 1900.0 and k["count"] == 2
    assert round(k["landed"], 4) == round(4700 / 1900, 4)                   # real cost uses KG that ARRIVED
    assert k["loss_kg"] == 100.0 and round(k["loss_value"], 2) == 270.0      # 100 KG short x GHS 2.70 paid per KG
    by = {s["name"]: s for s in pa["suppliers"]}
    assert round(by["Steady Agent"]["landed"], 2) == 2.0 and round(by["Leaky Agent"]["landed"], 2) == 3.0
    assert by["Steady Agent"]["grade"] == "A" and by["Leaky Agent"]["grade"] == "C"
    assert abs(sum(s["share"] for s in pa["suppliers"]) - 1) < 1e-9
    assert any("paid for maize that never arrived" in i["title"] for i in pa["insights"])
    assert pa["breakdown"]["goods"] == 4700.0


def test_purchase_restock_assistant_and_form_memory():
    import purchases_service
    from datetime import date
    _seed_analytics_data()
    conn = application.db()
    today = date.today()
    assert purchases_service.sales_pace(conn, today) == 30.0                 # 900 KG sold in 30 days
    comfy = purchases_service.build_purchase_analytics(conn, "all", today, application.money, 1000.0, 100.0, 30.0)["reorder"]
    assert comfy["suggested_kg"] == 0 and comfy["urgent"] is False and round(comfy["cover_days"], 1) == 33.3
    low = purchases_service.build_purchase_analytics(conn, "all", today, application.money, 300.0, 100.0, 30.0)["reorder"]
    assert low["suggested_kg"] == 600.0 and low["cover_days"] == 10.0 and low["urgent"] is True   # 30 days x 30 KG - 300 on hand
    none = purchases_service.build_purchase_analytics(conn, "all", today, application.money, 300.0, 100.0, 0.0)["reorder"]
    assert none["has_pace"] is False and none["suggested_kg"] == 0
    mem = purchases_service.supplier_index(conn, today)
    conn.close()
    assert mem["suppliers"]["Leaky Agent"]["typical_loss"] == 0.1 and mem["suppliers"]["Steady Agent"]["typical_loss"] == 0.0
    assert mem["suppliers"]["Steady Agent"]["last_price"] == 2.0
    assert mem["market_price"] == 2.35                                        # weighted by KG: (2.0x1000 + 2.7x1000) / 2000


def test_purchases_page_tabs_filters_and_export(client):
    seed_admin()
    _seed_analytics_data()
    login_session(client)
    html = client.get("/purchases").get_data(as_text=True)
    for needle in ("Record purchase", "Purchase check", "Supplier scorecard", "Restock assistant", "supplierList"):
        assert needle in html
    for q in ("?range=all", "?range=bad", "?from=2026-01-01&to=2026-12-31", "?q=Leaky", "?supplier=nobody", "?q=%25"):
        assert client.get("/purchases" + q).status_code == 200, q
    filtered = client.get("/purchases?supplier=Leaky+Agent").get_data(as_text=True)
    assert "PB" in filtered and "PA" not in filtered.split("purchaseTable")[1].split("</table>")[0]
    export = client.get("/purchases/export?supplier=Leaky+Agent")
    assert export.status_code == 200 and export.mimetype == "text/csv"
    lines = export.data.decode("utf-8-sig").strip().splitlines()
    assert len(lines) == 2 and "Leaky Agent" in lines[1] and "3.000" in lines[1]       # header + one row, landed cost 2700/900
    conn = application.db()
    conn.execute("UPDATE users SET role='VIEWER' WHERE id=1")
    conn.commit()
    conn.close()
    try:
        assert client.get("/purchases/export").status_code == 403           # viewers cannot export
    finally:
        conn = application.db()
        conn.execute("UPDATE users SET role='ADMIN' WHERE id=1")
        conn.commit()
        conn.close()


def test_startup_backup_is_skipped_when_nothing_changed_and_duplicates_can_be_removed(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(backup_service, "BACKUP_DIR", tmp_path)
    db_file = tmp_path / "live.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    first = backup_service.create_backup(db_file, skip_if_unchanged=True)
    again = backup_service.create_backup(db_file, skip_if_unchanged=True)
    assert again == first and len(list(tmp_path.glob("adufarms_*.db"))) == 1      # unchanged data: no second copy
    forced = backup_service.create_backup(db_file)                                 # a manual backup always creates one
    assert forced != first and len(list(tmp_path.glob("adufarms_*.db"))) == 2
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    conn.close()
    changed = backup_service.create_backup(db_file, skip_if_unchanged=True)        # data changed: a new backup is kept
    assert changed not in (first, forced) and len(list(tmp_path.glob("adufarms_*.db"))) == 3
    safety = tmp_path / "pre_fresh_start_20260930_000000.db"
    safety.write_bytes(first.read_bytes())                                         # same content, but a named safety snapshot
    preview = backup_service.dedupe_backups(dry_run=True)
    assert preview == [forced.name] and forced.exists()                            # dry run removes nothing
    assert backup_service.dedupe_backups() == [forced.name]
    assert not forced.exists() and not forced.with_suffix(".json").exists()
    assert first.exists() and changed.exists() and safety.exists()                # oldest copy, changed data and safety snapshot survive


def test_sales_analytics_numbers_status_mix_and_customer_memory():
    import sales_service
    from datetime import date
    _seed_analytics_data()        # Big Buyer: A1 500kg GHS3000 (paid), A2 300kg GHS1800 (unpaid) | Small Buyer: A3 100kg GHS600 (unpaid)
    conn = application.db()
    cogs = application.cogs_summary(conn)
    cost = cogs["unit_cost"]
    assert round(cost, 4) == round(4700 / 1900, 4)
    sa = sales_service.build_sales_analytics(conn, "all", date.today(), application.money, cost, 1000.0)
    k = sa["kpis"]
    assert k["revenue"] == 5400.0 and k["orders"] == 3 and k["kg"] == 900.0
    assert round(k["avg_price"], 4) == 6.0 and round(k["avg_order"], 2) == 1800.0
    assert round(k["profit"], 2) == round(5400 - 900 * cost, 2) and round(k["margin"], 4) == round((5400 - 900 * cost) / 5400, 4)
    assert k["settled"] == 3000.0 and k["outstanding"] == 2400.0 and round(k["collection_rate"], 4) == round(3000 / 5400, 4)
    mix = {m["label"]: m for m in sa["status_mix"]}
    assert mix["Fully paid"]["count"] == 1 and mix["Unpaid"]["count"] == 2 and round(mix["Unpaid"]["amount"], 2) == 2400.0
    assert [c["name"] for c in sa["top_customers"]] == ["Big Buyer", "Small Buyer"] and round(sa["top_customers"][0]["share"], 4) == round(4800 / 5400, 4)
    assert sa["risky"]["below_cost"] == 0
    assert round(sa["pricing"]["targets"][0]["price"], 4) == round(cost / 0.9, 4)            # 10% margin price
    memory = sales_service.customer_memory(conn, date.today())
    big = next(v for v in memory.values() if v["orders"] == 2)
    assert big["owed"] == 1800.0 and big["last_price"] == 6.0 and big["usual_qty"] == 400.0 and big["unpaid_sales"] == 1
    # a sale priced below cost must be flagged, with its loss valued
    cid = conn.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES('BC','S-BC','I-BC',?,?,100,1.5,150,'admin',?)",
                 (date.today().isoformat(), cid, application.now()))
    conn.commit()
    risky = sales_service.build_sales_analytics(conn, "all", date.today(), application.money, cost, 1000.0)["risky"]
    assert risky["below_cost"] == 1 and round(risky["lost_value"], 2) == round((cost - 1.5) * 100, 2) and risky["examples"][0]["sales_id"] == "S-BC"
    assert any("below your cost" in i["title"] for i in sales_service.build_sales_analytics(conn, "all", date.today(), application.money, cost, 1000.0)["insights"])
    conn.close()


def test_sales_page_filters_export_and_form_behaviour(client):
    import sales_service
    from werkzeug.datastructures import MultiDict
    seed_admin()
    _seed_analytics_data()
    login_session(client)
    html = client.get("/sales").get_data(as_text=True)
    for needle in ("Sale check", "Pricing guide", "Sales ledger", "saleForm"):
        assert needle in html
    for q in ("?range=all", "?range=bad", "?from=2026-01-01&to=2026-12-31", "?q=Big", "?status=zzz&customer=abc", "?q=%25"):
        assert client.get("/sales" + q).status_code == 200, q
    def ids(query):
        table = client.get("/sales" + query).get_data(as_text=True).split("salesTable")[1].split("</table>")[0]
        return sorted(x for x in ("S-A1", "S-A2", "S-A3") if x in table)
    assert ids("") == ["S-A1", "S-A2", "S-A3"]
    assert ids("?status=unpaid") == ["S-A2", "S-A3"] and ids("?status=paid") == ["S-A1"]
    assert ids("?q=Small") == ["S-A3"]
    where, params, echo = sales_service.sales_filters(MultiDict({"status": "hack'; DROP TABLE sales;--", "sfrom": "not-a-date"}), lambda a: "1=1")
    assert echo["status"] == "" and "DROP" not in where and params == []                      # junk filters are ignored, never injected
    export = client.get("/sales/export?status=unpaid")
    lines = export.data.decode("utf-8-sig").strip().splitlines()
    assert export.status_code == 200 and len(lines) == 3 and "UNPAID" in lines[1] and "Estimated profit" in lines[0]
    # the form still creates a sale end to end and stock drops
    before = application.stock_summary()[2]
    with client.session_transaction() as session:
        session["csrf_token"] = "t"
    client.post("/sales", data={"csrf_token": "t", "sale_date": "2026-09-30", "customer_name": "Brand New Buyer", "customer_phone": "0200000001",
                                "quantity_kg": "10", "selling_price_kg": "6"})
    assert application.stock_summary()[2] == before - 10


def test_customer_credit_score_maths_and_segments():
    import customers_service as cs
    from datetime import date, timedelta
    # Hand-checked: speed 94.3 (x30) + overdue 100 (x30) + exposure 45 (x20) + paid-in-full 28.6 (x20) = 73.0
    assert cs.credit_score(10, 10, 1800, 4800, 0.5) == (73, "Good")
    # no payments yet: speed is unknown, so only overdue (100), exposure (0) and paid-in-full (0) count: 3000/70 = 42.9
    assert cs.credit_score(None, 5, 600, 600, 0.0) == (43, "Risky")
    assert cs.credit_score(None, 0, 0, 0, None) == (None, "No history")
    assert cs.credit_score(3, 0, 0, 1000, 1.0)[0] == 100                                   # instant, always-paid customer

    _seed_analytics_data()
    conn = application.db()
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    def customer(name, sales, pay_same_day=True):
        cid = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)", (name, None, application.now())).lastrowid
        for i, (days_ago, total) in enumerate(sales):
            tid = f"{name[:3]}{i}"
            conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (tid, "S-" + tid, "I-" + tid, d(days_ago), cid, 100, total / 100, total, "admin", application.now()))
            if pay_same_day:
                conn.execute("INSERT INTO payments(payment_id,transaction_id,payment_date,amount,payment_method,staff_user,created_at) VALUES(?,?,?,?,'CASH','admin',?)",
                             ("P-" + tid, tid, d(days_ago), total, application.now()))
        return cid
    customer("Star Trader", [(21, 9000), (14, 9000), (7, 9000), (1, 9000)])                 # big, frequent, recent, pays at once
    customer("Quiet Regular", [(90, 500), (83, 500), (76, 500), (69, 500)])                  # every 7 days, silent for 69 days
    customer("Old Timer", [(300, 500), (250, 500)])                                          # nothing for 250 days, only 2 orders
    customer("Never Bought", [])
    conn.commit()
    profiles = cs.build_profiles(conn, today, 2.0)
    seg = {p["name"]: p["segment"] for p in profiles}
    assert seg["Star Trader"] == "champion" and seg["Quiet Regular"] == "at_risk" and seg["Old Timer"] == "dormant" and seg["Never Bought"] == "prospect"
    assert seg["Big Buyer"] == "new" and seg["Small Buyer"] == "new"
    by = {p["name"]: p for p in profiles}
    assert by["Big Buyer"]["score"] == 73 and by["Big Buyer"]["balance"] == 1800.0 and by["Big Buyer"]["oldest_unpaid"] == 10
    assert round(by["Big Buyer"]["days_to_pay"]) == 10 and by["Small Buyer"]["score"] == 43
    assert round(by["Quiet Regular"]["usual_interval"]) == 7 and by["Quiet Regular"]["days_since"] == 69
    assert round(sum(p["share"] for p in profiles), 9) == 1.0 and by["Star Trader"]["abc"] == "A" and by["Never Bought"]["abc"] == "-"
    summary = {s["key"]: s for s in cs.segment_summary(profiles)}
    assert summary["at_risk"]["count"] == 1 and summary["prospect"]["count"] == 1 and summary["champion"]["count"] == 1
    data = cs.build_customer_insights(conn, today, application.money, 2.0, profiles)
    assert data["call_list"][0]["name"] == "Quiet Regular" and data["top_debtors"][0]["name"] == "Big Buyer"
    assert any("gone quiet" in i["title"] for i in data["insights"])
    months = data["cohort"]
    # last 6 months = the three seeded sales (5400) + Star Trader (36000) + Quiet Regular (2000); Old Timer is older than that
    assert round(sum(months["new"]) + sum(months["returning"]), 2) == 43400.0
    conn.close()


def test_customer_directory_filters_page_and_export(client):
    import customers_service as cs
    from werkzeug.datastructures import MultiDict
    seed_admin()
    _seed_analytics_data()
    login_session(client)
    html = client.get("/customers").get_data(as_text=True)
    for needle in ("Duplicate check", "Customer insights", "customerForm", "Add customer"):
        assert needle in html
    for q in ("?q=Big", "?type=RETAIL", "?segment=at_risk", "?segment=nonsense", "?owing=1", "?page=9", "?q=%25&type=zzz"):
        assert client.get("/customers" + q).status_code == 200, q
    def names(query):
        body = client.get("/customers" + query).get_data(as_text=True).split("customersTable")[1].split("</table>")[0]
        return sorted(n for n in ("Big Buyer", "Small Buyer") if n in body)
    assert names("") == ["Big Buyer", "Small Buyer"] and names("?q=small") == ["Small Buyer"] and names("?owing=1") == ["Big Buyer", "Small Buyer"]
    assert names("?segment=prospect") == []
    shown, echo = cs.directory_filters(MultiDict({"segment": "'; DROP--", "type": "retail", "q": " BIG "}), [
        {"name": "Big Buyer", "phone": "", "location": "", "type": "RETAIL", "segment": "new", "balance": 0}])
    assert echo["segment"] == "" and echo["type"] == "RETAIL" and echo["q"] == "big" and len(shown) == 1
    export = client.get("/customers/export?owing=1")
    lines = export.data.decode("utf-8-sig").strip().splitlines()
    assert export.status_code == 200 and export.mimetype == "text/csv" and len(lines) == 3 and "Credit score" in lines[0]
    # duplicate registration still redirects to the existing customer (server-side safety net behind the live check)
    with client.session_transaction() as session:
        session["csrf_token"] = "t"
    def customer_count():
        conn = application.db()
        try:
            return conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
        finally:
            conn.close()
    before = customer_count()
    resp = client.post("/customers", data={"csrf_token": "t", "name": "Someone Else", "phone": "", "opening_balance": "0"})
    assert resp.status_code == 302 and customer_count() == before + 1
    client.post("/customers", data={"csrf_token": "t", "name": "Phone Owner", "phone": "0244123456", "opening_balance": "0"})
    after_owner = customer_count()
    dup = client.post("/customers", data={"csrf_token": "t", "name": "Different Name", "phone": "+233 244 123 456", "opening_balance": "0"})
    assert dup.status_code == 302 and "/sales" in dup.headers["Location"] and "customer_id=" in dup.headers["Location"]
    assert customer_count() == after_owner                      # same phone (other format): no second record is created


def test_phone_numbers_match_across_international_and_local_formats():
    assert application.normalized_phone("+233 24 412 3456") == application.normalized_phone("024 412 3456") == "0244123456"
    assert application.normalized_phone("00233244123456") == "0244123456"
    assert application.normalized_phone("233244123456") == "0244123456"
    assert application.normalized_phone("0244123456") == "0244123456"
    assert application.normalized_phone("") == "" and application.normalized_phone(None) == ""
    assert application.normalized_phone("2331") == "2331"               # too short to be an international number: left alone


def test_payment_analytics_numbers_watchlist_and_form_memory():
    import payments_service
    from datetime import date, timedelta
    _seed_analytics_data()       # one payment: GHS 3000 cash against A1 (sold 20 days ago), paid 10 days ago
    conn = application.db()
    today = date.today()
    pa = payments_service.build_payment_analytics(conn, "all", today, application.money)
    k = pa["kpis"]
    assert k["collected"] == 3000.0 and k["count"] == 1 and k["payers"] == 1 and k["biggest"] == 3000.0
    assert k["billed"] == 5400.0 and round(k["collection_ratio"], 4) == round(3000 / 5400, 4) and k["net_receivable_change"] == 2400.0
    assert round(k["days_to_pay"]) == 10 and [m["method"] for m in pa["methods"]] == ["CASH"]
    assert round(pa["receivables"]["total"], 2) == 2400.0 and pa["has_data"]
    assert pa["watch"]["about_to_age"] == 0                                   # nothing is 46-60 days old yet
    # an unpaid sale that is 50 days old is about to cross 60 days
    cid = conn.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES('OLD','S-OLD','I-OLD',?,?,10,70,700,'admin',?)",
                 ((today - timedelta(days=50)).isoformat(), cid, application.now()))
    conn.execute("INSERT INTO payments(payment_id,transaction_id,payment_date,amount,payment_method,payment_reference,staff_user,created_at) VALUES('PR','A2',?,100,'Mobile Money','MM-123','admin',?)",
                 (today.isoformat(), application.now()))
    conn.commit()
    watch = payments_service.build_payment_analytics(conn, "all", today, application.money)
    assert watch["watch"]["about_to_age"] == 700.0 and watch["watch"]["about_to_age_count"] == 1
    assert any("pass 60 days" in i["title"] for i in watch["insights"])
    mem = payments_service.payment_memory(conn, today)
    big = next(v for v in mem["customers"].values() if v["method"] and len(v["open"]) == 2)      # A2 (1800 less 100 paid) + the 700 sale
    assert big["owed"] == 2400.0 and big["method"] and big["avg"] == 1550.0                       # avg of payments 3000 and 100
    assert [o["sales_id"] for o in big["open"]] == ["S-OLD", "S-A2"]                              # oldest first
    assert big["open"][0]["balance"] == 700.0 and big["open"][0]["age"] == 50 and big["open"][1]["balance"] == 1700.0 and big["open"][1]["age"] == 10
    assert "mm-123" in mem["references"] and mem["large_cash"] == 10000.0
    conn.close()


def test_payment_header_totals_cover_every_payment_not_just_the_latest_100():
    import payments_service
    _seed_analytics_data()
    conn = application.db()
    conn.execute("DELETE FROM payments")
    cid = conn.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES('BIG','S-BIG','I-BIG','2026-09-01',?,1,1000,1000,'admin',?)", (cid, application.now()))
    for i in range(130):
        conn.execute("INSERT INTO payments(payment_id,transaction_id,payment_date,amount,payment_method,staff_user,created_at) VALUES(?,?,?,?,?,'admin',?)",
                     (f"PAY{i}", "BIG", "2026-09-02", 2.0, "Cash" if i % 2 else "Mobile Money", application.now()))
    conn.commit()
    m = payments_service.lifetime_metrics(conn)
    conn.close()
    assert m["count"] == 130 and m["total_collected"] == 260.0 and m["cash_collected"] == 130.0 and m["momo_collected"] == 130.0   # old code stopped at 100 rows


def test_payments_page_filters_export_and_entry_rules(client):
    import payments_service
    from werkzeug.datastructures import MultiDict
    seed_admin()
    _seed_analytics_data()
    login_session(client)
    html = client.get("/payments").get_data(as_text=True)
    for needle in ("Payment check", "Open sales", "Collections analytics", "paymentsTable"):
        assert needle in html
    for q in ("?range=all", "?range=bad", "?method=cash", "?method=zzz", "?customer=abc", "?q=%25", "?from=2026-01-01&to=2026-12-31"):
        assert client.get("/payments" + q).status_code == 200, q
    def ledger_rows(query):
        return client.get("/payments" + query).get_data(as_text=True).split('<table id="paymentsTable"')[1].split("</table>")[0].count("payment-id-pill")
    assert ledger_rows("") == 1 and ledger_rows("?method=cash") == 1 and ledger_rows("?method=mobile") == 0 and ledger_rows("?q=nomatch") == 0
    where, params, echo = payments_service.payment_filters(MultiDict({"method": "x'; DROP TABLE payments;--", "pfrom": "nope"}), lambda a: "1=1")
    assert echo["method"] == "" and "DROP" not in where and params == []
    export = client.get("/payments/export?method=cash")
    lines = export.data.decode("utf-8-sig").strip().splitlines()
    assert export.status_code == 200 and export.mimetype == "text/csv" and len(lines) == 2 and "CASH" in lines[1].upper() and "3000.00" in lines[1]
    with client.session_transaction() as session:
        session["csrf_token"] = "t"
    def paid(tid):
        conn = application.db()
        try:
            return conn.execute("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE transaction_id=? AND deleted=0", (tid,)).fetchone()["v"]
        finally:
            conn.close()
    conn = application.db()
    cid = conn.execute("SELECT customer_id FROM sales WHERE transaction_id='A3'").fetchone()["customer_id"]
    conn.close()
    base = {"csrf_token": "t", "customer_id": str(cid), "sales_id": "S-A3", "payment_date": "2026-09-30", "payment_method": "Cash"}
    client.post("/payments", data={**base, "amount": "700"})                       # A3 is GHS 600: must be rejected
    assert paid("A3") == 0
    client.post("/payments", data={**base, "amount": "250", "payment_reference": "REF-1"})
    assert paid("A3") == 250.0
    client.post("/payments", data={**base, "amount": "10", "payment_reference": "REF-1"})   # same reference on the same sale
    assert paid("A3") == 250.0


def test_inventory_stock_age_fifo_reconciliation_and_position():
    import inventory_service
    from datetime import date
    _seed_analytics_data()     # bought 1000 (40 days ago) + 900 received (35 days ago); sold 900 -> FIFO takes it all from the oldest lot
    conn = application.db()
    today = date.today()
    age = inventory_service.stock_age(conn, today)
    assert round(age["total"], 1) == 1000.0 and age["oldest_days"] == 40
    assert [(l["purchase_id"], round(l["kg"]), l["age"]) for l in age["lots"]] == [("PA", 100, 40), ("PB", 900, 35)]     # 900 KG sold came out of PA first
    assert round(age["avg_age"], 2) == 35.5 and age["bands"]["31-60 days"] == 1000.0 and age["old_share"] == 1.0
    rec = inventory_service.reconcile(conn)
    assert rec["ok"] and rec["book_stock"] == 1000.0 and rec["ledger_stock"] == 1000.0 and rec["difference"] == 0 and rec["movement_count"] == 5
    inv = inventory_service.build_inventory(conn, "all", today, application.money, 1000.0, 2.4737, 100.0, 30.0)
    assert inv["kpis"]["kg_in"] == 1900.0 and inv["kpis"]["kg_out"] == 900.0 and inv["kpis"]["net"] == 1000.0 and inv["kpis"]["lost_in_transit"] == 100.0
    assert inv["series"]["level"][-1] == 1000.0 and inv["position"]["kg"] == 1000.0 and round(inv["position"]["value"], 1) == 2473.7
    assert round(inv["position"]["cover_days"], 1) == 33.3 and round(inv["position"]["bags"][50]) == 20
    assert any("over 30 days old" in i["title"] for i in inv["insights"]) and not any("out by" in i["title"] for i in inv["insights"])
    # tampering with the ledger behind the system's back must be caught
    conn.execute("INSERT INTO stock_movements(movement_type,reference,quantity_kg,movement_date,created_by,notes,created_at) VALUES('ADJUSTMENT','MANUAL',25,?,'x','hand edit',?)",
                 (today.isoformat(), application.now()))
    conn.commit()
    bad = inventory_service.build_inventory(conn, "all", today, application.money, 1000.0, 2.4737, 100.0, 30.0)
    assert not bad["reconcile"]["ok"] and bad["reconcile"]["difference"] == 25.0
    assert any(i["level"] == "danger" and "out by 25.0 KG" in i["title"] for i in bad["insights"])
    # more sales than purchases (should never happen) still produces sane output
    conn.execute("DELETE FROM purchases")
    assert inventory_service.stock_age(conn, today)["total"] == 0
    conn.close()


def test_stock_page_filters_and_export(client):
    import inventory_service
    from werkzeug.datastructures import MultiDict
    seed_admin()
    _seed_analytics_data()
    login_session(client)
    html = client.get("/stock").get_data(as_text=True)
    for needle in ("How old is your stock?", "Stock ledger check", "Inventory analytics", "stockTable"):
        assert needle in html
    for q in ("?range=all", "?range=bad", "?type=SALE", "?type=zzz", "?q=%25", "?from=2026-01-01&to=2026-12-31"):
        assert client.get("/stock" + q).status_code == 200, q
    def rows(query):
        return client.get("/stock" + query).get_data(as_text=True).split('<table id="stockTable"')[1].split("</table>")[0].count("<code>")
    assert rows("") == 5 and rows("?type=SALE") == 3 and rows("?type=PURCHASE") == 2 and rows("?q=PB") == 1 and rows("?type=zzz") == 5
    where, params, echo = inventory_service.movement_filters(MultiDict({"type": "x'; DROP TABLE stock_movements;--", "sfrom": "bad"}))
    assert echo["type"] == "" and "DROP" not in where and params == []
    export = client.get("/stock?format=csv&type=SALE")
    lines = export.data.decode("utf-8-sig").strip().splitlines()
    assert export.status_code == 200 and export.mimetype == "text/csv" and len(lines) == 4 and all("SALE" in l for l in lines[1:])


def test_invoice_health_statuses_ageing_and_follow_up_order(client):
    import invoices_service
    from datetime import date, timedelta
    seed_admin()
    _seed_analytics_data()     # A1 paid 3000 | A2 unpaid 1800 (10 days old) | A3 unpaid 600 (5 days old)
    conn = application.db()
    today = date.today()
    h = invoices_service.build_invoice_health(conn, today, application.money)
    assert h["count"] == 3 and h["billed"] == 5400.0 and h["settled"] == 3000.0 and h["outstanding"] == 2400.0
    assert h["statuses"]["PAID"]["count"] == 1 and h["statuses"]["UNPAID"]["count"] == 2 and h["statuses"]["UNPAID"]["amount"] == 2400.0
    assert h["bands"]["8-30 days"] == {"count": 1, "amount": 1800.0} and h["bands"]["0-7 days"] == {"count": 1, "amount": 600.0}
    assert [f["sales_id"] for f in h["follow_up"]] == ["S-A2", "S-A3"] and h["follow_up"][0]["age"] == 10   # oldest first
    assert round(h["avg_invoice"], 2) == 1800.0 and round(h["paid_share"], 4) == round(1 / 3, 4) and h["tips"] == []
    # an invoice older than 60 days and an overpaid one both raise tips
    cid = conn.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at) VALUES('OLD','S-OLD','I-OLD',?,?,10,50,500,'admin',?)",
                 ((today - timedelta(days=75)).isoformat(), cid, application.now()))
    conn.execute("INSERT INTO payments(payment_id,transaction_id,payment_date,amount,payment_method,staff_user,created_at) VALUES('OVER','A3',?,650,'Cash','admin',?)", (today.isoformat(), application.now()))
    conn.commit()
    h = invoices_service.build_invoice_health(conn, today, application.money)
    assert h["statuses"]["OVERPAID"]["count"] == 1 and round(h["overpaid_total"], 2) == 50.0
    assert [t["level"] for t in h["tips"]] == ["danger", "warning"] and h["follow_up"][0]["sales_id"] == "S-OLD"
    assert invoices_service.status_of(100, 100) == "PAID" and invoices_service.status_of(100, 0) == "UNPAID" and invoices_service.status_of(100, 40) == "PART PAYMENT" and invoices_service.status_of(100, 101) == "OVERPAID"
    conn.close()
    login_session(client)
    landing = client.get("/invoice").get_data(as_text=True)
    assert "Invoice health" in landing and "Chase first" in landing
    assert "Invoice health" not in client.get("/invoice?status=UNPAID").get_data(as_text=True)      # only on the landing view
    assert client.get("/invoice/S-A2").status_code == 200


def test_audit_activity_insights_flag_bruteforce_clearing_and_after_hours(client):
    import audit_service
    from datetime import date
    seed_admin()
    conn = application.db()
    conn.execute("DELETE FROM audit_log")
    today = date.today().isoformat()
    def log(user, action, ref="", details="", hour="10", minute="00"):
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (user, action, ref, details, f"{today} {hour}:{minute}:00"))
    for i in range(6):
        log("unknown", "LOGIN FAILED", "admin", "ip=10.9.8.7; invalid credentials", minute=f"{i:02d}")
    log("unknown", "LOGIN FAILED", "sales", "ip=10.1.1.1; invalid credentials")
    log("admin", "LOGIN SUCCESS", "admin", "ip=127.0.0.1; role=ADMIN")
    for i in range(4):
        log("sales", "SALE CREATED", f"S{i}", "x", hour="11")
    for i in range(6):
        log("sales", "SALE REVERSED", f"R{i}", "x", hour="11")        # heavy sensitive share for one user
    log("admin", "SALE CREATED", "N1", "x", hour="23")               # after hours
    log("admin", "AUDIT LOG CLEARED", "ALL", "x", hour="12")
    log("admin", "SALES EXPORTED", "SALES", "rows=3", hour="12")
    conn.commit()
    a = audit_service.build_activity(conn, "30d", date.today())
    assert a["total"] == 21 and a["failed_logins"] == 7 and a["logins"] == 1 and a["exports"] == 1 and a["night_events"] == 1
    assert dict(a["failed_by_ip"])["10.9.8.7"] == 6
    titles = [i["title"] for i in a["insights"]]
    assert any("audit log was cleared" in t for t in titles) and any("6 failed sign-ins from 10.9.8.7" in t for t in titles)
    assert any("between 10pm and 5am" in t for t in titles) and any("sales made 6 sensitive" in t for t in titles)
    assert [i["level"] for i in a["insights"]][:2] == ["danger", "danger"]                     # most severe first
    sales_user = next(u for u in a["users"] if u["user"] == "sales")
    assert sales_user["events"] == 10 and sales_user["sensitive"] == 6
    assert audit_service.categorize("SALE REVERSED") == "Reversals & deletions" and audit_service.categorize("LOGIN SUCCESS") == "Sign-ins"
    assert audit_service.is_sensitive("PASSWORD CHANGED") and not audit_service.is_sensitive("LOGIN FAILED") and not audit_service.is_sensitive("SALE CREATED")
    conn.close()
    login_session(client)
    html = client.get("/admin/audit-logs").get_data(as_text=True)
    assert "Activity insights" in html and "log clearing" in html and "Activity Log" in html
    for q in ("?range=bad", "?from=2026-01-01&to=2026-12-31", "?from=zz&to=yy"):
        assert client.get("/admin/audit-logs" + q).status_code == 200
    conn = application.db()
    try:
        quiet = audit_service.build_activity(conn, "30d", date(2020, 1, 1))
    finally:
        conn.close()
    assert quiet["total"] == 0 and quiet["insights"] == []


def test_assistant_answers_new_questions_from_verified_data(client):
    import assistant_service
    _seed_analytics_data()
    conn = application.db()
    ask = lambda q: assistant_service.answer_question(conn, q)
    health = ask("How is the business doing?")
    assert health["title"] == "Business Health Score" and "/100" in health["answer"] and len(health["facts"]) >= 3
    calls = ask("Who should I call?")
    assert calls["title"] == "Who To Call" and any("Big Buyer" in f and "1,800.00" in f for f in calls["facts"])
    sup = ask("Which supplier is best?")
    assert sup["title"] == "Supplier Comparison" and "Steady Agent ranks first" in sup["answer"] and "270.00" in sup["answer"]     # 100 KG short x GHS 2.70
    age = ask("How old is my stock?")
    assert age["title"] == "How Old Is Your Stock" and ("36 days" in age["answer"] or "35 days" in age["answer"])                 # weighted average 35.5 days
    restock = ask("When should I reorder?")
    assert restock["title"] == "Restock Advice" and "30 KG per day" in restock["answer"] and "no purchase is needed" in restock["answer"]
    price = ask("What price should I sell at?")
    assert price["title"] == "Pricing Guide" and "2.47" in price["answer"] and any("10% margin" in f for f in price["facts"])
    pay = ask("How fast do customers pay?")
    assert pay["title"] == "How Fast Customers Pay" and "10 days" in pay["answer"]
    profit = ask("Who is our most profitable customer?")
    assert profit["title"] == "Most Profitable Customers" and "Big Buyer" in profit["answer"]
    assert ask("How much maize is currently available?")["title"] == "Current Stock Position"            # old intents untouched
    assert ask("Who owes ADUFARMS the most?")["title"] == "Outstanding Customer Balances"
    assert ask("tell me a joke")["title"] == "Verified Agribusiness Assistant" and ask("")["title"] == "Ask ADUFARMS"
    conn.close()
    seed_admin()
    login_session(client)
    page = client.get("/assistant?question=Who+should+I+call%3F").get_data(as_text=True)
    assert "Who To Call" in page and "Business Health" in page                                        # the result plus the new quick prompts


def test_notification_centre_groups_issues_and_respects_permissions(client):
    import alerts_service
    from datetime import date
    _seed_analytics_data()
    seed_admin()
    conn = application.db()
    full = {"inventory": True, "payments": True, "customers": True, "sales": True, "audit": True}
    cost = application.cogs_summary(conn)["unit_cost"]
    alerts = alerts_service.build_alerts(conn, date.today(), application.money, 1000.0, cost, 100.0, 30.0, full)
    kinds = [a["kind"] for a in alerts]
    assert kinds == sorted(kinds, key=lambda k: {"danger": 0, "warning": 1, "info": 2}[k])                 # most urgent first
    assert all(a["title"] and a["href"] for a in alerts)
    assert len([a for a in alerts if a["title"].endswith("owe GHS 2,400.00") or "customers owe" in a["title"] or "customer owes" in a["title"]]) == 1   # ONE summary, not a line per unpaid sale
    assert alerts_service.build_alerts(conn, date.today(), application.money, 1000.0, cost, 100.0, 30.0, {k: False for k in full}) == []   # nothing for a role with no access
    low = alerts_service.build_alerts(conn, date.today(), application.money, 50.0, cost, 100.0, 30.0, {**{k: False for k in full}, "inventory": True})
    assert low and low[0]["title"] == "Stock is low" and low[0]["kind"] == "warning"
    conn.close()
    login_session(client)
    assert client.get("/notifications").status_code == 200 and "Notification center" in client.get("/notifications").get_data(as_text=True)
    assert client.get("/notifications?mark_read=1").status_code == 302


def _csrf(client):
    client.get("/login")
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_login_redirects_back_to_requested_page_and_blocks_open_redirects(client):
    seed_admin()
    application._LOGIN_IP_ATTEMPTS.clear()
    redirect = client.get("/customers")
    assert redirect.headers["Location"].endswith("/login?next=/customers")
    token = _csrf(client)
    good = client.post("/login", data={"username": "admin", "password": "StrongPassword1!", "csrf_token": token, "next": "/customers"})
    assert good.status_code == 302 and good.headers["Location"].endswith("/customers")
    client.get("/logout")
    token = _csrf(client)
    evil = client.post("/login", data={"username": "admin", "password": "StrongPassword1!", "csrf_token": token, "next": "//evil.example"})
    assert evil.headers["Location"].endswith("/dashboard")
    client.get("/logout")


def test_failed_login_keeps_username_and_rate_limit_returns_429(client):
    seed_admin()
    application._LOGIN_IP_ATTEMPTS.clear()
    token = _csrf(client)
    failed = client.post("/login", data={"username": "ghost", "password": "x", "csrf_token": token})
    assert failed.status_code == 200 and b'value="ghost"' in failed.data
    for _ in range(8):
        application.record_ip_login_attempt("127.0.0.1")
    limited = client.post("/login", data={"username": "ghost", "password": "x", "csrf_token": token})
    assert limited.status_code == 429 and b"Too many failed sign-in attempts" in limited.data
    application._LOGIN_IP_ATTEMPTS.clear()


def test_password_reset_token_is_hashed_expires_and_clears_lockout(client):
    seed_admin()
    token = "reset-token-for-test"
    conn = application.db()
    conn.execute(
        "UPDATE users SET password_reset_token=?,password_reset_expires_at=?,failed_login_attempts=3,locked_until='2999-01-01 00:00:00' WHERE username='admin'",
        (application.hash_reset_token(token), "2999-01-01 00:00:00"),
    )
    conn.commit()
    conn.close()
    assert client.get(f"/reset-password/{token}").status_code == 200
    csrf = _csrf(client)
    done = client.post(f"/reset-password/{token}", data={"password": "NewStrongPass9!", "confirmation": "NewStrongPass9!", "csrf_token": csrf})
    assert done.status_code == 302
    conn = application.db()
    row = conn.execute("SELECT password_reset_token,failed_login_attempts,locked_until FROM users WHERE username='admin'").fetchone()
    conn.execute("UPDATE users SET password_hash=? WHERE username='admin'", (generate_password_hash("StrongPassword1!"),))
    conn.commit()
    conn.close()
    assert row["password_reset_token"] is None and row["failed_login_attempts"] == 0 and row["locked_until"] is None
    assert client.get(f"/reset-password/{token}").status_code == 302  # token is single-use
    expired = "expired-token"
    conn = application.db()
    conn.execute("UPDATE users SET password_reset_token=?,password_reset_expires_at='2000-01-01 00:00:00' WHERE username='admin'", (application.hash_reset_token(expired),))
    conn.commit()
    conn.close()
    assert client.get(f"/reset-password/{expired}").status_code == 302


def test_recovery_email_can_be_saved_then_used_for_password_reset(client):
    seed_admin()
    application._LOGIN_IP_ATTEMPTS.clear()
    login_session(client)
    token = "recovery-test-token"
    with client.session_transaction() as session:
        session["csrf_token"] = token
    bad = client.post("/profile", data={"action": "email", "email": "a@yahoo.com", "current_password": "StrongPassword1!", "csrf_token": token})
    assert b"valid Gmail address" in bad.data
    wrong = client.post("/profile", data={"action": "email", "email": "recover.me@gmail.com", "current_password": "nope", "csrf_token": token})
    assert b"Current password is incorrect" in wrong.data
    ok = client.post("/profile", data={"action": "email", "email": "Recover.Me@gmail.com", "current_password": "StrongPassword1!", "csrf_token": token})
    assert ok.status_code == 200 and b"Recovery email saved" in ok.data
    conn = application.db()
    row = conn.execute("SELECT email,email_verified FROM users WHERE id=1").fetchone()
    conn.close()
    assert row["email"] == "recover.me@gmail.com" and row["email_verified"] == 0
    # edit-user keeps the real role instead of silently demoting it
    conn = application.db()
    conn.execute("INSERT OR IGNORE INTO users(username,full_name,password_hash,role,active,created_at) VALUES('mgr_test','Mgr Test','x','MANAGER',1,?)", (application.now(),))
    conn.commit()
    mgr_id = conn.execute("SELECT id FROM users WHERE username='mgr_test'").fetchone()["id"]
    conn.execute("UPDATE users SET email=NULL WHERE id=1")
    conn.commit()
    conn.close()
    page = client.get(f"/users/{mgr_id}/edit")
    assert b'value="MANAGER" selected' in page.data
