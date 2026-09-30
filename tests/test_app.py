import re
import io
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
    assert len(re.findall(rb"/Type /Page(?![a-z])", response.data)) == 1


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


def test_period_helpers():
    assert application.pct_change(150, 100) == 50
    assert application.pct_change(50, 100) == -50
    assert application.pct_change(10, 0) is None
    months = application.month_range(6)
    assert len(months) == 6 and months == sorted(months)


def test_dashboard_shows_no_invented_figures(client):
    login_session(client)
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Executive overview" in html or "Welcome back" in html
    assert "Animal Feed" not in html and "Maize Seeds" not in html and "Organic Fertilizer" not in html
    assert "12% <span" not in html
