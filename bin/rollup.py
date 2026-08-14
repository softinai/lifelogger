#!/usr/bin/env python3
"""Weekly and monthly summaries -> logs/Summary.md

Chained after the nightly job. Refuses to run for a day whose daily job is
still in flight; a FAILED daily run is fine — the requirement is that the day
is finished, not that it succeeded.

    ./bin/rollup.py                       # whatever is due after yesterday
    ./bin/rollup.py --period week  --key 2026-W33 --force
    ./bin/rollup.py --period month --key 2026-08  --force
    ./bin/rollup.py --day 2026-08-16      # what would be due after that day
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import config, dashboard, db, models, notify, rollup   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", help="YYYY-MM-DD, 'today' or 'yesterday' (default: yesterday)")
    parser.add_argument("--period", choices=["week", "month", "year"])
    parser.add_argument("--key", help="e.g. 2026-W33, 2026-08 or 2026")
    parser.add_argument("--force", action="store_true", help="regenerate existing")
    parser.add_argument("--skip-guard", action="store_true",
                        help="don't wait for the daily run to finish")
    parser.add_argument("--model")
    parser.add_argument("--no-notify", action="store_true")


    args, _unknown = parser.parse_known_args()

    today = datetime.datetime.now(config.TZ).date()
    try:
        day = config.parse_day(args.day, today) if args.day else today - datetime.timedelta(days=1)
    except ValueError as exc:
        print("[fatal] {}".format(exc), file=sys.stderr)
        return 2

    con = db.connect()
    db.init_db(con)

    if not args.skip_guard and not rollup.daily_run_finished(con, day.isoformat()):
        print("[wait] daily run for {} has not finished; not rolling up".format(day))
        return 3

    jobs = ([(args.period, args.key)] if args.period and args.key
            else rollup.due(day))
    if not jobs:
        print("[skip] nothing due after {} ({})".format(day, day.strftime("%A")))
        dashboard.write(con)
        return 0

    model = models.get_model(args.model)
    models.assert_local(model)

    results = {}
    for period, key in jobs:
        started = db.start_run(con, "rollup-" + period, day.isoformat())
        status, error = rollup.generate(con, period, key, model, args.force)
        db.finish_run(con, started, "ok" if status == "ok" else "partial", 1,
                      {"period": period, "key": key, "status": status, "error": error})
        results["{} {}".format(period, key)] = status
        if error:
            print("[warn] {} {}: {}".format(period, key, error), file=sys.stderr)

    summary_path = rollup.write_summary(con)
    dashboard.write(con)
    print("[write] {}".format(summary_path))
    print("[done] " + "  ".join("{}={}".format(k, v) for k, v in results.items()))

    done = [k for k, v in results.items() if v == "ok"]
    if not args.no_notify and done:
        notify.announce("Life Log — review ready", ", ".join(done),
                        {"period": ", ".join(done), "status": "ok"},
                        summary_path)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
