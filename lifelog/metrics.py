"""Deterministic daily numbers. No model involved — these must be reproducible.

Recomputable at any time: the day's rows are deleted and rebuilt from `events`
and `bullets`, which are the source of truth.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Tuple

from . import classify


def compute_day(con: sqlite3.Connection, day: str) -> Dict[str, float]:
    con.execute("DELETE FROM metrics WHERE day=?", (day,))

    rows: List[Tuple[str, str, float, str]] = []

    counts = {r["origin"]: r["n"] for r in con.execute(
        "SELECT origin, count(*) n FROM bullets WHERE day=? AND status='current' "
        "GROUP BY origin", (day,))}
    rows.append(("all", "bullets_human", float(counts.get("human", 0)), "count"))
    rows.append(("all", "bullets_ai", float(counts.get("ai", 0)), "count"))
    rows.append(("all", "bullets_total", float(sum(counts.values())), "count"))

    for row in con.execute(
            "SELECT source, count(*) n FROM events WHERE day=? GROUP BY source", (day,)):
        rows.append(("all", "events_" + row["source"], float(row["n"]), "count"))

    active = con.execute(
        "SELECT COALESCE(sum(duration_s),0) s FROM events "
        "WHERE day=? AND source='activitywatch' AND kind='app_use'", (day,)).fetchone()["s"]
    rows.append(("all", "active_hours", round(active / 3600.0, 2), "hours"))

    per_domain: Dict[str, int] = {}
    for row in con.execute(
            "SELECT category_id, count(*) n FROM bullets "
            "WHERE day=? AND status='current' GROUP BY category_id", (day,)):
        key = classify.canonical(con, row["category_id"]) or "other"
        per_domain[key] = per_domain.get(key, 0) + row["n"]
    for domain, n in per_domain.items():
        rows.append((domain, "bullets", float(n), "count"))
    rows.append(("all", "domains_touched", float(len(per_domain)), "count"))

    con.executemany(
        "INSERT OR REPLACE INTO metrics(day,domain,metric,value,unit) VALUES (?,?,?,?,?)",
        [(day, d, m, v, u) for d, m, v, u in rows])
    con.commit()
    return {m: v for d, m, v, _ in rows if d == "all"}


def series(con: sqlite3.Connection, metric: str, days: int = 30) -> List[Tuple[str, float]]:
    return [(r["day"], r["value"]) for r in con.execute(
        "SELECT day, value FROM metrics WHERE domain='all' AND metric=? "
        "ORDER BY day DESC LIMIT ?", (metric, days))][::-1]


def domain_totals(con: sqlite3.Connection, since: str = "0000-00-00") -> List[Tuple[str, int]]:
    rows = con.execute(
        "SELECT domain, sum(value) v FROM metrics "
        "WHERE metric='bullets' AND domain != 'all' AND day >= ? "
        "GROUP BY domain ORDER BY v DESC", (since,)).fetchall()
    return [(r["domain"], int(r["v"])) for r in rows]
