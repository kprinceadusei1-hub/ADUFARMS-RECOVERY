"""Purchase-side analytics and form assistance for ADUFARMS.

Key idea: the *landed cost per KG* is what a KG really costs you - total outlay (maize + transport + other costs)
divided by the maize that actually arrived, not the price you agreed with the supplier.
"""
from datetime import date, datetime, timedelta

from analytics_core import RANGES, period_meta, bounds, bucket_keys, delta, deltas, grain, resolve_window

TARGET_COVER_DAYS = 30


def _landed(cost, received):
    return (cost / received) if received and received > 0 else None


def _window(conn, start, end):
    lo, hi = bounds(start, end)
    r = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_cost),0) spend, COALESCE(SUM(total_purchase_cost),0) goods,
                  COALESCE(SUM(transport_cost),0) transport, COALESCE(SUM(other_expenses),0) other,
                  COALESCE(SUM(quantity_kg),0) bought, COALESCE(SUM(quantity_received_kg),0) received,
                  COALESCE(SUM(MAX(quantity_kg - quantity_received_kg, 0) * price_per_kg),0) loss_value
           FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ?""", (lo, hi)).fetchone()
    spend, bought, received = float(r["spend"]), float(r["bought"]), float(r["received"])
    return {"count": r["n"], "spend": spend, "goods": float(r["goods"]), "transport": float(r["transport"]), "other": float(r["other"]),
            "bought": bought, "received": received, "loss_kg": max(bought - received, 0.0),
            "loss_pct": ((bought - received) / bought) if bought > 0 else None, "loss_value": float(r["loss_value"]),
            "landed": _landed(spend, received), "overhead_pct": ((float(r["transport"]) + float(r["other"])) / spend) if spend > 0 else None}


def _series(conn, start, end):
    gran = grain(start, end)
    keys = bucket_keys(start, end, gran)
    expr = {"week": "date(purchase_date, 'weekday 0', '-6 days')", "month": "substr(purchase_date,1,7)"}[gran]
    rows = {r["k"]: r for r in conn.execute(
        f"""SELECT {expr} k, SUM(total_cost) spend, SUM(quantity_received_kg) received FROM purchases
            WHERE deleted=0 AND purchase_date BETWEEN ? AND ? GROUP BY k""", (start.isoformat(), end.isoformat())).fetchall()}
    out = {"labels": [], "spend": [], "received": [], "landed": [], "granularity": gran}
    for key, label in keys:
        r = rows.get(key)
        spend, received = (float(r["spend"]), float(r["received"])) if r else (0.0, 0.0)
        out["labels"].append(label)
        out["spend"].append(round(spend, 2))
        out["received"].append(round(received, 1))
        out["landed"].append(round(spend / received, 3) if received > 0 else None)
    return out


def _suppliers(conn, start, end, market_landed):
    lo, hi = bounds(start, end)
    rows = conn.execute(
        """SELECT local_agent, COUNT(*) n, SUM(total_cost) spend, SUM(quantity_kg) bought, SUM(quantity_received_kg) received,
                  SUM(MAX(quantity_kg - quantity_received_kg, 0) * price_per_kg) loss_value, MAX(purchase_date) last_buy
           FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ? GROUP BY local_agent ORDER BY spend DESC""", (lo, hi)).fetchall()
    total = sum(float(r["spend"]) for r in rows)
    out = []
    for r in rows:
        spend, bought, received = float(r["spend"]), float(r["bought"]), float(r["received"])
        landed = _landed(spend, received)
        loss_pct = ((bought - received) / bought) if bought > 0 else 0.0
        cost_score = max(0.0, min(100.0, 50 + (market_landed - landed) / market_landed * 500)) if landed and market_landed else 50.0
        reliability = max(0.0, min(100.0, 100 - max(loss_pct, 0) / 0.05 * 100))
        score = round(0.6 * cost_score + 0.4 * reliability)
        out.append({"name": r["local_agent"], "purchases": r["n"], "spend": spend, "share": spend / total if total else 0.0,
                    "bought": bought, "received": received, "loss_pct": max(loss_pct, 0.0), "loss_value": float(r["loss_value"] or 0),
                    "landed": landed, "vs_market": ((landed - market_landed) / market_landed) if landed and market_landed else None,
                    "score": score, "grade": "A" if score >= 75 else "B" if score >= 50 else "C", "last_buy": r["last_buy"]})
    return out


def _price_trend(conn, start, end, names):
    lo, hi = bounds(start, end)
    points = {n: {} for n in names}
    dates = set()
    for r in conn.execute(
            """SELECT purchase_date, local_agent, SUM(total_cost) cost, SUM(quantity_received_kg) recv FROM purchases
               WHERE deleted=0 AND purchase_date BETWEEN ? AND ? AND quantity_received_kg>0 GROUP BY purchase_date, local_agent
               ORDER BY purchase_date""", (lo, hi)).fetchall():
        if r["local_agent"] in points:
            points[r["local_agent"]][r["purchase_date"]] = round(float(r["cost"]) / float(r["recv"]), 3)
            dates.add(r["purchase_date"])
    labels = sorted(dates)
    return {"labels": [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in labels],
            "datasets": [{"name": n, "data": [points[n].get(d) for d in labels]} for n in names if points[n]]}


def _locations(conn, start, end):
    lo, hi = bounds(start, end)
    return [{"name": r["loc"], "purchases": r["n"], "received": float(r["recv"]), "spend": float(r["spend"]),
             "landed": _landed(float(r["spend"]), float(r["recv"]))} for r in conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(location),''),'Unspecified') loc, COUNT(*) n, SUM(quantity_received_kg) recv, SUM(total_cost) spend
           FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ? GROUP BY loc ORDER BY spend DESC LIMIT 6""", (lo, hi)).fetchall()]


def supplier_index(conn, today):
    """What the entry form needs to be 'smart': per-supplier memory plus a recent market price."""
    rows = conn.execute(
        """SELECT local_agent, agent_phone, location, price_per_kg, purchase_date, quantity_kg, quantity_received_kg
           FROM purchases WHERE deleted=0 ORDER BY purchase_date DESC, id DESC LIMIT 800""").fetchall()
    index = {}
    for r in rows:
        s = index.setdefault(r["local_agent"], {"phone": "", "location": "", "last_price": None, "last_date": None,
                                                "prices": [], "bought": 0.0, "received": 0.0, "n": 0})
        s["phone"] = s["phone"] or (r["agent_phone"] or "")
        s["location"] = s["location"] or (r["location"] or "")
        if s["last_price"] is None:
            s["last_price"], s["last_date"] = float(r["price_per_kg"]), r["purchase_date"]
        if len(s["prices"]) < 5:
            s["prices"].append(float(r["price_per_kg"]))
        s["bought"] += float(r["quantity_kg"])
        s["received"] += float(r["quantity_received_kg"])
        s["n"] += 1
    for s in index.values():
        s["avg_price"] = round(sum(s["prices"]) / len(s["prices"]), 3) if s["prices"] else None
        s["typical_loss"] = round(max(s["bought"] - s["received"], 0.0) / s["bought"], 4) if s["bought"] > 0 else 0.0
        s["last_price"] = round(s["last_price"], 3) if s["last_price"] is not None else None
        s["last_date"] = datetime.strptime(s["last_date"][:10], "%Y-%m-%d").strftime("%d %b %Y") if s["last_date"] else None
        del s["prices"], s["bought"], s["received"]
    since = (today - timedelta(days=90)).isoformat()
    m = conn.execute("""SELECT SUM(price_per_kg*quantity_kg)/NULLIF(SUM(quantity_kg),0) p FROM purchases
                        WHERE deleted=0 AND purchase_date >= ?""", (since,)).fetchone()["p"]
    return {"suppliers": index, "market_price": round(float(m), 3) if m else None}


def _reorder(stock_kg, daily_rate, low_stock_kg, landed, today):
    cover = (stock_kg / daily_rate) if daily_rate > 0 and stock_kg > 0 else None
    need = max(daily_rate * TARGET_COVER_DAYS - max(stock_kg, 0.0), 0.0)
    suggested = float(int((need + 49) // 50) * 50) if need > 0 else 0.0
    reorder_by = None
    if daily_rate > 0:
        days_left = max((stock_kg - low_stock_kg) / daily_rate, 0.0)
        reorder_by = (today + timedelta(days=int(days_left))).strftime("%d %b %Y")
    urgent = stock_kg <= low_stock_kg or (cover is not None and cover < 14)
    return {"stock": stock_kg, "daily_rate": daily_rate, "cover_days": cover, "suggested_kg": suggested,
            "budget": suggested * landed if landed else None, "reorder_by": reorder_by, "urgent": urgent,
            "target_days": TARGET_COVER_DAYS, "has_pace": daily_rate > 0}


reorder_plan = _reorder  # public name for other modules


def _insights(d, money):
    items = []

    def add(level, icon, title, text):
        items.append({"level": level, "icon": icon, "title": title, "text": text})

    ro, k, prev = d["reorder"], d["kpis"], d["previous"]
    if ro["urgent"] and ro["has_pace"]:
        add("danger", "cart-plus", "Time to restock", f"About {ro['cover_days']:.0f} days of stock left. Suggested order: {ro['suggested_kg']:,.0f} KG to reach {ro['target_days']} days of cover."
            if ro["cover_days"] is not None else "Stock is at or below your alert level.")
    elif ro["suggested_kg"] > 0 and ro["has_pace"]:
        add("info", "cart-plus", f"Plan a purchase of ~{ro['suggested_kg']:,.0f} KG", f"That would give you {ro['target_days']} days of cover. Reorder around {ro['reorder_by']}.")
    last, market = d["latest"], d["market_landed"]
    if last and market and last["landed"]:
        diff = (last["landed"] - market) / market
        if diff >= 0.08:
            add("warning", "graph-up-arrow", f"Latest purchase cost {diff:.0%} above your average",
                f"{last['supplier']} cost {money(last['landed'])}/KG landed vs a {money(market)}/KG average. Negotiate or shop around.")
        elif diff <= -0.08:
            add("success", "graph-down-arrow", f"Latest purchase was {abs(diff):.0%} below average", f"{last['supplier']} at {money(last['landed'])}/KG landed. A good buy.")
    if k["loss_value"] > 0.005 and k["loss_pct"] and k["loss_pct"] > 0.01:
        worst = max(d["suppliers"], key=lambda s: s["loss_value"], default=None)
        add("warning", "box-seam", f"{money(k['loss_value'])} paid for maize that never arrived",
            f"{k['loss_kg']:,.0f} KG short ({k['loss_pct']:.1%})." + (f" Biggest loss: {worst['name']} ({money(worst['loss_value'])})." if worst and worst["loss_value"] > 0 else ""))
    priced = [s for s in d["suppliers"] if s["landed"]]
    if len(priced) >= 2:
        best, worst = min(priced, key=lambda s: s["landed"]), max(priced, key=lambda s: s["landed"])
        if worst["landed"] > best["landed"] * 1.05:
            add("info", "truck", f"{best['name']} is your best-value supplier", f"{money(best['landed'])}/KG landed vs {money(worst['landed'])}/KG from {worst['name']}.")
    if d["suppliers"] and d["suppliers"][0]["share"] >= 0.6 and len(d["suppliers"]) > 1:
        add("info", "exclamation-diamond", f"{d['suppliers'][0]['name']} is {d['suppliers'][0]['share']:.0%} of your spend", "Relying on one supplier is risky if they cannot deliver.")
    if k["overhead_pct"] is not None and k["overhead_pct"] > 0.12:
        add("info", "truck-front", f"Transport & other costs are {k['overhead_pct']:.0%} of spend", f"Adding about {money((k['transport'] + k['other']) / k['received'])} to every KG received." if k["received"] else "")
    dl = d["deltas"]["spend"]
    if dl and dl["pct"] >= 15:
        add("info", "cash-stack", f"Spending is {'up' if dl['direction'] == 'up' else 'down'} {dl['pct']:.0f}%", "Compared with the previous period.")
    if d["has_data"]:
        if k["loss_pct"] is not None and k["loss_pct"] <= 0.001 and k["received"] > 0:
            add("success", "patch-check", "Every KG you paid for arrived", "No transit losses recorded in this period.")
        if k["landed"]:
            add("info", "tag", f"Your maize costs {money(k['landed'])}/KG delivered",
                f"Across {k['count']} purchase{'s' if k['count'] != 1 else ''} and {k['received']:,.0f} KG received" + (f" · {money(k['spend'])} spent." if k["spend"] else "."))
        best_score = max(d["suppliers"], key=lambda s: s["score"], default=None)
        if best_score and len(d["suppliers"]) > 1:
            add("info", "trophy", f"{best_score['name']} ranks first on price and reliability", f"Grade {best_score['grade']} ({best_score['score']}/100) in this period.")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_purchase_analytics(conn, range_key, today, money, stock_kg, low_stock_kg, daily_rate, date_from=None, date_to=None):
    key, start, end, prev = resolve_window(range_key, today, conn, date_from, date_to)
    cur = _window(conn, start, end)
    previous = _window(conn, *prev) if prev else None
    since90 = today - timedelta(days=89)
    w90 = _window(conn, since90, today)
    market_landed = w90["landed"] or cur["landed"]

    suppliers = _suppliers(conn, start, end, market_landed)
    top_names = [s["name"] for s in suppliers[:4]]
    last = conn.execute("""SELECT local_agent, total_cost, quantity_received_kg, purchase_date FROM purchases
                           WHERE deleted=0 AND quantity_received_kg>0 ORDER BY purchase_date DESC, id DESC LIMIT 1""").fetchone()
    latest = {"supplier": last["local_agent"], "landed": float(last["total_cost"]) / float(last["quantity_received_kg"]), "date": last["purchase_date"]} if last else None
    kpis = dict(cur, received_kg=cur["received"], market_landed=market_landed)
    data = {
        **period_meta(key, start, end, prev, today),
        "kpis": kpis, "previous": previous, "market_landed": market_landed, "latest": latest,
        "deltas": deltas(cur, previous, ("spend", "received", "landed", "count", "loss_value")),
        "series": _series(conn, start, end), "suppliers": suppliers, "price_trend": _price_trend(conn, start, end, top_names),
        "locations": _locations(conn, start, end),
        "breakdown": {"goods": cur["goods"], "transport": cur["transport"], "other": cur["other"]},
        "reorder": _reorder(stock_kg, daily_rate, low_stock_kg, market_landed, today),
        "has_data": cur["count"] > 0,
    }
    data["insights"] = _insights(data, money)
    return data


def sales_pace(conn, today):
    """Average KG sold per day over the last 30 days."""
    since = (today - timedelta(days=29)).isoformat()
    kg = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?",
                      (since, today.isoformat())).fetchone()["v"]
    return float(kg) / 30.0
