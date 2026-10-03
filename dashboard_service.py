"""Data layer for the executive dashboard.

Everything here is computed from the live database - no placeholder figures. The single entry point is
``build_dashboard``; it returns plain dicts/lists so the template (and tests) stay simple.
"""
from datetime import date, datetime, timedelta

from analytics_core import LOW_DATE, bounds as _bounds, bucket_keys as _bucket_keys, delta as _delta  # noqa: F401

RANGES = [
    ("today", "Today"),
    ("7d", "7 days"),
    ("30d", "30 days"),
    ("mtd", "This month"),
    ("90d", "90 days"),
    ("all", "All time"),
]
DEFAULT_RANGE = "30d"
AGING_BUCKETS = [("0-7 days", 0, 7), ("8-30 days", 8, 30), ("31-60 days", 31, 60), ("Over 60 days", 61, None)]


def resolve_period(key, today):
    """Return (key, start, end, previous) where previous is (start, end) or None for all-time."""
    if key not in dict(RANGES):
        key = DEFAULT_RANGE
    if key == "today":
        start = end = today
    elif key == "mtd":
        start, end = today.replace(day=1), today
    elif key == "all":
        return key, None, today, None
    else:
        days = {"7d": 7, "30d": 30, "90d": 90}[key]
        start, end = today - timedelta(days=days - 1), today
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return key, start, end, (prev_end - timedelta(days=length - 1), prev_end)


def _totals(conn, start, end):
    lo, hi = _bounds(start, end)
    sales = conn.execute(
        """SELECT COALESCE(SUM(total_sale),0) revenue, COUNT(*) n, COALESCE(SUM(quantity_kg),0) kg
           FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?""", (lo, hi)).fetchone()
    collected = conn.execute(
        "SELECT COALESCE(SUM(amount),0) v FROM payments WHERE deleted=0 AND payment_date BETWEEN ? AND ?", (lo, hi)).fetchone()["v"]
    spend = conn.execute(
        "SELECT COALESCE(SUM(total_cost),0) v FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ?", (lo, hi)).fetchone()["v"]
    return {"revenue": float(sales["revenue"]), "count": int(sales["n"]), "kg": float(sales["kg"]),
            "collected": float(collected), "spend": float(spend)}


def _chart_window(key, today, conn):
    """Pick the time window + granularity for the trend chart."""
    if key in ("today", "7d"):
        return today - timedelta(days=6), "day"
    if key == "30d":
        return today - timedelta(days=29), "day"
    if key == "mtd":
        return today.replace(day=1), "day"
    if key == "90d":
        return today - timedelta(days=89), "week"
    first = conn.execute(
        """SELECT MIN(d) d FROM (SELECT MIN(sale_date) d FROM sales WHERE deleted=0
           UNION ALL SELECT MIN(purchase_date) FROM purchases WHERE deleted=0
           UNION ALL SELECT MIN(payment_date) FROM payments WHERE deleted=0)""").fetchone()["d"]
    floor = (today.replace(day=1) - timedelta(days=330)).replace(day=1)
    try:
        start = datetime.strptime(first[:7] + "-01", "%Y-%m-%d").date() if first else today.replace(day=1)
    except ValueError:
        start = today.replace(day=1)
    return max(start, floor), "month"


def _series(conn, start, end, gran):
    expr = {"day": "{c}", "week": "date({c}, 'weekday 0', '-6 days')", "month": "substr({c},1,7)"}[gran]
    lo, hi = start.isoformat(), end.isoformat()

    def grouped(table, col, value, flag="deleted=0"):
        sql = f"SELECT {expr.format(c=col)} k, COALESCE(SUM({value}),0) v FROM {table} WHERE {flag} AND {col} BETWEEN ? AND ? GROUP BY k"
        return {row["k"]: float(row["v"]) for row in conn.execute(sql, (lo, hi)).fetchall()}

    revenue = grouped("sales", "sale_date", "total_sale")
    collected = grouped("payments", "payment_date", "amount")
    purchases = grouped("purchases", "purchase_date", "total_cost")
    keys = _bucket_keys(start, end, gran)
    return {"labels": [label for _, label in keys],
            "revenue": [revenue.get(k, 0.0) for k, _ in keys],
            "collected": [collected.get(k, 0.0) for k, _ in keys],
            "purchases": [purchases.get(k, 0.0) for k, _ in keys],
            "granularity": gran}


def receivables(conn, today, start, end):
    """Outstanding balances, aged by sale date, plus per-customer totals and the period collection rate."""
    rows = conn.execute(
        """SELECT s.sale_date, s.total_sale, s.customer_id, c.name, c.phone,
                  COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
           FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.deleted=0""").fetchall()
    buckets = {label: 0.0 for label, _, _ in AGING_BUCKETS}
    buckets["Opening balances"] = 0.0
    debtors, billed, settled = {}, 0.0, 0.0
    lo, hi = _bounds(start, end)
    for row in rows:
        total, paid = float(row["total_sale"] or 0), float(row["paid"] or 0)
        if lo <= row["sale_date"] <= hi:
            billed += total
            settled += min(paid, total)
        balance = total - paid
        if balance <= 0.005:
            continue
        try:
            age = (today - datetime.strptime(row["sale_date"][:10], "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        for label, low, high in AGING_BUCKETS:
            if age >= low and (high is None or age <= high):
                buckets[label] += balance
                break
        debtor = debtors.setdefault(row["customer_id"], {"id": row["customer_id"], "name": row["name"], "phone": row["phone"],
                                                         "owed": 0.0, "oldest": 0, "invoices": 0})
        debtor["owed"] += balance
        debtor["oldest"] = max(debtor["oldest"], age)
        debtor["invoices"] += 1
    for row in conn.execute("SELECT id,name,phone,opening_balance FROM customers WHERE active=1 AND opening_balance>0.005").fetchall():
        buckets["Opening balances"] += float(row["opening_balance"])
        debtor = debtors.setdefault(row["id"], {"id": row["id"], "name": row["name"], "phone": row["phone"],
                                                "owed": 0.0, "oldest": 0, "invoices": 0})
        debtor["owed"] += float(row["opening_balance"])
    ordered = sorted(debtors.values(), key=lambda d: d["owed"], reverse=True)
    return {"buckets": buckets, "total": sum(buckets.values()), "debtors": ordered,
            "collection_rate": (settled / billed) if billed > 0 else None}


def _insights(ctx, money):
    items = []

    def add(level, icon, title, text, href=None):
        items.append({"level": level, "icon": icon, "title": title, "text": text, "href": href})

    stock, cover = ctx["stock"], ctx["days_cover"]
    if ctx["has_stock_history"]:
        if stock["available_kg"] <= 0:
            add("danger", "box-seam", "Stock is depleted", "There is no maize left to sell. Record a purchase to restock.", "purchases")
        elif stock["low"]:
            add("warning", "exclamation-triangle", "Stock is running low",
                f"{stock['available_kg']:,.0f} KG left, at or below your {stock['threshold']:,.0f} KG alert level.", "purchases")
        elif cover is not None and cover < 7:
            add("danger", "hourglass-bottom", f"About {cover:.0f} days of stock left", "At the last 30 days' selling pace you will run out within a week.", "purchases")
        elif cover is not None and cover < 14:
            add("warning", "hourglass-split", f"About {cover:.0f} days of stock left", "Consider scheduling your next purchase.", "purchases")
        elif cover is not None:
            add("success", "check2-circle", f"Stock covers about {cover:.0f} days", "Inventory is comfortable at the current selling pace.")
    rec = ctx["receivables"]
    over60 = rec["buckets"].get("Over 60 days", 0.0)
    mid = rec["buckets"].get("31-60 days", 0.0)
    if over60 > 0.005:
        n = sum(1 for d in rec["debtors"] if d["oldest"] > 60)
        add("danger", "cash-stack", f"{money(over60)} is over 60 days overdue",
            f"Spread across {n} customer{'s' if n != 1 else ''}. Follow up before it becomes a bad debt.", "customers")
    elif mid > 0.005:
        add("warning", "cash-stack", f"{money(mid)} is 31-60 days old", "These balances are approaching the 60-day mark.", "customers")
    rate = rec["collection_rate"]
    if rate is not None:
        if rate < 0.6:
            add("warning", "graph-down-arrow", f"Only {rate:.0%} of billed sales collected",
                "Most of this period's sales are still unpaid. Chase payments to protect cash flow.", "payments")
        elif rate >= 0.9:
            add("success", "patch-check", f"{rate:.0%} of billed sales collected", "Collections are healthy for this period.")
    d = ctx["revenue_delta"]
    if d and d["direction"] != "flat" and d["pct"] >= 10:
        if d["direction"] == "up":
            add("success", "graph-up-arrow", f"Sales are up {d['pct']:.0f}%", f"Compared with the previous {ctx['period_label'].lower()}.")
        else:
            add("warning", "graph-down-arrow", f"Sales are down {d['pct']:.0f}%", f"Compared with the previous {ctx['period_label'].lower()}.", "sales")
    if ctx["margin"] is not None and ctx["totals"]["revenue"] > 0 and ctx["margin"] < 0.10:
        add("warning", "percent", f"Gross margin is only {ctx['margin']:.0%}",
            "Selling prices are close to your average purchase cost. Review your pricing.", "reports")
    top = ctx["top_customers"]
    if top and ctx["totals"]["revenue"] > 0 and len(top) > 1 and top[0]["share"] >= 0.4:
        add("info", "person-check", f"{top[0]['name']} is {top[0]['share']:.0%} of revenue", "A large share of sales depends on one customer.")
    if ctx["inactive_customers"]:
        n = ctx["inactive_customers"]
        add("info", "person-dash", f"{n} customer{'s' if n != 1 else ''} inactive for 30+ days", "They have bought before but not recently. A call could win them back.", "customers")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_dashboard(conn, range_key, today, money, cogs, stock_kg, low_stock_kg):
    """Assemble every figure the dashboard shows."""
    key, start, end, prev = resolve_period(range_key, today)
    period_label = dict(RANGES)[key]
    totals = _totals(conn, start, end)
    previous = _totals(conn, *prev) if prev else None

    unit_cost = cogs["unit_cost"]
    profit = totals["revenue"] - totals["kg"] * unit_cost
    prev_profit = (previous["revenue"] - previous["kg"] * unit_cost) if previous else None
    margin = (profit / totals["revenue"]) if totals["revenue"] > 0 else None
    avg_price = (totals["revenue"] / totals["kg"]) if totals["kg"] > 0 else None

    chart_start, gran = _chart_window(key, today, conn)
    series = _series(conn, chart_start, today, gran)
    rec = receivables(conn, today, start, end)

    lo, hi = _bounds(start, end)
    top_rows = conn.execute(
        """SELECT c.id, c.name, COALESCE(SUM(s.total_sale),0) revenue, COUNT(*) n, COALESCE(SUM(s.quantity_kg),0) kg
           FROM sales s JOIN customers c ON c.id=s.customer_id
           WHERE s.deleted=0 AND s.sale_date BETWEEN ? AND ? GROUP BY c.id ORDER BY revenue DESC LIMIT 5""", (lo, hi)).fetchall()
    top_customers = [{"id": r["id"], "name": r["name"], "revenue": float(r["revenue"]), "count": r["n"], "kg": float(r["kg"]),
                      "share": (float(r["revenue"]) / totals["revenue"]) if totals["revenue"] > 0 else 0.0} for r in top_rows]
    methods = [{"method": r["payment_method"] or "Unspecified", "amount": float(r["amount"])} for r in conn.execute(
        """SELECT payment_method, SUM(amount) amount FROM payments WHERE deleted=0 AND payment_date BETWEEN ? AND ?
           GROUP BY payment_method ORDER BY amount DESC""", (lo, hi)).fetchall()]

    last30 = today - timedelta(days=29)
    kg30 = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?",
                        (last30.isoformat(), today.isoformat())).fetchone()["v"]
    daily_rate = float(kg30) / 30.0
    days_cover = (stock_kg / daily_rate) if daily_rate > 0 and stock_kg > 0 else None
    cutoff = (today - timedelta(days=30)).isoformat()
    inactive = conn.execute(
        """SELECT COUNT(*) v FROM (SELECT s.customer_id, MAX(s.sale_date) m FROM sales s
           JOIN customers c ON c.id=s.customer_id AND c.active=1 WHERE s.deleted=0 GROUP BY s.customer_id HAVING m < ?)""", (cutoff,)).fetchone()["v"]

    counts = {name: conn.execute(f"SELECT COUNT(*) v FROM {table} WHERE {where}").fetchone()["v"] for name, table, where in [
        ("purchases", "purchases", "deleted=0"), ("sales", "sales", "deleted=0"),
        ("payments", "payments", "deleted=0"), ("customers", "customers", "active=1")]}
    stock = {"available_kg": stock_kg, "unit_cost": unit_cost, "value": max(stock_kg, 0) * unit_cost,
             "daily_rate": daily_rate, "threshold": low_stock_kg, "low": stock_kg <= low_stock_kg}
    stock["cover_pct"] = min(100.0, (days_cover / 30.0) * 100.0) if days_cover is not None else (100.0 if stock_kg > low_stock_kg else 0.0)

    ctx = {"stock": stock, "days_cover": days_cover, "has_stock_history": counts["purchases"] > 0 or counts["sales"] > 0,
           "receivables": rec, "revenue_delta": _delta(totals["revenue"], previous["revenue"] if previous else None),
           "period_label": period_label, "margin": margin, "totals": totals, "top_customers": top_customers,
           "inactive_customers": inactive}

    recent_sales = conn.execute(
        """SELECT s.sales_id, s.sale_date, c.name, s.quantity_kg, s.total_sale,
                  COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
           FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.deleted=0 ORDER BY s.sale_date DESC, s.id DESC LIMIT 8""").fetchall()
    recent_payments = conn.execute(
        """SELECT p.payment_id, p.payment_date, p.amount, p.payment_method, c.name customer_name
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id JOIN customers c ON c.id=s.customer_id
           WHERE p.deleted=0 AND s.deleted=0 ORDER BY p.payment_date DESC, p.id DESC LIMIT 5""").fetchall()
    recent_purchases = conn.execute(
        """SELECT purchase_id, purchase_date, local_agent, quantity_received_kg, total_cost
           FROM purchases WHERE deleted=0 ORDER BY purchase_date DESC, id DESC LIMIT 5""").fetchall()
    activity = sorted(
        [{"when": r["sale_date"], "kind": "sale", "title": f"Sale to {r['name']}",
          "detail": f"{r['quantity_kg']:,.0f} KG · {money(r['total_sale'])}"} for r in recent_sales]
        + [{"when": r["payment_date"], "kind": "payment", "title": f"Payment from {r['customer_name']}",
            "detail": f"{money(r['amount'])} · {r['payment_method']}"} for r in recent_payments]
        + [{"when": r["purchase_date"], "kind": "purchase", "title": f"Purchase from {r['local_agent']}",
            "detail": f"{r['quantity_received_kg']:,.0f} KG · {money(r['total_cost'])}"} for r in recent_purchases],
        key=lambda item: item["when"], reverse=True)[:7]

    return {
        "range_key": key, "ranges": RANGES, "period_label": period_label,
        "period_text": ("All time" if start is None else start.strftime("%d %b %Y") if start == end
                        else f"{start.strftime('%d %b')} - {end.strftime('%d %b %Y')}"),
        "compare_text": None if not prev else f"vs {prev[0].strftime('%d %b')} - {prev[1].strftime('%d %b')}",
        "totals": totals, "previous": previous,
        "deltas": {"revenue": ctx["revenue_delta"],
                   "collected": _delta(totals["collected"], previous["collected"] if previous else None),
                   "count": _delta(totals["count"], previous["count"] if previous else None),
                   "profit": _delta(profit, prev_profit),
                   "spend": _delta(totals["spend"], previous["spend"] if previous else None)},
        "profit": profit, "margin": margin, "avg_price": avg_price,
        "avg_sale": (totals["revenue"] / totals["count"]) if totals["count"] else None,
        "series": series, "receivables": rec, "top_customers": top_customers, "methods": methods,
        "stock": stock, "days_cover": days_cover, "counts": counts,
        "is_empty": not (counts["purchases"] or counts["sales"] or counts["payments"]),
        "insights": _insights(ctx, money), "activity": activity,
        "recent_sales": recent_sales, "debtors": rec["debtors"][:6],
        "generated_at": datetime.now().strftime("%H:%M"),
    }


_receivables = receivables  # backwards-compatible name
