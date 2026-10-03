"""Verified, database-backed management assistant for ADUFARMS."""
from __future__ import annotations

from datetime import date, timedelta

import analytics_service
import customers_service
import inventory_service
import payments_service
import purchases_service
import sales_service


def _money(value):
    return f"GH₵{float(value or 0):,.2f}"


def _cogs(conn):
    """Average purchase cost per KG and stock on hand, the same basis the dashboard uses."""
    r = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0) recv, COALESCE(SUM(total_cost),0) cost FROM purchases WHERE deleted=0").fetchone()
    sold = float(conn.execute("SELECT COALESCE(SUM(quantity_kg),0) v FROM sales WHERE deleted=0").fetchone()[0])
    recv, cost = float(r["recv"]), float(r["cost"])
    return {"unit_cost": (cost / recv) if recv > 0 else 0.0, "available_kg": recv - sold}


def _has(text, *words):
    return any(w in text for w in words)


def smart_answer(conn, text: str, today: date):
    """Questions an owner actually asks. Each answer is computed by the same engines as the analytics pages."""
    cogs = _cogs(conn)
    cost, stock = cogs["unit_cost"], cogs["available_kg"]

    if _has(text, "health", "how are we doing", "how is the business", "how is business", "overall performance"):
        a = analytics_service.build_analytics(conn, "90d", today, _money, cogs)
        h = a["health"]
        if h["score"] is None:
            return {"title": "Business Health", "answer": "There is not enough activity in the last 90 days to score the business yet.", "facts": []}
        return {"title": "Business Health Score", "answer": f"Your business health is {h['score']}/100 ({h['grade']}) over the last 90 days.",
                "facts": [f"{c['name']}: {c['score']}/100 - {c['note']}" for c in h["components"]]}

    if _has(text, "who should i call", "follow up", "follow-up", "chase", "gone quiet", "at risk", "at-risk", "who to call", "churn"):
        profiles = customers_service.build_profiles(conn, today, cost)
        quiet = sorted([p for p in profiles if p["segment"] == "at_risk"], key=lambda p: p["revenue"], reverse=True)
        owing = sorted([p for p in profiles if p["balance"] > 0.005], key=lambda p: (p["oldest_unpaid"], p["balance"]), reverse=True)
        facts = [f"Quiet regular: {p['name']} - usually buys every ~{p['usual_interval']:.0f} days, silent {p['days_since']} days ({_money(p['revenue'])} lifetime)" for p in quiet[:4]]
        facts += [f"Owes money: {p['name']} - {_money(p['balance'])}" + (f", oldest {p['oldest_unpaid']} days" if p["oldest_unpaid"] else "") for p in owing[:5]]
        return {"title": "Who To Call", "answer": f"{len(quiet)} regular customer(s) have gone quiet and {len(owing)} customer(s) owe money. Start with the list below.",
                "facts": facts or ["Everyone is buying and paid up."]}

    if (_has(text, "supplier", "agent") and _has(text, "best", "cheap", "compare", "reliab", "loss", "lose", "short", "rank", "worst")) \
            or _has(text, "transit loss", "lost in transit", "shrink"):
        pa = purchases_service.build_purchase_analytics(conn, "all", today, _money, stock, 1000.0, purchases_service.sales_pace(conn, today))
        if not pa["suppliers"]:
            return {"title": "Supplier Comparison", "answer": "No purchases are recorded yet.", "facts": []}
        best = max(pa["suppliers"], key=lambda s: s["score"])
        return {"title": "Supplier Comparison", "answer": f"{best['name']} ranks first (grade {best['grade']}, {best['score']}/100) on price and reliability. "
                f"In total {_money(pa['kpis']['loss_value'])} was paid for maize that never arrived.",
                "facts": [f"{s['name']}: grade {s['grade']} - {_money(s['landed'] or 0)}/KG real cost, {s['loss_pct']:.1%} lost in transit, {_money(s['spend'])} spent" for s in pa["suppliers"][:6]]}

    if _has(text, "stock age", "old stock", "how old", "fifo", "spoil", "sitting"):
        age = inventory_service.stock_age(conn, today)
        if age["total"] <= 0.5:
            return {"title": "Stock Age", "answer": "There is no stock in store.", "facts": []}
        return {"title": "How Old Is Your Stock", "answer": f"Stock on hand averages {age['avg_age']:.0f} days old; the oldest lot is {age['oldest_days']} days. {age['old_share']:.0%} is over 30 days old.",
                "facts": [f"{label}: {kg:,.0f} KG" for label, kg in age["bands"].items()] + [f"Lot {l['purchase_id']}: {l['kg']:,.0f} KG from {l['supplier']}, {l['age']} days old" for l in age["lots"][:3]]}

    if _has(text, "reorder", "restock", "run out", "running out", "when should i buy", "how much should i buy", "how long will"):
        plan = purchases_service.reorder_plan(stock, purchases_service.sales_pace(conn, today), 1000.0, cost or None, today)
        if not plan["has_pace"]:
            return {"title": "Restock Advice", "answer": f"You hold {stock:,.0f} KG, but there are no recent sales to measure your pace yet.", "facts": []}
        advice = (f"To hold {plan['target_days']} days of cover, buy about {plan['suggested_kg']:,.0f} KG" + (f" (around {_money(plan['budget'])})" if plan["budget"] else "") + "."
                  if plan["suggested_kg"] > 0 else f"That is already more than the {plan['target_days']} days of cover you aim for, so no purchase is needed yet.")
        return {"title": "Restock Advice", "answer": f"At {plan['daily_rate']:,.0f} KG per day your {stock:,.0f} KG lasts about {plan['cover_days']:.0f} days. {advice}",
                "facts": [f"Reorder by: {plan['reorder_by']}", f"Stock on hand: {stock:,.0f} KG", f"Selling pace: {plan['daily_rate']:,.0f} KG/day"]}

    if _has(text, "forecast", "predict", "projection", "next month", "next 4 weeks", "next four weeks", "expect to sell", "will we sell"):
        f = analytics_service._forecast(conn, today, None)
        if not f["ready"]:
            return {"title": "Sales Outlook", "answer": "A forecast needs sales in at least 4 of the last 8 weeks. Keep recording sales and it will appear.", "facts": []}
        trend = f"{f['trend_pct']:+.0f}% versus the last four weeks" if f["trend_pct"] is not None else "trend unavailable"
        return {"title": "Sales Outlook", "answer": f"On the current trend you can expect about {_money(f['projected_total'])} in sales over the next 4 weeks ({trend}).",
                "facts": [f"Week of {l}: {_money(v)}" for l, v in zip(f["future_labels"], f["future"])] + ["This is a straight-line trend through recent weeks, not a promise."]}

    if _has(text, "slow pay", "late pay", "days to pay", "how fast", "pay slowly", "collection speed", "collect"):
        pa = payments_service.build_payment_analytics(conn, "90d", today, _money)
        k = pa["kpis"]
        if k["days_to_pay"] is None:
            return {"title": "Collections", "answer": "No payments in the last 90 days, so there is nothing to measure yet.", "facts": []}
        return {"title": "How Fast Customers Pay", "answer": f"Customers pay in about {k['days_to_pay']:.0f} days on average. You collected {_money(k['collected'])} against {_money(k['billed'])} billed in the last 90 days.",
                "facts": [f"Slow payer: {s['name']} - usually {s['days']:.0f} days, owes {_money(s['owed'])}" for s in pa["slow"][:5]] or ["No habitual slow payers with open balances."]}

    if _has(text, "what price", "selling price", "price should", "pricing", "how much should i sell", "sell at"):
        p = sales_service.pricing_assistant(conn, today, cost)
        if p["cost"] <= 0:
            return {"title": "Pricing Guide", "answer": "Record a purchase first so I can work out your cost per KG.", "facts": []}
        return {"title": "Pricing Guide", "answer": f"Your average cost is {_money(p['cost'])}/KG. To earn a 15% margin, sell at about {_money(p['cost'] / 0.85)}/KG.",
                "facts": [f"{t['margin']:.0%} margin: {_money(t['price'])}/KG" for t in p["targets"]] + ([f"Last 30 days you averaged {_money(p['recent_avg'])}/KG"] if p["recent_avg"] else [])}

    if _has(text, "profitable", "most profit", "best margin", "highest margin"):
        profiles = [p for p in customers_service.build_profiles(conn, today, cost) if p["revenue"] > 0]
        if not profiles:
            return {"title": "Most Profitable Customers", "answer": "No sales recorded yet.", "facts": []}
        ranked = sorted(profiles, key=lambda p: p["profit"], reverse=True)
        return {"title": "Most Profitable Customers", "answer": f"{ranked[0]['name']} has earned you the most estimated profit: {_money(ranked[0]['profit'])}.",
                "facts": [f"{p['name']}: {_money(p['profit'])} profit on {_money(p['revenue'])} sales" for p in ranked[:6]]}
    return None


def answer_question(conn, question: str) -> dict:
    """Answer supported business questions from current database facts only."""
    text = (question or "").strip().lower()
    today = date.today()
    if not text:
        return {"title": "Ask ADUFARMS", "answer": "Enter a business question to search verified ADUFARMS records.", "facts": []}

    smart = smart_answer(conn, text, today)
    if smart is not None:
        return smart

    # Stock & Inventory Queries (KG + Bags)
    if ("stock" in text or "inventory" in text or "maize" in text or "bag" in text) and not ("top" in text or "purchase" in text):
        purchased = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0) FROM purchases WHERE deleted=0").fetchone()[0]
        sold = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM sales WHERE deleted=0").fetchone()[0]
        avail = max(float(purchased) - float(sold), 0.0)
        bags_50 = round(avail / 50)
        bags_100 = round(avail / 100)
        return {
            "title": "Current Stock Position",
            "answer": f"Current available maize stock is {avail:,.2f} KG (~{bags_50:,} bags of 50 KG, or ~{bags_100:,} bags of 100 KG).",
            "facts": [
                f"Total Inbound Purchased: {float(purchased):,.2f} KG",
                f"Total Dispatched Sold: {float(sold):,.2f} KG",
                f"Remaining Stock: {avail:,.2f} KG",
                f"Standard 50kg Bags: ~{bags_50:,}",
                f"Jumbo 100kg Bags: ~{bags_100:,}"
            ]
        }

    # Outstanding Balances / Debtors
    if "owe" in text or "outstanding" in text or "unpaid" in text or "debt" in text or "balance" in text:
        rows = conn.execute("""SELECT c.name, COALESCE(SUM(s.total_sale),0)+c.opening_balance billed,
            COALESCE((SELECT SUM(p.amount) FROM payments p JOIN sales ps ON ps.transaction_id=p.transaction_id
                      WHERE ps.customer_id=c.id AND p.deleted=0),0) paid
            FROM customers c LEFT JOIN sales s ON s.customer_id=c.id AND s.deleted=0
            WHERE c.active=1 GROUP BY c.id ORDER BY (billed-paid) DESC""").fetchall()
        balances = [(r["name"], max(float(r["billed"]) - float(r["paid"]), 0)) for r in rows]
        balances = [(name, value) for name, value in balances if value > 0.005]
        if not balances:
            return {"title": "Outstanding Balances", "answer": "All customer accounts are fully settled. No outstanding balances.", "facts": []}
        total_debt = sum(val for _, val in balances)
        return {
            "title": "Outstanding Customer Balances",
            "answer": f"{len(balances)} customer(s) have outstanding balances totaling {_money(total_debt)}.",
            "facts": [f"{name}: {_money(value)}" for name, value in balances[:10]]
        }

    # Purchases / Procurement / Suppliers
    if "purchase" in text or "supplier" in text or "procurement" in text or "intake" in text:
        p_data = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0), COALESCE(SUM(total_cost),0), COUNT(DISTINCT local_agent) FROM purchases WHERE deleted=0").fetchone()
        qty, cost, suppliers = float(p_data[0]), float(p_data[1]), int(p_data[2])
        return {
            "title": "Maize Procurement Summary",
            "answer": f"A total of {qty:,.2f} KG of maize has been procured across {suppliers} supplier(s) for a total acquisition cost of {_money(cost)}.",
            "facts": [
                f"Total Inbound Maize: {qty:,.2f} KG",
                f"Total Purchase Cost: {_money(cost)}",
                f"Active Suppliers: {suppliers}",
                f"Average Cost/KG: {_money(cost/qty if qty > 0 else 0)}"
            ]
        }

    # Profit / Financial Position / Revenue
    if "profit" in text or "revenue" in text or "expense" in text or "financial" in text:
        sales_val = conn.execute("SELECT COALESCE(SUM(total_sale),0) FROM sales WHERE deleted=0").fetchone()[0]
        purchase_data = conn.execute("SELECT COALESCE(SUM(quantity_received_kg),0), COALESCE(SUM(total_cost),0) FROM purchases WHERE deleted=0").fetchone()
        sold_qty = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM sales WHERE deleted=0").fetchone()[0]
        received_qty, acquisition_cost = float(purchase_data[0]), float(purchase_data[1])
        cost_val = float(sold_qty) * (acquisition_cost / received_qty if received_qty > 0 else 0)
        paid_val = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE deleted=0").fetchone()[0]
        gross_profit = float(sales_val) - float(cost_val)
        return {
            "title": "Financial Performance Overview",
            "answer": f"Total sales revenue is {_money(sales_val)} with estimated cost of goods sold of {_money(cost_val)}, resulting in an estimated gross profit of {_money(gross_profit)}.",
            "facts": [
                f"Gross Sales Revenue: {_money(sales_val)}",
                f"Estimated Cost of Goods Sold: {_money(cost_val)}",
                f"Payments Collected: {_money(paid_val)}",
                f"Estimated Gross Margin: {_money(gross_profit)}"
            ]
        }

    # Time-based Activity (Today / Yesterday / Month)
    if "today" in text or "yesterday" in text or "month" in text or "week" in text:
        if "yesterday" in text:
            target = today - timedelta(days=1)
            start = end = target.isoformat()
            label = "yesterday"
        elif "month" in text:
            start = today.replace(day=1).isoformat()
            end = today.isoformat()
            label = "this month"
        elif "week" in text:
            start = (today - timedelta(days=7)).isoformat()
            end = today.isoformat()
            label = "the past 7 days"
        else:
            start = end = today.isoformat()
            label = "today"
        sales = conn.execute("SELECT COALESCE(SUM(total_sale),0), COALESCE(SUM(quantity_kg),0), COUNT(*) FROM sales WHERE deleted=0 AND sale_date BETWEEN ? AND ?", (start, end)).fetchone()
        payments = conn.execute("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM payments WHERE deleted=0 AND payment_date BETWEEN ? AND ?", (start, end)).fetchone()
        purchases = conn.execute("SELECT COALESCE(SUM(total_cost),0), COALESCE(SUM(quantity_received_kg),0), COUNT(*) FROM purchases WHERE deleted=0 AND purchase_date BETWEEN ? AND ?", (start, end)).fetchone()
        return {
            "title": f"Operations Summary for {label.title()}",
            "answer": f"Sales: {_money(sales[0])} ({sales[2]} sale(s), {float(sales[1]):,.2f} KG). Payments: {_money(payments[0])} ({payments[1]} transaction(s)). Purchases: {_money(purchases[0])} ({purchases[2]} intake(s)).",
            "facts": [
                f"Maize Dispatched: {float(sales[1]):,.2f} KG",
                f"Maize Received: {float(purchases[1]):,.2f} KG",
                f"Sales Total: {_money(sales[0])}",
                f"Payments Collected: {_money(payments[0])}",
                f"Period: {start} to {end}"
            ]
        }

    # Top Customers
    if "customer" in text and ("most" in text or "top" in text or "best" in text or "largest" in text):
        rows = conn.execute("""SELECT c.name, COALESCE(SUM(s.total_sale),0) total, COALESCE(SUM(s.quantity_kg),0) qty
            FROM customers c JOIN sales s ON s.customer_id=c.id AND s.deleted=0
            GROUP BY c.id ORDER BY total DESC LIMIT 5""").fetchall()
        return {
            "title": "Top Customers by Volume & Revenue",
            "answer": "Top customer accounts ranked by total billed sales volume:",
            "facts": [f"{r['name']}: {_money(r['total'])} ({float(r['qty']):,.2f} KG)" for r in rows] or ["No sales data is available yet."]
        }

    return {
        "title": "Verified Agribusiness Assistant",
        "answer": "I can answer specific operational questions from your current database records. Try asking one of the suggestions below.",
        "facts": [
            "Current stock in KG and standard bags",
            "Outstanding customer balances and debtor totals",
            "Today's, yesterday's, or monthly sales and payments",
            "Total maize procurement and supplier acquisition costs",
            "Estimated gross profit and financial position",
            "Top customers ranked by sales volume",
            "\"How is the business doing?\" (health score)",
            "\"Who should I call?\" (quiet customers and debtors)",
            "\"Which supplier is best?\" (price, reliability, transit loss)",
            "\"How old is my stock?\" and \"When should I reorder?\"",
            "\"What will we sell next month?\" (forecast)",
            "\"What price should I sell at?\" and \"How fast do customers pay?\""
        ]
    }
