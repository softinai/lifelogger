#!/usr/bin/env python3
"""Nightly Life Log builder.

  gather -> life.db -> summarize (local model) -> classify -> render markdown

Runs at 00:01 for the day that just ended. `--day` is always explicit in the
record, never inferred at write time, so a run delayed by sleep still lands on
the right date.

    ./bin/nightly.py                      # yesterday
    ./bin/nightly.py --day 2026-08-11
    ./bin/nightly.py --day 2026-08-11 --force
    ./bin/nightly.py --backfill 5         # last 5 days
    ./bin/nightly.py --dry-run

Stdlib only: runs on stock /usr/bin/python3, no venv, no pip.
"""
from __future__ import annotations

import argparse
import datetime
import shutil
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import (classify, config, dashboard, db, digest, embed,      # noqa: E402
                     metrics, models, notify, render)
from lifelog.ingest.activitywatch import ActivityWatch                      # noqa: E402
from lifelog.ingest.agents import Antigravity, Codex, OpenCode              # noqa: E402
from lifelog.ingest.browser import BrowserHistory                           # noqa: E402
from lifelog.ingest.claude_code import ClaudeCode                           # noqa: E402
from lifelog.ingest.health import AppleHealth                               # noqa: E402
from lifelog.ingest.misc import Downloads, GitCommits                       # noqa: E402
from lifelog.ingest.obsidian import ObsidianDiary                           # noqa: E402

SOURCES = [ObsidianDiary(), AppleHealth(),
           ClaudeCode(), Codex(), OpenCode(), Antigravity(),
           BrowserHistory(), ActivityWatch(), GitCommits(), Downloads()]


def free_gb() -> float:
    return shutil.disk_usage(str(config.REPO)).free / 1e9


def ingest_day(con, day: datetime.date) -> dict:
    detail = {}
    for source in SOURCES:
        events, error = source.safe_fetch(day)
        inserted = db.insert_events(con, events) if events else 0
        detail[source.name] = {"found": len(events), "new": inserted}
        if error:
            detail[source.name]["error"] = error
    return detail


def process(con, day: datetime.date, force: bool, dry_run: bool,
            model, quiet: bool) -> str:
    key = day.isoformat()
    previous = db.last_run_for_day(con, key)
    if previous and previous["status"] == "ok" and not force:
        print("[skip] {} already completed; --force to redo".format(key))
        return "skipped"

    started = None if dry_run else db.start_run(con, "nightly", key)
    detail = ingest_day(con, day)
    print("[gather] " + "  ".join(
        "{}={}/{}".format(name, d["new"], d["found"]) for name, d in detail.items()))

    human = digest.store_human_bullets(con, key)
    status, attempts, error = digest.generate(con, key, model)
    if error:
        detail["model"] = {"error": error, "attempts": attempts}
        print("[warn] model: {} (after {} attempts)".format(error, attempts), file=sys.stderr)

    broken = [name for name, d in detail.items()
              if isinstance(d, dict) and d.get("error") not in (None, "unavailable")]
    if broken and status == "ok":
        status = "partial"

    counts = db.source_counts(con, key)
    if not counts:
        status = "failed"
        detail["fatal"] = "no events captured from any source"

    embedded = embed.index_new(con)
    if embedded:
        print("[embed] {} new bullet(s) indexed for semantic search".format(embedded))

    day_metrics = metrics.compute_day(con, key)
    detail["metrics"] = day_metrics

    if dry_run:
        print("\n===== DRY RUN =====\n")
        print(render.render_day(con, key))
        return status

    db.finish_run(con, started, status, attempts or 1, detail)

    month_path = render.write_month(con, day)
    render.write_index(con)
    dashboard.write(con)
    print("[write] {}  (bullets: {} mine, {} model)".format(
        month_path, int(day_metrics.get("bullets_human", 0)),
        int(day_metrics.get("bullets_ai", 0))))

    if not quiet:
        icon = "" if status == "ok" else " ⚠️"
        notify.announce(
            "Life Log{} — {}".format(icon, key),
            "{} bullets · {}".format(len(render.bullets_mod.for_day(con, key)), status),
            {"day": key, "status": status,
             "bullets_human": int(day_metrics.get("bullets_human", 0)),
             "bullets_ai": int(day_metrics.get("bullets_ai", 0)),
             "attempts": attempts or 1},
            month_path,
            priority="high" if status != "ok" else "default")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", help="YYYY-MM-DD, 'today' or 'yesterday' (default: yesterday, local time)")
    parser.add_argument("--backfill", type=int, metavar="N",
                        help="process the last N days, oldest first")
    parser.add_argument("--force", action="store_true", help="redo a completed day")
    parser.add_argument("--dry-run", action="store_true", help="print the entry; write no files and record no run")
    parser.add_argument("--model", help="ollama model id (default: {})".format(config.MODEL))
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    if free_gb() < config.MIN_FREE_GB:
        notify.send("Life Log — stopped", "Only {:.1f} GB free".format(free_gb()))
        print("[fatal] {:.1f} GB free, below {} GB floor".format(
            free_gb(), config.MIN_FREE_GB), file=sys.stderr)
        return 2

    today = datetime.datetime.now(config.TZ).date()
    if args.day:
        try:
            days = [config.parse_day(args.day, today)]
        except ValueError as exc:
            print("[fatal] {}".format(exc), file=sys.stderr)
            return 2
    elif args.backfill:
        days = [today - datetime.timedelta(days=n)
                for n in range(args.backfill, 0, -1)]
    else:
        days = [today - datetime.timedelta(days=1)]

    con = db.connect()
    db.init_db(con)
    classify.sync_seed(con)
    model = models.get_model(args.model)
    models.assert_local(model)

    results = {}
    for day in days:
        results[day.isoformat()] = process(
            con, day, args.force, args.dry_run, model, args.no_notify)
    con.close()

    print("[done] " + "  ".join("{}={}".format(k, v) for k, v in results.items()))
    return 0 if all(v in ("ok", "skipped") for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
