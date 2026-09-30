"""The notification centre: one prioritised alert per real issue, each pointing at the screen that fixes it.

Every check reuses the same engines as the analytics pages, so the numbers always agree with them.
"""
from datetime import timedelta

import audit_service
import customers_service
import dashboard_service
import inventory_service
import invoices_service
import payments_service
import sales_service

SEVERITY = {"danger": 0, "warning": 1, "info": 2, "success": 3}


def build_alerts(conn, today, money, stock_kg, unit_cost, low_stock_kg, daily_rate, can):
    """`can` is a dict of permission booleans so people are only told about areas they may open."""
    items = []

    def add(kind, icon, title, body, href=None, label="Open"):
        items.append({"kind": kind, "icon": icon, "title": title, "body": body, "href": href, "label": label})

    # ---- stock
    if can.get("inventory"):
        rec = inventory_service.reconcile(conn)
        age = inventory_service.stock_age(conn, today)
        cover = (stock_kg / daily_rate) if daily_rate > 0 and stock_kg > 0 else None
        if stock_kg <= 0:
            add("danger", "box-seam", "Stock depleted", "There is no maize left to sell.", "purchases", "Record a purchase")
        elif stock_kg <= low_stock_kg:
            add("warning", "exclamation-triangle", "Stock is low", f"{stock_kg:,.0f} KG left (alert level {low_stock_kg:,.0f} KG).", "purchases", "Plan a purchase")
        elif cover is not None and cover < 14:
            add("warning", "hourglass-split", f"About {cover:.0f} days of stock left", f"At {daily_rate:,.0f} KG/day you run out around {(today + timedelta(days=int(cover))).strftime('%d %b')}.", "purchases", "Plan a purchase")
        if not rec["ok"]:
            add("danger", "exclamation-octagon", f"Stock ledger is out by {abs(rec['difference']):,.1f} KG", "Purchases minus sales does not match the movement ledger. Records may have been edited outside the normal screens.", "stock", "Investigate")
        if age["bands"].get("Over 60 days", 0) > 0.5:
            add("warning", "hourglass-bottom", f"{age['bands']['Over 60 days']:,.0f} KG has been stored over 60 days", "Old grain loses quality. Sell it first.", "stock", "See stock age")
        elif age["old_share"] > 0.3:
            add("info", "hourglass-bottom", f"{age['old_share']:.0%} of your stock is over 30 days old", "Move the oldest lots first.", "stock", "See stock age")

    # ---- money owed
    if can.get("payments"):
        rec_data = dashboard_service.receivables(conn, today, today, today)
        total = rec_data["total"]
        over60 = rec_data["buckets"].get("Over 60 days", 0.0)
        if over60 > 0.005:
            n = sum(1 for d in rec_data["debtors"] if d["oldest"] > 60)
            add("danger", "cash-stack", f"{money(over60)} is over 60 days overdue", f"Across {n} customer{'s' if n != 1 else ''}. Chase these first.", "customers", "See debtors")
        watch = payments_service.aging_watch(conn, today)
        if watch["about_to_age"] > 0.005:
            add("warning", "alarm", f"{money(watch['about_to_age'])} will pass 60 days within two weeks", f"{watch['about_to_age_count']} sale{'s' if watch['about_to_age_count'] != 1 else ''}. Collect before it turns into bad debt.", "payments", "Record / chase")
        if total > 0.005:
            debtors = rec_data["debtors"]
            names = ", ".join(d["name"] for d in debtors[:3])
            add("info", "hourglass-split", f"{len(debtors)} customer{'s' if len(debtors) != 1 else ''} owe {money(total)}", f"Largest: {names}.", "customers", "Open customers")
        health = invoices_service.build_invoice_health(conn, today, money)
        if health["overpaid_total"] > 0.005:
            add("warning", "arrow-left-right", f"{money(health['overpaid_total'])} overpaid by customers", "Refund it or credit it against their next sale.", "invoice", "See invoices")

    # ---- customers & pricing
    if can.get("customers"):
        profiles = customers_service.build_profiles(conn, today, unit_cost)
        quiet = [p for p in profiles if p["segment"] == "at_risk"]
        if quiet:
            top = max(quiet, key=lambda p: p["revenue"])
            add("warning", "person-exclamation", f"{len(quiet)} regular customer{'s have' if len(quiet) != 1 else ' has'} gone quiet",
                f"{top['name']} usually buys every ~{top['usual_interval']:.0f} days but has been silent for {top['days_since']}.", "customers", "See who")
        risky = [p for p in profiles if p["score"] is not None and p["score"] < 45 and p["balance"] > 0.005]
        if risky:
            add("warning", "shield-exclamation", f"{len(risky)} risky account{'s' if len(risky) != 1 else ''} owe {money(sum(p['balance'] for p in risky))}", ", ".join(p["name"] for p in risky[:3]) + ".", "customers", "Review credit")
    if can.get("sales") and unit_cost > 0:
        month_start = today.replace(day=1)
        risky_sales = sales_service.risky_sales(conn, month_start, today, unit_cost)
        if risky_sales["below_cost"]:
            add("danger", "exclamation-octagon", f"{risky_sales['below_cost']} sale{'s' if risky_sales['below_cost'] != 1 else ''} this month priced below cost",
                f"About {money(risky_sales['lost_value'])} lost on those loads.", "sales", "Review sales")

    # ---- security (admins only)
    if can.get("audit"):
        a = audit_service.build_activity(conn, "30d", today, None, None)
        if a["cleared"]:
            add("danger", "shield-exclamation", "The audit log was cleared", f"By {a['cleared'][-1]['user']} on {a['cleared'][-1]['when']}.", "audit_logs", "Open audit log")
        if a["failed_logins"] >= 5:
            add("warning", "person-lock", f"{a['failed_logins']} failed sign-ins in the last 30 days", "Check the activity insights for repeated attempts.", "audit_logs", "Open audit log")

    items.sort(key=lambda i: SEVERITY[i["kind"]])
    return items
