"""Customer intelligence for ADUFARMS: profiles, credit scores, segments and portfolio insights.

Everything is "as of today" and calculated from live records (deleted sales and payments are excluded).
"""
from datetime import timedelta

from analytics_core import bucket_keys, parse_date

SEGMENTS = {
    "champion": ("Champion", "success", "Top revenue, recent, pays well"),
    "loyal": ("Loyal", "info", "Buys regularly on their usual rhythm"),
    "new": ("New", "info", "First purchase in the last 30 days"),
    "at_risk": ("At risk", "warning", "Regular buyer gone quiet for longer than usual"),
    "dormant": ("Dormant", "flat", "No purchase for 120+ days"),
    "prospect": ("Prospect", "flat", "Registered but has never bought"),
    "active": ("Active", "info", "Buying, no special signal"),
}


def _scale(value, best, worst):
    """Map value to 0-100 where `best` -> 100 and `worst` -> 0 (either direction), clamped."""
    if best == worst:
        return 100.0
    return max(0.0, min(100.0, (value - worst) / (best - worst) * 100.0))


def credit_score(days_to_pay, oldest_unpaid, balance, billed, paid_share):
    """0-100 from whichever signals exist; returns (score or None, rating). Weights: speed 30, overdue 30, exposure 20, paid-in-full 20."""
    if billed <= 0:
        return None, "No history"
    parts = []   # (weight, score)
    if days_to_pay is not None:
        parts.append((30, _scale(days_to_pay, 7, 60)))
    parts.append((30, _scale(oldest_unpaid, 14, 75) if oldest_unpaid else 100.0))
    parts.append((20, _scale(max(balance, 0) / billed, 0.1, 0.6)))
    if paid_share is not None:
        parts.append((20, _scale(paid_share, 1.0, 0.3)))
    score = round(sum(w * s for w, s in parts) / sum(w for w, _ in parts))
    return score, "Excellent" if score >= 80 else "Good" if score >= 65 else "Fair" if score >= 45 else "Risky"


def build_profiles(conn, today, unit_cost):
    """One dict per active customer, sorted by name."""
    sales = {}
    for r in conn.execute(
            """SELECT s.customer_id cid, s.sale_date, s.total_sale, s.quantity_kg,
                      COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
               FROM sales s WHERE s.deleted=0 ORDER BY s.sale_date, s.id""").fetchall():
        sales.setdefault(r["cid"], []).append(r)
    dtp = {r["cid"]: r["d"] for r in conn.execute(
        """SELECT s.customer_id cid, SUM(p.amount*(julianday(p.payment_date)-julianday(s.sale_date)))/NULLIF(SUM(p.amount),0) d
           FROM payments p JOIN sales s ON s.transaction_id=p.transaction_id WHERE p.deleted=0 AND s.deleted=0 GROUP BY s.customer_id""").fetchall()}

    profiles = []
    for c in conn.execute("SELECT * FROM customers WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall():
        rows = sales.get(c["id"], [])
        dates = [parse_date(r["sale_date"]) for r in rows if parse_date(r["sale_date"])]
        revenue = sum(float(r["total_sale"]) for r in rows)
        kg = sum(float(r["quantity_kg"]) for r in rows)
        paid_total = sum(float(r["paid"]) for r in rows)      # same basis as the directory: everything paid against their sales
        opening = float(c["opening_balance"] or 0)
        unpaid = [(today - parse_date(r["sale_date"])).days for r in rows
                  if float(r["total_sale"]) - float(r["paid"]) > 0.005 and parse_date(r["sale_date"])]
        billed = revenue + opening
        balance = billed - paid_total
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        usual = (sum(gaps) / len(gaps)) if gaps else None
        last = dates[-1] if dates else None
        silent = (today - last).days if last else None
        paid_share = (sum(1 for r in rows if float(r["paid"]) >= float(r["total_sale"]) - 0.005) / len(rows)) if rows else None
        score, rating = credit_score(dtp.get(c["id"]), max(unpaid) if unpaid else 0, balance, billed, paid_share)

        if not rows:
            segment = "prospect"
        elif len(dates) >= 3 and usual and silent > max(14, usual * 2) and silent <= 240:
            segment = "at_risk"
        elif silent > 120:
            segment = "dormant"
        elif dates and (today - dates[0]).days <= 30 and len(rows) <= 2:
            segment = "new"
        else:
            segment = "active"
        profiles.append({
            "id": c["id"], "name": c["name"], "phone": c["phone"], "location": c["location"], "address": c["address"],
            "type": (c["customer_type"] or "").upper(), "created_at": c["created_at"], "opening": opening,
            "orders": len(rows), "revenue": revenue, "kg": kg, "avg_order": revenue / len(rows) if rows else None,
            "profit": revenue - kg * unit_cost, "billed": billed, "paid": paid_total, "balance": balance,
            "first_sale": dates[0] if dates else None, "last_sale": last, "days_since": silent, "usual_interval": usual,
            "days_to_pay": dtp.get(c["id"]), "oldest_unpaid": max(unpaid) if unpaid else 0, "unpaid_sales": len(unpaid),
            "score": score, "rating": rating, "segment": segment})

    # Champions need the revenue ranking, so they are promoted after the first pass.
    total = sum(p["revenue"] for p in profiles)
    cumulative = 0.0
    for p in sorted(profiles, key=lambda x: x["revenue"], reverse=True):
        before = cumulative / total if total else 1.0
        cumulative += p["revenue"]
        p["share"] = p["revenue"] / total if total else 0.0
        p["abc"] = "A" if p["revenue"] > 0 and before < 0.8 else "B" if p["revenue"] > 0 and before < 0.95 else "C" if p["revenue"] > 0 else "-"
        if p["segment"] in ("active", "new") and p["abc"] == "A" and p["days_since"] is not None and p["days_since"] <= 30 \
                and (p["score"] is None or p["score"] >= 70) and p["orders"] >= 3:
            p["segment"] = "champion"
        elif p["segment"] == "active" and p["orders"] >= 3:
            p["segment"] = "loyal"
    for p in profiles:
        p["segment_label"], p["segment_tone"], p["segment_hint"] = SEGMENTS[p["segment"]]
    return profiles


def segment_summary(profiles):
    out = []
    for key, (label, tone, hint) in SEGMENTS.items():
        members = [p for p in profiles if p["segment"] == key]
        out.append({"key": key, "label": label, "tone": tone, "hint": hint, "count": len(members),
                    "revenue": sum(p["revenue"] for p in members), "owed": sum(max(p["balance"], 0) for p in members)})
    return out


def cohort_revenue(conn, today, months=6):
    """New-customer vs returning-customer revenue per month for the last `months` months."""
    start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    first = {r["cid"]: r["f"][:7] for r in conn.execute(
        "SELECT customer_id cid, MIN(sale_date) f FROM sales WHERE deleted=0 GROUP BY customer_id").fetchall()}
    keys = bucket_keys(start, today, "month")
    new_rev, ret_rev, new_cnt = {k: 0.0 for k, _ in keys}, {k: 0.0 for k, _ in keys}, {k: 0 for k, _ in keys}
    for cid, month in first.items():
        if month in new_cnt:
            new_cnt[month] += 1
    for r in conn.execute("SELECT customer_id cid, substr(sale_date,1,7) m, SUM(total_sale) v FROM sales WHERE deleted=0 AND sale_date >= ? GROUP BY cid, m",
                          (start.isoformat(),)).fetchall():
        if r["m"] in new_rev:
            (new_rev if first.get(r["cid"]) == r["m"] else ret_rev)[r["m"]] += float(r["v"])
    return {"labels": [label for _, label in keys], "new": [round(new_rev[k], 2) for k, _ in keys],
            "returning": [round(ret_rev[k], 2) for k, _ in keys], "new_customers": [new_cnt[k] for k, _ in keys]}


def _insights(d, money):
    items = []

    def add(level, icon, title, text):
        items.append({"level": level, "icon": icon, "title": title, "text": text})

    k, seg = d["kpis"], {s["key"]: s for s in d["segments"]}
    risk = seg["at_risk"]
    if risk["count"]:
        add("warning", "person-exclamation", f"{risk['count']} regular customer{'s' if risk['count'] != 1 else ''} gone quiet",
            f"They have bought {money(risk['revenue'])} in total. A call now is cheaper than winning new buyers.")
    risky_debt = [p for p in d["profiles"] if p["score"] is not None and p["score"] < 45 and p["balance"] > 0.005]
    if risky_debt:
        owed = sum(p["balance"] for p in risky_debt)
        add("danger", "shield-exclamation", f"{len(risky_debt)} risky account{'s' if len(risky_debt) != 1 else ''} owe {money(owed)}",
            ", ".join(p["name"] for p in risky_debt[:3]) + ". Consider cash-only terms until they catch up.")
    if k["receivables"] > 0 and d["top_debtors"]:
        top3 = sum(p["balance"] for p in d["top_debtors"][:3])
        if top3 / k["receivables"] >= 0.6 and len(d["top_debtors"]) > 3:
            add("info", "cash-stack", f"3 customers hold {top3 / k['receivables']:.0%} of your receivables", "Collecting from them fixes most of the problem.")
    if seg["champion"]["count"]:
        add("success", "trophy", f"{seg['champion']['count']} champion customer{'s' if seg['champion']['count'] != 1 else ''}",
            f"They generate {money(seg['champion']['revenue'])} and pay reliably. Keep them happy.")
    if seg["prospect"]["count"]:
        add("info", "person-plus", f"{seg['prospect']['count']} registered customer{'s have' if seg['prospect']['count'] != 1 else ' has'} never bought", "Worth a follow-up call or a clean-up of the list.")
    if seg["dormant"]["count"]:
        add("info", "moon", f"{seg['dormant']['count']} dormant customer{'s' if seg['dormant']['count'] != 1 else ''}", "No purchase in 120+ days.")
    if k["top5_share"] is not None and k["buyers"] > 5 and k["top5_share"] >= 0.7:
        add("info", "pie-chart", f"Top 5 customers = {k['top5_share']:.0%} of lifetime revenue", "Your sales depend on a few relationships.")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items


def build_customer_insights(conn, today, money, unit_cost, profiles=None):
    profiles = profiles if profiles is not None else build_profiles(conn, today, unit_cost)
    buyers = [p for p in profiles if p["orders"]]
    revenue = sum(p["revenue"] for p in profiles)
    kpis = {
        "total": len(profiles), "buyers": len(buyers),
        "active90": sum(1 for p in buyers if p["days_since"] is not None and p["days_since"] <= 90),
        "new30": sum(1 for p in buyers if p["first_sale"] and (today - p["first_sale"]).days <= 30),
        "receivables": sum(max(p["balance"], 0) for p in profiles), "avg_ltv": (revenue / len(buyers)) if buyers else None,
        "repeat_rate": (sum(1 for p in buyers if p["orders"] >= 2) / len(buyers)) if buyers else None,
        "top5_share": (sum(p["revenue"] for p in sorted(buyers, key=lambda x: x["revenue"], reverse=True)[:5]) / revenue) if revenue else None,
        "avg_score": (sum(p["score"] for p in profiles if p["score"] is not None) / max(sum(1 for p in profiles if p["score"] is not None), 1))
        if any(p["score"] is not None for p in profiles) else None}
    data = {"profiles": profiles, "kpis": kpis, "segments": segment_summary(profiles), "cohort": cohort_revenue(conn, today),
            "top_debtors": sorted([p for p in profiles if p["balance"] > 0.005], key=lambda x: x["balance"], reverse=True)[:8],
            "call_list": sorted([p for p in profiles if p["segment"] == "at_risk"], key=lambda x: x["revenue"], reverse=True)[:8],
            "top_customers": sorted(buyers, key=lambda x: x["revenue"], reverse=True)[:8], "has_data": bool(profiles)}
    data["insights"] = _insights(data, money)
    return data


def directory_filters(args, profiles):
    """Apply the directory filters (query string) to in-memory profiles. Returns (filtered, echo)."""
    q = (args.get("q") or "").strip().lower()
    ctype = (args.get("type") or "").strip().upper()
    segment = (args.get("segment") or "").strip()
    owing = (args.get("owing") or "").strip() == "1"
    out = profiles
    if q:
        out = [p for p in out if q in (p["name"] or "").lower() or q in (p["phone"] or "").lower() or q in (p["location"] or "").lower()]
    if ctype:
        out = [p for p in out if p["type"] == ctype]
    if segment in SEGMENTS:
        out = [p for p in out if p["segment"] == segment]
    else:
        segment = ""
    if owing:
        out = [p for p in out if p["balance"] > 0.005]
    return out, {"q": q, "type": ctype, "segment": segment, "owing": "1" if owing else ""}
