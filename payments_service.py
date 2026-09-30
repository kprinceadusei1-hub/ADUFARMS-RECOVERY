"""Payment / collections analytics and entry-form assistance for ADUFARMS."""
from collections import Counter
from datetime import timedelta

import analytics_service
import dashboard_service
from analytics_core import bounds, bucket_keys, deltas, grain, grouped, parse_date, period_meta, resolve_window

SOURCES = [("payments", "payment_date", "deleted=0"), ("sales", "sale_date", "deleted=0")]
LARGE_CASH = 10000.0


def payment_filters(args, visible_sql):
    """Ledger filters from request args -> (where sql, params, echo). The ledger query aliases payments p, sales s, customers c."""
    q = (args.get("q") or "").strip()
    method = (args.get("method") or "").strip().lower()
    customer = args.get("customer", type=int) if hasattr(args, "get") else None
    pfrom, pto = (args.get("pfrom") or "").strip(), (args.get("pto") or "").strip()
    clauses, params = [visible_sql("p"), visible_sql("s")], []
    if q:
        clauses.append("(p.payment_id LIKE ? OR p.sales_id LIKE ? OR p.payment_reference LIKE ? OR c.name LIKE ? OR c.phone LIKE ?)")
        params += [f"%{q}%"] * 5
    if method in ("mobile", "cash", "bank", "cheque"):
        clauses.append("LOWER(p.payment_method) LIKE ?")
        params.append(f"%{method}%")
    else:
        method = ""
    if customer:
        clauses.append("s.customer_id = ?")
        params.append(customer)
    for value, op in ((pfrom, ">="), (pto, "<=")):
        if value and parse_date(value):
            clauses.append(f"p.payment_date {op} ?")
            params.append(value)
    return " AND ".join(clauses), params, {"q": q, "method": method, "customer": customer or "", "pfrom": pfrom, "pto": pto}


def lifetime_metrics(conn):
    """Headline totals over ALL live payments (not just the rows a page happens to show)."""
    r = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(p.amount),0) total,
                  COALESCE(SUM(CASE WHEN LOWER(p.payment_method) LIKE '%mobile%' THEN p.amount END),0) momo,
                  COALESCE(SUM(CASE WHEN LOWER(p.payment_method) LIKE '%cash%' THEN p.amount END),0) cash,
                  COALESCE(SUM(CASE WHEN LOWER(p.payment_method) LIKE '%bank%' THEN p.amount END),0) bank
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id WHERE p.deleted=0 AND s.deleted=0""").fetchone()
    owed = conn.execute(
        """SELECT COALESCE(SUM(MAX(s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p
                  WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0), 0)),0) v FROM sales s WHERE s.deleted=0""").fetchone()["v"]
    return {"count": r["n"], "total_collected": float(r["total"]), "momo_collected": float(r["momo"]), "cash_collected": float(r["cash"]),
            "bank_collected": float(r["bank"]), "total_outstanding": float(owed)}


def _window(conn, start, end):
    lo, hi = bounds(start, end)
    r = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(p.amount),0) total, COALESCE(MAX(p.amount),0) biggest, COUNT(DISTINCT s.customer_id) payers
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
           WHERE p.deleted=0 AND s.deleted=0 AND p.payment_date BETWEEN ? AND ?""", (lo, hi)).fetchone()
    billed = float(conn.execute("SELECT COALESCE(SUM(total_sale),0) v FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?", (lo, hi)).fetchone()["v"])
    dtp = conn.execute(
        """SELECT SUM(p.amount*(julianday(p.payment_date)-julianday(s.sale_date)))/NULLIF(SUM(p.amount),0) d
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
           WHERE p.deleted=0 AND s.deleted=0 AND p.payment_date BETWEEN ? AND ?""", (lo, hi)).fetchone()["d"]
    total, n = float(r["total"]), r["n"]
    return {"count": n, "collected": total, "avg_payment": (total / n) if n else None, "biggest": float(r["biggest"]), "payers": r["payers"],
            "billed": billed, "net_receivable_change": billed - total, "collection_ratio": (total / billed) if billed > 0 else None,
            "days_to_pay": float(dtp) if dtp is not None else None}


def _series(conn, start, end):
    gran = grain(start, end)
    rows = grouped(conn, gran, """SELECT {k} k, SUM(amount) collected, COUNT(*) n FROM payments
                                   WHERE deleted=0 AND payment_date BETWEEN ? AND ? GROUP BY k""", start, end, "payment_date")
    billed = grouped(conn, gran, "SELECT {k} k, SUM(total_sale) billed FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ? GROUP BY k",
                     start, end, "sale_date")
    out = {"labels": [], "collected": [], "billed": [], "count": [], "cumulative": [], "granularity": gran}
    running = 0.0
    for key, label in bucket_keys(start, end, gran):
        got = float(rows[key]["collected"]) if key in rows else 0.0
        running += got
        out["labels"].append(label)
        out["collected"].append(round(got, 2))
        out["billed"].append(round(float(billed[key]["billed"]), 2) if key in billed else 0.0)
        out["count"].append(rows[key]["n"] if key in rows else 0)
        out["cumulative"].append(round(running, 2))
    return out


def _methods(conn, start, end):
    lo, hi = bounds(start, end)
    return [{"method": r["m"], "count": r["n"], "total": float(r["total"]), "avg": float(r["avg"])} for r in conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(payment_method),''),'Unspecified') m, COUNT(*) n, SUM(amount) total, AVG(amount) avg FROM payments
           WHERE deleted=0 AND payment_date BETWEEN ? AND ? GROUP BY m ORDER BY total DESC""", (lo, hi)).fetchall()]


def _weekday(conn, start, end):
    lo, hi = bounds(start, end)
    by = {r["w"]: r for r in conn.execute(
        """SELECT CAST(strftime('%w', payment_date) AS INTEGER) w, COUNT(*) n, SUM(amount) total FROM payments
           WHERE deleted=0 AND payment_date BETWEEN ? AND ? GROUP BY w""", (lo, hi)).fetchall()}
    return [{"day": analytics_service.WEEKDAYS[i], "count": by[i]["n"] if i in by else 0, "total": float(by[i]["total"]) if i in by else 0.0}
            for i in (1, 2, 3, 4, 5, 6, 0)]


def _payers(conn, start, end, today):
    """Top payers in the window, plus customers who habitually pay slowly and still owe money."""
    lo, hi = bounds(start, end)
    top = [{"id": r["id"], "name": r["name"], "count": r["n"], "total": float(r["total"])} for r in conn.execute(
        """SELECT c.id, c.name, COUNT(*) n, SUM(p.amount) total FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
           JOIN customers c ON c.id=s.customer_id WHERE p.deleted=0 AND s.deleted=0 AND p.payment_date BETWEEN ? AND ?
           GROUP BY c.id ORDER BY total DESC LIMIT 6""", (lo, hi)).fetchall()]
    owed = {d["id"]: d for d in dashboard_service.receivables(conn, today, start, end)["debtors"]}
    slow = []
    for r in conn.execute(
            """SELECT c.id, c.name, SUM(p.amount*(julianday(p.payment_date)-julianday(s.sale_date)))/NULLIF(SUM(p.amount),0) d, COUNT(*) n
               FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id JOIN customers c ON c.id=s.customer_id
               WHERE p.deleted=0 AND s.deleted=0 GROUP BY c.id HAVING d > 30""").fetchall():
        if r["id"] in owed:
            slow.append({"id": r["id"], "name": r["name"], "days": float(r["d"]), "owed": owed[r["id"]]["owed"], "oldest": owed[r["id"]]["oldest"]})
    return top, sorted(slow, key=lambda x: x["owed"], reverse=True)[:6]


def _aging_watch(conn, today):
    """Open balances, aged, plus the money about to cross the 60-day line within two weeks."""
    about, total = 0.0, 0
    for r in conn.execute(
            """SELECT s.sale_date, s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p
                      WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) bal FROM sales s WHERE s.deleted=0""").fetchall():
        d = parse_date(r["sale_date"])
        if d and float(r["bal"]) > 0.005:
            age = (today - d).days
            if 46 <= age <= 60:
                about += float(r["bal"])
                total += 1
    return {"about_to_age": about, "about_to_age_count": total}


aging_watch = _aging_watch  # public name for other modules


def payment_memory(conn, today):
    """Per-customer open sales (oldest first), usual channel and average payment; plus recent references for duplicate warnings."""
    memory = {}
    for r in conn.execute(
            """SELECT s.customer_id cid, s.sales_id, s.sale_date, s.total_sale,
                      s.total_sale - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) bal
               FROM sales s WHERE s.deleted=0 ORDER BY s.sale_date, s.id""").fetchall():
        if float(r["bal"]) > 0.005:
            d = parse_date(r["sale_date"])
            memory.setdefault(str(r["cid"]), {"open": [], "owed": 0.0, "method": None, "avg": None})
            entry = memory[str(r["cid"])]
            entry["open"].append({"sales_id": r["sales_id"], "date": d.strftime("%d %b %Y") if d else r["sale_date"],
                                  "balance": round(float(r["bal"]), 2), "age": (today - d).days if d else 0})
            entry["owed"] = round(entry["owed"] + float(r["bal"]), 2)
    methods, amounts = {}, {}
    for r in conn.execute(
            """SELECT s.customer_id cid, p.payment_method m, p.amount a FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id
               WHERE p.deleted=0 AND s.deleted=0 ORDER BY p.payment_date DESC, p.id DESC LIMIT 1500""").fetchall():
        methods.setdefault(r["cid"], Counter())[r["m"]] += 1
        amounts.setdefault(r["cid"], []).append(float(r["a"]))
    for cid in set(methods) | {int(k) for k in memory}:
        entry = memory.setdefault(str(cid), {"open": [], "owed": 0.0, "method": None, "avg": None})
        if cid in methods:
            entry["method"] = methods[cid].most_common(1)[0][0]
            entry["avg"] = round(sum(amounts[cid]) / len(amounts[cid]), 2)
    refs = [r["ref"].strip().lower() for r in conn.execute(
        "SELECT payment_reference ref FROM payments WHERE deleted=0 AND TRIM(COALESCE(payment_reference,'')) != '' ORDER BY id DESC LIMIT 600").fetchall()]
    return {"customers": memory, "references": refs, "large_cash": LARGE_CASH}


def _insights(d, money):
    items = []

    def add(level, icon, title, text, href=None):
        items.append({"level": level, "icon": icon, "title": title, "text": text, "href": href})

    k, watch = d["kpis"], d["watch"]
    if watch["about_to_age"] > 0.005:
        add("warning", "alarm", f"{money(watch['about_to_age'])} will pass 60 days within two weeks",
            f"Across {watch['about_to_age_count']} sale{'s' if watch['about_to_age_count'] != 1 else ''}. Chase these now, before they turn into bad debt.", "customers")
    dl = d["deltas"]["collected"]
    if dl and dl["pct"] >= 10 and dl["direction"] != "flat":
        add("success" if dl["direction"] == "up" else "warning", "graph-up-arrow" if dl["direction"] == "up" else "graph-down-arrow",
            f"Collections are {'up' if dl['direction'] == 'up' else 'down'} {dl['pct']:.0f}%", "Compared with the previous period.")
    ratio = k["collection_ratio"]
    if ratio is not None:
        if ratio < 0.7:
            add("warning", "hourglass-split", f"You collected only {ratio:.0%} of what you billed", f"Receivables grew by {money(k['net_receivable_change'])} in this period.")
        elif ratio >= 1:
            add("success", "patch-check", "You collected more than you billed", "Receivables are shrinking.")
    if k["days_to_pay"] is not None and k["days_to_pay"] > 30:
        add("warning", "clock-history", f"Customers take {k['days_to_pay']:.0f} days to pay on average", "Slow payment ties up cash that could buy more maize.")
    if d["slow"]:
        s = d["slow"][0]
        add("warning", "person-exclamation", f"{len(d['slow'])} habitually slow payer{'s' if len(d['slow']) != 1 else ''} still owe money",
            f"{s['name']} pays in ~{s['days']:.0f} days and owes {money(s['owed'])}.")
    methods = d["methods"]
    total = sum(m["total"] for m in methods)
    if methods and total:
        top = methods[0]
        if top["total"] / total >= 0.6 and len(methods) > 1:
            add("info", "wallet2", f"{top['method']} is {top['total'] / total:.0%} of collections", "Most of your money arrives one way. Keep that channel healthy.")
        cash = next((m for m in methods if "cash" in m["method"].lower()), None)
        if cash and cash["total"] / total >= 0.5:
            add("info", "cash-stack", f"Cash is {cash['total'] / total:.0%} of collections", "Bank or mobile money leaves a clearer audit trail.")
    if k["biggest"] and k["biggest"] >= LARGE_CASH:
        add("info", "coin", f"Largest single payment: {money(k['biggest'])}", "")
    days = [x for x in d["weekday"] if x["total"] > 0]
    if len(days) >= 3:
        best = max(days, key=lambda x: x["total"])
        add("info", "calendar-week", f"{best['day']} is your best collection day", f"{money(best['total'])} across {best['count']} payments.")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_payment_analytics(conn, range_key, today, money, date_from=None, date_to=None):
    key, start, end, prev = resolve_window(range_key, today, conn, date_from, date_to, sources=SOURCES)
    cur = _window(conn, start, end)
    previous = _window(conn, *prev) if prev else None
    top_payers, slow_payers = _payers(conn, start, end, today)
    data = {**period_meta(key, start, end, prev, today), "kpis": cur, "previous": previous,
            "deltas": deltas(cur, previous, ("collected", "count", "avg_payment", "billed", "days_to_pay", "payers")),
            "series": _series(conn, start, end), "methods": _methods(conn, start, end), "weekday": _weekday(conn, start, end),
            "top_payers": top_payers, "slow": slow_payers, "watch": _aging_watch(conn, today),
            "receivables": dashboard_service.receivables(conn, today, start, end), "has_data": cur["count"] > 0}
    data["insights"] = _insights(data, money)
    return data
