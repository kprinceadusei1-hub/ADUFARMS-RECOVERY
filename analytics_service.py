"""Deeper business analytics for ADUFARMS.

Profitability, pricing, suppliers, customers, patterns, cash flow, stock, a composite health score and a simple
sales outlook. Everything is computed from live records and returned as plain dicts/lists (easy to test).
"""
from datetime import date, datetime, timedelta

import dashboard_service as ds
from analytics_core import (DAYS, period_meta, DEFAULT_RANGE, RANGES, bounds, bucket_keys, delta, deltas, grain as _grain,  # noqa: F401
                            grouped as _grouped, parse_date as _parse, resolve_window as resolve)

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
SIZE_BANDS = [("Under 100 KG", 0, 100), ("100-299 KG", 100, 300), ("300-499 KG", 300, 500), ("500 KG and over", 500, None)]


def _timeline(conn, start, end, unit_cost):
    gran = _grain(start, end)
    keys = bucket_keys(start, end, gran)
    sales = _grouped(conn, gran, "SELECT {k} k, SUM(total_sale) revenue, SUM(quantity_kg) kg FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ? GROUP BY k", start, end, "sale_date")
    buys = _grouped(conn, gran, "SELECT {k} k, SUM(total_cost) cost, SUM(quantity_received_kg) recv FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ? GROUP BY k", start, end, "purchase_date")
    pays = _grouped(conn, gran, "SELECT {k} k, SUM(amount) collected FROM payments WHERE deleted=0 AND payment_date BETWEEN ? AND ? GROUP BY k", start, end, "payment_date")
    moves = _grouped(conn, gran, "SELECT {k} k, SUM(quantity_kg) net FROM stock_movements WHERE movement_date BETWEEN ? AND ? GROUP BY k", start, end, "movement_date")
    level = float(conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM stock_movements WHERE movement_date < ?", (start.isoformat(),)).fetchone()["v"])
    out = {k: [] for k in ("labels", "revenue", "cogs", "profit", "margin", "sell_price", "buy_price", "collected", "spend", "net_cash", "stock")}
    out["granularity"] = gran
    for key, label in keys:
        s, b, p, m = sales.get(key), buys.get(key), pays.get(key), moves.get(key)
        revenue, kg = (float(s["revenue"]), float(s["kg"])) if s else (0.0, 0.0)
        cogs, collected, spend = kg * unit_cost, (float(p["collected"]) if p else 0.0), (float(b["cost"]) if b else 0.0)
        level += float(m["net"]) if m else 0.0
        out["labels"].append(label)
        out["revenue"].append(round(revenue, 2))
        out["cogs"].append(round(cogs, 2))
        out["profit"].append(round(revenue - cogs, 2))
        out["margin"].append(round((revenue - cogs) / revenue * 100, 1) if revenue > 0 else None)
        out["sell_price"].append(round(revenue / kg, 3) if kg > 0 else None)
        out["buy_price"].append(round(float(b["cost"]) / float(b["recv"]), 3) if b and b["recv"] else None)
        out["collected"].append(round(collected, 2))
        out["spend"].append(round(spend, 2))
        out["net_cash"].append(round(collected - spend, 2))
        out["stock"].append(round(level, 1))
    return out


def _core(conn, start, end, unit_cost):
    """Headline figures for one window; used for both the current and the previous period."""
    lo, hi = bounds(start, end)
    sales = conn.execute("SELECT COALESCE(SUM(total_sale),0) revenue, COALESCE(SUM(quantity_kg),0) kg, COUNT(*) n FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?", (lo, hi)).fetchone()
    buys = conn.execute("SELECT COALESCE(SUM(total_cost),0) cost, COALESCE(SUM(quantity_received_kg),0) recv, COALESCE(SUM(quantity_kg),0) bought FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ?", (lo, hi)).fetchone()
    collected = float(conn.execute("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE deleted=0 AND payment_date BETWEEN ? AND ?", (lo, hi)).fetchone()["v"])
    dtp = conn.execute(
        """SELECT SUM(p.amount*(julianday(p.payment_date)-julianday(s.sale_date)))/NULLIF(SUM(p.amount),0) d
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
           WHERE p.deleted=0 AND s.deleted=0 AND p.payment_date BETWEEN ? AND ?""", (lo, hi)).fetchone()["d"]
    revenue, kg, cost, recv, bought = float(sales["revenue"]), float(sales["kg"]), float(buys["cost"]), float(buys["recv"]), float(buys["bought"])
    profit = revenue - kg * unit_cost
    sell, buy = (revenue / kg) if kg > 0 else None, (cost / recv) if recv > 0 else None
    return {"revenue": revenue, "kg": kg, "orders": sales["n"], "spend": cost, "received": recv, "profit": profit,
            "margin": (profit / revenue) if revenue > 0 else None, "sell_price": sell, "buy_price": buy,
            "spread": (sell - buy) if sell is not None and buy is not None else None,
            "shrink_kg": max(bought - recv, 0.0), "shrink_pct": ((bought - recv) / bought) if bought > 0 else None,
            "days_to_pay": float(dtp) if dtp is not None else None, "collected": collected, "net_cash": collected - cost}


def _customers(conn, start, end, today, owed_by_customer, unit_cost):
    lo, hi = bounds(start, end)
    rows = conn.execute(
        """SELECT c.id, c.name, c.phone, c.customer_type, COUNT(s.id) n, COALESCE(SUM(s.total_sale),0) revenue,
                  COALESCE(SUM(s.quantity_kg),0) kg, MAX(s.sale_date) last_sale
           FROM sales s JOIN customers c ON c.id=s.customer_id
           WHERE s.deleted=0 AND s.sale_date BETWEEN ? AND ? GROUP BY c.id ORDER BY revenue DESC""", (lo, hi)).fetchall()
    days_to_pay = {r["customer_id"]: r["d"] for r in conn.execute(
        """SELECT s.customer_id, SUM(p.amount*(julianday(p.payment_date)-julianday(s.sale_date)))/NULLIF(SUM(p.amount),0) d
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
           WHERE p.deleted=0 AND s.deleted=0 AND p.payment_date BETWEEN ? AND ? GROUP BY s.customer_id""", (lo, hi)).fetchall()}
    first_sale = {r["customer_id"]: r["f"] for r in conn.execute(
        "SELECT customer_id, MIN(sale_date) f FROM sales WHERE deleted=0 GROUP BY customer_id").fetchall()}
    total = sum(float(r["revenue"]) for r in rows)
    cumulative, table = 0.0, []
    for r in rows:
        revenue, kg = float(r["revenue"]), float(r["kg"])
        before = cumulative / total if total else 0.0
        cumulative += revenue
        profit = revenue - kg * unit_cost
        last = _parse(r["last_sale"])
        table.append({
            "id": r["id"], "name": r["name"], "phone": r["phone"], "type": r["customer_type"], "orders": r["n"],
            "revenue": revenue, "kg": kg, "avg_order": revenue / r["n"] if r["n"] else 0.0,
            "profit": profit, "margin": (profit / revenue) if revenue > 0 else None,
            "share": revenue / total if total else 0.0, "cum_share": cumulative / total if total else 0.0,
            "abc": "A" if before < 0.8 else "B" if before < 0.95 else "C",
            "last_sale": r["last_sale"], "days_since": (today - last).days if last else None,
            "days_to_pay": days_to_pay.get(r["id"]), "owed": owed_by_customer.get(r["id"], 0.0),
            "is_new": bool(first_sale.get(r["id"]) and lo <= first_sale[r["id"]] <= hi)})
    return table


def _at_risk(conn, today):
    """Customers whose silence is long compared with their own normal buying rhythm (needs 3+ past orders)."""
    dates = {}
    for r in conn.execute("""SELECT s.customer_id, c.name, s.sale_date FROM sales s JOIN customers c ON c.id=s.customer_id
                             WHERE s.deleted=0 AND c.active=1 ORDER BY s.customer_id, s.sale_date""").fetchall():
        d = _parse(r["sale_date"])
        if d:
            dates.setdefault((r["customer_id"], r["name"]), []).append(d)
    out = []
    for (cid, name), ds_ in dates.items():
        if len(ds_) < 3:
            continue
        gaps = [(b - a).days for a, b in zip(ds_, ds_[1:]) if (b - a).days > 0]
        if not gaps:
            continue
        usual = sum(gaps) / len(gaps)
        silent = (today - ds_[-1]).days
        if silent > max(14, usual * 2) and silent <= 240:
            out.append({"id": cid, "name": name, "silent_days": silent, "usual_days": round(usual), "orders": len(ds_), "last": ds_[-1].isoformat()})
    return sorted(out, key=lambda x: x["silent_days"] / max(x["usual_days"], 1), reverse=True)[:6]


def _suppliers(conn, start, end):
    lo, hi = bounds(start, end)
    out = []
    for r in conn.execute(
            """SELECT local_agent, COUNT(*) n, COALESCE(SUM(total_cost),0) spend, COALESCE(SUM(quantity_kg),0) bought,
                      COALESCE(SUM(quantity_received_kg),0) received, MAX(purchase_date) last_buy
               FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ? GROUP BY local_agent ORDER BY spend DESC""", (lo, hi)).fetchall():
        bought, received = float(r["bought"]), float(r["received"])
        out.append({"name": r["local_agent"], "purchases": r["n"], "spend": float(r["spend"]), "bought": bought, "received": received,
                    "loss_kg": max(bought - received, 0.0), "loss_pct": ((bought - received) / bought) if bought > 0 else 0.0,
                    "cost_per_kg": (float(r["spend"]) / received) if received > 0 else None, "last_buy": r["last_buy"]})
    return out


def _patterns(conn, start, end):
    lo, hi = bounds(start, end)
    weekdays = {r["w"]: r for r in conn.execute(
        """SELECT CAST(strftime('%w', sale_date) AS INTEGER) w, COUNT(*) n, SUM(total_sale) revenue
           FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ? GROUP BY w""", (lo, hi)).fetchall()}
    by_weekday = [{"day": WEEKDAYS[i], "orders": weekdays[i]["n"] if i in weekdays else 0,
                   "revenue": float(weekdays[i]["revenue"]) if i in weekdays else 0.0} for i in (1, 2, 3, 4, 5, 6, 0)]
    sizes = []
    for label, low, high in SIZE_BANDS:
        cond = "quantity_kg >= ?" + (" AND quantity_kg < ?" if high else "")
        row = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(total_sale),0) revenue FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ? AND {cond}",
                           [lo, hi, low] + ([high] if high else [])).fetchone()
        sizes.append({"band": label, "orders": row["n"], "revenue": float(row["revenue"])})
    methods = [{"method": r["payment_method"] or "Unspecified", "count": r["n"], "total": float(r["total"]), "avg": float(r["avg"])} for r in conn.execute(
        """SELECT payment_method, COUNT(*) n, SUM(amount) total, AVG(amount) avg FROM payments
           WHERE deleted=0 AND payment_date BETWEEN ? AND ? GROUP BY payment_method ORDER BY total DESC""", (lo, hi)).fetchall()]
    return {"weekday": by_weekday, "sizes": sizes, "methods": methods}


patterns = _patterns  # public name for other modules


def _forecast(conn, today, days_cover):
    """Least-squares trend over the last 8 full weeks of sales, projected 4 weeks ahead."""
    this_monday = today - timedelta(days=today.weekday())
    first = this_monday - timedelta(weeks=8)
    rows = {r["k"]: float(r["v"]) for r in conn.execute(
        """SELECT date(sale_date, 'weekday 0', '-6 days') k, SUM(total_sale) v FROM sales
           WHERE deleted=0 AND sale_date >= ? AND sale_date < ? GROUP BY k""", (first.isoformat(), this_monday.isoformat())).fetchall()}
    weeks = [first + timedelta(weeks=i) for i in range(8)]
    ys = [rows.get(w.isoformat(), 0.0) for w in weeks]
    active = sum(1 for y in ys if y > 0)
    out = {"ready": active >= 4, "labels": [w.strftime("%d %b") for w in weeks], "history": [round(y, 2) for y in ys],
           "future_labels": [], "future": [], "projected_total": None, "trend_pct": None, "runout_date": None}
    if days_cover is not None:
        out["runout_date"] = (today + timedelta(days=int(days_cover))).strftime("%d %b %Y")
    if not out["ready"]:
        return out
    n = len(ys)
    mean_x, mean_y = (n - 1) / 2, sum(ys) / n
    var_x = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys)) / var_x
    intercept = mean_y - slope * mean_x
    future = [max(intercept + slope * (n + j), 0.0) for j in range(4)]
    out["future_labels"] = [(this_monday + timedelta(weeks=j)).strftime("%d %b") for j in range(4)]
    out["future"] = [round(v, 2) for v in future]
    out["projected_total"] = round(sum(future), 2)
    base = sum(ys[-4:]) or 0.0
    out["trend_pct"] = round((sum(future) - base) / base * 100, 1) if base > 0 else None
    return out


def _health(core, rec, days_cover, stock_kg, top3_share, customers_count):
    """Composite 0-100 score from whichever components have enough data; weights are re-normalised."""
    if not (core["revenue"] or core["spend"] or core["collected"]):      # nothing happened: do not invent a score
        return {"score": None, "grade": "Not enough data", "tone": "flat", "components": []}
    comps = []

    def add(name, icon, weight, score, note):
        if score is not None:
            comps.append({"name": name, "icon": icon, "weight": weight, "score": max(0.0, min(100.0, score)), "note": note})

    m = core["margin"]
    add("Profitability", "piggy-bank", 25, (m / 0.25 * 100) if m is not None else None,
        f"{m:.0%} gross margin (25% scores full marks)" if m is not None else "")
    cr = rec["collection_rate"]
    add("Collections", "wallet2", 25, (cr * 100) if cr is not None else None, f"{cr:.0%} of billed sales collected" if cr is not None else "")
    if core["collected"] or core["spend"]:
        net = core["net_cash"]
        base = core["spend"] or core["collected"]
        add("Cash flow", "cash-stack", 15, 100.0 if net >= 0 else (1 + net / base) * 100,
            "Collections cover purchases" if net >= 0 else "Purchases are ahead of collections")
    if days_cover is not None:
        add("Stock cover", "box-seam", 15, days_cover / 21 * 100, f"about {days_cover:.0f} days of stock (21+ days scores full marks)")
    elif stock_kg <= 0:
        add("Stock cover", "box-seam", 15, 0.0, "No stock on hand")
    if top3_share is not None and customers_count >= 4:
        add("Customer spread", "people", 10, (1 - top3_share) / 0.5 * 100, f"top 3 customers = {top3_share:.0%} of revenue")
    sp = core["shrink_pct"]
    add("Supplier reliability", "truck", 10, (100 - sp / 0.05 * 100) if sp is not None else None,
        f"{sp:.1%} of bought maize lost in transit" if sp is not None else "")
    if not comps:
        return {"score": None, "grade": "Not enough data", "tone": "flat", "components": []}
    total_w = sum(c["weight"] for c in comps)
    score = sum(c["score"] * c["weight"] for c in comps) / total_w
    for c in comps:
        c["score"] = round(c["score"])
        c["tone"] = "good" if c["score"] >= 75 else "warn" if c["score"] >= 50 else "bad"
    grade, tone = ("Excellent", "good") if score >= 80 else ("Good", "good") if score >= 65 else ("Fair", "warn") if score >= 50 else ("Needs attention", "bad")
    return {"score": round(score), "grade": grade, "tone": tone, "components": sorted(comps, key=lambda c: c["score"])}


def _insights(a, money):
    items = []

    def add(level, icon, title, text):
        items.append({"level": level, "icon": icon, "title": title, "text": text})

    k = a["kpis"]
    if k["spread"] is not None:
        if k["spread"] <= 0:
            add("danger", "exclamation-octagon", "You are selling below your buying cost",
                f"Average sale {money(k['sell_price'])}/KG vs average purchase {money(k['buy_price'])}/KG. Every KG sold loses money.")
        elif k["buy_price"] and k["spread"] / k["buy_price"] < 0.08:
            add("warning", "percent", f"Thin price spread: {money(k['spread'])}/KG", "Less than 8% above your purchase cost. A small cost rise could wipe out your profit.")
        else:
            add("success", "graph-up-arrow", f"Healthy spread: {money(k['spread'])}/KG", "Selling prices comfortably exceed purchase cost.")
    if k["shrink_pct"] is not None and k["shrink_pct"] > 0.02:
        worst = max(a["suppliers"], key=lambda s: s["loss_pct"], default=None)
        extra = f" Worst: {worst['name']} ({worst['loss_pct']:.1%})." if worst and worst["loss_pct"] > 0.02 else ""
        add("warning", "box-seam", f"{k['shrink_pct']:.1%} of bought maize never arrived", f"{k['shrink_kg']:,.0f} KG purchased but not received.{extra}")
    priced = [s for s in a["suppliers"] if s["cost_per_kg"] and s["received"] > 0]
    if len(priced) >= 2:
        best, worst = min(priced, key=lambda s: s["cost_per_kg"]), max(priced, key=lambda s: s["cost_per_kg"])
        if worst["cost_per_kg"] > best["cost_per_kg"] * 1.05:
            add("info", "truck", f"{best['name']} is your cheapest supplier", f"{money(best['cost_per_kg'])}/KG vs {money(worst['cost_per_kg'])}/KG from {worst['name']}.")
    if k["days_to_pay"] is not None and k["days_to_pay"] > 30:
        add("warning", "hourglass-split", f"Customers take {k['days_to_pay']:.0f} days to pay on average", "Slow collection ties up your cash.")
    elif k["days_to_pay"] is not None:
        add("success", "patch-check", f"Customers pay in about {k['days_to_pay']:.0f} days", "Collection speed is healthy.")
    if a["at_risk"]:
        top = a["at_risk"][0]
        add("warning", "person-exclamation", f"{len(a['at_risk'])} regular customer{'s have' if len(a['at_risk']) != 1 else ' has'} gone quiet",
            f"{top['name']} usually buys every ~{top['usual_days']} days but hasn't for {top['silent_days']}.")
    if len(a["customers"]) >= 3:
        a_count = sum(1 for c in a["customers"] if c["abc"] == "A")
        add("info", "people", f"{a_count} of {len(a['customers'])} customers bring 80% of revenue", "Protect these relationships; they carry the business.")
    best_margin = [c for c in a["customers"] if c["margin"] is not None and c["revenue"] > 0]
    if len(best_margin) >= 3:
        hi, lo = max(best_margin, key=lambda c: c["margin"]), min(best_margin, key=lambda c: c["margin"])
        if hi["margin"] - lo["margin"] > 0.08:
            add("info", "bullseye", f"{hi['name']} is your most profitable buyer ({hi['margin']:.0%})", f"{lo['name']} earns only {lo['margin']:.0%} - consider the price you give them.")
    days = [d for d in a["patterns"]["weekday"] if d["revenue"] > 0]
    if len(days) >= 3:
        best = max(days, key=lambda d: d["revenue"])
        add("info", "calendar-week", f"{best['day']} is your strongest sales day", f"{money(best['revenue'])} across {best['orders']} orders.")
    if k["net_cash"] < 0:
        add("warning", "cash-stack", f"Cash out exceeded cash in by {money(abs(k['net_cash']))}", "Purchases are outpacing collections in this period.")
    fc = a["forecast"]
    if fc["ready"] and fc["trend_pct"] is not None and abs(fc["trend_pct"]) >= 10:
        add("success" if fc["trend_pct"] > 0 else "warning", "binoculars", f"Sales trending {'up' if fc['trend_pct'] > 0 else 'down'} {abs(fc['trend_pct']):.0f}%",
            f"Projected {money(fc['projected_total'])} over the next 4 weeks.")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_analytics(conn, range_key, today, money, cogs, date_from=None, date_to=None):
    key, start, end, prev = resolve(range_key, today, conn, date_from, date_to)
    unit_cost = cogs["unit_cost"]
    core = _core(conn, start, end, unit_cost)
    previous = _core(conn, prev[0], prev[1], unit_cost) if prev else None
    timeline = _timeline(conn, start, end, unit_cost)
    rec = ds.receivables(conn, today, start, end)
    customers = _customers(conn, start, end, today, {d["id"]: d["owed"] for d in rec["debtors"]}, unit_cost)
    suppliers, patterns = _suppliers(conn, start, end), _patterns(conn, start, end)

    last30 = today - timedelta(days=29)
    rate = float(conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?",
                              (last30.isoformat(), today.isoformat())).fetchone()["v"]) / 30.0
    stock_now = cogs["available_kg"]
    days_cover = (stock_now / rate) if rate > 0 and stock_now > 0 else None
    avg_stock = (sum(timeline["stock"]) / len(timeline["stock"])) if timeline["stock"] else 0.0
    top3 = sum(c["share"] for c in customers[:3]) if customers else None

    change_names = ("revenue", "profit", "sell_price", "buy_price", "collected", "spend", "orders", "days_to_pay", "spread")
    deltas_ = deltas(core, previous, change_names)
    kpis = dict(core, turnover=(core["kg"] / avg_stock) if avg_stock > 1 else None, stock_now=stock_now,
                new_customers=sum(1 for c in customers if c["is_new"]), active_customers=len(customers), top3_share=top3,
                repeat_rate=(sum(1 for c in customers if c["orders"] >= 2) / len(customers)) if customers else None,
                kg_sold=core["kg"], kg_bought=core["received"])
    data = {**period_meta(key, start, end, prev, today),
            "timeline": timeline, "customers": customers, "suppliers": suppliers, "patterns": patterns, "kpis": kpis,
            "deltas": deltas_, "at_risk": _at_risk(conn, today), "forecast": _forecast(conn, today, days_cover), "days_cover": days_cover,
            "health": _health(core, rec, days_cover, stock_now, top3, len(customers)),
            "has_data": bool(core["orders"] or core["spend"] or core["collected"])}
    data["insights"] = _insights(data, money)
    return data
