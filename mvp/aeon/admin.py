"""Admin integrity + moderation views (gated by accounts.is_admin / AEON_ADMIN_USERS).

Read-side helpers only — mutations go through db (set_user_flags, delete_artifact).
This is the ONE place that surfaces the otherwise-secret honeypot accuracy, so it must
never be reachable without an admin session (enforced in app.py).
"""
from __future__ import annotations

from . import accounts, arena, db


def evaluators():
    """Per-account integrity: honeypot accuracy, current eligibility (>= TRUST_ACCURACY),
    contribution, and ban status. Accounts needing attention (banned, or below the bar)
    sort to the top."""
    acc = db.honeypot_accuracy()
    admins = accounts.admin_usernames()
    rows = []
    for u in db.all_users():
        s = db.user_stats(u["id"])
        a = acc.get(u["id"], {"passed": 0, "failed": 0, "adjudicated": 0, "accuracy": None})
        eligible = a["adjudicated"] >= 1 and (a["accuracy"] or 0) >= arena.TRUST_ACCURACY
        rows.append({
            "id": u["id"],
            "username": u["username"],
            "status": u["status"],
            "admin": u["username"] in admins,
            "created_at": u["created_at"],
            "votes": s["votes"],
            "real_votes": s["real_votes"],
            "passed": a["passed"],
            "failed": a["failed"],
            "adjudicated": a["adjudicated"],
            "accuracy": a["accuracy"],
            "eligible": eligible,
        })
    # surface problems first: banned, then below-bar, then by ascending accuracy
    rows.sort(key=lambda r: (
        r["status"] == "active",
        r["eligible"],
        r["accuracy"] if r["accuracy"] is not None else 2,
        -r["votes"],
    ))
    return rows


def summary():
    rows = evaluators()
    return {
        "threshold": arena.TRUST_ACCURACY,
        "total": len(rows),
        "eligible": sum(1 for r in rows if r["eligible"] and r["status"] == "active"),
        "below_bar": sum(1 for r in rows if not r["eligible"] and r["adjudicated"] >= 1 and r["status"] == "active"),
        "banned": sum(1 for r in rows if r["status"] != "active"),
        "evaluators": rows,
    }
