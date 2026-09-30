"""Stock and inventory analytics for ADUFARMS: position, stock age (FIFO), ledger reconciliation and movement trends."""
from datetime import timedelta

from analytics_core import bounds, bucket_keys, deltas, grain, grouped, parse_date, period_meta, resolve_window
from purchases_service import reorder_plan

BAG_SIZES = (50, 100)
AGE_BANDS = [("0-14 days", 0, 14), ("15-30 days", 15, 30), ("31-60 days", 31, 60), ("Over 60 days", 61, None)]
MOVEMENT_TYPES = ("PURCHASE", "SALE", "REVERSAL", "ADJUSTMENT")
SOURCES = [("purchases", "purchase_date", "deleted=0"), ("sales", "sale_date", "deleted=0"), ("stock_movements", "movement_date", "1=1")]


def movement_filters(args):
    """Filters for the movements ledger -> (where sql, params, echo)."""
    q = (args.get("q") or "").strip()
    mtype = (args.get("type") or "").strip().upper()
    sfrom, sto = (args.get("sfrom") or "").strip(), (args.get("sto") or "").strip()
    clauses, params = ["1=1"], []
    if q:
        clauses.append("(reference LIKE ? OR notes LIKE ? OR created_by LIKE ?)")
        params += [f"%{q}%"] * 3
    if mtype in MOVEMENT_TYPES:
        clauses.append("movement_type = ?")
        params.append(mtype)
    else:
        mtype = ""
    for value, op in ((sfrom, ">="), (sto, "<=")):
        if value and parse_date(value):
            clauses.append(f"movement_date {op} ?")
            params.append(value)
    return " AND ".join(clauses), params, {"q": q, "type": mtype, "sfrom": sfrom, "sto": sto}


def stock_age(conn, today):
    """FIFO: sales consume the oldest purchases first; whatever is left is 'on the floor' and has an age."""
    sold = float(conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0").fetchone()["v"])
    bands = {label: 0.0 for label, _, _ in AGE_BANDS}
    lots, oldest = [], None
    for r in conn.execute("""SELECT purchase_id, purchase_date, local_agent, quantity_received_kg q FROM purchases
                             WHERE deleted=0 AND quantity_received_kg>0 ORDER BY purchase_date, id""").fetchall():
        left = float(r["q"])
        take = min(left, sold)
        sold -= take
        left -= take
        if left <= 0.0001:
            continue
        d = parse_date(r["purchase_date"])
        age = (today - d).days if d else 0
        for label, low, high in AGE_BANDS:
            if age >= low and (high is None or age <= high):
                bands[label] += left
                break
        lots.append({"purchase_id": r["purchase_id"], "supplier": r["local_agent"], "date": r["purchase_date"], "age": age, "kg": left})
        oldest = age if oldest is None else max(oldest, age)
    total = sum(bands.values())
    return {"bands": bands, "total": total, "lots": lots[:8], "oldest_days": oldest,
            "avg_age": (sum(l["age"] * l["kg"] for l in lots) / total) if total > 0 else None,
            "old_share": (sum(v for k, v in bands.items() if k in ("31-60 days", "Over 60 days")) / total) if total > 0 else 0.0}


def reconcile(conn):
    """Does the movements ledger agree with purchases received minus sales?"""
    def one(sql):
        return float(conn.execute(sql).fetchone()["v"])
    received = one("SELECT COALESCE(SUM(quantity_received_kg),0) v FROM purchases WHERE deleted=0")
    sold = one("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0")
    ledger = one("SELECT COALESCE(SUM(quantity_kg),0) v FROM stock_movements")
    by_type = {r["t"]: float(r["v"]) for r in conn.execute("SELECT movement_type t, COALESCE(SUM(quantity_kg),0) v FROM stock_movements GROUP BY t").fetchall()}
    difference = ledger - (received - sold)
    return {"book_stock": received - sold, "ledger_stock": ledger, "difference": difference, "ok": abs(difference) < 0.5,
            "received": received, "sold": sold, "by_type": by_type, "movement_count": conn.execute("SELECT COUNT(*) c FROM stock_movements").fetchone()["c"]}


def _window(conn, start, end):
    lo, hi = bounds(start, end)
    r_in = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0) recv, COALESCE(SUM(quantity_kg),0) bought, COUNT(*) n FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ?", (lo, hi)).fetchone()
    r_out = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) kg, COUNT(*) n FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?", (lo, hi)).fetchone()
    kg_in, kg_out = float(r_in["recv"]), float(r_out["kg"])
    lost = max(float(r_in["bought"]) - kg_in, 0.0)
    return {"kg_in": kg_in, "kg_out": kg_out, "net": kg_in - kg_out, "purchases": r_in["n"], "sales": r_out["n"], "lost_in_transit": lost}


def _series(conn, start, end):
    gran = grain(start, end)
    ins = grouped(conn, gran, "SELECT {k} k, SUM(quantity_received_kg) v FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ? GROUP BY k", start, end, "purchase_date")
    outs = grouped(conn, gran, "SELECT {k} k, SUM(quantity_kg) v FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ? GROUP BY k", start, end, "sale_date")
    level = float(conn.execute(
        "SELECT (SELECT COALESCE(SUM(quantity_received_kg),0) FROM purchases WHERE deleted=0 AND purchase_date < ?) - "
        "(SELECT COALESCE(SUM(quantity_kg),0) FROM sales WHERE deleted=0 AND sale_date < ?) v", (start.isoformat(), start.isoformat())).fetchone()["v"])
    out = {"labels": [], "kg_in": [], "kg_out": [], "level": [], "granularity": gran}
    for key, label in bucket_keys(start, end, gran):
        got = float(ins[key]["v"]) if key in ins else 0.0
        gone = float(outs[key]["v"]) if key in outs else 0.0
        level += got - gone
        out["labels"].append(label)
        out["kg_in"].append(round(got, 1))
        out["kg_out"].append(round(gone, 1))
        out["level"].append(round(level, 1))
    return out


def _movement_mix(conn, start, end):
    lo, hi = bounds(start, end)
    return [{"type": r["t"], "count": r["n"], "kg": float(r["v"])} for r in conn.execute(
        """SELECT movement_type t, COUNT(*) n, SUM(quantity_kg) v FROM stock_movements WHERE movement_date BETWEEN ? AND ?
           GROUP BY t ORDER BY n DESC""", (lo, hi)).fetchall()]


def _insights(d, money):
    items = []

    def add(level, icon, title, text, href=None):
        items.append({"level": level, "icon": icon, "title": title, "text": text, "href": href})

    pos, age, rec = d["position"], d["age"], d["reconcile"]
    if not rec["ok"]:
        add("danger", "exclamation-octagon", f"Stock ledger is out by {abs(rec['difference']):,.1f} KG",
            f"Purchases minus sales say {rec['book_stock']:,.1f} KG, but the movement ledger adds up to {rec['ledger_stock']:,.1f} KG. Someone edited or removed records outside the normal flow.")
    if pos["kg"] <= 0:
        add("danger", "box-seam", "No stock on hand", "Record a purchase before you can sell.", "purchases")
    elif pos["low"]:
        add("warning", "exclamation-triangle", "Stock is low", f"{pos['kg']:,.0f} KG, at or below your {pos['low_level']:,.0f} KG alert level.", "purchases")
    elif pos["cover_days"] is not None and pos["cover_days"] < 14:
        add("warning", "hourglass-split", f"About {pos['cover_days']:.0f} days of cover left", f"Reorder by {pos['reorder_by']}.", "purchases")
    if age["total"] > 0:
        if age["bands"]["Over 60 days"] > 0.5:
            add("danger", "hourglass-bottom", f"{age['bands']['Over 60 days']:,.0f} KG has been in store over 60 days",
                "Old grain loses moisture quality and attracts pests. Sell this first.")
        elif age["old_share"] > 0.3:
            add("warning", "hourglass-bottom", f"{age['old_share']:.0%} of your stock is over 30 days old", "Move the oldest lots first.")
        elif age["avg_age"] is not None:
            add("success", "patch-check", f"Stock is fresh: {age['avg_age']:.0f} days old on average", "Nothing is sitting too long.")
    k = d["kpis"]
    if d["turnover"] is not None:
        if d["turnover"] < 0.5 and k["kg_out"] > 0:
            add("info", "arrow-repeat", f"Slow turnover ({d['turnover']:.1f}× in this period)", "You hold a lot of stock compared with what you sell.")
        elif d["turnover"] >= 3:
            add("info", "arrow-repeat", f"Fast turnover ({d['turnover']:.1f}×)", "Stock moves quickly. Make sure you can restock in time.")
    if k["lost_in_transit"] > 0.5:
        add("warning", "truck", f"{k['lost_in_transit']:,.0f} KG lost in transit this period", "Bought but never received.", "analytics")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_inventory(conn, range_key, today, money, stock_kg, unit_cost, low_stock_kg, daily_rate, date_from=None, date_to=None):
    key, start, end, prev = resolve_window(range_key, today, conn, date_from, date_to, sources=SOURCES)
    cur = _window(conn, start, end)
    previous = _window(conn, *prev) if prev else None
    plan = reorder_plan(stock_kg, daily_rate, low_stock_kg, unit_cost or None, today)
    series = _series(conn, start, end)
    avg_stock = sum(series["level"]) / len(series["level"]) if series["level"] else 0.0
    days = (end - start).days + 1
    data = {**period_meta(key, start, end, prev, today), "kpis": cur, "previous": previous,
            "deltas": deltas(cur, previous, ("kg_in", "kg_out", "net", "purchases", "sales")),
            "position": {"kg": stock_kg, "value": max(stock_kg, 0) * unit_cost, "unit_cost": unit_cost, "low": stock_kg <= low_stock_kg,
                         "low_level": low_stock_kg, "daily_rate": daily_rate, "cover_days": plan["cover_days"], "reorder_by": plan["reorder_by"],
                         "suggested_kg": plan["suggested_kg"], "budget": plan["budget"], "bags": {size: stock_kg / size for size in BAG_SIZES}},
            "age": stock_age(conn, today), "reconcile": reconcile(conn), "series": series, "mix": _movement_mix(conn, start, end),
            "turnover": (cur["kg_out"] / avg_stock) if avg_stock > 1 else None,
            "days_to_sell": (avg_stock / (cur["kg_out"] / days)) if avg_stock > 1 and cur["kg_out"] > 0 else None,
            "has_data": bool(cur["purchases"] or cur["sales"])}
    data["insights"] = _insights(data, money)
    return data
