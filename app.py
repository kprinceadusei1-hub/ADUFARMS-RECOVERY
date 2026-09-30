from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import os
import io
import csv
import secrets
import hmac
import smtplib
from email.message import EmailMessage
from markupsafe import Markup
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

import stock_service as stock_svc
import backup_service as backup_svc
import assistant_service

BASE_DIR = Path(__file__).resolve().parent
_DB_ENV = os.environ.get("DATABASE_PATH") or os.environ.get("ADUFARMS_DB_PATH")
DB_PATH = Path(_DB_ENV) if _DB_ENV else (BASE_DIR / "adufarms.db")
EXPORT_DIR = BASE_DIR / "exports" / "invoices"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = BASE_DIR / "static" / "images" / "users"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
DASHBOARD_IMAGE_DIR = BASE_DIR / "static" / "images" / "dashboard" / "custom"
DASHBOARD_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
environment = os.environ.get("ADUFARMS_ENV", "development").strip().lower()
configured_secret = os.environ.get("ADUFARMS_SECRET_KEY")
if environment == "production" and not configured_secret:
    raise RuntimeError("ADUFARMS_SECRET_KEY must be configured in production.")
app.secret_key = configured_secret or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ADUFARMS_COOKIE_SECURE", "0") == "1",
    SESSION_COOKIE_NAME="adufarms_session",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    DATABASE_TIMEOUT=float(os.environ.get("ADUFARMS_DATABASE_TIMEOUT", "30")),
)
app.config["DATABASE"] = str(DB_PATH)

ALLOWED_PROFILE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_PROFILE_MIMES = {"image/jpeg", "image/png", "image/webp"}
DASHBOARD_SLOT_ALIASES = {
    "delivery": "sales",
    "sales": "sales",
    "inventory": "inventory",
    "inventory_banner": "inventory",
    "stock_inventory": "inventory",
    "invoice_module": "invoice",
    "sales_module": "sales",
    "purchase_module": "purchases",
    "reports_module": "reports",
    "user_management": "users",
}
DASHBOARD_IMAGE_DEFAULTS = {
    "hero": "images/branding/maize-farmers.webp",
    "inventory": "images/dashboard/warehouse.webp",
    "stock": "images/modules/stock.webp",
    "sales": "images/modules/sales.webp",
    "purchases": "images/modules/purchase.webp",
    "customers": "images/modules/search.jpg",
    "payments": "images/modules/payment.webp",
    "invoice": "images/modules/invoice.jpg",
    "reports": "images/branding/maize-harvest.webp",
    "assistant": "images/branding/maize-harvest.jpg",
    "profile": "images/branding/d11d7134-86b8-4264-b7b1-79e5be58a001.png",
    "users": "images/branding/adufarms-logo.jpg",
    "audit_logs": "images/dashboard/warehouse.jpg",
    "deleted_records": "images/modules/payment.jpg",
}
DASHBOARD_IMAGE_SLOT_ORDER = [
    ("hero", "Dashboard overview", "Main banner image for the executive dashboard."),
    ("inventory", "Stock & Inventory", "Matches the warehouse and live stock management view."),
    ("stock", "Stock module", "Controls the main stock page banner."),
    ("sales", "Sales", "Matches the sales and customer order workflow."),
    ("purchases", "Purchases", "Controls supplier intake and purchasing pages."),
    ("customers", "Customers", "Controls the customer directory banner."),
    ("payments", "Payments", "Controls the payment tracking banner."),
    ("invoice", "Invoices", "Controls the invoice module banner."),
    ("reports", "Reports", "Powers the analytics and reports workspace."),
    ("assistant", "Assistant", "Sets the agribusiness assistant and guidance area."),
    ("profile", "Profile", "User account, profile and security imagery."),
    ("users", "User management", "Controls admin team and access management pages."),
    ("audit_logs", "Audit logs", "Security and operational activity tracking page."),
    ("deleted_records", "Recovery & archive", "Deleted records and restoration imagery."),
]
ALLOWED_DASHBOARD_SLOTS = set(DASHBOARD_IMAGE_DEFAULTS)
PAGE_BANNER_SLOT_MAP = {
    "dashboard": "hero",
    "inventory": "inventory",
    "stock": "stock",
    "sales": "sales",
    "purchases": "purchases",
    "customers": "customers",
    "payments": "payments",
    "invoice": "invoice",
    "reports": "reports",
    "assistant": "assistant",
    "profile": "profile",
    "users": "users",
    "audit_logs": "audit_logs",
    "deleted_records": "deleted_records",
}


@app.context_processor
def inject_dashboard_images():
    conn = None
    try:
        conn = db()
        rows = conn.execute("SELECT slot, filename FROM dashboard_images").fetchall()
    except Exception:
        rows = []
    finally:
        if conn is not None:
            conn.close()

    dashboard_images = dict(DASHBOARD_IMAGE_DEFAULTS)
    for row in rows:
        slot_name = DASHBOARD_SLOT_ALIASES.get(row["slot"], row["slot"])
        if slot_name in dashboard_images:
            dashboard_images[slot_name] = row["filename"]

    def module_banner_slot_for(endpoint_name):
        return PAGE_BANNER_SLOT_MAP.get(endpoint_name, "hero")

    return {
        "dashboard_images": dashboard_images,
        "dashboard_image_slots": DASHBOARD_IMAGE_SLOT_ORDER,
        "module_banner_slot_for": module_banner_slot_for,
    }


def db():
    conn = sqlite3.connect(
        app.config["DATABASE"],
        timeout=app.config["DATABASE_TIMEOUT"],
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    # Backup before migrations (never silently overwrite the only backup)
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        try:
            backup_svc.create_backup(str(DB_PATH))
        except Exception:
            app.logger.exception("Pre-migration backup failed")
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL,
        email TEXT,
        email_verified INTEGER NOT NULL DEFAULT 0,
        verification_token TEXT,
        verification_expires_at TEXT,
        login_otp_hash TEXT,
        login_otp_expires_at TEXT,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'STAFF',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login TEXT,
        failed_login_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        profile_image TEXT
    );

    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_id TEXT UNIQUE NOT NULL,
        purchase_date TEXT NOT NULL,
        local_agent TEXT NOT NULL,
        agent_phone TEXT,
        location TEXT,
        quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
        price_per_kg REAL NOT NULL CHECK(price_per_kg >= 0),
        total_purchase_cost REAL NOT NULL,
        transport_cost REAL NOT NULL DEFAULT 0,
        other_expenses REAL NOT NULL DEFAULT 0,
        total_cost REAL NOT NULL,
        quantity_received_kg REAL NOT NULL DEFAULT 0 CHECK(quantity_received_kg >= 0),
        staff_user TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        deleted_at TEXT,
        deleted_by TEXT
    );

    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT UNIQUE NOT NULL,
        sales_id TEXT UNIQUE,
        invoice_number TEXT UNIQUE,
        sale_date TEXT NOT NULL,
        customer_id INTEGER NOT NULL,
        quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
        selling_price_kg REAL NOT NULL CHECK(selling_price_kg >= 0),
        total_sale REAL NOT NULL,
        staff_user TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        deleted_at TEXT,
        deleted_by TEXT,
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id TEXT UNIQUE,
        transaction_id TEXT NOT NULL,
        sales_id TEXT,
        payment_date TEXT NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        payment_method TEXT NOT NULL,
        payment_reference TEXT,
        staff_user TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        deleted_at TEXT,
        deleted_by TEXT,
        FOREIGN KEY(transaction_id) REFERENCES sales(transaction_id)
    );

    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_number TEXT UNIQUE NOT NULL,
        transaction_id TEXT UNIQUE NOT NULL,
        sales_id TEXT UNIQUE,
        invoice_date TEXT NOT NULL,
        generated_by TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        last_regenerated_at TEXT,
        deleted INTEGER NOT NULL DEFAULT 0,
        deleted_at TEXT,
        deleted_by TEXT,
        deletion_reason TEXT,
        FOREIGN KEY(transaction_id) REFERENCES sales(transaction_id)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        action TEXT NOT NULL,
        reference TEXT,
        details TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS reversals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name TEXT NOT NULL,
        record_id INTEGER NOT NULL,
        reference TEXT NOT NULL,
        reason TEXT NOT NULL,
        reversed_by TEXT NOT NULL,
        reversed_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        movement_type TEXT NOT NULL CHECK(movement_type IN ('PURCHASE', 'SALE', 'REVERSAL', 'ADJUSTMENT')),
        reference TEXT NOT NULL,
        quantity_kg REAL NOT NULL,
        movement_date TEXT NOT NULL,
        created_by TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS dashboard_images (
        slot TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
    CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);
    CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date);
    CREATE INDEX IF NOT EXISTS idx_customer_name ON customers(name);
    CREATE INDEX IF NOT EXISTS idx_customer_phone ON customers(phone);
    CREATE INDEX IF NOT EXISTS idx_user_role ON users(role);
    CREATE INDEX IF NOT EXISTS idx_payments_transaction ON payments(transaction_id);
    CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(payment_date);
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
    CREATE INDEX IF NOT EXISTS idx_stock_reference ON stock_movements(reference);
    CREATE INDEX IF NOT EXISTS idx_invoices_sales ON invoices(sales_id);
    """)
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "last_login" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
    if "profile_image" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN profile_image TEXT")
    for column, definition in {
        "email": "TEXT",
        "email_verified": "INTEGER NOT NULL DEFAULT 0",
        "verification_token": "TEXT",
        "verification_expires_at": "TEXT",
        "password_reset_token": "TEXT",
        "password_reset_expires_at": "TEXT",
        "login_otp_hash": "TEXT",
        "login_otp_expires_at": "TEXT",
        "failed_login_attempts": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TEXT",
    }.items():
        if column not in user_columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL AND email != ''")
    customer_columns = {row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()}
    for column, definition in {
        "address": "TEXT",
        "location": "TEXT",
        "customer_type": "TEXT NOT NULL DEFAULT 'RETAIL'",
        "opening_balance": "REAL NOT NULL DEFAULT 0",
        "notes": "TEXT",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
        "deletion_reason": "TEXT",
    }.items():
        if column not in customer_columns:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {column} {definition}")
    sales_columns = {row[1] for row in conn.execute("PRAGMA table_info(sales)").fetchall()}
    if "sales_id" not in sales_columns:
        conn.execute("ALTER TABLE sales ADD COLUMN sales_id TEXT")
    if "invoice_number" not in sales_columns:
        conn.execute("ALTER TABLE sales ADD COLUMN invoice_number TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_sales_id ON sales(sales_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_invoice_number ON sales(invoice_number)")
    existing_sales = conn.execute("SELECT id, sale_date FROM sales WHERE sales_id IS NULL OR sales_id = '' ORDER BY sale_date, id").fetchall()
    for row in existing_sales:
        sale_date = row["sale_date"] or date.today().isoformat()
        try:
            id_date = datetime.strptime(sale_date, "%Y-%m-%d").date()
        except ValueError:
            id_date = date.today()
        generated = next_daily_id("ADU-SAL", "sales", "sales_id", conn, id_date)
        conn.execute("UPDATE sales SET sales_id=? WHERE id=?", (generated, row["id"]))
    for table in ("purchases", "sales", "payments"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "deleted" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        if "deleted_at" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")
        if "deleted_by" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted_by TEXT")
        if "deletion_reason" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN deletion_reason TEXT")
    payment_columns = {row[1] for row in conn.execute("PRAGMA table_info(payments)").fetchall()}
    if "payment_id" not in payment_columns:
        conn.execute("ALTER TABLE payments ADD COLUMN payment_id TEXT")
    if "sales_id" not in payment_columns:
        conn.execute("ALTER TABLE payments ADD COLUMN sales_id TEXT")
    if "notes" not in payment_columns:
        conn.execute("ALTER TABLE payments ADD COLUMN notes TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_payment_id ON payments(payment_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_sales_id ON payments(sales_id)")
    conn.execute("UPDATE payments SET sales_id=(SELECT s.sales_id FROM sales s WHERE s.transaction_id=payments.transaction_id) WHERE sales_id IS NULL OR sales_id='' ")
    invoice_columns = {row[1] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
    for column, definition in {
        "deleted": "INTEGER NOT NULL DEFAULT 0",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
        "deletion_reason": "TEXT",
    }.items():
        if column not in invoice_columns:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {column} {definition}")
    invoice_columns = {row[1] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
    if "sales_id" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN sales_id TEXT")
    conn.execute("UPDATE invoices SET sales_id=(SELECT s.sales_id FROM sales s WHERE s.transaction_id=invoices.transaction_id) WHERE sales_id IS NULL OR sales_id='' ")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_sales_id ON invoices(sales_id)")
    conn.execute("UPDATE sales SET invoice_number=(SELECT i.invoice_number FROM invoices i WHERE i.transaction_id=sales.transaction_id) WHERE invoice_number IS NULL OR invoice_number='' ")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_deleted_customer ON sales(deleted, customer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_deleted_transaction ON sales(deleted, transaction_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_deleted_date ON sales(deleted, sale_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_deleted_date ON purchases(deleted, purchase_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_active_name ON customers(active, name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_active_phone ON customers(active, phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_deleted_transaction ON payments(deleted, transaction_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_deleted_date ON payments(deleted, payment_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_deleted_transaction ON invoices(deleted, transaction_id)")
    conn.execute("ANALYZE")
    payment_rows = conn.execute("SELECT id,payment_date,transaction_id FROM payments WHERE payment_id IS NULL OR payment_id='' ORDER BY payment_date,id").fetchall()
    for row in payment_rows:
        try:
            payment_date = datetime.strptime(row["payment_date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            payment_date = date.today()
        payment_id = next_daily_id("ADU-PAY", "payments", "payment_id", conn, payment_date)
        conn.execute("UPDATE payments SET payment_id=? WHERE id=?", (payment_id, row["id"]))
    conn.execute("UPDATE payments SET sales_id=(SELECT s.sales_id FROM sales s WHERE s.transaction_id=payments.transaction_id) WHERE sales_id IS NULL OR sales_id='' ")
    invoice_rows = conn.execute("""SELECT s.transaction_id,s.sales_id,s.sale_date,s.staff_user,s.created_at
        FROM sales s LEFT JOIN invoices i ON i.transaction_id=s.transaction_id
        WHERE i.transaction_id IS NULL ORDER BY s.sale_date,s.id""").fetchall()
    for row in invoice_rows:
        try:
            invoice_date = datetime.strptime(row["sale_date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            invoice_date = date.today()
        invoice_number = next_daily_id("ADU-INV", "invoices", "invoice_number", conn, invoice_date)
        conn.execute("""INSERT INTO invoices(invoice_number,transaction_id,sales_id,invoice_date,generated_by,generated_at)
                        VALUES(?,?,?,?,?,?)""",
                     (invoice_number, row["transaction_id"], row["sales_id"], row["sale_date"],
                      row["staff_user"], row["created_at"] or now()))
        conn.execute("UPDATE sales SET invoice_number=? WHERE transaction_id=?",
                 (invoice_number, row["transaction_id"]))
    conn.execute("""INSERT INTO stock_movements
        (movement_type,reference,quantity_kg,movement_date,created_by,notes,created_at)
        SELECT 'PURCHASE',p.purchase_id,p.quantity_received_kg,p.purchase_date,p.staff_user,
               'Historical movement backfill',p.created_at FROM purchases p
        WHERE p.deleted=0 AND NOT EXISTS
        (SELECT 1 FROM stock_movements m WHERE m.movement_type='PURCHASE' AND m.reference=p.purchase_id)""")
    conn.execute("""INSERT INTO stock_movements
        (movement_type,reference,quantity_kg,movement_date,created_by,notes,created_at)
        SELECT 'SALE',s.transaction_id,-s.quantity_kg,s.sale_date,s.staff_user,
               'Historical movement backfill',s.created_at FROM sales s
        WHERE s.deleted=0 AND NOT EXISTS
        (SELECT 1 FROM stock_movements m WHERE m.movement_type='SALE' AND m.reference=s.transaction_id)""")
    conn.commit()
    conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def validate_password(password):
    """Return a clear policy error, or None when a new password is strong enough."""
    if len(password or "") < 12:
        return "Password must be at least 12 characters long."
    if not any(char.islower() for char in password):
        return "Password must include a lowercase letter."
    if not any(char.isupper() for char in password):
        return "Password must include an uppercase letter."
    if not any(char.isdigit() for char in password):
        return "Password must include a number."
    if not any(not char.isalnum() for char in password):
        return "Password must include a symbol."
    return None


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_input():
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


@app.context_processor
def security_context():
    notifications = []
    if session.get("user_id"):
        conn = db()
        stock = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0) v FROM purchases WHERE deleted=0").fetchone()["v"] - conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0").fetchone()["v"]
        unpaid = conn.execute("""SELECT COUNT(*) v FROM sales s WHERE s.deleted=0 AND
            COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) < s.total_sale""").fetchone()["v"]
        latest = conn.execute("SELECT action,reference FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if stock <= 0:
            notifications.append(("Stock depleted", "No available maize stock remains.", "danger", "box-seam"))
        if unpaid:
            notifications.append((f"{unpaid} unpaid balance" if unpaid != 1 else "1 unpaid balance", "Review customer balances.", "warning", "exclamation-circle"))
        if latest:
            notifications.append(("Recent transaction", f"{latest['action'].title()} {latest['reference'] or ''}".strip(), "info", "activity"))
    return {"csrf_input": csrf_input, "csrf_token": csrf_token, "ui_notifications": notifications}


app.jinja_env.globals.update(csrf_input=csrf_input, csrf_token=csrf_token)


@app.before_request
def protect_post_requests():
    if session.get("user_id"):
        conn = db()
        current_user = conn.execute(
            "SELECT username,full_name,role,profile_image FROM users WHERE id=? AND active=1",
            (session["user_id"],)
        ).fetchone()
        conn.close()
        if not current_user:
            session.clear()
            return redirect(url_for("login"))
        session["username"] = current_user["username"]
        session["full_name"] = current_user["full_name"]
        session["role"] = current_user["role"]
        session["profile_image"] = current_user["profile_image"]

    # Server-side RBAC: staff must not bypass admin URLs.
    admin_only_endpoints = {"delete_record_route", "restore_record", "users", "edit_user",
                            "toggle_user", "delete_user", "audit_logs", "delete_audit_logs",
                            "admin_backup", "admin_restore", "admin_gallery"}
    if request.endpoint in admin_only_endpoints and normalize_role(session.get("role")) != "ADMIN":
        # Allow unauthenticated to fall through to login_required (redirect) rather than 403
        if "user_id" in session:
            abort(403)
    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        if not submitted or not hmac.compare_digest(submitted, session.get("csrf_token", "")):
            abort(400, description="Your form session expired. Please reload the page and try again.")


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Origin-Agent-Cluster"] = "?1"
    if request.endpoint != "static":
        response.headers.setdefault("Cache-Control", "no-store, max-age=0")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def valid_date(value, field_name):
    if value is None:
        raise ValueError(f"Enter a valid {field_name}.")
    value = str(value).strip()
    if not value:
        raise ValueError(f"Enter a valid {field_name}.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {field_name}.")


def normalized_phone(value):
    return "".join(char for char in str(value or "") if char.isdigit())


def normalized_customer_name(value):
    return " ".join(str(value or "").split()).casefold()


def nonnegative_float(name, default=0):
    value = get_float(name, default)
    if value < 0:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} cannot be negative.")
    return value


def add_stock_movement(movement_type, reference, quantity, movement_date, notes=""):
    # Legacy helper retained for compatibility; delegates to centralized service
    # within its own atomic connection.
    conn = db()
    try:
        stock_svc.record_movement(conn, movement_type, reference, quantity,
                                  movement_date, session.get("username", "system"), notes, now())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


ROLE_ALIASES = {
    "ADMIN": "ADMIN",
    "ADMINISTRATOR": "ADMIN",
    "MANAGER": "MANAGER",
    "SALES_OFFICER": "SALES_OFFICER",
    "SALES OFFICER": "SALES_OFFICER",
    "INVENTORY_OFFICER": "INVENTORY_OFFICER",
    "INVENTORY OFFICER": "INVENTORY_OFFICER",
    "ACCOUNTANT": "ACCOUNTANT",
    "VIEWER": "VIEWER",
    "STAFF": "STAFF",
}

ROLE_LABELS = {
    "ADMIN": "Administrator",
    "MANAGER": "Manager",
    "SALES_OFFICER": "Sales Officer",
    "INVENTORY_OFFICER": "Inventory Officer",
    "ACCOUNTANT": "Accountant",
    "VIEWER": "Viewer",
    "STAFF": "Staff",
}

ROLE_PERMISSIONS = {
    "ADMIN": {
        "view_dashboard", "view_users", "create_users", "edit_users", "deactivate_users",
        "view_roles", "create_roles", "edit_roles", "delete_roles", "assign_permissions",
        "view_customers", "create_customers", "edit_customers", "delete_customers",
        "view_sales", "create_sales", "edit_sales", "delete_sales",
        "view_purchases", "create_purchases", "edit_purchases", "delete_purchases",
        "view_inventory", "manage_inventory",
        "view_invoices", "create_invoices", "print_invoices", "download_invoices",
        "view_reports", "export_reports",
        "view_payments", "manage_payments",
        "view_profiles", "edit_profile",
    },
    "MANAGER": {
        "view_dashboard", "view_users", "create_users", "edit_users",
        "view_customers", "create_customers", "edit_customers",
        "view_sales", "create_sales", "edit_sales",
        "view_purchases", "create_purchases", "edit_purchases",
        "view_inventory", "manage_inventory",
        "view_invoices", "create_invoices", "print_invoices",
        "view_reports", "export_reports",
        "view_payments", "manage_payments",
        "view_profiles", "edit_profile",
    },
    "SALES_OFFICER": {
        "view_dashboard", "view_customers", "create_customers", "edit_customers",
        "view_sales", "create_sales", "edit_sales",
        "view_invoices", "create_invoices", "print_invoices",
        "view_payments", "manage_payments",
        "view_reports", "export_reports",
        "view_profiles", "edit_profile",
    },
    "INVENTORY_OFFICER": {
        "view_dashboard", "view_purchases", "create_purchases", "edit_purchases",
        "view_inventory", "manage_inventory",
        "view_sales", "view_customers",
        "view_reports", "export_reports",
        "view_profiles", "edit_profile",
    },
    "ACCOUNTANT": {
        "view_dashboard", "view_sales", "view_customers", "view_purchases",
        "view_invoices", "print_invoices", "download_invoices",
        "view_payments", "manage_payments",
        "view_reports", "export_reports",
        "view_profiles", "edit_profile",
    },
    "VIEWER": {
        "view_dashboard", "view_reports", "view_profiles",
    },
    "STAFF": {
        "view_dashboard", "view_customers", "create_customers", "edit_customers",
        "view_sales", "create_sales", "edit_sales",
        "view_purchases", "create_purchases", "edit_purchases",
        "view_inventory", "manage_inventory",
        "view_invoices", "print_invoices",
        "view_reports", "export_reports",
        "view_profiles", "edit_profile",
    },
}


def normalize_role(role):
    normalized = str(role or "STAFF").strip().upper().replace(" ", "_")
    return ROLE_ALIASES.get(normalized, normalized)


def user_permissions(role=None):
    current = normalize_role(role if role is not None else session.get("role"))
    return set(ROLE_PERMISSIONS.get(current, ROLE_PERMISSIONS.get("STAFF", set())))


def user_has_permission(permission, role=None):
    if role is not None:
        perms = user_permissions(role)
    else:
        perms = user_permissions()
    return "ADMIN" == normalize_role(role if role is not None else session.get("role")) or permission in perms


def valid_role_name(raw_role):
    normalized = normalize_role(raw_role)
    if normalized not in ROLE_PERMISSIONS:
        raise ValueError("Select a valid user role.")
    return normalized


def require_permission(permission_name):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if not user_has_permission(permission_name):
                abort(403)
            return view_func(*args, **kwargs)
        return wrapper
    return decorator


def require_any_permission(*permission_names):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if not any(user_has_permission(permission) for permission in permission_names):
                abort(403)
            return view_func(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if normalize_role(session.get("role")) != "ADMIN":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


COMPANY = {
    "legal_name": "PRINCE",
    "service_line": "Maize Trading & Distribution",
    "tagline": "Fresh stock. Trusted service. Better business.",
    "document_note": "Professional maize trading and delivery invoice",
    "momo_label": "Mobile Money",
    "momo_number": "024 000 0000",
    "momo_name": "PRINCE Business",
    "bank_name": "GCB Bank",
    "bank_account": "0000000000",
    "bank_account_name": "PRINCE BUSINESS",
    "bank_branch": "Main Branch",
}


def money(v):
    return f"GHS {float(v or 0):,.2f}"


def get_float(name, default=0):
    raw = request.form.get(name, "").strip()
    if raw == "":
        return float(default)
    try:
        value = float(Decimal(raw))
        return value
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid value for {name.replace('_',' ')}.")


def send_verification_email(email, token):
    host = os.environ.get("ADUFARMS_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("ADUFARMS_SMTP_PORT", "587"))
    username = os.environ.get("ADUFARMS_SMTP_USERNAME")
    password = os.environ.get("ADUFARMS_SMTP_PASSWORD")
    sender = os.environ.get("ADUFARMS_SMTP_FROM") or username
    if not all((host, username, password, sender)):
        return False
    assert host is not None and username is not None and password is not None and sender is not None
    base_url = os.environ.get("ADUFARMS_PUBLIC_URL", request.url_root.rstrip("/"))
    verify_url = f"{base_url}/verify-email/{token}"
    message = EmailMessage()
    message["Subject"] = "Verify your ADUFARMS account"
    message["From"] = sender
    message["To"] = email
    message.set_content(f"Verify your ADUFARMS account by opening this link:\n\n{verify_url}\n\nThis link expires in 24 hours.")
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Verification email could not be sent")
        return False


def send_password_reset_email(email, token):
    host = os.environ.get("ADUFARMS_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("ADUFARMS_SMTP_PORT", "587"))
    username = os.environ.get("ADUFARMS_SMTP_USERNAME")
    password = os.environ.get("ADUFARMS_SMTP_PASSWORD")
    sender = os.environ.get("ADUFARMS_SMTP_FROM") or username
    if not all((host, username, password, sender)):
        return False
    assert host is not None and username is not None and password is not None and sender is not None
    base_url = os.environ.get("ADUFARMS_PUBLIC_URL", request.url_root.rstrip("/"))
    reset_url = f"{base_url}/reset-password/{token}"
    message = EmailMessage()
    message["Subject"] = "Reset your ADUFARMS password"
    message["From"] = sender
    message["To"] = email
    message.set_content(f"Reset your ADUFARMS password by opening this link:\n\n{reset_url}\n\nThis link expires in 1 hour. If you did not request this, ignore this message.")
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Password reset email could not be sent")
        return False


def send_login_otp_email(email, code):
    """Send a short-lived login code through the configured Gmail SMTP account."""
    host = os.environ.get("ADUFARMS_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("ADUFARMS_SMTP_PORT", "587"))
    username = os.environ.get("ADUFARMS_SMTP_USERNAME")
    password = os.environ.get("ADUFARMS_SMTP_PASSWORD")
    sender = os.environ.get("ADUFARMS_SMTP_FROM") or username
    if not all((host, username, password, sender)):
        return False
    assert host is not None and username is not None and password is not None and sender is not None
    message = EmailMessage()
    message["Subject"] = "Your ADUFARMS sign-in code"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Your ADUFARMS sign-in code is: {code}\n\n"
        "It expires in 10 minutes. Do not share this code with anyone."
    )
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Login OTP email could not be sent")
        return False


def is_gmail_address(value):
    """Accept consumer Google mailboxes only for the sign-in identity."""
    email = (value or "").strip().lower()
    local, separator, domain = email.rpartition("@")
    return bool(local and separator and domain == "gmail.com")


def next_daily_id(prefix, table, column, conn=None, id_date=None):
    today = (id_date or date.today()).strftime("%Y%m%d")
    owns_connection = conn is None
    conn = conn or db()
    pattern = f"{prefix}-{today}-%"
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE {column} LIKE ?",
        (pattern,)
    ).fetchall()
    if owns_connection:
        conn.close()
    n = 1
    numbers = []
    for row in rows:
        try:
            numbers.append(int(row[0].rsplit("-", 1)[1]))
        except Exception:
            continue
    if numbers:
        n = max(numbers) + 1
    return f"{prefix}-{today}-{n:04d}"


def invoice_number_for(sale_reference: str | None) -> str:
    ref = (sale_reference or "").strip()
    if not ref:
        return ""
    ref = ref.upper()
    if ref.startswith("ADU-INV-"):
        return ref
    if ref.startswith("INV-"):
        return ref
    if ref.startswith("ADU-SAL-"):
        return ref.replace("ADU-SAL-", "ADU-INV-", 1)
    if ref.startswith("ADU-"):
        return f"ADU-INV-{ref[4:]}"
    return f"ADU-INV-{ref}"


def ensure_invoice_record(info):
    """Create or retrieve the single persisted invoice for a saved sale."""
    conn = db()
    try:
        existing = conn.execute(
            "SELECT invoice_number,deleted FROM invoices WHERE transaction_id=?",
            (info["transaction_id"],)
        ).fetchone()
        if existing and not existing["deleted"]:
            conn.execute("UPDATE sales SET invoice_number=? WHERE transaction_id=?",
                         (existing["invoice_number"], info["transaction_id"]))
            conn.commit()
            return existing["invoice_number"]
        sale_date = datetime.strptime(info["sale_date"], "%Y-%m-%d").date()
        invoice_number = next_daily_id("ADU-INV", "invoices", "invoice_number", conn, sale_date)
        timestamp = now()
        if existing:
            conn.execute("""UPDATE invoices SET invoice_number=?,sales_id=?,invoice_date=?,generated_by=?,
                            generated_at=?,deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL
                            WHERE transaction_id=?""",
                         (invoice_number, info["sales_id"], info["sale_date"], session.get("username", "system"),
                          timestamp, info["transaction_id"]))
        else:
            conn.execute("""INSERT INTO invoices(invoice_number,transaction_id,sales_id,invoice_date,generated_by,generated_at)
                            VALUES(?,?,?,?,?,?)""",
                         (invoice_number, info["transaction_id"], info["sales_id"], info["sale_date"],
                          session.get("username", "system"), timestamp))
        conn.execute("UPDATE sales SET invoice_number=? WHERE transaction_id=?",
                 (invoice_number, info["transaction_id"]))
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (actor_label() if session.get("username") else "system", "INVOICE GENERATED",
                      invoice_number, f"sales_id={info['sales_id']}; transaction_id={info['transaction_id']}", timestamp))
        conn.commit()
        return invoice_number
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT invoice_number FROM invoices WHERE transaction_id=?",
            (info["transaction_id"],)
        ).fetchone()
        if existing:
            return existing["invoice_number"]
        raise
    finally:
        conn.close()


def can_see_deleted():
    try:
        from flask import has_request_context
        if not has_request_context():
            return False
        return str(session.get("role", "")).upper() == "ADMIN"
    except Exception:
        return False


def visible_sql(alias):
    return "1=1" if can_see_deleted() else f"{alias}.deleted=0"


def actor_label():
    return f"{session.get('username')} (id={session.get('user_id')})"


SPEC_DELETE = {"purchases": "PURCHASE", "sales": "SALE", "payments": "PAYMENT"}
SPEC_REVERSE = {"purchases": "PURCHASE REVERSED", "sales": "SALE REVERSED", "payments": "PAYMENT REVERSED"}
SPEC_RESTORE = {"purchases": "PURCHASE RESTORED", "sales": "SALE RESTORED", "payments": "PAYMENT RESTORED"}


class PurchaseStockInUseError(ValueError):
    """Raised when a purchase cannot be removed without invalidating stock."""

    def __init__(self, purchase_id):
        super().__init__("Purchase stock is already used by active sales.")
        self.purchase_id = purchase_id


class CustomerHasDependenciesError(ValueError):
    """Raised when financial history prevents permanent customer deletion."""

    def __init__(self, customer_id):
        super().__init__("Customer has existing financial records.")
        self.customer_id = customer_id


def hard_delete_record(table, record_id, reference):
    conn = db()
    try:
        reason = request.form.get("reason", "").strip()
        if not reason:
            raise ValueError("A reason for deletion is required.")
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise ValueError("This transaction is unavailable.")
        if table == "purchases" and not row["deleted"]:
            available = stock_svc.available_stock(conn)
            if float(row["quantity_received_kg"]) > available + 1e-9:
                raise PurchaseStockInUseError(record_id)
        details = "; ".join(f"{key}={row[key]}" for key in row.keys()) + f"; reason={reason}"
        transaction_type = SPEC_DELETE.get(table, table.rstrip("s").upper())
        conn.execute(
            "INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
            (actor_label(), "DELETE", reference, f"type={transaction_type}; {details}", now())
        )
        if table == "sales":
            conn.execute("DELETE FROM payments WHERE transaction_id=?", (row["transaction_id"],))
            conn.execute("DELETE FROM invoices WHERE transaction_id=?", (row["transaction_id"],))
            conn.execute("DELETE FROM stock_movements WHERE reference=?", (reference,))
        elif table == "purchases":
            conn.execute("DELETE FROM stock_movements WHERE reference=?", (reference,))
        conn.execute("DELETE FROM reversals WHERE table_name=? AND record_id=?", (table, record_id))
        conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
        conn.commit()
    except (sqlite3.Error, ValueError):
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_record(table, record_id, reference):
    conn = db()
    try:
        reason = request.form.get("reason", "").strip()
        if not reason:
            raise ValueError("A reversal reason is required.")
        row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND deleted=0", (record_id,)).fetchone()
        if not row:
            raise ValueError("This transaction is already reversed or unavailable.")
        ts = now()
        by = session["username"]
        if table == "sales":
            qty = float(row["quantity_kg"])
            stock_svc.apply_reversal(conn, table, reference, qty, row["sale_date"], by, ts, reason)
            conn.execute("UPDATE sales SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=? WHERE id=?", (ts, by, reason, record_id))
            conn.execute("UPDATE payments SET deleted=1,deleted_at=?,deleted_by=? WHERE transaction_id=? AND deleted=0",
                         (ts, by, row["transaction_id"]))
            conn.execute("UPDATE payments SET deletion_reason=? WHERE transaction_id=? AND deleted=1",
                         (reason, row["transaction_id"]))
        elif table == "purchases":
            qty = float(row["quantity_received_kg"])
            stock_svc.apply_reversal(conn, table, reference, qty, row["purchase_date"], by, ts, reason)
            conn.execute("UPDATE purchases SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=? WHERE id=?", (ts, by, reason, record_id))
        else:
            conn.execute("UPDATE payments SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=? WHERE id=?", (ts, by, reason, record_id))
            stock_svc.apply_reversal(conn, table, reference, 0, row["payment_date"], by, ts, reason)
        conn.execute("INSERT INTO reversals(table_name,record_id,reference,reason,reversed_by,reversed_at) VALUES(?,?,?,?,?,?)",
                     (table, record_id, reference, reason, by, ts))
        spec_action = SPEC_REVERSE.get(table, "TRANSACTION REVERSED")
        qty_log = row['quantity_kg'] if table == 'sales' else row['quantity_received_kg'] if table == 'purchases' else 0
        amt_log = row['total_sale'] if table == 'sales' else row['total_cost'] if table == 'purchases' else row['amount'] if table == 'payments' else 0
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (f"{by} (id={session.get('user_id')})", spec_action, reference,
                      f"type={table}; previous_status=ACTIVE; new_status=REVERSED; reason={reason}; quantity_kg={qty_log}; amount={amt_log}", ts))
        if table == "sales":
            conn.execute("UPDATE invoices SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=? WHERE transaction_id=?",
                         (ts, by, reason, row["transaction_id"]))
            conn.execute("UPDATE sales SET invoice_number=NULL WHERE transaction_id=?", (row["transaction_id"],))
        conn.commit()
    except (sqlite3.Error, ValueError):
        conn.rollback()
        raise
    finally:
        conn.close()


def cogs_summary(conn=None):
    own_conn = conn is None
    conn = conn or db()
    try:
        purchased = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0) v FROM purchases WHERE deleted=0").fetchone()["v"]
        sold = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0").fetchone()["v"]
        cost_rows = conn.execute("SELECT quantity_received_kg, total_cost FROM purchases WHERE deleted=0 ORDER BY id").fetchall()
        total_received = sum(float(row["quantity_received_kg"]) for row in cost_rows)
        total_cost = sum(float(row["total_cost"]) for row in cost_rows)
        unit_cost = (total_cost / total_received) if total_received > 0 else 0.0
        sold_qty = float(sold)
        cogs = sold_qty * unit_cost
        inventory_qty = max(float(purchased) - sold_qty, 0.0)
        inventory_value = inventory_qty * unit_cost
        return {"purchased_kg": float(purchased), "sold_kg": float(sold), "available_kg": float(purchased) - float(sold),
                "unit_cost": unit_cost, "cogs": cogs, "inventory_value": inventory_value}
    finally:
        if own_conn:
            conn.close()


def stock_summary():
    conn = db()
    purchased = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0) v FROM purchases WHERE deleted=0").fetchone()["v"]
    sold = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0").fetchone()["v"]
    conn.close()
    return float(purchased), float(sold), float(purchased - sold)


def invoice_status_for(total_sale, total_paid):
    total_sale = float(total_sale or 0)
    total_paid = float(total_paid or 0)
    if total_paid > total_sale + 0.005:
        return "OVERPAID"
    if total_sale - total_paid <= 0.005:
        return "PAID"
    if total_paid > 0:
        return "PART PAYMENT"
    return "UNPAID"


def payment_info(payment_ref):
    ref = (payment_ref or "").strip()
    if not ref:
        return None
    conn = db()
    row = conn.execute("""
        SELECT p.*, s.sales_id, s.transaction_id, s.sale_date, s.total_sale, s.quantity_kg,
               s.selling_price_kg, c.name customer_name, c.phone customer_phone,
               c.address customer_address, c.location customer_location,
               (SELECT SUM(pp.amount) FROM payments pp WHERE pp.transaction_id=s.transaction_id AND pp.deleted=0) total_paid
        FROM payments p
        JOIN sales s ON s.transaction_id=p.transaction_id
        JOIN customers c ON c.id=s.customer_id
        WHERE p.deleted=0 AND s.deleted=0 AND (p.payment_id=? OR p.id=? OR p.transaction_id=? OR p.sales_id=? OR s.sales_id=?)
    """, (ref, ref if str(ref).isdigit() else -1, ref, ref, ref)).fetchone()
    conn.close()
    if not row:
        return None
    info = dict(row)
    info["balance"] = max(float(info["total_sale"]) - float(info["total_paid"] or 0), 0)
    info["status"] = invoice_status_for(info["total_sale"], info["total_paid"])
    info["invoice_number"] = info.get("invoice_number") or invoice_number_for(str(info.get("sales_id") or ""))
    return info


def sale_info(transaction_id):
    ref = (transaction_id or "").strip()
    if not ref:
        return None
    conn = db()
    row = conn.execute(f"""
         SELECT s.*, c.name customer_name, c.phone customer_phone,
             c.address customer_address, c.location customer_location,
            COALESCE(s.invoice_number, (SELECT i.invoice_number FROM invoices i WHERE i.transaction_id=s.transaction_id AND i.deleted=0)) invoice_number,
             COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) total_paid
        FROM sales s JOIN customers c ON c.id=s.customer_id
        WHERE ({visible_sql('s')}) AND (s.transaction_id=? OR s.sales_id=?
            OR EXISTS (SELECT 1 FROM invoices i WHERE i.transaction_id=s.transaction_id AND i.deleted=0 AND i.invoice_number=?)
            OR EXISTS (SELECT 1 FROM payments p WHERE p.transaction_id=s.transaction_id AND (p.payment_id=? OR p.sales_id=?)))
    """, (ref, ref, ref, ref, ref)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["sales_id"] = d.get("sales_id") or d.get("transaction_id") or ""
    d["balance"] = max(float(d["total_sale"]) - float(d["total_paid"]), 0)
    d["status"] = invoice_status_for(d["total_sale"], d["total_paid"])
    d["invoice_number"] = d.get("invoice_number") or invoice_number_for(str(d.get("sales_id") or ""))
    return d


def log_action(action, reference="", details=""):
    conn = db()
    username = session.get("username") or "system"
    reference = "" if reference is None else str(reference)
    details = "" if details is None else str(details)
    conn.execute(
        "INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
        (actor_label() if session.get("username") else username, action, reference, details, now())
    )
    conn.commit()
    conn.close()


@app.template_filter("money")
def money_filter(v):
    return money(v)


@app.template_filter("pretty_date")
def pretty_date(v):
    if v is None or str(v).strip() == "":
        return "-"
    text = str(v).strip()
    parsed = None
    for fmt, slice_to in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            parsed = datetime.strptime(text[:slice_to], fmt)
            break
        except (ValueError, TypeError):
            continue
    if parsed is None:
        return text
    stamp = f"{parsed.day} {parsed.strftime('%B %Y')}"
    if len(text) > 10 and " " in text:
        stamp += parsed.strftime(" %I:%M %p").replace(" 0", " ")
    return stamp


CUSTOMER_COLOR_THEMES = [
    "emerald", "cobalt", "amber", "amethyst",
    "teal", "terracotta", "slate", "ruby"
]

@app.template_filter("customer_theme")
def customer_theme_filter(val):
    if not val:
        return "customer-theme-slate"
    text = str(val).strip()
    idx = sum(ord(c) for c in text) % len(CUSTOMER_COLOR_THEMES)
    return f"customer-theme-{CUSTOMER_COLOR_THEMES[idx]}"

@app.template_filter("customer_initials")
def customer_initials_filter(val):
    if not val:
        return "C"
    parts = [p.strip() for p in str(val).split() if p.strip()]
    if not parts:
        return "C"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()

@app.template_filter("customer_type_badge")
def customer_type_badge_filter(val):
    ctype = str(val or "RETAIL").strip().upper()
    if ctype == "WHOLESALE":
        return Markup('<span class="badge-customer-type badge-type-wholesale"><i class="bi bi-building"></i> WHOLESALE</span>')
    elif ctype == "INSTITUTION":
        return Markup('<span class="badge-customer-type badge-type-institution"><i class="bi bi-bank"></i> INSTITUTION</span>')
    elif ctype == "RETAIL":
        return Markup('<span class="badge-customer-type badge-type-retail"><i class="bi bi-shop"></i> RETAIL</span>')
    return Markup(f'<span class="badge-customer-type badge-type-other"><i class="bi bi-person"></i> {ctype}</span>')

@app.template_filter("customer_balance_badge")
def customer_balance_badge_filter(balance):
    try:
        bal = float(balance or 0)
    except (ValueError, TypeError):
        bal = 0.0
    if bal <= 0.005:
        return Markup('<span class="badge-balance badge-balance-cleared"><i class="bi bi-check-circle-fill"></i> CLEARED</span>')
    elif bal < 5000:
        return Markup(f'<span class="badge-balance badge-balance-owing"><i class="bi bi-clock-history"></i> OWING {money(bal)}</span>')
    else:
        return Markup(f'<span class="badge-balance badge-balance-high"><i class="bi bi-exclamation-triangle-fill"></i> HIGH DEBT {money(bal)}</span>')


# Enterprise Security: In-Memory IP Login Rate Limiter & Magic Byte Validator
_LOGIN_IP_ATTEMPTS = {}

def is_ip_login_rate_limited(ip_addr, max_attempts=8, window_seconds=600):
    if not ip_addr:
        return False
    now_ts = datetime.now().timestamp()
    attempts = [ts for ts in _LOGIN_IP_ATTEMPTS.get(ip_addr, []) if now_ts - ts < window_seconds]
    _LOGIN_IP_ATTEMPTS[ip_addr] = attempts
    return len(attempts) >= max_attempts

def record_ip_login_attempt(ip_addr):
    if not ip_addr:
        return
    now_ts = datetime.now().timestamp()
    attempts = [ts for ts in _LOGIN_IP_ATTEMPTS.get(ip_addr, []) if now_ts - ts < 600]
    attempts.append(now_ts)
    _LOGIN_IP_ATTEMPTS[ip_addr] = attempts

def clear_ip_login_attempts(ip_addr):
    if ip_addr in _LOGIN_IP_ATTEMPTS:
        _LOGIN_IP_ATTEMPTS.pop(ip_addr, None)

def is_valid_image_bytes(stream):
    """Deep inspect magic bytes of an uploaded image stream to prevent polyglot or disguised executable uploads."""
    if stream is None:
        return False
    try:
        pos = stream.tell()
        header = stream.read(16)
        stream.seek(pos)
        if len(header) < 4:
            return False
        # JPEG: \xff\xd8\xff
        if header.startswith(b"\xff\xd8\xff"):
            return True
        # PNG: \x89PNG\r\n\x1a\n
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        # WEBP: starts with RIFF and has WEBP at offset 8
        if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return True
        return False
    except Exception:
        return False


@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()
        if is_ip_login_rate_limited(client_ip):
            log_action("SECURITY RATE LIMIT", "SYSTEM", f"ip={client_ip}; login attempts exceeded threshold")
            abort(429, description="Too many failed sign-in attempts from your network. Please wait 10 minutes and try again.")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        locked = bool(user and user["locked_until"] and user["locked_until"] > now())
        if user and not locked and check_password_hash(user["password_hash"], password):
            clear_ip_login_attempts(client_ip)
            conn.execute("UPDATE users SET last_login=?,failed_login_attempts=0,locked_until=NULL WHERE id=?", (now(), user["id"]))
            conn.commit()
            conn.close()
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            log_action("LOGIN SUCCESS", username, f"user_id={user['id']}; ip={client_ip}; role={user['role']}")
            return redirect(url_for("dashboard"))
        record_ip_login_attempt(client_ip)
        if user and not locked:
            attempts = int(user["failed_login_attempts"] or 0) + 1
            lock_until = None
            if attempts >= 5:
                lock_until = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                attempts = 0
                log_action("LOGIN LOCKED", username, f"Too many failed sign-in attempts; locked for 15 minutes; ip={client_ip}")
            conn.execute("UPDATE users SET failed_login_attempts=?,locked_until=? WHERE id=?", (attempts, lock_until, user["id"]))
            conn.commit()
        else:
            log_action("LOGIN FAILED", username or "unknown", f"ip={client_ip}; invalid credentials or inactive account")
        conn.close()
        flash("Invalid credentials or account temporarily unavailable.", "danger")
    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if is_gmail_address(email):
            conn = db()
            user = conn.execute("SELECT id,email FROM users WHERE lower(email)=lower(?) AND active=1", (email,)).fetchone()
            if user and user["email"]:
                token = secrets.token_urlsafe(32)
                expires_at = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
                conn.execute("UPDATE users SET password_reset_token=?,password_reset_expires_at=? WHERE id=?", (token, expires_at, user["id"]))
                conn.commit()
                send_password_reset_email(user["email"], token)
            conn.close()
        flash("If that email is registered, a password reset link has been sent.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = db()
    user = conn.execute("SELECT id FROM users WHERE password_reset_token=? AND password_reset_expires_at>? AND active=1", (token, now())).fetchone()
    if not user:
        conn.close()
        flash("That password reset link is invalid or has expired.", "danger")
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        policy_error = validate_password(password)
        if policy_error or password != confirmation:
            conn.close()
            flash(policy_error or "Passwords must match.", "danger")
            return render_template("reset_password.html")
        conn.execute("UPDATE users SET password_hash=?,password_reset_token=NULL,password_reset_expires_at=NULL WHERE id=?", (generate_password_hash(password), user["id"]))
        conn.commit()
        conn.close()
        flash("Password reset successfully. You can now sign in.", "success")
        return redirect(url_for("login"))
    conn.close()
    return render_template("reset_password.html")


@app.route("/verify-email/<token>")
def verify_email(token):
    conn = db()
    user = conn.execute("SELECT id FROM users WHERE verification_token=? AND verification_expires_at>?", (token, now())).fetchone()
    if not user:
        conn.close()
        flash("That verification link is invalid or has expired.", "danger")
        return redirect(url_for("login"))
    conn.execute("UPDATE users SET email_verified=1,verification_token=NULL,verification_expires_at=NULL WHERE id=?", (user["id"],))
    conn.commit()
    conn.close()
    flash("Email verified. You can now sign in with your email address.", "success")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    if session.get("username"):
        log_action("LOGOUT", str(session.get("username") or ""), f"user_id={session.get('user_id') or ''}")
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
@require_permission("view_dashboard")
def dashboard():
    purchased, sold, stock = stock_summary()
    conn = db()
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    summary_row = conn.execute("""
        WITH sales_summary AS (
            SELECT
                COALESCE(SUM(CASE WHEN deleted=0 THEN total_sale ELSE 0 END), 0) AS sales_total,
                COUNT(CASE WHEN deleted=0 THEN 1 END) AS transactions_count,
                COALESCE(SUM(CASE WHEN deleted=0 AND sale_date=? THEN total_sale ELSE 0 END), 0) AS today_sales,
                COALESCE(SUM(CASE WHEN deleted=0 AND sale_date>=? THEN total_sale ELSE 0 END), 0) AS month_sales
            FROM sales
        ),
        payments_summary AS (
            SELECT
                COALESCE(SUM(CASE WHEN deleted=0 THEN amount ELSE 0 END), 0) AS payments_total,
                COALESCE(SUM(CASE WHEN deleted=0 AND payment_date=? THEN amount ELSE 0 END), 0) AS today_payments,
                COALESCE(SUM(CASE WHEN deleted=0 AND payment_date>=? THEN amount ELSE 0 END), 0) AS month_payments
            FROM payments
        ),
        purchases_summary AS (
            SELECT
                COALESCE(SUM(CASE WHEN deleted=0 THEN quantity_received_kg ELSE 0 END), 0) AS purchased_kg,
                COALESCE(SUM(CASE WHEN deleted=0 THEN total_purchase_cost ELSE 0 END), 0) AS purchase_cost_total,
                COALESCE(SUM(CASE WHEN deleted=0 THEN transport_cost ELSE 0 END), 0) AS transport_total,
                COALESCE(SUM(CASE WHEN deleted=0 THEN other_expenses ELSE 0 END), 0) AS other_total,
                COUNT(DISTINCT CASE WHEN deleted=0 THEN local_agent END) AS supplier_count
            FROM purchases
        ),
        customer_summary AS (
            SELECT
                COALESCE(SUM(CASE WHEN active=1 THEN opening_balance ELSE 0 END), 0) AS opening_balances,
                COUNT(CASE WHEN active=1 THEN 1 END) AS customers_count
            FROM customers
        )
        SELECT
            pus.purchased_kg,
            ss.sales_total,
            pms.payments_total,
            cs.opening_balances,
            cs.customers_count,
            ss.transactions_count,
            pus.purchase_cost_total,
            pus.transport_total,
            pus.other_total,
            pus.supplier_count,
            ss.today_sales,
            pms.today_payments,
            ss.month_sales,
            pms.month_payments
        FROM sales_summary ss
        CROSS JOIN payments_summary pms
        CROSS JOIN purchases_summary pus
        CROSS JOIN customer_summary cs
    """, (today, month_start, today, month_start)).fetchone()

    sale_payment_totals = conn.execute("""
        SELECT s.transaction_id, s.total_sale,
               COALESCE(SUM(p.amount),0) AS total_paid
        FROM sales s
        LEFT JOIN payments p ON p.transaction_id=s.transaction_id AND p.deleted=0
        WHERE s.deleted=0
        GROUP BY s.transaction_id, s.total_sale
    """).fetchall()

    paid = 0
    part = 0
    sale_outstanding = 0.0
    for row in sale_payment_totals:
        total_sale = float(row["total_sale"] or 0)
        total_paid = float(row["total_paid"] or 0)
        if total_paid >= total_sale - 0.005:
            paid += 1
        elif total_paid > 0:
            part += 1
        sale_outstanding += max(total_sale - total_paid, 0.0)

    sales = float(summary_row["sales_total"] or 0)
    payments = float(summary_row["payments_total"] or 0)
    opening_balances = float(summary_row["opening_balances"] or 0)
    purchase_cost = float(summary_row["purchase_cost_total"] or 0)
    transport = float(summary_row["transport_total"] or 0)
    other = float(summary_row["other_total"] or 0)
    customers = int(summary_row["customers_count"] or 0)
    transactions = int(summary_row["transactions_count"] or 0)
    unpaid = max(transactions - paid - part, 0)
    outstanding = max(float(opening_balances), 0) + float(sale_outstanding)
    expenses = float(purchase_cost) + float(transport) + float(other)
    cost_summary = cogs_summary(conn)
    profit = float(sales) - float(cost_summary["cogs"])

    recent = conn.execute("""SELECT s.sales_id transaction_id,s.sales_id,s.sale_date,c.name,s.quantity_kg,s.total_sale,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
        FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.deleted=0 ORDER BY s.id DESC LIMIT 8""").fetchall()
    recent_purchases = conn.execute("""SELECT purchase_id,purchase_date,local_agent,quantity_received_kg,total_cost
        FROM purchases WHERE deleted=0 ORDER BY id DESC LIMIT 6""").fetchall()
    recent_payments = conn.execute("""SELECT p.payment_id,p.transaction_id,s.sales_id,p.payment_date,p.amount,p.payment_method,c.name customer_name
        FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id JOIN customers c ON c.id=s.customer_id
        WHERE p.deleted=0 AND s.deleted=0 ORDER BY p.id DESC LIMIT 6""").fetchall()
    recent_invoices = conn.execute("""SELECT i.invoice_number,s.sales_id,i.invoice_date,c.name customer_name
        FROM invoices i JOIN sales s ON s.transaction_id=i.transaction_id JOIN customers c ON c.id=s.customer_id
        WHERE s.deleted=0 ORDER BY i.id DESC LIMIT 6""").fetchall()
    outstanding_customers = conn.execute("""SELECT c.id,c.name,c.phone,
        COALESCE(SUM(s.total_sale),0) + c.opening_balance total_sales,
        COALESCE((SELECT SUM(p.amount) FROM payments p JOIN sales ps ON ps.transaction_id=p.transaction_id
                  WHERE ps.customer_id=c.id AND ps.deleted=0 AND p.deleted=0),0) total_paid
        FROM customers c LEFT JOIN sales s ON s.customer_id=c.id AND s.deleted=0
        GROUP BY c.id HAVING total_sales-total_paid > 0.005 ORDER BY total_sales-total_paid DESC LIMIT 8""").fetchall()
    monthly_sales = [dict(row) for row in conn.execute("""SELECT substr(sale_date,1,7) month,COALESCE(SUM(total_sale),0) revenue,
        COALESCE(SUM(quantity_kg),0) quantity FROM sales WHERE deleted=0 GROUP BY month ORDER BY month DESC LIMIT 6""").fetchall()]
    monthly_purchases = [dict(row) for row in conn.execute("""SELECT substr(purchase_date,1,7) month,COALESCE(SUM(total_cost),0) cost,
        COALESCE(SUM(quantity_received_kg),0) quantity FROM purchases WHERE deleted=0 GROUP BY month ORDER BY month DESC LIMIT 6""").fetchall()]
    supplier_count = int(summary_row["supplier_count"] or 0)
    today_sales = float(summary_row["today_sales"] or 0)
    today_payments = float(summary_row["today_payments"] or 0)
    month_sales = float(summary_row["month_sales"] or 0)
    month_payments = float(summary_row["month_payments"] or 0)
    pending_invoices = conn.execute("""SELECT COUNT(*) v FROM sales s WHERE s.deleted=0 AND
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) < s.total_sale""").fetchone()["v"]
    recent_customers = conn.execute("""SELECT c.id, c.name, c.phone, c.location, c.customer_type,
        COALESCE(SUM(s.total_sale),0) + c.opening_balance total_purchases
        FROM customers c LEFT JOIN sales s ON s.customer_id=c.id AND s.deleted=0
        WHERE c.active=1
        GROUP BY c.id ORDER BY c.id DESC LIMIT 5""").fetchall()
    saved_dashboard_images = conn.execute("SELECT slot,filename FROM dashboard_images").fetchall()
    conn.close()
    stats = dict(purchased=purchased,sold=sold,stock=stock,sales=sales,payments=payments,
                 outstanding=outstanding,purchase_cost=float(purchase_cost),transport=float(transport),
                 other=float(other),expenses=expenses,profit=profit,customers=customers,transactions=transactions,
                 paid=paid,part=part,unpaid=unpaid,suppliers=supplier_count,pending_invoices=pending_invoices,
                 today_sales=today_sales,today_payments=today_payments,
                 month_sales=month_sales,month_payments=month_payments)
    dashboard_images = dict(DASHBOARD_IMAGE_DEFAULTS)
    normalized_saved = {}
    for row in saved_dashboard_images:
        slot_name = DASHBOARD_SLOT_ALIASES.get(row["slot"], row["slot"])
        if slot_name in ALLOWED_DASHBOARD_SLOTS:
            normalized_saved[slot_name] = row["filename"]
    dashboard_images.update(normalized_saved)
    return render_template("dashboard.html", stats=stats, recent=recent,
                           recent_purchases=recent_purchases, recent_payments=recent_payments,
                           recent_invoices=recent_invoices, outstanding_customers=outstanding_customers,
                           recent_customers=recent_customers,
                           monthly_sales=monthly_sales, monthly_purchases=monthly_purchases,
                           dashboard_images=dashboard_images,
                           dashboard_image_slots=DASHBOARD_IMAGE_SLOT_ORDER)


@app.route("/dashboard/settings")
@login_required
@admin_required
def dashboard_settings():
    conn = db()
    saved_dashboard_images = conn.execute("SELECT slot,filename FROM dashboard_images").fetchall()
    conn.close()
    dashboard_images = dict(DASHBOARD_IMAGE_DEFAULTS)
    normalized_saved = {}
    for row in saved_dashboard_images:
        slot_name = DASHBOARD_SLOT_ALIASES.get(row["slot"], row["slot"])
        if slot_name in ALLOWED_DASHBOARD_SLOTS:
            normalized_saved[slot_name] = row["filename"]
    dashboard_images.update(normalized_saved)
    return render_template("dashboard_settings.html",
                           dashboard_images=dashboard_images,
                           dashboard_image_slots=DASHBOARD_IMAGE_SLOT_ORDER)


@app.route("/dashboard/images", methods=["POST"])
@login_required
@admin_required
def update_dashboard_image():
    slot = request.form.get("slot", "").strip().lower()
    slot = DASHBOARD_SLOT_ALIASES.get(slot, slot)
    action = request.form.get("action", "upload")
    if slot not in ALLOWED_DASHBOARD_SLOTS:
        abort(400, description="Choose a valid dashboard image area.")
    conn = db()
    existing = conn.execute("SELECT filename FROM dashboard_images WHERE slot=?", (slot,)).fetchone()
    old_filename = existing["filename"] if existing else None
    if action == "reset":
        conn.execute("DELETE FROM dashboard_images WHERE slot=?", (slot,))
        if old_filename and old_filename.startswith("images/dashboard/custom/"):
            old_path = BASE_DIR / "static" / old_filename
            if old_path.exists(): old_path.unlink()
        conn.commit()
        conn.close()
        log_action("DASHBOARD IMAGE RESET", slot, f"user={session.get('username')}")
        flash(f"{slot.title()} image reset to the ADUFARMS default.", "success")
        return redirect(url_for("dashboard_settings") + "#dashboard-imagery")
    image = request.files.get("image")
    if not image or not image.filename:
        conn.close()
        flash("Select a JPG, PNG, or WEBP image first.", "danger")
        return redirect(url_for("dashboard_settings") + "#dashboard-imagery")
    extension = Path(image.filename).suffix.lower()
    if extension not in ALLOWED_PROFILE_EXTS or image.mimetype not in ALLOWED_PROFILE_MIMES or not is_valid_image_bytes(image.stream):
        conn.close()
        flash("Invalid or corrupted image file. Please upload a genuine JPG, PNG, or WEBP file.", "danger")
        return redirect(url_for("dashboard_settings") + "#dashboard-imagery")
    filename = secure_filename(f"dashboard-{slot}-{secrets.token_hex(8)}{extension}")
    relative_filename = f"images/dashboard/custom/{filename}"
    image.save(DASHBOARD_IMAGE_DIR / filename)
    conn.execute("""INSERT INTO dashboard_images(slot,filename,updated_at,updated_by) VALUES(?,?,?,?)
                    ON CONFLICT(slot) DO UPDATE SET filename=excluded.filename,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                 (slot, relative_filename, now(), session.get("username", "admin")))
    conn.commit()
    conn.close()
    if old_filename and old_filename.startswith("images/dashboard/custom/") and old_filename != relative_filename:
        old_path = BASE_DIR / "static" / old_filename
        if old_path.exists(): old_path.unlink()
    log_action("DASHBOARD IMAGE UPDATED", slot, f"user={session.get('username')}")
    flash(f"{slot.title()} image updated.", "success")
    return redirect(url_for("dashboard_settings") + "#dashboard-imagery")


@app.route("/purchases", methods=["GET","POST"])
@login_required
@require_permission("view_purchases")
def purchases():
    if request.method == "POST":
        try:
            purchase_date = valid_date(request.form.get("purchase_date"), "purchase date")
            local_agent = request.form.get("local_agent", "").strip()
            if not local_agent:
                raise ValueError("Supplier or local agent is required.")
            qty = get_float("quantity_kg")
            price = nonnegative_float("price_per_kg")
            transport = nonnegative_float("transport_cost")
            other = nonnegative_float("other_expenses")
            received = get_float("quantity_received_kg")
            if qty <= 0 or received < 0 or received > qty:
                raise ValueError("Quantity received must be between 0 and quantity purchased.")
            total_purchase = qty * price
            total_cost = total_purchase + transport + other
            pid = next_daily_id("ADU-PUR", "purchases", "purchase_id")
            ts = now()
            by = session["username"]
            purchase_date = valid_date(purchase_date, "purchase date")
            conn = db()
            try:
                conn.execute("""INSERT INTO purchases
                    (purchase_id,purchase_date,local_agent,agent_phone,location,quantity_kg,price_per_kg,
                     total_purchase_cost,transport_cost,other_expenses,total_cost,quantity_received_kg,staff_user,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid,purchase_date,local_agent,
                     request.form.get("agent_phone","").strip(),request.form.get("location","").strip(),
                     qty,price,total_purchase,transport,other,total_cost,received,by,ts))
                stock_svc.apply_purchase_create(conn, pid, received, purchase_date, by, ts)
                conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                             (actor_label(), "PURCHASE CREATED", pid, f"{received:g} KG received; total_cost={total_cost:.2f}", ts))
                conn.commit()
            except (sqlite3.Error, ValueError):
                conn.rollback()
                raise
            finally:
                conn.close()
            flash(f"Purchase {pid} recorded.", "success")
            return redirect(url_for("purchases"))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else "The purchase could not be saved.", "danger")
    blocked_purchase = request.args.get("blocked_purchase", type=int)
    conn = db()
    rows = conn.execute(f"SELECT p.* FROM purchases p WHERE {visible_sql('p')} ORDER BY p.id DESC LIMIT 100").fetchall()
    blocked_purchase_row = None
    if blocked_purchase:
        blocked_purchase_row = conn.execute(
            "SELECT id,purchase_id,purchase_date,local_agent,quantity_received_kg,total_cost FROM purchases WHERE id=?",
            (blocked_purchase,)
        ).fetchone()
    conn.close()
    return render_template("purchases.html", rows=rows, today=date.today().isoformat(),
                           blocked_purchase=blocked_purchase_row)


@app.route("/sales", methods=["GET","POST"])
@login_required
@require_permission("view_sales")
def sales():
    if request.method == "POST":
        try:
            sale_date = valid_date(request.form.get("sale_date"), "sale date")
            qty = get_float("quantity_kg")
            price = nonnegative_float("selling_price_kg")
            if qty <= 0 or price < 0:
                raise ValueError("Enter valid quantity and selling price.")
            customer_id_form = request.form.get("customer_id", type=int)
            name = request.form["customer_name"].strip()
            phone = request.form.get("customer_phone","").strip()
            if not name:
                raise ValueError("Customer name is required.")
            total = qty * price
            ts = now()
            by = session["username"]
            conn = db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                sale_date = valid_date(sale_date, "sale date")
                sale_day = datetime.strptime(sale_date, "%Y-%m-%d").date()
                tid = next_daily_id("ADU", "sales", "transaction_id", conn, sale_day)
                sales_id = next_daily_id("ADU-SAL", "sales", "sales_id", conn, sale_day)
                invoice_id = next_daily_id("ADU-INV", "invoices", "invoice_number", conn, sale_day)
                stock_svc.assert_stock_available(conn, qty)
                cid = None
                if customer_id_form:
                    c_row = conn.execute("SELECT id, name FROM customers WHERE id=? AND active=1", (customer_id_form,)).fetchone()
                    if c_row:
                        cid = c_row["id"]
                if not cid and phone:
                    clean_p = normalized_phone(phone)
                    if clean_p:
                        all_c = conn.execute("SELECT id, phone, name FROM customers WHERE active=1").fetchall()
                        p_match = next((c for c in all_c if normalized_phone(c["phone"]) == clean_p), None)
                        if p_match:
                            cid = p_match["id"]
                if not cid and name:
                    norm_n = normalized_customer_name(name)
                    tokens = set(norm_n.split())
                    all_c = conn.execute("SELECT id, phone, name FROM customers WHERE active=1").fetchall()
                    n_match = next((c for c in all_c if normalized_customer_name(c["name"]) == norm_n
                                    or (len(tokens) > 1 and set(normalized_customer_name(c["name"]).split()) == tokens)), None)
                    if n_match:
                        cid = n_match["id"]
                if not cid:
                    cur = conn.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)", (name,phone,ts))
                    cid = cur.lastrowid
                conn.execute("""INSERT INTO sales(transaction_id,sales_id,invoice_number,sale_date,customer_id,quantity_kg,selling_price_kg,total_sale,staff_user,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                             (tid,sales_id,invoice_id,sale_date,cid,qty,price,total,by,ts))
                conn.execute("""INSERT INTO invoices(invoice_number,transaction_id,sales_id,invoice_date,generated_by,generated_at)
                                VALUES(?,?,?,?,?,?)""",
                             (invoice_id, tid, sales_id, sale_date, by, ts))
                stock_svc.apply_sale_create(conn, tid, qty, sale_date, by, ts)
                conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                             (actor_label(), "SALE CREATED", sales_id, f"{qty:g} KG sold to {name}; sales_id={sales_id}; total={total:.2f}", ts))
                conn.commit()
            except (sqlite3.Error, ValueError):
                conn.rollback()
                raise
            finally:
                conn.close()
            flash(f"Sale {sales_id} created and invoice {invoice_id} is ready. Record payment when received.", "success")
            return redirect(url_for("invoice", transaction_id=sales_id))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else "The sale could not be saved.", "danger")
    related_purchase = request.args.get("related_purchase", type=int)
    selected_customer = request.args.get("customer_id", type=int)
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 80
    conn = db()
    selected_customer_row = conn.execute(
        "SELECT id,name,phone,location,address,customer_type FROM customers WHERE id=? AND active=1",
        (selected_customer,)
    ).fetchone() if selected_customer else None
    all_customers = conn.execute(
        "SELECT id,name,phone,location,address,customer_type FROM customers WHERE active=1 ORDER BY name ASC"
    ).fetchall()
    related_purchase_row = None
    sales_filter = ""
    sales_params = []
    if related_purchase:
        related_purchase_row = conn.execute(
            "SELECT purchase_id,purchase_date,local_agent FROM purchases WHERE id=?",
            (related_purchase,)
        ).fetchone()
        if related_purchase_row:
            sales_filter = " AND s.sale_date >= ?"
            sales_params.append(related_purchase_row["purchase_date"])
    total_sales = conn.execute(f"SELECT COUNT(*) AS c FROM sales s WHERE {visible_sql('s')} {sales_filter}", sales_params).fetchone()["c"]
    page_count = max((total_sales + per_page - 1) // per_page, 1)
    page = min(page, page_count)
    offset = (page - 1) * per_page
    rows = conn.execute(f"""SELECT s.*,c.name customer_name,c.phone,
        COALESCE(s.invoice_number, i.invoice_number) invoice_number,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
        FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN invoices i ON i.transaction_id=s.transaction_id AND i.deleted=0
        WHERE {visible_sql('s')} {sales_filter}
        ORDER BY s.sale_date ASC, s.id ASC LIMIT ? OFFSET ?""", [*sales_params, per_page, offset]).fetchall()
    conn.close()
    _, _, stock = stock_summary()
    return render_template("sales.html", rows=rows, stock=stock, today=date.today().isoformat(),
                           related_purchase=related_purchase_row, selected_customer=selected_customer_row,
                           customers=all_customers, page=page, page_count=page_count,
                           per_page=per_page, total_sales=total_sales)


@app.route("/payments", methods=["GET","POST"])
@login_required
@require_permission("view_payments")
def payments():
    selected_sales_id = (request.args.get("sales_id") or request.args.get("transaction_id") or "").strip()
    selected_customer_id = request.args.get("customer_id", type=int)
    linked_customer_id = None
    if selected_sales_id:
        conn = db()
        linked_customer = conn.execute(
            """SELECT s.customer_id FROM sales s
               WHERE (s.sales_id=? OR s.transaction_id=?) AND s.deleted=0""",
            (selected_sales_id, selected_sales_id)
        ).fetchone()
        conn.close()
        if linked_customer:
            linked_customer_id = linked_customer["customer_id"]
            selected_customer_id = linked_customer_id
    if request.method == "POST":
        try:
            sale_ref = (request.form.get("sales_id") or request.form.get("transaction_id") or "").strip()
            customer_id = request.form.get("customer_id", type=int)
            if not sale_ref:
                raise ValueError("Select an outstanding sale.")
            if not customer_id:
                raise ValueError("Select a customer first.")
            info = sale_info(sale_ref)
            if not info:
                raise ValueError("Sales ID was not found.")
            if int(info["customer_id"]) != customer_id:
                raise ValueError("The selected sale does not belong to that customer.")
            tid = info["transaction_id"]
            pay_date = valid_date(request.form.get("payment_date"), "payment date")
            amount = get_float("amount")
            if amount <= 0:
                raise ValueError("Payment amount must be greater than zero.")
            if amount > info["balance"] + 0.005:
                raise ValueError(f"Payment exceeds outstanding balance of {money(info['balance'])}.")
            ts = now()
            by = session["username"]
            conn = db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                live_paid = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) v FROM payments WHERE transaction_id=? AND deleted=0",
                    (tid,)
                ).fetchone()["v"]
                live_balance = max(float(info["total_sale"]) - float(live_paid), 0)
                if amount > live_balance + 0.005:
                    raise ValueError(f"Payment exceeds outstanding balance of {money(live_balance)}.")
                payment_reference = request.form.get("payment_reference", "").strip()
                pay_date = valid_date(pay_date, "payment date")
                if payment_reference and conn.execute(
                    "SELECT 1 FROM payments WHERE transaction_id=? AND payment_reference=? AND deleted=0",
                    (tid, payment_reference)
                ).fetchone():
                    raise ValueError("A payment with this reference already exists for the sale.")
                payment_id = next_daily_id("ADU-PAY", "payments", "payment_id", conn,
                                           datetime.strptime(pay_date, "%Y-%m-%d").date())
                conn.execute(
                    """INSERT INTO payments(payment_id,transaction_id,sales_id,payment_date,amount,
                       payment_method,payment_reference,notes,staff_user,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (payment_id, tid, info["sales_id"], pay_date, amount, request.form["payment_method"],
                     payment_reference, request.form.get("notes", "").strip(), by, ts))
                conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                             (actor_label(), "PAYMENT CREATED", payment_id, f"sales_id={info['sales_id']}; amount={amount:.2f}; method={request.form['payment_method']}", ts))
                conn.commit()
            except (sqlite3.Error, ValueError):
                conn.rollback()
                raise
            finally:
                conn.close()
            flash(f"Payment recorded against {info['sales_id']}.", "success")
            return redirect(url_for("payments"))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else "The payment could not be saved.", "danger")
    conn = db()
    rows = conn.execute(f"""SELECT p.*, COALESCE(p.sales_id, s.sales_id) AS sales_id, c.name customer_name, c.phone, s.total_sale invoice_amount
        FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
        JOIN customers c ON c.id=s.customer_id WHERE {visible_sql('p')} AND {visible_sql('s')} ORDER BY p.id DESC LIMIT 100""").fetchall()
    customers = conn.execute(
        "SELECT id,name,phone,active FROM customers WHERE active=1 OR id=? ORDER BY active DESC,name",
        (linked_customer_id or selected_customer_id or -1,)
    ).fetchall()
    outstanding_sales = conn.execute("""SELECT s.sales_id,s.customer_id,s.sale_date,s.total_sale,c.name customer_name,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
        FROM sales s JOIN customers c ON c.id=s.customer_id
        WHERE s.deleted=0 AND s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p
            WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > 0.005
        ORDER BY c.name,s.sale_date,s.id""").fetchall()
    conn.close()
    total_collected = sum(float(r["amount"]) for r in rows if not r["deleted"])
    momo_collected = sum(float(r["amount"]) for r in rows if not r["deleted"] and 'mobile' in (r["payment_method"] or '').lower())
    cash_collected = sum(float(r["amount"]) for r in rows if not r["deleted"] and 'cash' in (r["payment_method"] or '').lower())
    bank_collected = sum(float(r["amount"]) for r in rows if not r["deleted"] and 'bank' in (r["payment_method"] or '').lower())
    total_outstanding = sum(max(float(s["total_sale"]) - float(s["paid"]), 0) for s in outstanding_sales)
    payment_metrics = {
        "total_collected": total_collected,
        "momo_collected": momo_collected,
        "cash_collected": cash_collected,
        "bank_collected": bank_collected,
        "total_outstanding": total_outstanding,
        "count": len([r for r in rows if not r["deleted"]])
    }
    return render_template("payments.html", rows=rows, customers=customers,
                           outstanding_sales=outstanding_sales, today=date.today().isoformat(),
                           selected_customer_id=selected_customer_id,
                           selected_sales_id=selected_sales_id,
                           metrics=payment_metrics)


@app.route("/purchases/<int:purchase_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("edit_purchases")
def edit_purchase(purchase_id):
    conn = db()
    row = conn.execute(f"SELECT p.* FROM purchases p WHERE p.id=? AND {visible_sql('p')}", (purchase_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    if request.method == "POST":
        try:
            purchase_date = valid_date(request.form.get("purchase_date"), "purchase date")
            qty = get_float("quantity_kg")
            price = nonnegative_float("price_per_kg")
            transport = nonnegative_float("transport_cost")
            other = nonnegative_float("other_expenses")
            received = get_float("quantity_received_kg")
            if qty <= 0 or received < 0 or received > qty:
                raise ValueError("Quantity received must be between 0 and quantity purchased.")
            ts = now()
            by = session["username"]
            purchase_date = valid_date(purchase_date, "purchase date")
            conn = db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT * FROM purchases WHERE id=? AND deleted=0", (purchase_id,)).fetchone()
                if not current:
                    raise ValueError("This transaction is unavailable.")
                old_received = float(current["quantity_received_kg"])
                stock_svc.apply_purchase_edit(conn, current["purchase_id"], old_received, received,
                                              purchase_date, by, ts)
                conn.execute("""UPDATE purchases SET purchase_date=?,local_agent=?,agent_phone=?,location=?,
                    quantity_kg=?,price_per_kg=?,total_purchase_cost=?,transport_cost=?,other_expenses=?,
                    total_cost=?,quantity_received_kg=? WHERE id=?""",
                    (purchase_date, request.form["local_agent"].strip(), request.form.get("agent_phone", "").strip(),
                     request.form.get("location", "").strip(), qty, price, qty * price, transport, other,
                     qty * price + transport + other, received, purchase_id))
                conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                             (actor_label(), "PURCHASE UPDATED", current["purchase_id"],
                              f"received {old_received:g}->{received:g} KG; total_cost={qty*price+transport+other:.2f}", ts))
                conn.commit()
            except (sqlite3.Error, ValueError):
                conn.rollback()
                raise
            finally:
                conn.close()
            flash("Purchase updated.", "success")
            return redirect(url_for("purchases"))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else "The purchase could not be updated.", "danger")
    return render_template("edit_record.html", kind="purchase", record=row)


@app.route("/sales/<int:sale_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("edit_sales")
def edit_sale(sale_id):
    conn = db()
    row = conn.execute(f"""SELECT s.*,c.name customer_name,c.phone customer_phone,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
        FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.id=? AND {visible_sql('s')}""", (sale_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    if request.method == "POST":
        try:
            sale_date = valid_date(request.form.get("sale_date"), "sale date")
            qty = get_float("quantity_kg")
            price = nonnegative_float("selling_price_kg")
            if qty <= 0 or price < 0:
                raise ValueError("Enter valid quantity and selling price.")
            total = qty * price
            if total + 0.005 < float(row["paid"]):
                raise ValueError("Sale total cannot be lower than payments already received.")
            name = request.form["customer_name"].strip()
            phone = request.form.get("customer_phone", "").strip()
            if not name:
                raise ValueError("Customer name is required.")
            ts = now()
            by = session["username"]
            sale_date = valid_date(sale_date, "sale date")
            conn = db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT * FROM sales WHERE id=? AND deleted=0", (sale_id,)).fetchone()
                if not current:
                    raise ValueError("This transaction is unavailable.")
                old_qty = float(current["quantity_kg"])
                stock_svc.apply_sale_edit(conn, current["transaction_id"], old_qty, qty, sale_date, by, ts)
                customer = conn.execute("SELECT id FROM customers WHERE lower(trim(name))=lower(trim(?)) AND replace(replace(replace(phone,' ',''),'-',''),'+','')=replace(replace(replace(?,' ',''),'-',''),'+','') AND active=1", (name, phone)).fetchone()
                cid = customer["id"] if customer else conn.execute(
                    "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)", (name, phone, ts)
                ).lastrowid
                conn.execute("UPDATE sales SET sale_date=?,customer_id=?,quantity_kg=?,selling_price_kg=?,total_sale=? WHERE id=?",
                             (sale_date, cid, qty, price, total, sale_id))
                conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                             (actor_label(), "SALE UPDATED", current["transaction_id"],
                              f"qty {old_qty:g}->{qty:g} KG; total={total:.2f}", ts))
                conn.commit()
            except (sqlite3.Error, ValueError):
                conn.rollback()
                raise
            finally:
                conn.close()
            flash("Sale updated.", "success")
            return redirect(url_for("sales"))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else "The sale could not be updated.", "danger")
    return render_template("edit_record.html", kind="sale", record=row)


@app.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("manage_payments")
def edit_payment(payment_id):
    conn = db()
    row = conn.execute(f"""SELECT p.*,s.total_sale,s.deleted sale_deleted
        FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
        WHERE p.id=? AND {visible_sql('p')} AND {visible_sql('s')}""", (payment_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    if request.method == "POST":
        try:
            payment_date = valid_date(request.form.get("payment_date"), "payment date")
            amount = get_float("amount")
            if amount <= 0:
                raise ValueError("Payment amount must be greater than zero.")
            conn = db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT * FROM payments WHERE id=? AND deleted=0", (payment_id,)).fetchone()
                if not current:
                    raise ValueError("This payment is reversed or unavailable.")
                other_paid = conn.execute("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE transaction_id=? AND id!=? AND deleted=0",
                                          (row["transaction_id"], payment_id)).fetchone()["v"]
                if amount + float(other_paid) > float(row["total_sale"]) + 0.005:
                    raise ValueError("Payment exceeds the sale balance.")
                conn.execute("UPDATE payments SET payment_date=?,amount=?,payment_method=?,payment_reference=?,notes=? WHERE id=?",
                             (payment_date, amount, request.form["payment_method"],
                              request.form.get("payment_reference", "").strip(),
                              request.form.get("notes", "").strip(), payment_id))
                conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                             (actor_label(), "PAYMENT UPDATED", row["transaction_id"], f"amount={amount:.2f}", now()))
                conn.commit()
            except (sqlite3.Error, ValueError):
                conn.rollback()
                raise
            finally:
                conn.close()
            flash("Payment updated.", "success")
            return redirect(url_for("payments"))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else "The payment could not be updated.", "danger")
    return render_template("edit_record.html", kind="payment", record=row)


@app.route("/customers", methods=["GET", "POST"])
@login_required
@require_permission("view_customers")
def customers():
    if request.method == "POST":
        conn = None
        try:
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            opening_balance = nonnegative_float("opening_balance")
            if not name:
                raise ValueError("Customer name is required.")
            conn = db()
            existing_customers = conn.execute(
                "SELECT id,name,phone FROM customers WHERE active=1"
            ).fetchall()
            duplicate = next((row for row in existing_customers
                              if (normalized_phone(row["phone"]) and normalized_phone(row["phone"]) == normalized_phone(phone))
                              or (normalized_customer_name(row["name"]) == normalized_customer_name(name) and normalized_phone(row["phone"]) == normalized_phone(phone))), None)
            if duplicate:
                conn.close()
                flash(f"Customer '{duplicate['name']}' ({duplicate['phone'] or 'no phone'}) is already registered. Proceeding directly to sales.", "info")
                return redirect(url_for("sales", customer_id=duplicate["id"]))
            customer_cursor = conn.execute("""INSERT INTO customers
                (name,phone,address,location,customer_type,opening_balance,notes,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (
                    name, phone,
                    request.form.get("address", "").strip(), request.form.get("location", "").strip(),
                    request.form.get("customer_type", "RETAIL"), opening_balance,
                    request.form.get("notes", "").strip(), now()))
            customer_id = customer_cursor.lastrowid
            conn.commit()
            conn.close()
            flash("Customer created. Enter the sale details to generate the Sales ID and invoice number.", "success")
            return redirect(url_for("sales", customer_id=customer_id))
        except (ValueError, sqlite3.Error) as error:
            if conn is not None:
                conn.rollback()
                conn.close()
            flash(str(error) if isinstance(error, ValueError) else "The customer could not be saved.", "danger")
    blocked_customer = request.args.get("blocked_customer", type=int)
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 100
    offset = (page - 1) * per_page
    conn = db()
    total_customers = conn.execute("SELECT COUNT(*) AS c FROM customers WHERE active=1").fetchone()["c"]
    page_count = max((total_customers + per_page - 1) // per_page, 1)
    page = min(page, page_count)
    offset = (page - 1) * per_page
    rows = conn.execute("""SELECT c.*, COALESCE(SUM(s.total_sale), 0) total_sales,
        COALESCE((SELECT SUM(p.amount) FROM payments p JOIN sales ps ON ps.transaction_id=p.transaction_id
              WHERE ps.customer_id=c.id AND ps.deleted=0 AND p.deleted=0), 0) total_paid
        FROM customers c LEFT JOIN sales s ON s.customer_id=c.id AND s.deleted=0
        WHERE c.active=1 GROUP BY c.id ORDER BY c.name LIMIT ? OFFSET ?""", (per_page, offset)).fetchall()
    blocked_customer_row = None
    if blocked_customer:
        blocked_customer_row = conn.execute("SELECT id,name FROM customers WHERE id=?", (blocked_customer,)).fetchone()
    conn.close()
    return render_template("customers.html", rows=rows, blocked_customer=blocked_customer_row,
                           page=page, page_count=page_count, per_page=per_page,
                           total_customers=total_customers)


@app.route("/customers/<int:customer_id>")
@login_required
@require_permission("view_customers")
def customer_statement(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)
    sales_rows = conn.execute("""SELECT s.*, i.invoice_number, COALESCE((SELECT SUM(p.amount) FROM payments p
        WHERE p.transaction_id=s.transaction_id AND p.deleted=0), 0) paid
        FROM sales s LEFT JOIN invoices i ON i.transaction_id=s.transaction_id AND i.deleted=0
        WHERE s.customer_id=? AND s.deleted=0 ORDER BY s.sale_date DESC, s.id DESC""", (customer_id,)).fetchall()
    conn.close()
    total_sales = sum(float(row["total_sale"]) for row in sales_rows) + float(customer["opening_balance"] or 0)
    total_paid = sum(float(row["paid"]) for row in sales_rows)
    return render_template("customer_statement.html", customer=customer, sales_rows=sales_rows,
                           total_sales=total_sales, total_paid=total_paid,
                           balance=max(total_sales - total_paid, 0))


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("edit_customers")
def edit_customer(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            opening_balance = nonnegative_float("opening_balance")
            if not name:
                raise ValueError("Customer name is required.")
            conn.execute("""UPDATE customers SET name=?,phone=?,address=?,location=?,customer_type=?,
                opening_balance=?,notes=? WHERE id=?""", (
                name, request.form.get("phone", "").strip(), request.form.get("address", "").strip(),
                request.form.get("location", "").strip(), request.form.get("customer_type", "RETAIL"),
                opening_balance, request.form.get("notes", "").strip(), customer_id))
            conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                         (actor_label(), "CUSTOMER EDITED", name,
                          f"customer_id={customer_id}; old_name={customer['name']}; old_phone={customer['phone'] or ''}; new_phone={request.form.get('phone', '').strip()}", now()))
            conn.commit()
            conn.close()
            flash("Customer updated.", "success")
            return redirect(url_for("customers"))
        except (ValueError, sqlite3.Error) as error:
            conn.rollback()
            conn.close()
            flash(str(error) if isinstance(error, ValueError) else "The customer could not be updated.", "danger")
    conn.close()
    return render_template("customer_edit.html", customer=customer)


@app.route("/customers/<int:customer_id>/delete", methods=["POST"])
@login_required
@require_permission("delete_customers")
def delete_customer(customer_id):
    conn = db()
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
        if not customer:
            abort(404)
        reason = request.form.get("reason", "").strip()
        if not reason:
            raise ValueError("A reason for deletion is required.")
        ts = now()
        conn.execute("BEGIN IMMEDIATE")
        sales = conn.execute("SELECT transaction_id,quantity_kg,sale_date FROM sales WHERE customer_id=? AND deleted=0", (customer_id,)).fetchall()
        transaction_ids = [row["transaction_id"] for row in sales]
        for sale in sales:
            stock_svc.apply_reversal(conn, "sales", sale["transaction_id"], float(sale["quantity_kg"]),
                                      sale["sale_date"], session["username"], ts, reason)
        conn.execute("UPDATE customers SET active=0,deleted_at=?,deleted_by=?,deletion_reason=? WHERE id=?",
                     (ts, session["username"], reason, customer_id))
        conn.execute("UPDATE sales SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=? WHERE customer_id=? AND deleted=0",
                     (ts, session["username"], reason, customer_id))
        conn.execute("""UPDATE payments SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=?
                        WHERE transaction_id IN (SELECT transaction_id FROM sales WHERE customer_id=?) AND deleted=0""",
                     (ts, session["username"], reason, customer_id))
        conn.execute("""UPDATE invoices SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=?
                        WHERE transaction_id IN (SELECT transaction_id FROM sales WHERE customer_id=?) AND deleted=0""",
                     (ts, session["username"], reason, customer_id))
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (actor_label(), "CUSTOMER DELETED", customer["name"],
                      f"type=CUSTOMER; customer_id={customer_id}; sales={len(transaction_ids)}; reason={reason}", ts))
        conn.commit()
    except ValueError as error:
        conn.rollback()
        flash(str(error), "danger")
        return redirect(request.referrer or url_for("customers"))
    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Customer deletion failed: customer_id=%s", customer_id)
        flash("Customer could not be deleted. No changes were made.", "danger")
        return redirect(url_for("customers"))
    finally:
        conn.close()
    flash("Customer deleted successfully.", "success")
    return redirect(url_for("customers"))


@app.route("/admin/deleted-records")
@login_required
@admin_required
def deleted_records():
    conn = db()
    customers = conn.execute("SELECT * FROM customers WHERE active=0 ORDER BY deleted_at DESC").fetchall()
    purchases = conn.execute("SELECT * FROM purchases WHERE deleted=1 ORDER BY deleted_at DESC LIMIT 200").fetchall()
    sales = conn.execute("""SELECT s.*,c.name customer_name,COALESCE(s.invoice_number,i.invoice_number) invoice_number
        FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN invoices i ON i.transaction_id=s.transaction_id
        WHERE s.deleted=1 ORDER BY s.deleted_at DESC LIMIT 200""").fetchall()
    payments = conn.execute("""SELECT p.*,s.sales_id,c.name customer_name
        FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id JOIN customers c ON c.id=s.customer_id
        WHERE p.deleted=1 ORDER BY p.deleted_at DESC LIMIT 200""").fetchall()
    invoices = conn.execute("""SELECT i.*,s.sales_id,c.name customer_name
        FROM invoices i JOIN sales s ON s.transaction_id=i.transaction_id JOIN customers c ON c.id=s.customer_id
        WHERE i.deleted=1 ORDER BY i.deleted_at DESC LIMIT 200""").fetchall()
    conn.close()
    return render_template("deleted_records.html", customers=customers, purchases=purchases,
                           sales=sales, payments=payments, invoices=invoices)


@app.route("/admin/customers/<int:customer_id>/restore", methods=["POST"])
@login_required
@admin_required
def restore_customer(customer_id):
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A restoration reason is required.", "danger")
        return redirect(request.referrer or url_for("deleted_records"))
    conn = db()
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=0", (customer_id,)).fetchone()
        if not customer:
            abort(404)
        ts = now()
        deletion_timestamp = customer["deleted_at"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE customers SET active=1,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL WHERE id=?", (customer_id,))
        deleted_sales = conn.execute("SELECT transaction_id,quantity_kg,sale_date FROM sales WHERE customer_id=? AND deleted=1 AND deleted_at=?",
                                     (customer_id, deletion_timestamp)).fetchall()
        for sale in deleted_sales:
            stock_svc.apply_restoration(conn, "sales", sale["transaction_id"], float(sale["quantity_kg"]),
                                        sale["sale_date"], session["username"], ts)
        conn.execute("UPDATE sales SET deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL WHERE customer_id=? AND deleted=1 AND deleted_at=?",
                     (customer_id, deletion_timestamp))
        conn.execute("""UPDATE payments SET deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL
                       WHERE deleted=1 AND deleted_at=? AND transaction_id IN (SELECT transaction_id FROM sales WHERE customer_id=? AND deleted=0)""",
                     (deletion_timestamp, customer_id))
        conn.execute("""UPDATE invoices SET deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL
                       WHERE deleted=1 AND deleted_at=? AND transaction_id IN (SELECT transaction_id FROM sales WHERE customer_id=?)""",
                     (deletion_timestamp, customer_id))
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (actor_label(), "CUSTOMER RESTORED", customer["name"],
                      f"customer_id={customer_id}; reason={reason}", ts))
        conn.commit()
        flash("Customer and related records restored.", "success")
    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Customer restore failed: customer_id=%s", customer_id)
        flash("Customer could not be restored. No changes were made.", "danger")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("deleted_records"))


@app.route("/admin/customers/<int:customer_id>/permanent-delete", methods=["POST"])
@login_required
@admin_required
def permanently_delete_customer(customer_id):
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A permanent deletion reason is required.", "danger")
        return redirect(request.referrer or url_for("deleted_records"))
    conn = db()
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=0", (customer_id,)).fetchone()
        if not customer:
            abort(404)
        transaction_ids = [r["transaction_id"] for r in conn.execute("SELECT transaction_id FROM sales WHERE customer_id=?", (customer_id,)).fetchall()]
        ts = now()
        conn.execute("BEGIN IMMEDIATE")
        if transaction_ids:
            marks = ",".join("?" for _ in transaction_ids)
            conn.execute(f"DELETE FROM payments WHERE transaction_id IN ({marks})", transaction_ids)
            conn.execute(f"DELETE FROM invoices WHERE transaction_id IN ({marks})", transaction_ids)
            conn.execute(f"DELETE FROM stock_movements WHERE reference IN ({marks})", transaction_ids)
            conn.execute(f"DELETE FROM sales WHERE transaction_id IN ({marks})", transaction_ids)
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (actor_label(), "CUSTOMER PERMANENTLY DELETED", customer["name"],
                      f"customer_id={customer_id}; transactions={len(transaction_ids)}; reason={reason}", ts))
        conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
        conn.commit()
        flash("Customer and related records permanently deleted.", "success")
    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Permanent customer deletion failed: customer_id=%s", customer_id)
        flash("Customer could not be permanently deleted. No changes were made.", "danger")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("deleted_records"))


@app.route("/admin/invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
@require_permission("delete_sales")
def delete_invoice(invoice_id):
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A deletion reason is required.", "danger")
        return redirect(request.referrer or url_for("invoice"))
    conn = db()
    try:
        row = conn.execute("SELECT * FROM invoices WHERE id=? AND deleted=0", (invoice_id,)).fetchone()
        if not row:
            abort(404)
        ts = now()
        conn.execute("UPDATE invoices SET deleted=1,deleted_at=?,deleted_by=?,deletion_reason=? WHERE id=?",
                     (ts, session["username"], reason, invoice_id))
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (actor_label(), "INVOICE DELETED", row["invoice_number"], f"invoice_id={invoice_id}; reason={reason}", ts))
        conn.commit()
        flash("Invoice cancelled and retained in the recycle bin.", "success")
    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Invoice deletion failed: invoice_id=%s", invoice_id)
        flash("Invoice could not be deleted. No changes were made.", "danger")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("invoice"))


@app.route("/records/<table>/<int:record_id>/reverse", methods=["POST"])
@login_required
@admin_required
def reverse_record_route(table, record_id):
    if table not in {"purchases", "sales", "payments"}:
        abort(404)
    conn = db()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    reference = row["purchase_id"] if table == "purchases" else row["transaction_id"]
    try:
        delete_record(table, record_id, reference)
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(request.referrer or url_for("dashboard"))
    flash("Transaction reversed and retained in the audit history.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/records/<table>/<int:record_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_record_route(table, record_id):
    if table not in {"purchases", "sales", "payments"}:
        abort(404)
    conn = db()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
    conn.close()
    if not row:
        app.logger.warning("Transaction deletion requested for missing record: table=%s record_id=%s", table, record_id)
        abort(404)
    reference = row["purchase_id"] if table == "purchases" else row["transaction_id"]
    try:
        hard_delete_record(table, record_id, reference)
    except PurchaseStockInUseError as error:
        return redirect(url_for("purchases", blocked_purchase=error.purchase_id))
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(request.referrer or url_for("dashboard"))
    except sqlite3.Error:
        app.logger.exception("Transaction deletion failed: table=%s record_id=%s reference=%s", table, record_id, reference)
        flash("Transaction could not be deleted. No changes were made.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    success_messages = {
        "purchases": "Purchase deleted successfully.",
        "sales": "Sale deleted successfully.",
        "payments": "Payment deleted successfully.",
    }
    flash(success_messages[table], "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/records/<table>/<int:record_id>/restore", methods=["POST"])
@login_required
@admin_required
def restore_record(table, record_id):
    if table not in {"purchases", "sales", "payments"}:
        abort(404)
    conn = db()
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND deleted=1", (record_id,)).fetchone()
        if not row:
            flash("This record is not available for restoration.", "danger")
            return redirect(request.referrer or url_for("dashboard"))
        ts = now()
        by = session["username"]
        reference = row["purchase_id"] if table == "purchases" else row["transaction_id"]
        qty = float(row["quantity_received_kg"]) if table == "purchases" else (-float(row["quantity_kg"]) if False else 0)
        if table == "purchases":
            qty = float(row["quantity_received_kg"])
            movement_date = row["purchase_date"]
        elif table == "sales":
            qty = float(row["quantity_kg"])
            movement_date = row["sale_date"]
        else:
            qty = 0
            movement_date = row["payment_date"]
        stock_svc.apply_restoration(conn, table, reference, qty, movement_date, by, ts)
        conn.execute(f"UPDATE {table} SET deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL WHERE id=?", (record_id,))
        if table == "sales":
            conn.execute("UPDATE payments SET deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL WHERE transaction_id=? AND deleted=1 AND deleted_at=?",
                         (row["transaction_id"], row["deleted_at"]))
            conn.execute("UPDATE invoices SET deleted=0,deleted_at=NULL,deleted_by=NULL,deletion_reason=NULL WHERE transaction_id=? AND deleted=1 AND deleted_at=?",
                         (row["transaction_id"], row["deleted_at"]))
        spec_action = SPEC_RESTORE.get(table, "TRANSACTION RESTORED")
        conn.execute("INSERT INTO audit_log(username,action,reference,details,created_at) VALUES(?,?,?,?,?)",
                     (actor_label(), spec_action, reference, f"type={table}; restored_by={by}", ts))
        conn.commit()
    except ValueError as error:
        try:
            conn.rollback()
        except Exception:
            pass
        flash(str(error), "danger")
        return redirect(request.referrer or url_for("dashboard"))
    except sqlite3.Error:
        try:
            conn.rollback()
        except Exception:
            pass
        flash("Record could not be restored.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    finally:
        conn.close()
    flash("Record restored.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/invoice", methods=["GET","POST"])
@app.route("/invoices", methods=["GET","POST"])
@app.route("/invoice/<transaction_id>", methods=["GET"])
@app.route("/invoice/<transaction_id>/print", methods=["GET"])
@login_required
@require_permission("view_invoices")
def invoice(transaction_id=None):
    if transaction_id is not None:
        tid = str(transaction_id).strip()
    else:
        tid = request.values.get("transaction_id", "").strip()
    info = sale_info(tid) if tid else None
    if info:
        info["invoice_number"] = ensure_invoice_record(info)
    if info and request.path.rstrip("/").endswith("/print"):
        return redirect(url_for("invoice_pdf", transaction_id=info["sales_id"], inline=1))
    payments_list = []
    invoice_matches = []
    invoice_history = []
    if info:
        conn = db()
        payments_list = conn.execute("SELECT * FROM payments WHERE transaction_id=? AND deleted=0 ORDER BY payment_date,id", (info["transaction_id"],)).fetchall()
        conn.close()
    customer_query = request.values.get("customer", "").strip()
    phone_query = request.values.get("phone", "").strip()
    invoice_no = request.values.get("invoice_no", "").strip()
    invoice_date = request.values.get("invoice_date", "").strip()
    status_query = request.values.get("status", "").strip().upper()
    if customer_query or phone_query or invoice_no or invoice_date or status_query:
        conn = db()
        conditions = ["s.deleted=0"]
        params = []
        if customer_query:
            conditions.append("(c.name LIKE ? OR c.phone LIKE ?)")
            params.extend([f"%{customer_query}%", f"%{customer_query}%"])
        if phone_query:
            conditions.append("c.phone LIKE ?")
            params.append(f"%{phone_query}%")
        if invoice_no:
            conditions.append("(UPPER(COALESCE(i.invoice_number, '')) LIKE ? OR UPPER(COALESCE(s.invoice_number, '')) LIKE ? OR UPPER(COALESCE(s.sales_id, '')) LIKE ?)")
            needle = f"%{invoice_no.upper()}%"
            params.extend([needle, needle, needle])
        if invoice_date:
            conditions.append("s.sale_date=?")
            params.append(invoice_date)
        if status_query:
            statuses = []
            if status_query in {"PAID", "PART PAYMENT", "PARTIALLY PAID", "UNPAID", "OVERPAID"}:
                statuses = [status_query, status_query.replace("PART PAYMENT", "PARTIALLY PAID")]
            if not statuses:
                statuses = [status_query]
            clause = " OR ".join(["(CASE WHEN (s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0)) <= 0.005 THEN 'PAID' WHEN COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > s.total_sale + 0.005 THEN 'OVERPAID' WHEN COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > 0 THEN 'PART PAYMENT' ELSE 'UNPAID' END) = ?" for _ in statuses])
            conditions.append(f"({clause})")
            params.extend(statuses)
        invoice_matches = conn.execute(f"""SELECT s.transaction_id,s.sales_id,COALESCE(i.invoice_number,s.invoice_number) invoice_number,s.sale_date,c.name customer_name,c.phone customer_phone,s.total_sale,
            COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) total_paid,
            CASE WHEN COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > s.total_sale + 0.005 THEN 'OVERPAID'
                 WHEN s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) <= 0.005 THEN 'PAID'
                 WHEN COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > 0 THEN 'PART PAYMENT'
                 ELSE 'UNPAID' END status
            FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN invoices i ON i.transaction_id=s.transaction_id AND i.deleted=0
            WHERE {' AND '.join(conditions)} ORDER BY s.sale_date DESC,s.id DESC LIMIT 50""", params).fetchall()
        conn.close()
        if not info and len(invoice_matches) == 1:
            match = invoice_matches[0]
            info = sale_info(match["sales_id"] or match["transaction_id"])
            if info:
                info["invoice_number"] = ensure_invoice_record(info)
                conn = db()
                payments_list = conn.execute("SELECT * FROM payments WHERE transaction_id=? AND deleted=0 ORDER BY payment_date,id", (info["transaction_id"],)).fetchall()
                conn.close()
    conn = db()
    invoice_history = conn.execute("""SELECT s.transaction_id,s.sales_id,COALESCE(i.invoice_number,s.invoice_number) invoice_number,s.sale_date,c.name customer_name,c.phone customer_phone,s.total_sale,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) total_paid,
        CASE WHEN COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > s.total_sale + 0.005 THEN 'OVERPAID'
             WHEN s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) <= 0.005 THEN 'PAID'
             WHEN COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) > 0 THEN 'PART PAYMENT'
             ELSE 'UNPAID' END status
        FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN invoices i ON i.transaction_id=s.transaction_id AND i.deleted=0
        WHERE s.deleted=0 ORDER BY s.sale_date DESC,s.id DESC LIMIT 20""").fetchall()
    conn.close()
    print_view = request.path.rstrip("/").endswith("/print") or request.args.get("print") in {"1", "true", "yes"}
    return render_template("invoice.html", info=info, payments=payments_list,
                           invoice_matches=invoice_matches, invoice_history=invoice_history,
                           customer_query=customer_query, phone_query=phone_query,
                           invoice_no=invoice_no, invoice_date=invoice_date,
                           status_query=status_query, print_view=print_view, company=COMPANY,
                           searching=bool(customer_query or phone_query or invoice_no or invoice_date or status_query))


@app.route("/invoice/<transaction_id>/pdf")
@app.route("/invoices/<transaction_id>/pdf")
@login_required
@require_permission("view_invoices")
def invoice_pdf(transaction_id):
    info = sale_info(transaction_id)
    if not info:
        abort(404)
    info["invoice_number"] = ensure_invoice_record(info)
    conn = db()
    pdf_payments = conn.execute("""SELECT payment_id,payment_date,payment_method,payment_reference,amount
        FROM payments WHERE transaction_id=? AND deleted=0 ORDER BY payment_date,id""",
                               (info["transaction_id"],)).fetchall()
    conn.close()
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
        from reportlab.lib.utils import ImageReader
        from reportlab.lib.units import mm
        from PIL import Image as PILImage
    except ImportError:
        flash("Install reportlab with: pip install reportlab", "danger")
        return redirect(url_for("invoice", transaction_id=transaction_id))

    buf = io.BytesIO()
    logo_path = BASE_DIR / "static" / "images" / "branding" / "adufarms-logo.jpg"
    watermark_buffer = io.BytesIO()
    if logo_path.exists():
        try:
            with PILImage.open(logo_path) as source_logo:
                watermark_logo = source_logo.convert("RGBA")
                watermark_logo.thumbnail((480, 380), PILImage.Resampling.LANCZOS)
                watermark_logo.putalpha(watermark_logo.getchannel("A").point(lambda value: int(value * 0.12)))
                watermark_logo.save(watermark_buffer, format="PNG", optimize=True, compress_level=9)
            watermark_buffer.seek(0)
        except Exception:
            app.logger.exception("Invoice logo unreadable, continuing without watermark")
            watermark_buffer = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=10*mm,leftMargin=10*mm,topMargin=8*mm,bottomMargin=8*mm)
    def draw_watermark(canvas, document):
        if not watermark_buffer.getbuffer().nbytes:
            return
        canvas.saveState()
        canvas.drawImage(ImageReader(watermark_buffer), (A4[0] - 120*mm) / 2, (A4[1] - 92*mm) / 2,
                         width=120*mm, height=92*mm, preserveAspectRatio=True,
                         anchor='c', mask='auto')
        canvas.restoreState()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SmallRight", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=9))
    styles.add(ParagraphStyle(name="Center", parent=styles["Normal"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Muted", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#52665b")))
    story = []
    brand = [Paragraph(f"<b>{COMPANY['legal_name']}</b>", styles["Title"]),
             Paragraph(COMPANY["service_line"].upper(), styles["Heading3"]),
             Paragraph(COMPANY["document_note"], styles["Muted"])]
    header = Table([[Image(str(logo_path), width=30*mm, height=22*mm, kind="proportional") if logo_path.exists() else "", brand,
                    [Paragraph("<b>INVOICE</b>", styles["SmallRight"]), Paragraph(info["invoice_number"], styles["SmallRight"]), Paragraph(pretty_date(info["sale_date"]), styles["SmallRight"]), Paragraph(f"<b>{info['status']}</b>", styles["SmallRight"])] ]], colWidths=[32*mm,82*mm,64*mm])
    header.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LINEBELOW",(0,0),(-1,-1),1,colors.HexColor("#c99b2f")),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),4)]))
    story.append(header)
    story.append(Spacer(1,6))
    meta = [["BILLED TO", info["customer_name"], "TRANSACTION DETAILS", ""],
            ["Phone", info["customer_phone"] or "-", "Sales ID", info["sales_id"]],
            ["Address", info["customer_address"] or info["customer_location"] or "-", "Invoice No.", info["invoice_number"]],
            ["", "", "Issued by", info.get("staff_user") or "-"],
            ["", "", "Invoice / due date", pretty_date(info["sale_date"])]]
    t = Table(meta, colWidths=[28*mm,70*mm,32*mm,48*mm])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.HexColor("#dce6df")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e9f2eb")),("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#173c2b")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold")]))
    story += [t, Spacer(1,8)]
    data = [["Description","Qty","Unit","Unit Price (GHS)","Total (GHS)"],
            [Paragraph("<b>Maize supply</b><br/><font color='#52665b'>Fresh maize distribution</font>", styles["Normal"]),
             f'{info["quantity_kg"]:,.2f}', "KG", money(info["selling_price_kg"]), money(info["total_sale"])]]
    t2=Table(data,colWidths=[68*mm,20*mm,17*mm,39*mm,42*mm])
    t2.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#dce6df")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#173c2b")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                    ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("ALIGN",(1,1),(-1,-1),"RIGHT"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                    ("FONTSIZE",(0,0),(-1,0),7.5),("FONTSIZE",(0,1),(-1,-1),8)]))
    payment_cell = ParagraphStyle(name="PaymentCell", parent=styles["Normal"], fontSize=7.2, leading=8.4, spaceAfter=0)
    payment_header = ParagraphStyle(name="PaymentHeader", parent=payment_cell, fontName="Helvetica-Bold", textColor=colors.white)
    payment_data = [[Paragraph(label, payment_header) for label in ["Payment ID", "Date", "Method", "Reference", "Amount (GHS)"]]]
    payment_data.extend([[Paragraph(str(payment["payment_id"] or "-"), payment_cell),
                          Paragraph(pretty_date(payment["payment_date"]), payment_cell),
                          Paragraph(str(payment["payment_method"] or "-"), payment_cell),
                          Paragraph(str(payment["payment_reference"] or "-"), payment_cell),
                          Paragraph(money(payment["amount"]), payment_cell)] for payment in pdf_payments])
    if len(payment_data) == 1:
        payment_data.append([Paragraph("No payment recorded", payment_cell), Paragraph("-", payment_cell), Paragraph("-", payment_cell), Paragraph("-", payment_cell), Paragraph(money(0), payment_cell)])
    payment_table = Table(payment_data, colWidths=[32*mm,30*mm,34*mm,50*mm,28*mm])
    payment_table.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),
                                       ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#173c2b")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                                       ("ALIGN",(4,1),(4,-1),"RIGHT")]))
    delivery_text = f"<b>Delivery status:</b> Completed<br/>"
    if info.get("customer_location"):
        delivery_text += f"<b>Delivery location:</b> {info['customer_location']}<br/>"
    delivery_text += "Payment is due on the invoice date unless otherwise agreed."
    delivery = Table([[Paragraph("<b>DELIVERY INFORMATION</b>", styles["Muted"]), Paragraph(delivery_text, styles["Normal"])]], colWidths=[42*mm,144*mm])
    delivery.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f7faf7")),("BOX",(0,0),(-1,-1),0.4,colors.HexColor("#dce6df")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8)]))
    story += [t2, Spacer(1,6), delivery, Spacer(1,6), Paragraph("<b>PAYMENT HISTORY</b>", styles["Heading3"]), payment_table, Spacer(1,8)]
    totals=[["Subtotal",money(info["total_sale"])],
            ["Delivery fee","GHS 0.00"],
            ["Discount","GHS 0.00"],
            ["Grand total",money(info["total_sale"])],
            ["Amount paid",money(info["total_paid"])],
            ["Outstanding balance",money(info["balance"])],
            ["Payment status",info["status"]]]
    t3=Table(totals,colWidths=[120*mm,60*mm],hAlign="RIGHT")
    t3.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),("ALIGN",(1,0),(1,-1),"RIGHT"),
                            ("BACKGROUND",(0,3),(1,3),colors.HexColor("#e9f2eb")),
                            ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#f3f8f4"))]))
    payment_lines = [Paragraph(f"<b>MOBILE MONEY</b>: {COMPANY['momo_number']} · {COMPANY['momo_name']}", styles["Normal"]),
                     Paragraph(f"<b>BANK</b>: {COMPANY['bank_account']} · {COMPANY['bank_account_name']} · {COMPANY['bank_name']} ({COMPANY['bank_branch']})", styles["Normal"]),
                     Spacer(1, 6), Paragraph("Authorized signature: ____________________________", styles["Normal"]),
                     Paragraph("Payment is due on the invoice date unless otherwise agreed in writing.", styles["Muted"])]
    story += [t3, Spacer(1,6)] + payment_lines + [Spacer(1,8), Paragraph(f"Thank you for doing business with {COMPANY['legal_name']}. {COMPANY['tagline']}", styles["Center"])]
    doc.build(story, onFirstPage=draw_watermark, onLaterPages=draw_watermark); buf.seek(0)
    inline = request.args.get("inline", "0").lower() in {"1", "true", "yes"}
    return send_file(buf, as_attachment=not inline, download_name=f"{info['invoice_number']}.pdf", mimetype="application/pdf")


@app.route("/search")
@login_required
@require_any_permission("view_customers", "view_sales", "view_purchases", "view_payments")
def search():
    q = request.args.get("q","").strip()
    results=[]
    if q:
        conn=db()
        results=conn.execute(f"""SELECT s.transaction_id,s.sales_id,i.invoice_number,s.sale_date,c.name customer_name,c.phone,
            s.quantity_kg,s.selling_price_kg,s.total_sale,
            COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
            FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN invoices i ON i.transaction_id=s.transaction_id AND i.deleted=0
            WHERE ({visible_sql('s')}) AND (s.sales_id LIKE ? OR s.transaction_id LIKE ? OR i.invoice_number LIKE ?
                OR c.name LIKE ? OR c.phone LIKE ? OR s.sale_date LIKE ?
                OR EXISTS (SELECT 1 FROM payments sp WHERE sp.transaction_id=s.transaction_id AND sp.payment_id LIKE ?))
            ORDER BY s.id DESC""", tuple(f"%{q}%" for _ in range(7))).fetchall()
        purchases=conn.execute(f"""SELECT p.* FROM purchases p WHERE ({visible_sql('p')}) AND (purchase_id LIKE ? OR local_agent LIKE ? OR location LIKE ? OR agent_phone LIKE ? OR purchase_date LIKE ?)
            ORDER BY id DESC""",tuple(f"%{q}%" for _ in range(5))).fetchall()
        payments=conn.execute(f"""SELECT p.*,s.sales_id,c.name customer_name FROM payments p
            JOIN sales s ON s.transaction_id=p.transaction_id JOIN customers c ON c.id=s.customer_id
            WHERE ({visible_sql('p')}) AND ({visible_sql('s')})
            AND (p.payment_id LIKE ? OR CAST(p.id AS TEXT) LIKE ? OR p.transaction_id LIKE ? OR p.payment_reference LIKE ? OR s.sales_id LIKE ? OR p.payment_date LIKE ?)
            ORDER BY p.id DESC""", tuple(f"%{q}%" for _ in range(6))).fetchall()
        conn.close()
    else:
        purchases=[]
        payments=[]
    return render_template("search.html", q=q, results=results, purchases=purchases, payments=payments)


@app.route("/reports")
@login_required
@require_permission("view_reports")
def reports():
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    params = []
    date_filter = ""
    if start:
        date_filter += " AND sale_date >= ?"
        params.append(start)
    if end:
        date_filter += " AND sale_date <= ?"
        params.append(end)
    if start and end and start > end:
        flash("The report start date must be before the end date.", "danger")
        return redirect(url_for("reports"))
    purchase_params = []
    purchase_filter = ""
    payment_params = []
    payment_filter = ""
    if start:
        purchase_filter += " AND purchase_date >= ?"; purchase_params.append(start)
        payment_filter += " AND payment_date >= ?"; payment_params.append(start)
    if end:
        purchase_filter += " AND purchase_date <= ?"; purchase_params.append(end)
        payment_filter += " AND payment_date <= ?"; payment_params.append(end)
    conn = db()
    sales_rows = conn.execute(f"""SELECT s.transaction_id, s.sales_id, s.sale_date, c.name customer_name,
        s.quantity_kg, s.total_sale, COALESCE(SUM(p.amount), 0) paid
        FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN payments p
        ON p.transaction_id=s.transaction_id AND p.deleted=0
        WHERE s.deleted=0 {date_filter.replace('sale_date', 's.sale_date')}
        GROUP BY s.id ORDER BY s.sale_date DESC, s.id DESC""", params).fetchall()
    purchase_rows = conn.execute(f"""SELECT purchase_id, purchase_date, local_agent,
        quantity_received_kg, total_cost FROM purchases WHERE deleted=0 {purchase_filter}
        ORDER BY purchase_date DESC, id DESC""", purchase_params).fetchall()
    payment_rows = conn.execute(f"""SELECT p.payment_id,p.transaction_id, s.sales_id, p.payment_date, p.amount,
        p.payment_method, c.name customer_name FROM payments p
        JOIN sales s ON s.transaction_id=p.transaction_id JOIN customers c ON c.id=s.customer_id
        WHERE p.deleted=0 AND s.deleted=0 {payment_filter.replace('payment_date','p.payment_date')}
        ORDER BY p.payment_date DESC, p.id DESC""", payment_params).fetchall()
    payment_total = conn.execute(f"SELECT COALESCE(SUM(amount),0) v FROM payments WHERE deleted=0 {payment_filter}", payment_params).fetchone()["v"]
    balance_rows = conn.execute(f"""SELECT s.transaction_id, s.sales_id, s.sale_date, c.name customer_name, c.phone,
        s.total_sale, COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid,
        s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) balance
        FROM sales s JOIN customers c ON c.id=s.customer_id
        WHERE s.deleted=0 {date_filter.replace('sale_date','s.sale_date')} ORDER BY s.sale_date DESC""", params).fetchall()
    purchased = conn.execute(f"SELECT COALESCE(SUM(quantity_received_kg),0) v FROM purchases WHERE deleted=0 {purchase_filter}", purchase_params).fetchone()["v"]
    sold = conn.execute(f"SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0 {date_filter.replace('sale_date','sale_date')}", params).fetchone()["v"]
    stock_kg = float(purchased) - float(sold)
    sales_total = sum(float(r["total_sale"]) for r in sales_rows)
    purchase_cost_total = sum(float(r["total_cost"]) for r in purchase_rows)
    expenses_total = conn.execute(f"SELECT COALESCE(SUM(total_purchase_cost),0)+COALESCE(SUM(transport_cost),0)+COALESCE(SUM(other_expenses),0) v FROM purchases WHERE deleted=0 {purchase_filter}", purchase_params).fetchone()["v"]
    period_received = float(purchased or 0)
    period_unit_cost = float(purchase_cost_total) / period_received if period_received > 0 else 0
    period_cogs = float(sold or 0) * period_unit_cost
    profit_est = float(sales_total) - period_cogs
    customer_count = conn.execute("SELECT COUNT(DISTINCT customer_id) v FROM sales WHERE deleted=0" + date_filter.replace("sale_date", "sale_date"), params).fetchone()["v"]
    counts = dict(sales=len(sales_rows), purchases=len(purchase_rows),
                  payments=len(payment_rows), customers=int(customer_count))
    conn.close()
    summary = dict(stock_kg=stock_kg, sales_total=sales_total, purchase_cost_total=purchase_cost_total,
                   payment_total=float(payment_total), expenses_total=float(expenses_total),
                   profit_est=profit_est, counts=counts)
    if request.args.get("format") == "csv":
        export_type = request.args.get("type", "sales")
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if export_type == "purchases":
            writer.writerow(["Purchase ID", "Date", "Supplier", "Quantity KG", "Total Cost"])
            for row in purchase_rows:
                writer.writerow([row["purchase_id"], row["purchase_date"], row["local_agent"],
                                 row["quantity_received_kg"], row["total_cost"]])
            filename = "adufarms-purchases-report.csv"
        elif export_type == "payments":
            writer.writerow(["Payment ID", "Sales ID", "Date", "Customer", "Amount", "Method"])
            for row in payment_rows:
                writer.writerow([row["payment_id"], row["sales_id"], row["payment_date"], row["customer_name"],
                                 row["amount"], row["payment_method"]])
            filename = "adufarms-payments-report.csv"
        elif export_type == "balances":
            writer.writerow(["Sales ID", "Date", "Customer", "Total Sale", "Paid", "Balance"])
            for row in balance_rows:
                writer.writerow([row["sales_id"], row["sale_date"], row["customer_name"],
                                 row["total_sale"], row["paid"], row["balance"]])
            filename = "adufarms-balances-report.csv"
        else:
            writer.writerow(["Sales ID", "Date", "Customer", "Quantity KG", "Total Sale", "Paid", "Balance"])
            for row in sales_rows:
                writer.writerow([row["sales_id"], row["sale_date"], row["customer_name"],
                                 row["quantity_kg"], row["total_sale"], row["paid"],
                                 float(row["total_sale"]) - float(row["paid"])])
            filename = "adufarms-sales-report.csv"
        return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), as_attachment=True,
                         download_name=filename, mimetype="text/csv")
    return render_template("reports.html", sales_rows=sales_rows, purchase_rows=purchase_rows,
                           payment_rows=payment_rows, balance_rows=balance_rows,
                           payment_total=payment_total, summary=summary, start=start, end=end)


@app.route("/search/transaction/<transaction_id>")
@login_required
@require_permission("view_sales")
def transaction_history(transaction_id):
    info=sale_info(transaction_id)
    if not info: abort(404)
    conn=db()
    pay=conn.execute("SELECT * FROM payments WHERE transaction_id=? AND deleted=0 ORDER BY payment_date,id",(info["transaction_id"],)).fetchall()
    conn.close()
    return render_template("history.html", info=info, payments=pay)


@app.route("/profile", methods=["GET", "POST"])
@login_required
@require_permission("view_profiles")
def profile():
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            current = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            policy_error = validate_password(new_password)
            current_password_valid = check_password_hash(user["password_hash"], current)
            if not current_password_valid or policy_error:
                if not current_password_valid:
                    flash("Current password is incorrect.", "danger")
                else:
                    flash(policy_error or "The new password does not meet the password policy.", "danger")
            else:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user["id"]))
                conn.commit()
                log_action("PASSWORD CHANGED", user["username"], f"user_id={user['id']}")
                flash("Password changed successfully.", "success")
        elif action == "image":
            image = request.files.get("profile_image")
            if image and image.filename:
                extension = Path(image.filename).suffix.lower()
                if extension not in ALLOWED_PROFILE_EXTS or image.mimetype not in ALLOWED_PROFILE_MIMES or not is_valid_image_bytes(image.stream):
                    flash("Profile image must be a genuine JPG, PNG, or WEBP file.", "danger")
                    conn.close()
                    return render_template("profile.html", user=user)
                if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
                    flash("Profile image must be 2 MB or smaller.", "danger")
                    conn.close()
                    return render_template("profile.html", user=user)
                old_filename = user["profile_image"]
                filename = secure_filename(f"user-{user['id']}-{secrets.token_hex(8)}{extension}")
                image.save(PROFILE_DIR / filename)
                conn.execute("UPDATE users SET profile_image=? WHERE id=?", (filename, user["id"]))
                conn.commit()
                session["profile_image"] = filename
                if old_filename and old_filename != filename:
                    old_path = PROFILE_DIR / old_filename
                    if old_path.exists():
                        old_path.unlink()
                log_action("PROFILE UPDATED", user["username"], f"user_id={user['id']}; profile_image updated")
                flash("Profile image updated.", "success")
        elif action == "remove_image":
            old_filename = user["profile_image"]
            conn.execute("UPDATE users SET profile_image=NULL WHERE id=?", (user["id"],))
            conn.commit()
            session.pop("profile_image", None)
            if old_filename:
                old_path = PROFILE_DIR / old_filename
                if old_path.exists():
                    old_path.unlink()
            log_action("PROFILE UPDATED", user["username"], f"user_id={user['id']}; profile_image removed")
            flash("Profile image removed.", "success")
        user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return render_template("profile.html", user=user)


@app.route("/users", methods=["GET","POST"])
@app.route("/admin/users", methods=["GET","POST"])
@login_required
@require_permission("view_users")
def users():
    if request.method=="POST":
        username=request.form.get("username", "").strip()
        full_name=request.form.get("full_name", "").strip()
        password=request.form.get("password", "")
        role=request.form.get("role","STAFF")
        conn = None
        try:
            normalized_role = valid_role_name(role)
            policy_error = validate_password(password)
            if not username or not full_name:
                raise ValueError("Username and full name are required.")
            if policy_error:
                raise ValueError(policy_error)
            conn=db()
            conn.execute("INSERT INTO users(username,full_name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                         (username,full_name,generate_password_hash(password),normalized_role,now()))
            conn.commit()
            log_action("USER CREATED", username, f"role={normalized_role}; created_by={actor_label()}")
            log_action("ADMIN ACTION", username, f"USER CREATED role={normalized_role}")
            flash("User created.", "success")
        except ValueError as error:
            flash(str(error), "danger")
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")
        except sqlite3.OperationalError:
            flash("The user could not be saved because the database is busy. Please try again.", "danger")
        finally:
            if conn is not None:
                conn.close()
    conn=db()
    rows=conn.execute("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY id").fetchall()
    conn.close()
    return render_template("users.html", rows=rows, role_permissions=ROLE_PERMISSIONS, role_labels=ROLE_LABELS)


@app.route("/roles")
@login_required
@require_permission("view_roles")
def roles():
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("edit_users")
def edit_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if request.method == "POST":
        username = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "STAFF")
        try:
            normalized_role = valid_role_name(role)
            if not username or not full_name:
                raise ValueError("Enter valid user details.")
            if password:
                policy_error = validate_password(password)
                if policy_error:
                    raise ValueError(policy_error)
            if password:
                conn.execute("UPDATE users SET username=?,full_name=?,password_hash=?,role=? WHERE id=?",
                             (username, full_name, generate_password_hash(password), normalized_role, user_id))
                conn.commit()
                log_action("PASSWORD CHANGED", username, f"user_id={user_id}; changed_by={actor_label()}")
            else:
                conn.execute("UPDATE users SET username=?,full_name=?,role=? WHERE id=?",
                             (username, full_name, normalized_role, user_id))
                conn.commit()
            log_action("USER UPDATED", username, f"user_id={user_id}; role={normalized_role}")
            log_action("ADMIN ACTION", username, f"USER UPDATED user_id={user_id}")
            flash("User updated.", "success")
            conn.close()
            return redirect(url_for("users"))
        except ValueError as error:
            flash(str(error), "danger")
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")
    conn.close()
    return render_template("edit_record.html", kind="user", record=row)


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@require_permission("deactivate_users")
def toggle_user(user_id):
    conn=db()
    target = conn.execute("SELECT id, username, role, active FROM users WHERE id=?", (user_id,)).fetchone()
    if target and target["username"] == "admin":
        conn.close()
        flash("The primary admin account cannot be disabled.", "danger")
        return redirect(url_for("users"))
    if user_id == session.get("user_id"):
        conn.close()
        flash("You cannot disable your own account.", "danger")
        return redirect(url_for("users"))
    if target and (target["role"] or "").upper() == "ADMIN" and target["active"]:
        remaining = conn.execute("SELECT COUNT(*) AS c FROM users WHERE upper(role)='ADMIN' AND active=1 AND id!=?", (user_id,)).fetchone()
        if (remaining["c"] if remaining else 0) < 1:
            conn.close()
            flash("Cannot disable the last active administrator.", "danger")
            return redirect(url_for("users"))
    conn.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND username!='admin'",(user_id,))
    conn.commit()
    updated = conn.execute("SELECT username, active FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if updated:
        state = "USER ENABLED" if updated["active"] else "USER DISABLED"
        log_action(state, updated["username"], f"user_id={user_id}; by={actor_label()}")
        log_action("ADMIN ACTION", updated["username"], f"{state} user_id={user_id}")
    return redirect(url_for("users"))


@app.route("/admin/gallery")
@login_required
@admin_required
def admin_gallery():
    return redirect(url_for("dashboard") + "#dashboard-imagery")


@app.route("/admin/audit-logs")
@login_required
@admin_required
def audit_logs():
    query = request.args.get("q", "").strip()
    action = request.args.get("action", "").strip().upper()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    conn = db()
    clauses, params = [], []
    if query:
        clauses.append("(username LIKE ? OR action LIKE ? OR reference LIKE ? OR details LIKE ?)")
        params.extend([f"%{query}%"] * 4)
    if action:
        clauses.append("upper(action) LIKE ?")
        params.append(f"%{action}%")
    if start:
        clauses.append("created_at >= ?")
        params.append(start)
    if end:
        try:
            end_exclusive = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            end_exclusive = end
        clauses.append("created_at < ?")
        params.append(end_exclusive)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT 1000", params
    ).fetchall()
    reversals = conn.execute("SELECT * FROM reversals ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    if request.args.get("format") == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["Timestamp", "User", "Action", "Reference", "Details"])
        for row in rows:
            writer.writerow([row["created_at"], row["username"], row["action"], row["reference"], row["details"]])
        return send_file(
            io.BytesIO(buffer.getvalue().encode("utf-8-sig")),
            as_attachment=True,
            download_name=f"adufarms-audit-log-{date.today().isoformat()}.csv",
            mimetype="text/csv",
        )
    return render_template("audit_logs.html", rows=rows, reversals=reversals,
                           query=query, action=action, start=start, end=end)


@app.route("/admin/audit-logs/delete", methods=["POST"])
@login_required
@admin_required
def delete_audit_logs():
    log_ids = request.form.getlist("log_ids")
    single_id = request.form.get("log_id")
    if single_id and single_id not in log_ids:
        log_ids.append(single_id)
    delete_all = request.form.get("delete_all") == "1"
    valid_ids = []
    for lid in log_ids:
        try:
            valid_ids.append(int(lid))
        except (ValueError, TypeError):
            pass
    conn = db()
    try:
        count_deleted = 0
        if delete_all:
            cur = conn.execute("DELETE FROM audit_log")
            count_deleted = cur.rowcount
            conn.commit()
            log_action("AUDIT LOG CLEARED", "ALL", f"cleared_by={actor_label()}; total_deleted={count_deleted}")
            flash(f"All audit logs ({count_deleted} entries) cleared successfully.", "success")
        elif valid_ids:
            placeholders = ",".join("?" for _ in valid_ids)
            cur = conn.execute(f"DELETE FROM audit_log WHERE id IN ({placeholders})", valid_ids)
            count_deleted = cur.rowcount
            conn.commit()
            log_action("AUDIT LOG DELETED", f"{count_deleted} logs", f"total_deleted={count_deleted}; by={actor_label()}")
            flash(f"Successfully deleted {count_deleted} audit log entry(ies).", "success")
        else:
            flash("No audit log entries were selected for deletion.", "warning")
    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Audit log deletion failed")
        flash("Audit log deletion failed.", "danger")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("audit_logs"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@require_permission("deactivate_users")
def delete_user(user_id):
    conn = db()
    target = conn.execute("SELECT id, username, role, active FROM users WHERE id=?", (user_id,)).fetchone()
    if target and target["username"] == "admin":
        conn.close()
        flash("The primary admin account cannot be deleted.", "danger")
        return redirect(url_for("users"))
    if user_id == session.get("user_id"):
        conn.close()
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("users"))
    if target and (target["role"] or "").upper() == "ADMIN" and target["active"]:
        remaining = conn.execute("SELECT COUNT(*) AS c FROM users WHERE upper(role)='ADMIN' AND active=1 AND id!=?", (user_id,)).fetchone()
        if (remaining["c"] if remaining else 0) < 1:
            conn.close()
            flash("Cannot delete the last active administrator.", "danger")
            return redirect(url_for("users"))
    conn.execute("DELETE FROM users WHERE id=? AND username!='admin'", (user_id,))
    conn.commit()
    conn.close()
    if target:
        log_action("ADMIN ACTION", target["username"], f"USER DELETED user_id={user_id} by={actor_label()}")
    flash("User permanently deleted.", "success")
    return redirect(url_for("users"))


@app.route("/admin/backup", methods=["GET", "POST"])
@login_required
@admin_required
def admin_backup():
    if request.method == "POST":
        try:
            dst = backup_svc.create_backup(app.config["DATABASE"])
            log_action("DATABASE BACKUP", dst.name, f"created_by={actor_label()}; size={dst.stat().st_size}")
            log_action("ADMIN ACTION", dst.name, "DATABASE BACKUP")
            flash(f"Backup {dst.name} created.", "success")
        except Exception:
            app.logger.exception("Backup failed")
            flash("Backup could not be created.", "danger")
        return redirect(url_for("admin_backup"))
    backups = backup_svc.list_backups()
    items = [{"name": p.name, "size": p.stat().st_size,
              "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")} for p in backups]
    return render_template("backup.html", backups=items)


@app.route("/admin/restore", methods=["POST"])
@login_required
@admin_required
def admin_restore():
    name = request.form.get("backup_name", "").strip()
    if not name:
        flash("Select a backup to restore.", "danger")
        return redirect(url_for("admin_backup"))
    try:
        pre = backup_svc.safe_restore(app.config["DATABASE"], name)
        log_action("DATABASE RESTORE", name, f"restored_by={actor_label()}; pre_restore_backup={pre.name}")
        log_action("ADMIN ACTION", name, "DATABASE RESTORE")
        flash(f"Database restored from {name}. Safety backup {pre.name} retained.", "success")
    except ValueError as error:
        flash(str(error), "danger")
    except Exception:
        app.logger.exception("Restore failed")
        flash("Restore failed. No changes were applied beyond the safety backup.", "danger")
    return redirect(url_for("admin_backup"))


@app.route("/stock")
@login_required
@require_permission("view_inventory")
def stock():
    purchased, sold, available = stock_summary()
    conn = db()
    movements = conn.execute(
        "SELECT * FROM stock_movements ORDER BY id DESC LIMIT 1000").fetchall()
    low = available <= 100
    depleted = available <= 0
    conn.close()
    if request.args.get("format") == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["Date", "Movement Type", "Reference", "Quantity KG", "Recorded By", "Notes"])
        for movement in movements:
            writer.writerow([movement["movement_date"], movement["movement_type"], movement["reference"],
                             movement["quantity_kg"], movement["created_by"], movement["notes"]])
        return send_file(
            io.BytesIO(buffer.getvalue().encode("utf-8-sig")),
            as_attachment=True,
            download_name=f"adufarms-stock-movements-{date.today().isoformat()}.csv",
            mimetype="text/csv",
        )
    return render_template("stock.html", purchased=purchased, sold=sold,
                           available=available, movements=movements,
                           low=low, depleted=depleted)


@app.route("/notifications")
@login_required
def notifications():
    conn = db()
    purchased = conn.execute(
        "SELECT COALESCE(SUM(quantity_received_kg),0) v FROM purchases WHERE deleted=0").fetchone()["v"]
    sold = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0").fetchone()["v"]
    stock = float(purchased) - float(sold)
    unpaid_rows = conn.execute("""SELECT s.transaction_id, c.name customer_name,
        s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) balance
        FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.deleted=0
        AND COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) < s.total_sale
        ORDER BY s.id DESC LIMIT 20""").fetchall()
    recent_audit = conn.execute(
        "SELECT username, action, reference, created_at FROM audit_log ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()
    items = []
    if stock <= 0:
        items.append({"kind": "danger", "icon": "box-seam", "title": "Stock depleted",
                      "body": "No available maize stock remains.", "time": now()})
    elif stock <= 100:
        items.append({"kind": "warning", "icon": "exclamation-triangle", "title": "Low stock",
                      "body": f"Only {stock:,.2f} KG remains.", "time": now()})
    for r in unpaid_rows:
        items.append({"kind": "warning", "icon": "hourglass-split",
                      "title": f"Outstanding balance — {r['customer_name']}",
                      "body": f"{r['transaction_id']} owes {money(r['balance'])}.", "time": now()})
    for r in recent_audit:
        items.append({"kind": "info", "icon": "activity",
                      "title": f"{r['action'].title()} {r['reference'] or ''}".strip(),
                      "body": f"by {r['username']} · {r['created_at']}", "time": r["created_at"]})
    if request.args.get("mark_read") == "1":
        session["notifications_read_at"] = now()
        return redirect(url_for("notifications"))
    return render_template("notifications.html", items=items, stock=stock)


@app.route("/assistant", methods=["GET", "POST"])
@login_required
def assistant():
    question = request.values.get("question", "").strip()
    result = None
    if question:
        conn = db()
        try:
            result = assistant_service.answer_question(conn, question)
        finally:
            conn.close()
    return render_template("assistant.html", question=question, result=result)


@app.route("/health")
@app.route("/ready")
def health():
    try:
        conn = db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return jsonify({
            "status": "ok",
            "ready": True,
            "application": "ADUFARMS",
            "environment": environment,
            "database": str(DB_PATH),
            "time": now(),
        })
    except Exception as exc:
        app.logger.exception("Health check database failure")
        return jsonify({
            "status": "error",
            "ready": False,
            "application": "ADUFARMS",
            "environment": environment,
            "database": str(DB_PATH),
            "error": str(exc),
            "time": now(),
        }), 503


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You do not have permission to perform this action."), 403


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "The request could not be processed.")), 400


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="The requested page could not be found."), 404


@app.errorhandler(405)
def method_not_allowed(error):
    return render_template("error.html", code=405, message="This action is not allowed with the current method."), 405


@app.errorhandler(500)
def server_error(error):
    app.logger.exception("500 Internal Server Error: %s", error)
    return render_template("error.html", code=500, message="Something went wrong while processing your request."), 500


if __name__ == "__main__":
    init_db()
    app.run(debug=os.environ.get("ADUFARMS_DEBUG", "0") == "1", use_reloader=False,
            host=os.environ.get("ADUFARMS_HOST", "127.0.0.1"),
            port=int(os.environ.get("ADUFARMS_PORT", "5000")))

