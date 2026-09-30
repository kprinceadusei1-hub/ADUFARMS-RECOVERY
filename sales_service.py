"""Sales-side analytics and form assistance for ADUFARMS.

Profit is estimated from the weighted-average purchase cost per KG (`unit_cost`), the same basis the dashboard uses.
"""
from datetime import timedelta

import analytics_service
import dashboard_service
from analytics_core import bounds, deltas, grouped, grain, bucket_keys, parse_date, period_meta, resolve_window

TARGET_MARGINS = (0.10, 0.15, 0.20)
LOW_MARGIN = 0.05

# Sales with the amount paid against each one attached: the base of most figures below.
_SALES_WITH_PAID = """(SELECT s.*, COALESCE((SELECT SUM(p.amount) FROM payments p
                        WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
                      FROM sales s WHERE s.deleted=0 AND s.sale_date BETWEEN ? AND ?) s"""


def sales_filters(args, visible_sql):
    """Ledger filters from a request-args mapping -> (where sql, params, echo dict)."""
    q = (args.get("q") or "").strip()
    customer = args.get("customer", type=int) if hasattr(args, "get") else None
    status = (args.get("status") or "").strip()
    sfrom, sto = (args.get("sfrom") or "").strip(), (args.get("sto") or "").strip()
    paid_sql = "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0)"
    clauses, params = [visible_sql("s")], []
    if q:
        clauses.append("(s.sales_id LIKE ? OR s.invoice_number LIKE ? OR c.name LIKE ? OR c.phone LIKE ?)")
        params += [f"%{q}%"] * 4
    if customer:
        clauses.append("s.customer_id = ?")
        params.append(customer)
    if status == "paid":
        clauses.append(f"{paid_sql} >= s.total_sale - 0.005")
    elif status == "part":
        clauses.append(f"{paid_sql} > 0.005 AND {paid_sql} < s.total_sale - 0.005")
    elif status == "unpaid":
        clauses.append(f"{paid_sql} <= 0.005")
    else:
        status = ""
    for value, op in ((sfrom, ">="), (sto, "<=")):
        if value and parse_date(value):
            clauses.append(f"s.sale_date {op} ?")
            params.append(value)
    return " AND ".join(clauses), params, {"q": q, "customer": customer or "", "status": status, "sfrom": sfrom, "sto": sto}


def _window(conn, start, end, unit_cost):
    lo, hi = bounds(start, end)
    r = conn.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(total_sale),0) revenue, COALESCE(SUM(quantity_kg),0) kg,
                   COALESCE(SUM(MIN(total_sale, paid)),0) settled, MIN(selling_price_kg) lo, MAX(selling_price_kg) hi
            FROM {_SALES_WITH_PAID}""", (lo, hi)).fetchone()
    revenue, kg, settled = float(r["revenue"]), float(r["kg"]), float(r["settled"])
    profit = revenue - kg * unit_cost
    return {"orders": r["n"], "revenue": revenue, "kg": kg, "settled": settled, "outstanding": revenue - settled,
            "collection_rate": (settled / revenue) if revenue > 0 else None, "profit": profit,
            "margin": (profit / revenue) if revenue > 0 else None, "avg_price": (revenue / kg) if kg > 0 else None,
            "avg_order": (revenue / r["n"]) if r["n"] else None,
            "min_price": float(r["lo"]) if r["lo"] is not None else None, "max_price": float(r["hi"]) if r["hi"] is not None else None}


def _series(conn, start, end, unit_cost):
    gran = grain(start, end)
    rows = grouped(conn, gran, """SELECT {k} k, SUM(total_sale) revenue, SUM(quantity_kg) kg, COUNT(*) n,
                                   MIN(selling_price_kg) lo, MAX(selling_price_kg) hi FROM sales
                                   WHERE deleted=0 AND sale_date BETWEEN ? AND ? GROUP BY k""", start, end, "sale_date")
    out = {k: [] for k in ("labels", "revenue", "profit", "kg", "orders", "avg_price", "min_price", "max_price")}
    out["granularity"] = gran
    for key, label in bucket_keys(start, end, gran):
        r = rows.get(key)
        revenue, kg = (float(r["revenue"]), float(r["kg"])) if r else (0.0, 0.0)
        out["labels"].append(label)
        out["revenue"].append(round(revenue, 2))
        out["profit"].append(round(revenue - kg * unit_cost, 2))
        out["kg"].append(round(kg, 1))
        out["orders"].append(r["n"] if r else 0)
        out["avg_price"].append(round(revenue / kg, 3) if kg > 0 else None)
        out["min_price"].append(round(float(r["lo"]), 3) if r else None)
        out["max_price"].append(round(float(r["hi"]), 3) if r else None)
    return out


def _status_mix(conn, start, end):
    lo, hi = bounds(start, end)
    mix = {"paid": {"label": "Fully paid", "count": 0, "amount": 0.0}, "part": {"label": "Part paid", "count": 0, "amount": 0.0},
           "unpaid": {"label": "Unpaid", "count": 0, "amount": 0.0}}
    for r in conn.execute(f"SELECT total_sale, paid FROM {_SALES_WITH_PAID}", (lo, hi)).fetchall():
        total, paid = float(r["total_sale"]), float(r["paid"])
        key = "paid" if paid >= total - 0.005 else "part" if paid > 0.005 else "unpaid"
        mix[key]["count"] += 1
        mix[key]["amount"] += max(total - paid, 0.0) if key != "paid" else total
    return list(mix.values())


def _top_customers(conn, start, end, revenue, unit_cost):
    lo, hi = bounds(start, end)
    out = []
    for r in conn.execute(
            """SELECT c.id, c.name, COUNT(*) n, SUM(s.total_sale) revenue, SUM(s.quantity_kg) kg
               FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.deleted=0 AND s.sale_date BETWEEN ? AND ?
               GROUP BY c.id ORDER BY revenue DESC LIMIT 6""", (lo, hi)).fetchall():
        rev, kg = float(r["revenue"]), float(r["kg"])
        out.append({"id": r["id"], "name": r["name"], "orders": r["n"], "revenue": rev, "kg": kg, "share": rev / revenue if revenue else 0.0,
                    "margin": (rev - kg * unit_cost) / rev if rev else None})
    return out


def _customer_types(conn, start, end):
    lo, hi = bounds(start, end)
    return [{"type": r["t"], "orders": r["n"], "revenue": float(r["revenue"])} for r in conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(c.customer_type),''),'Unspecified') t, COUNT(*) n, SUM(s.total_sale) revenue
           FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.deleted=0 AND s.sale_date BETWEEN ? AND ?
           GROUP BY t ORDER BY revenue DESC""", (lo, hi)).fetchall()]


def _risky_sales(conn, start, end, unit_cost):
    """Sales priced below cost or with a margin under LOW_MARGIN."""
    if unit_cost <= 0:
        return {"below_cost": 0, "low_margin": 0, "lost_value": 0.0, "examples": []}
    lo, hi = bounds(start, end)
    rows = conn.execute(
        """SELECT s.sales_id, s.sale_date, c.name, s.selling_price_kg, s.quantity_kg FROM sales s JOIN customers c ON c.id=s.customer_id
           WHERE s.deleted=0 AND s.sale_date BETWEEN ? AND ? AND s.selling_price_kg < ? ORDER BY s.selling_price_kg ASC""",
        (lo, hi, unit_cost / (1 - LOW_MARGIN))).fetchall()
    below = [r for r in rows if float(r["selling_price_kg"]) < unit_cost]
    return {"below_cost": len(below), "low_margin": len(rows) - len(below),
            "lost_value": sum((unit_cost - float(r["selling_price_kg"])) * float(r["quantity_kg"]) for r in below),
            "examples": [{"sales_id": r["sales_id"], "date": r["sale_date"], "customer": r["name"], "price": float(r["selling_price_kg"]),
                          "margin": (float(r["selling_price_kg"]) - unit_cost) / float(r["selling_price_kg"]) if float(r["selling_price_kg"]) else None}
                         for r in rows[:5]]}


risky_sales = _risky_sales  # public name for other modules


def pricing_assistant(conn, today, unit_cost):
    since = (today - timedelta(days=29)).isoformat()
    r = conn.execute("""SELECT SUM(total_sale)/NULLIF(SUM(quantity_kg),0) avg, MIN(selling_price_kg) lo, MAX(selling_price_kg) hi
                        FROM sales WHERE deleted=0 AND sale_date >= ?""", (since,)).fetchone()
    return {"cost": unit_cost,
            "targets": [{"margin": m, "price": unit_cost / (1 - m)} for m in TARGET_MARGINS] if unit_cost > 0 else [],
            "recent_avg": float(r["avg"]) if r["avg"] else None,
            "recent_low": float(r["lo"]) if r["lo"] is not None else None, "recent_high": float(r["hi"]) if r["hi"] is not None else None}


def customer_memory(conn, today):
    """What the sale form needs to be 'smart': each customer's balance, last price and usual order size."""
    debt = {d["id"]: d for d in dashboard_service.receivables(conn, today, today, today)["debtors"]}
    memory = {}
    for r in conn.execute(
            """SELECT customer_id, selling_price_kg, quantity_kg, sale_date FROM sales WHERE deleted=0
               ORDER BY sale_date DESC, id DESC LIMIT 1500""").fetchall():
        m = memory.setdefault(r["customer_id"], {"prices": [], "qtys": [], "last_date": r["sale_date"], "orders": 0})
        m["orders"] += 1
        if len(m["prices"]) < 5:
            m["prices"].append(float(r["selling_price_kg"]))
            m["qtys"].append(float(r["quantity_kg"]))
    out = {}
    for cid in set(memory) | set(debt):
        m, d = memory.get(cid), debt.get(cid)
        out[str(cid)] = {
            "owed": round(d["owed"], 2) if d else 0.0, "oldest": d["oldest"] if d else 0, "unpaid_sales": d["invoices"] if d else 0,
            "last_price": round(m["prices"][0], 3) if m else None, "avg_price": round(sum(m["prices"]) / len(m["prices"]), 3) if m else None,
            "usual_qty": round(sum(m["qtys"]) / len(m["qtys"]), 1) if m else None, "orders": m["orders"] if m else 0,
            "last_date": parse_date(m["last_date"]).strftime("%d %b %Y") if m and parse_date(m["last_date"]) else None}
    return out


def _insights(d, money):
    items = []

    def add(level, icon, title, text, href=None):
        items.append({"level": level, "icon": icon, "title": title, "text": text, "href": href})

    k, risky = d["kpis"], d["risky"]
    if risky["below_cost"]:
        add("danger", "exclamation-octagon", f"{risky['below_cost']} sale{'s' if risky['below_cost'] != 1 else ''} priced below your cost",
            f"Roughly {money(risky['lost_value'])} lost on those loads. Average cost is {money(d['pricing']['cost'])}/KG.")
    elif risky["low_margin"]:
        add("warning", "percent", f"{risky['low_margin']} sale{'s' if risky['low_margin'] != 1 else ''} with a margin under {LOW_MARGIN:.0%}", "Very little profit on these. Check the prices you agreed.")
    cr = k["collection_rate"]
    if cr is not None:
        if cr < 0.6:
            add("warning", "hourglass-split", f"Only {cr:.0%} of this period's sales are paid", f"{money(k['outstanding'])} is still owed on them.", "payments")
        elif cr >= 0.9:
            add("success", "patch-check", f"{cr:.0%} of this period's sales are paid", "Collections are healthy.")
    dl = d["deltas"]["avg_price"]
    if dl and dl["pct"] >= 4 and dl["direction"] != "flat":
        add("success" if dl["direction"] == "up" else "warning", "tag", f"Average selling price {'up' if dl['direction'] == 'up' else 'down'} {dl['pct']:.0f}%", "Compared with the previous period.")
    dl = d["deltas"]["revenue"]
    if dl and dl["pct"] >= 10 and dl["direction"] != "flat":
        add("success" if dl["direction"] == "up" else "warning", "graph-up-arrow" if dl["direction"] == "up" else "graph-down-arrow",
            f"Sales are {'up' if dl['direction'] == 'up' else 'down'} {dl['pct']:.0f}%", "Compared with the previous period.")
    if d["top_customers"] and len(d["top_customers"]) > 1 and d["top_customers"][0]["share"] >= 0.4:
        t = d["top_customers"][0]
        add("info", "person-check", f"{t['name']} is {t['share']:.0%} of sales", "A large share depends on one buyer.")
    unpaid = next((m for m in d["status_mix"] if m["label"] == "Unpaid"), None)
    if unpaid and unpaid["count"]:
        add("info", "cash-coin", f"{unpaid['count']} sale{'s' if unpaid['count'] != 1 else ''} with no payment yet", f"{money(unpaid['amount'])} outstanding.", "payments")
    days = [x for x in d["patterns"]["weekday"] if x["revenue"] > 0]
    if len(days) >= 3:
        best = max(days, key=lambda x: x["revenue"])
        add("info", "calendar-week", f"{best['day']} is your best sales day", f"{money(best['revenue'])} across {best['orders']} orders.")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_sales_analytics(conn, range_key, today, money, unit_cost, stock_kg, date_from=None, date_to=None):
    key, start, end, prev = resolve_window(range_key, today, conn, date_from, date_to,
                                            sources=[("sales", "sale_date", "deleted=0")])
    cur = _window(conn, start, end, unit_cost)
    previous = _window(conn, *prev, unit_cost) if prev else None
    data = {**period_meta(key, start, end, prev, today), "kpis": cur, "previous": previous,
            "deltas": deltas(cur, previous, ("revenue", "profit", "orders", "avg_price", "avg_order", "kg", "settled")),
            "series": _series(conn, start, end, unit_cost), "status_mix": _status_mix(conn, start, end),
            "top_customers": _top_customers(conn, start, end, cur["revenue"], unit_cost), "customer_types": _customer_types(conn, start, end),
            "patterns": analytics_service.patterns(conn, start, end), "risky": _risky_sales(conn, start, end, unit_cost),
            "pricing": pricing_assistant(conn, today, unit_cost), "stock_kg": stock_kg, "has_data": cur["orders"] > 0}
    data["insights"] = _insights(data, money)
    return data
