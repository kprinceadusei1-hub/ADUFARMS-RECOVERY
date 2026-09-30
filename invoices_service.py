"""Invoice-center health: how many invoices are paid, what is overdue, and who to chase first."""
from datetime import timedelta

from analytics_core import delta, parse_date

AGE_BANDS = [("0-7 days", 0, 7), ("8-30 days", 8, 30), ("31-60 days", 31, 60), ("Over 60 days", 61, None)]


def status_of(total, paid):
    """Same rule the invoice pages use."""
    if paid > total + 0.005:
        return "OVERPAID"
    if total - paid <= 0.005:
        return "PAID"
    return "PART PAYMENT" if paid > 0 else "UNPAID"


def build_invoice_health(conn, today, money):
    rows = conn.execute(
        """SELECT s.sales_id, COALESCE(i.invoice_number, s.invoice_number) invoice_number, s.sale_date, s.total_sale,
                  c.id cid, c.name customer, c.phone,
                  COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.transaction_id=s.transaction_id AND p.deleted=0),0) paid
           FROM sales s JOIN customers c ON c.id=s.customer_id LEFT JOIN invoices i ON i.transaction_id=s.transaction_id AND i.deleted=0
           WHERE s.deleted=0 ORDER BY s.sale_date, s.id""").fetchall()
    statuses = {k: {"count": 0, "amount": 0.0} for k in ("PAID", "PART PAYMENT", "UNPAID", "OVERPAID")}
    bands = {label: {"count": 0, "amount": 0.0} for label, _, _ in AGE_BANDS}
    follow, overpaid = [], []
    billed = settled = outstanding = 0.0
    month = today.strftime("%Y-%m")
    prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    issued = {month: [0, 0.0], prev_month: [0, 0.0]}
    for r in rows:
        total, paid = float(r["total_sale"]), float(r["paid"])
        st = status_of(total, paid)
        statuses[st]["count"] += 1
        statuses[st]["amount"] += total if st in ("PAID", "OVERPAID") else total - paid
        billed += total
        settled += min(paid, total)
        if (r["sale_date"] or "")[:7] in issued:
            issued[r["sale_date"][:7]][0] += 1
            issued[r["sale_date"][:7]][1] += total
        d = parse_date(r["sale_date"])
        age = (today - d).days if d else 0
        if st in ("UNPAID", "PART PAYMENT"):
            balance = total - paid
            outstanding += balance
            for label, low, high in AGE_BANDS:
                if age >= low and (high is None or age <= high):
                    bands[label]["count"] += 1
                    bands[label]["amount"] += balance
                    break
            follow.append({"sales_id": r["sales_id"], "invoice": r["invoice_number"], "customer": r["customer"], "phone": r["phone"], "cid": r["cid"],
                           "date": r["sale_date"], "age": age, "balance": balance, "status": st})
        elif st == "OVERPAID":
            overpaid.append({"sales_id": r["sales_id"], "customer": r["customer"], "excess": paid - total})
    follow.sort(key=lambda x: (x["age"], x["balance"]), reverse=True)
    n = len(rows)
    over60 = bands["Over 60 days"]
    data = {"count": n, "billed": billed, "settled": settled, "outstanding": outstanding, "avg_invoice": (billed / n) if n else None,
            "paid_share": (statuses["PAID"]["count"] / n) if n else None, "statuses": statuses, "bands": bands,
            "follow_up": follow[:6], "overpaid": overpaid[:5], "overpaid_total": sum(o["excess"] for o in overpaid),
            "this_month": {"count": issued[month][0], "billed": issued[month][1],
                           "delta": delta(issued[month][1], issued[prev_month][1] or None)}}
    tips = []
    if over60["count"]:
        tips.append({"level": "danger", "icon": "alarm", "title": f"{over60['count']} invoice{'s' if over60['count'] != 1 else ''} unpaid for over 60 days",
                     "text": f"{money(over60['amount'])} at risk. Start with the oldest in the list below."})
    if data["overpaid_total"] > 0.005:
        tips.append({"level": "warning", "icon": "arrow-left-right", "title": f"{money(data['overpaid_total'])} overpaid on {len(overpaid)} invoice{'s' if len(overpaid) != 1 else ''}",
                     "text": "Refund the customer or credit it against their next sale."})
    data["tips"] = tips
    return data
