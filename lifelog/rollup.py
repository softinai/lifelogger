"""Weekly and monthly summaries -> logs/Summary.md

Runs after the daily job, never beside it: a rollup over a day that is still
being written would summarise half a day. `daily_run_finished()` is the guard,
and it deliberately accepts a FAILED daily run — the requirement is that the
day is finished, not that it succeeded.

Weekly fires on Monday for the ISO week that just ended.
Monthly fires on the 1st for the month that just ended.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import List, Tuple

from . import classify, config, db, models

SYSTEM = """You write a periodic review of one person's progress. You receive
every logged bullet for the period, grouped by domain. Bullets marked [me] are
their own words; bullets marked [auto] were generated from their machine activity.

Output EXACTLY:

## HIGHLIGHTS
- 3 to 6 bullets naming the most significant things achieved or learned. Be specific.

## BY DOMAIN
- **Domain** — one sentence on what moved in this area over the period.

## PATTERNS
- 2 to 4 observations only visible across the whole period: what recurred, what
  stalled, where time actually went versus where they said it would.

Rules:
- Trust [me] bullets over [auto] ones where they conflict; they know what they did.
- Do not invent anything absent from the input. Do not repeat a bullet verbatim.
- No praise, no encouragement, no closing paragraph. English."""


def week_key(day: datetime.date) -> str:
    year, week, _ = day.isocalendar()
    return "{}-W{:02d}".format(year, week)


def month_key(day: datetime.date) -> str:
    return "{:04d}-{:02d}".format(day.year, day.month)


def week_days(key: str) -> List[str]:
    year, week = int(key[:4]), int(key[6:])
    monday = datetime.date.fromisocalendar(year, week, 1) if hasattr(
        datetime.date, "fromisocalendar") else _iso_monday(year, week)
    return [(monday + datetime.timedelta(days=n)).isoformat() for n in range(7)]


def _iso_monday(year: int, week: int) -> datetime.date:
    fourth = datetime.date(year, 1, 4)
    return fourth - datetime.timedelta(days=fourth.isoweekday() - 1) +\
        datetime.timedelta(weeks=week - 1)


def month_days(key: str) -> List[str]:
    year, month = int(key[:4]), int(key[5:])
    start = datetime.date(year, month, 1)
    end = datetime.date(year + (month == 12), month % 12 + 1, 1)
    return [(start + datetime.timedelta(days=n)).isoformat()
            for n in range((end - start).days)]


def year_days(key: str) -> List[str]:
    year = int(key)
    start, end = datetime.date(year, 1, 1), datetime.date(year + 1, 1, 1)
    return [(start + datetime.timedelta(days=n)).isoformat()
            for n in range((end - start).days)]


def period_days(period: str, key: str) -> List[str]:
    if period == "week":
        return week_days(key)
    if period == "month":
        return month_days(key)
    return year_days(key)


def due(day: datetime.date) -> List[Tuple[str, str]]:
    """What should run on the morning after `day` was logged.

    Ordered narrow -> wide so a 31 December run writes the month before the
    year, and the year review is generated with the month already in place.
    """
    jobs = []
    if day.isoweekday() == 7:
        jobs.append(("week", week_key(day)))
    tomorrow = day + datetime.timedelta(days=1)
    if tomorrow.day == 1:
        jobs.append(("month", month_key(day)))
        if tomorrow.month == 1:
            jobs.append(("year", str(day.year)))
    return jobs


def daily_run_finished(con: sqlite3.Connection, day: str) -> bool:
    """True once the daily job has stopped running for `day`, success or not."""
    row = db.last_run_for_day(con, day)
    return bool(row) and row["status"] != "running"


def _is_current(con: sqlite3.Connection, period: str, key: str) -> bool:
    """A stored review only counts if it was written AFTER the period closed.

    Without this, a mid-period review — e.g. "2026-08" generated on the 12th
    from 5 days of data — would be treated as done, and the real month-end run
    would skip it. The month would be summarised forever from its first week.
    """
    row = con.execute(
        "SELECT max(generated_at) g FROM digests WHERE period=? AND period_key=?",
        (period, key)).fetchone()
    if not row or not row["g"]:
        return False
    last_day = period_days(period, key)[-1]
    return row["g"][:10] > last_day


def collect(con: sqlite3.Connection, period: str, key: str):
    days = period_days(period, key)
    marks = ",".join("?" for _ in days)
    rows = con.execute(
        "SELECT day, category_id, text, origin FROM bullets "
        "WHERE status='current' AND day IN ({}) ORDER BY day".format(marks),
        days).fetchall()
    grouped = {}
    for row in rows:
        cat = classify.canonical(con, row["category_id"]) or "other"
        grouped.setdefault(cat, []).append(row)
    return grouped, rows


def generate(con: sqlite3.Connection, period: str, key: str, model=None,
             force: bool = False):
    """Returns (status, error). status: ok | skipped | partial."""
    if not force and _is_current(con, period, key):
        return "skipped", None

    grouped, rows = collect(con, period, key)
    logged_days = len({r["day"] for r in rows})
    if logged_days < config.ROLLUP_MIN_DAYS:
        return "skipped", "only {} logged day(s) in {}".format(logged_days, key)

    label_of = classify.labels(con)
    parts = ["{} {} — {} bullets across {} day(s)".format(
        period.upper(), key, len(rows), logged_days), ""]
    for cat, items in sorted(grouped.items(),
                             key=lambda kv: -len(kv[1])):
        parts.append("### {}".format(label_of.get(cat, cat)))
        for row in items:
            mark = "[me]" if row["origin"] == "human" else "[auto]"
            first = (row["text"] or "").splitlines()[0]
            parts.append("- {} {} {}".format(row["day"], mark, first[:220]))
        parts.append("")

    model = model or models.get_model()
    text, attempts, error = models.complete_with_retries(
        model, SYSTEM, "\n".join(parts))
    if not text:
        return "partial", error

    db.save_digest(con, period, key, model.id, text.strip())
    return "ok", None


def write_summary(con: sqlite3.Connection) -> str:
    """One file holding every weekly and monthly review, newest first."""
    lines = ["# Summaries", "",
             "*Weekly and monthly reviews, generated from `data/life.db`.*",
             "*Regenerating is safe — nothing here is hand-written.*", ""]

    for period, heading in (("year", "Yearly"), ("month", "Monthly"), ("week", "Weekly")):
        rows = con.execute(
            "SELECT period_key, overview, model, max(generated_at) g FROM digests "
            "WHERE period=? GROUP BY period_key ORDER BY period_key DESC",
            (period,)).fetchall()
        if not rows:
            continue
        lines += ["---", "", "# {} reviews".format(heading), ""]
        for row in rows:
            days = period_days(period, row["period_key"])
            counted = con.execute(
                "SELECT count(*) n FROM bullets WHERE status='current' AND day IN ({})"
                .format(",".join("?" for _ in days)), days).fetchone()["n"]
            lines += ["## {}".format(row["period_key"]),
                      "<sub>{} bullets · {} · generated {}</sub>".format(
                          counted, row["model"], row["g"][:10]),
                      "", row["overview"], ""]

    path = config.LOGS_DIR / "Summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
