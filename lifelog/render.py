"""Render the DB into monthly markdown: logs/2026/08-August.md

The markdown is a VIEW. Regenerating it never loses anything, because every
bullet lives in `life.db`. Days are newest-first so the file opens on today.

Provenance is visible at a glance:
  🧍 the owner wrote it   🤖 the model wrote it
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import List, Optional

from . import bullets as bullets_mod
from . import classify, config, db

HEADER = """# {year} · {month}

*Generated from `data/life.db` — this file is a view, safe to regenerate.*
*{human} = written by me · {ai} = written by the local model. My bullets are never altered.*

"""


def _indent_block(text: str) -> str:
    """Normalise the diary's mixed tab/space nesting to 2-space markdown levels.

    Raw indents of 5+ spaces render as a code block instead of a nested list,
    so depths are mapped to levels rather than copied literally.
    """
    lines = text.splitlines()
    if not lines:
        return ""
    depths = sorted({len(l) - len(l.lstrip()) for l in lines[1:] if l.strip()})
    level_of = {depth: i for i, depth in enumerate(depths)}
    out = [lines[0].strip()]
    for line in lines[1:]:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip())
        out.append("  " * (level_of.get(depth, 0) + 1) + line.strip())
    return "\n".join(out)


def _short_reason(run) -> str:
    """A one-line cause, not the whole gather dump."""
    import json as _json
    try:
        detail = _json.loads(run["detail"] or "{}")
    except ValueError:
        return "see runs table"
    if "fatal" in detail:
        return str(detail["fatal"])
    model = detail.get("model") or {}
    if model.get("error"):
        return str(model["error"])
    broken = {name: d["error"] for name, d in detail.items()
              if isinstance(d, dict) and d.get("error") not in (None, "unavailable")}
    if broken:
        return "; ".join("{}: {}".format(k, v) for k, v in broken.items())
    return "see runs table"


def render_day(con: sqlite3.Connection, day: str) -> str:
    date = datetime.date.fromisoformat(day)
    parts = ["## {} ({})".format(day, date.strftime("%A"))]

    run = db.last_run_for_day(con, day)
    if run and run["status"] in ("failed", "partial"):
        parts.append(
            "> ⚠️ **Run {}** after {} attempt(s) — {}. Entry built from raw signals; "
            "re-run with `./bin/nightly.sh --day {} --force`.".format(
                run["status"], run["attempts"] or 1, _short_reason(run), day))

    overview = db.latest_overview(con, "day", day)
    if overview:
        parts.append("")
        parts.append(overview)

    rows = bullets_mod.for_day(con, day)
    if not rows:
        parts.append("")
        parts.append("*No entries recorded.*")
        return "\n".join(parts) + "\n"

    label_of = classify.labels(con)
    grouped = {}
    for row in rows:
        cat = classify.canonical(con, row["category_id"]) or "other"
        grouped.setdefault(cat, []).append(row)

    counts = db.source_counts(con, day)
    ordered = sorted(grouped.items(),
                     key=lambda kv: (0 if any(r["origin"] == "human" for r in kv[1]) else 1,
                                     label_of.get(kv[0], kv[0])))
    for cat, items in ordered:
        parts.append("")
        parts.append("**{}**".format(label_of.get(cat, cat.replace("_", " ").title())))
        for row in items:
            mark = config.MARK_HUMAN if row["origin"] == "human" else config.MARK_AI
            parts.append("- {} {}".format(mark, _indent_block(row["text"])))

    pending = classify.proposed(con)
    if pending:
        parts.append("")
        parts.append("**Proposed new domains — approve?**")
        for row in pending:
            parts.append("- [ ] `{}` — {} ({})".format(
                row["id"], row["label"], row["description"] or "no reason given"))

    if counts:
        parts.append("")
        parts.append("<sub>sources: " +
                     ", ".join("{}={}".format(k, v) for k, v in sorted(counts.items())) +
                     "</sub>")
    return "\n".join(parts) + "\n"


def days_in_month(con: sqlite3.Connection, year: int, month: int) -> List[str]:
    prefix = "{:04d}-{:02d}-".format(year, month)
    rows = con.execute(
        "SELECT DISTINCT day FROM ("
        "  SELECT day AS day FROM bullets WHERE day LIKE ? AND status='current' "
        "  UNION SELECT period_key AS day FROM digests "
        "         WHERE period='day' AND period_key LIKE ? "
        "  UNION SELECT day AS day FROM runs WHERE day LIKE ? AND day IS NOT NULL"
        ") ORDER BY day DESC", (prefix + "%", prefix + "%", prefix + "%")).fetchall()
    return [r["day"] for r in rows]


def write_month(con: sqlite3.Connection, day: datetime.date) -> Optional[str]:
    path = config.month_file(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = HEADER.format(year=day.year, month=config.MONTH_NAMES[day.month - 1],
                         human=config.MARK_HUMAN, ai=config.MARK_AI)
    sections = [render_day(con, d) for d in days_in_month(con, day.year, day.month)]
    path.write_text(body + "\n---\n\n".join(sections), encoding="utf-8")
    return str(path)


def write_index(con: sqlite3.Connection) -> str:
    """LEARNING_LOG.md becomes a thin index over the monthly files."""
    rows = con.execute(
        "SELECT substr(day,1,7) ym, count(DISTINCT day) days, count(*) n "
        "FROM bullets WHERE status='current' GROUP BY ym ORDER BY ym DESC").fetchall()
    lines = ["# Learning & Growth Log", "",
             "Daily entries live in monthly files under [`logs/`](logs/).",
             "This index is generated — do not edit it by hand.", ""]
    if rows:
        lines += ["| Month | Days logged | Bullets |", "|---|---|---|"]
        for row in rows:
            year, month = row["ym"].split("-")
            name = "{}-{}.md".format(month, config.MONTH_NAMES[int(month) - 1])
            lines.append("| [{}](logs/{}/{}) | {} | {} |".format(
                row["ym"], year, name, row["days"], row["n"]))
    failures = db.recent_failures(con, 5)
    if failures:
        lines += ["", "## ⚠️ Recent failed or partial runs", ""]
        for row in failures:
            lines.append("- `{}` — **{}** after {} attempt(s): {}".format(
                row["day"], row["status"], row["attempts"] or 1,
                (row["detail"] or "")[:140]))
    path = config.REPO / "LEARNING_LOG.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
