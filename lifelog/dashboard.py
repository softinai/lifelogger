"""logs/Dashboard.md — the review surface (P2).

Deliberately plugin-free. Obsidian renders mermaid and markdown natively, so
this works in the vault today, works on the iPhone, and keeps the product from
depending on Dataview or Charts View — which the customer would have to install
and which would break the moment a plugin author moves on.

Everything here is derived from `metrics` and `runs`; regenerating is safe.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import List

from . import classify, config, db, metrics

SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: List[float]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK[3] * len(values)
    step = (high - low) / (len(SPARK) - 1)
    return "".join(SPARK[int((v - low) / step)] for v in values)


def bar(value: float, peak: float, width: int = 24) -> str:
    if peak <= 0:
        return ""
    return "█" * max(1, int(round(value / peak * width)))


def streak(days: List[str]) -> int:
    """Consecutive days logged, counting back from the most recent."""
    if not days:
        return 0
    ordered = sorted(days, reverse=True)
    count, cursor = 1, datetime.date.fromisoformat(ordered[0])
    for value in ordered[1:]:
        day = datetime.date.fromisoformat(value)
        if (cursor - day).days == 1:
            count += 1
            cursor = day
        elif (cursor - day).days > 1:
            break
    return count


def render(con: sqlite3.Connection) -> str:
    logged = [r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM bullets WHERE status='current' ORDER BY day")]
    totals = {r["origin"]: r["n"] for r in con.execute(
        "SELECT origin, count(*) n FROM bullets WHERE status='current' GROUP BY origin")}
    human, ai = totals.get("human", 0), totals.get("ai", 0)

    out = ["# Dashboard", "",
           "*Generated {} from `data/life.db`. Safe to regenerate.*".format(
               datetime.datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M")),

           "", "[[Summary]] · [[{}]]".format(
               (logged[-1] if logged else
                datetime.datetime.now(config.TZ).date().isoformat())[:7]), "",
           "## At a glance", "",
           "| | |", "|---|---|",
           "| Days logged | **{}** |".format(len(logged)),
           "| Current streak | **{}** day(s) |".format(streak(logged)),
           "| Bullets total | **{}** |".format(human + ai),
           "| — mine {} | {} |".format(config.MARK_HUMAN, human),
           "| — model {} | {} |".format(config.MARK_AI, ai),
           "| First / latest | {} → {} |".format(
               logged[0] if logged else "—", logged[-1] if logged else "—"),
           ""]


    per_day = metrics.series(con, "bullets_total", 30)
    if per_day:
        out += ["## Bullets per day", "",
                "`{}`  _{} → {}_".format(
                    sparkline([v for _, v in per_day]),
                    per_day[0][0][5:], per_day[-1][0][5:]),
                "", "| Day | Mine | Model | |", "|---|---:|---:|---|"]
        human_by = dict(metrics.series(con, "bullets_human", 30))
        ai_by = dict(metrics.series(con, "bullets_ai", 30))
        peak = max(v for _, v in per_day)
        for day, total in reversed(per_day[-14:]):
            out.append("| {} | {:.0f} | {:.0f} | {} |".format(
                day, human_by.get(day, 0), ai_by.get(day, 0), bar(total, peak, 16)))
        out.append("")


    domains = metrics.domain_totals(con)
    if domains:
        label_of = classify.labels(con)
        peak = domains[0][1]
        out += ["## Where the progress is", "",
                "| Domain | Bullets | |", "|---|---:|---|"]
        for domain, count in domains:
            out.append("| {} | {} | {} |".format(
                label_of.get(domain, domain), count, bar(count, peak)))
        out += ["", "```mermaid", "pie showData title Bullets by domain"]
        for domain, count in domains[:8]:
            out.append('    "{}" : {}'.format(
                label_of.get(domain, domain).replace('"', ""), count))
        out += ["```", ""]


    hours = metrics.series(con, "active_hours", 30)
    tracked = [(d, v) for d, v in hours if v > 0]
    if tracked:
        out += ["## Tracked screen time", "",
                "`{}`  peak {:.1f} h".format(
                    sparkline([v for _, v in tracked]),
                    max(v for _, v in tracked)),
                "", "<sub>ActivityWatch history begins 2026-08-12; earlier days "
                "have no app data.</sub>", ""]


    pending = classify.proposed(con)
    if pending:
        out += ["## Proposed domains awaiting your approval", ""]
        for row in pending:
            out.append("- [ ] `{}` — **{}** · {}".format(
                row["id"], row["label"], row["description"] or "no reason given"))
        out += ["", "<sub>Approve with: "
                "`/usr/bin/python3 bin/approve.py <id>`</sub>", ""]


    runs = con.execute(
        "SELECT day, status, attempts, started_at FROM runs WHERE job='nightly' "
        "GROUP BY day ORDER BY day DESC LIMIT 10").fetchall()
    if runs:
        icons = {"ok": "✅", "partial": "⚠️", "failed": "❌", "running": "⏳"}
        out += ["## Job health", "", "| Day | Result | Attempts |", "|---|---|---:|"]
        for row in runs:
            out.append("| {} | {} {} | {} |".format(
                row["day"], icons.get(row["status"], "?"), row["status"],
                row["attempts"] or 1))
        out.append("")

    failures = db.recent_failures(con, 5)
    if failures:
        out += ["> [!warning] Recent failed or partial runs", ""]
        for row in failures:
            out.append("> - `{}` **{}** — {}".format(
                row["day"], row["status"], (row["detail"] or "")[:120]))
        out.append("")

    return "\n".join(out)


def write(con: sqlite3.Connection) -> str:
    path = config.LOGS_DIR / "Dashboard.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(con), encoding="utf-8")
    return str(path)
