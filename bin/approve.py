#!/usr/bin/env python3
"""Approve, reject or merge a proposed domain.

The model may propose a new domain but never creates one silently (R-024). A
proposal sits in `categories` with status='proposed' and is rendered as an
unticked checkbox in the month file and dashboard — visible, but never treated
as fact — until you run this.

    ./bin/approve.py --list
    ./bin/approve.py cooking
    ./bin/approve.py --merge cooking other
    ./bin/approve.py --reject cooking
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import classify, dashboard, db, render   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("category_id", nargs="?")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--merge", nargs=2, metavar=("FROM", "INTO"))
    parser.add_argument("--reject", metavar="ID")
    args = parser.parse_args()

    con = db.connect()
    db.init_db(con)

    if args.list or not (args.category_id or args.merge or args.reject):
        pending = classify.proposed(con)
        if not pending:
            print("No proposed domains.")
        for row in pending:
            print("  {:<24} {:<28} {}".format(
                row["id"], row["label"], row["description"] or ""))
        print("\nActive: " + ", ".join(r["id"] for r in classify.active(con)))
        return 0

    if args.merge:
        classify.merge(con, args.merge[0], args.merge[1])
        print("merged {} -> {}".format(*args.merge))
    elif args.reject:
        cur = con.execute(
            "UPDATE categories SET status='archived' WHERE id=?", (args.reject,))
        con.commit()
        if cur.rowcount == 0:
            print("no category with id {!r} — see --list".format(args.reject),
                  file=sys.stderr)
            return 1
        print("rejected {}".format(args.reject))
    else:
        if not classify.approve(con, args.category_id):
            print("no category with id {!r} — see --list".format(args.category_id),
                  file=sys.stderr)
            return 1
        print("approved {}".format(args.category_id))

    render.write_index(con)
    dashboard.write(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
