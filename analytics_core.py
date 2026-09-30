"""Shared building blocks for every analytics module (dashboard, analytics, purchases, sales, customers, ...).

Keeping period maths, bucketing and change calculations here means each module only contains its own business rules.
"""
from datetime import datetime, timedelta

LOW_DATE = "0000-01-01"
RANGES = [("30d", "30 days"), ("90d", "90 days"), ("6m", "6 months"), ("12m", "12 months"), ("all", "All time")]
DEFAULT_RANGE = "90d"
DAYS = {"30d": 30, "90d": 90, "6m": 182, "12m": 365}


def parse_date(text):
    try:
        return datetime.strptime((text or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def bounds(start, end):
    """ISO strings usable in `BETWEEN ? AND ?`; a missing start means 'from the beginning'."""
    return (start.isoformat() if start else LOW_DATE), end.isoformat()


def delta(current, previous):
    """Percent change vs the previous period, or None when there is nothing meaningful to compare."""
    if current is None or previous is None or not previous:
        return None
    change = (current - previous) / previous * 100
    return {"pct": abs(round(change, 1)), "direction": "up" if change > 0.05 else "down" if change < -0.05 else "flat"}


def deltas(now, before, names):
    """{name: delta} for several headline figures at once; `before` may be None (no comparison window)."""
    return {name: delta(now.get(name), before.get(name) if before else None) for name in names}


def grain(start, end):
    """Weekly buckets for short windows, monthly for long ones."""
    return "week" if (end - start).days + 1 <= 60 else "month"


def bucket_keys(start, end, gran):
    """[(key, label)] for every bucket between start and end, including empty ones."""
    keys, cur = [], start
    if gran == "week":
        cur = start - timedelta(days=start.weekday())
    if gran == "month":
        cur = start.replace(day=1)
    while cur <= end:
        if gran == "month":
            keys.append((cur.strftime("%Y-%m"), cur.strftime("%b %Y")))
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            keys.append((cur.isoformat(), cur.strftime("%d %b")))
            cur += timedelta(days=7 if gran == "week" else 1)
    return keys


def bucket_sql(gran, column):
    """SQLite expression that maps a date column to the bucket keys produced by bucket_keys()."""
    return {"day": column, "week": f"date({column}, 'weekday 0', '-6 days')", "month": f"substr({column},1,7)"}[gran]


def grouped(conn, gran, sql_body, start, end, date_col):
    """Run `sql_body` (which must contain {k} for the bucket expression and two ? for the window) -> {key: row}."""
    rows = conn.execute(sql_body.format(k=bucket_sql(gran, date_col)), (start.isoformat(), end.isoformat())).fetchall()
    return {r["k"]: r for r in rows}


def first_activity_date(conn, sources):
    """Earliest date across several (table, column, where) sources, or None."""
    union = " UNION ALL ".join(f"SELECT MIN({col}) d FROM {table} WHERE {where}" for table, col, where in sources)
    return conn.execute(f"SELECT MIN(d) d FROM ({union})").fetchone()["d"]


def resolve_window(key, today, conn, date_from=None, date_to=None, sources=None):
    """Return (key, start, end, previous_window_or_None).

    A valid from/to pair wins and yields key 'custom' (clamped to today). `sources` tells the 'all' preset where
    your data starts; it defaults to sales, purchases and payments.
    """
    start_c, end_c = parse_date(date_from), parse_date(date_to)
    if start_c and end_c and start_c <= end_c:
        end_c = min(end_c, today)
        start_c = min(start_c, end_c)
        length = (end_c - start_c).days + 1
        prev_end = start_c - timedelta(days=1)
        return "custom", start_c, end_c, (prev_end - timedelta(days=length - 1), prev_end)
    if key not in dict(RANGES):
        key = DEFAULT_RANGE
    if key == "all":
        sources = sources or [("sales", "sale_date", "deleted=0"), ("purchases", "purchase_date", "deleted=0"),
                              ("payments", "payment_date", "deleted=0")]
        return key, parse_date(first_activity_date(conn, sources)) or today - timedelta(days=29), today, None
    start = today - timedelta(days=DAYS[key] - 1)
    prev_end = start - timedelta(days=1)
    return key, start, today, (prev_end - timedelta(days=DAYS[key] - 1), prev_end)


def period_meta(key, start, end, prev, today):
    """The period fields every analytics page header needs (keeps the modules from each rebuilding this dict)."""
    return {"range_key": key, "ranges": RANGES, "date_from": start.isoformat(), "date_to": end.isoformat(),
            "max_date": today.isoformat(), "period_text": f"{start.strftime('%d %b %Y')} - {end.strftime('%d %b %Y')}",
            "compare_text": f"vs {prev[0].strftime('%d %b')} - {prev[1].strftime('%d %b')}" if prev else None}
