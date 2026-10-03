"""Activity and security insights derived from the audit log."""
import re
from collections import Counter, defaultdict
from datetime import datetime

from analytics_core import bounds, bucket_keys, grain, parse_date, period_meta, resolve_window

SENSITIVE = ("REVERSED", "DELETED", "RESTORED", "CLEARED", "PERMANENT", "PASSWORD", "ROLE", "USER CREATED", "USER MODIFIED", "USER ENABLED",
             "USER DISABLED", "BACKUP", "RESTORE", "EXPORTED", "ADMIN ACTION")
CATEGORIES = [
    ("Sign-ins", ("LOGIN", "LOGOUT")),
    ("Security", ("SECURITY", "PASSWORD", "LOCKED")),
    ("Reversals & deletions", ("REVERSED", "DELETED", "RESTORED", "CLEARED", "PERMANENT")),
    ("Exports", ("EXPORTED",)),
    ("Admin & backups", ("USER", "BACKUP", "RESTORE", "ADMIN", "ROLE", "PROFILE")),
    ("Records created", ("CREATED",)),
    ("Records edited", ("UPDATED", "EDITED", "CHANGED")),
]
NIGHT = set(range(22, 24)) | set(range(0, 5))
IP = re.compile(r"ip=([^;\s]+)")


def categorize(action):
    a = (action or "").upper()
    for name, words in CATEGORIES:
        if any(w in a for w in words):
            return name
    return "Other"


def is_sensitive(action):
    a = (action or "").upper()
    return any(w in a for w in SENSITIVE) and "LOGIN" not in a


def build_activity(conn, range_key, today, date_from=None, date_to=None):
    key, start, end, prev = resolve_window(range_key, today, conn, date_from, date_to, sources=[("audit_log", "substr(created_at,1,10)", "1=1")])
    lo, hi = bounds(start, end)
    rows = conn.execute("SELECT username, action, reference, details, created_at FROM audit_log WHERE substr(created_at,1,10) BETWEEN ? AND ? ORDER BY id",
                        (lo, hi)).fetchall()
    users = defaultdict(lambda: {"events": 0, "sensitive": 0, "failed": 0, "last": None, "logins": 0, "night": 0})
    cats, actions, per_day, hours = Counter(), Counter(), Counter(), Counter()
    failed_by_ip, failed_by_user = Counter(), Counter()
    cleared, night_events, sensitive_rows = [], 0, []
    for r in rows:
        action, who = (r["action"] or "").upper(), r["username"] or r["reference"] or "system"
        stamp = r["created_at"] or ""
        try:
            hour = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S").hour
        except ValueError:
            hour = None
        u = users[who]
        u["events"] += 1
        u["last"] = stamp[:16]
        cats[categorize(action)] += 1
        actions[action] += 1
        per_day[stamp[:10]] += 1
        if hour is not None:
            hours[hour] += 1
        if "LOGIN SUCCESS" in action:
            u["logins"] += 1
        if "LOGIN FAILED" in action or "LOGIN LOCKED" in action:
            target = r["reference"] or who
            users[target]["failed"] += 1
            failed_by_user[target] += 1
            m = IP.search(r["details"] or "")
            if m:
                failed_by_ip[m.group(1)] += 1
        if is_sensitive(action):
            u["sensitive"] += 1
            sensitive_rows.append({"when": stamp[:16], "user": who, "action": action.title(), "reference": r["reference"] or "", "details": (r["details"] or "")[:90]})
        if hour in NIGHT and "LOGIN" not in action and "LOGOUT" not in action:
            u["night"] += 1
            night_events += 1
        if "CLEARED" in action:
            cleared.append({"when": stamp[:16], "user": who, "action": action.title()})

    gran = grain(start, end) if (end - start).days > 45 else "day"
    keys = bucket_keys(start, end, "day" if gran == "day" else gran)
    if gran == "day":
        series = {"labels": [l for _, l in keys], "events": [per_day.get(k, 0) for k, _ in keys], "granularity": "day"}
    else:
        bucket = Counter()
        for day, n in per_day.items():
            d = parse_date(day)
            if d:
                bucket[(d.strftime("%Y-%m") if gran == "month" else (d.fromordinal(d.toordinal() - d.weekday())).isoformat())] += n
        series = {"labels": [l for _, l in keys], "events": [bucket.get(k, 0) for k, _ in keys], "granularity": gran}
    by_user = sorted(({"user": k, **v} for k, v in users.items()), key=lambda x: x["events"], reverse=True)
    total = len(rows)
    sensitive_total = sum(u["sensitive"] for u in users.values())
    failed_total = sum(failed_by_user.values())
    data = {**period_meta(key, start, end, prev, today), "total": total, "users": by_user[:10], "active_users": sum(1 for u in users.values() if u["events"]),
            "logins": sum(u["logins"] for u in users.values()), "failed_logins": failed_total, "sensitive": sensitive_total,
            "exports": cats.get("Exports", 0), "night_events": night_events, "categories": [{"name": n, "count": c} for n, c in cats.most_common()],
            "top_actions": [{"action": a.title(), "count": c} for a, c in actions.most_common(8)], "hours": [hours.get(h, 0) for h in range(24)],
            "series": series, "failed_by_ip": failed_by_ip.most_common(5), "failed_by_user": failed_by_user.most_common(5),
            "cleared": cleared, "recent_sensitive": sensitive_rows[-8:][::-1], "has_data": total > 0}
    data["insights"] = _insights(data)
    return data


def _insights(d):
    items = []

    def add(level, icon, title, text):
        items.append({"level": level, "icon": icon, "title": title, "text": text})

    if d["cleared"]:
        c = d["cleared"][-1]
        add("danger", "exclamation-octagon", "The audit log was cleared", f"{c['user']} cleared it on {c['when']}. History before that point is gone. Treat this as a red flag.")
    for ip, n in d["failed_by_ip"]:
        if n >= 5:
            add("danger", "shield-exclamation", f"{n} failed sign-ins from {ip}", "That is a pattern consistent with password guessing. Consider blocking that address.")
            break
    for user, n in d["failed_by_user"]:
        if n >= 3:
            add("warning", "person-lock", f"{n} failed sign-ins for '{user}'", "Someone may be guessing this password, or the user has forgotten it.")
            break
    if d["night_events"]:
        add("warning", "moon-stars", f"{d['night_events']} change{'s' if d['night_events'] != 1 else ''} made between 10pm and 5am", "Unusual hours for a trading business. Check they were expected.")
    heavy = [u for u in d["users"] if u["sensitive"] >= 5 and u["sensitive"] / max(u["events"], 1) >= 0.4]
    if heavy:
        add("warning", "person-exclamation", f"{heavy[0]['user']} made {heavy[0]['sensitive']} sensitive changes",
            "Reversals, deletions, exports and admin actions make up a large share of their activity.")
    if d["exports"]:
        add("info", "download", f"{d['exports']} data export{'s' if d['exports'] != 1 else ''}", "Sales, customer and supplier data left the system as files.")
    if d["total"] and not items:
        add("success", "shield-check", "Nothing unusual in this period", f"{d['total']} events from {d['active_users']} user{'s' if d['active_users'] != 1 else ''}.")
    order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(key=lambda i: order[i["level"]])
    return items
