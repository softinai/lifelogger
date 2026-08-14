"""Edge + Chrome history. Full URLs (R-014).

The browser holds a lock on History, so it is copied before reading.
Chromium timestamps are microseconds since 1601-01-01 UTC — the conversion
has its own test, because getting it wrong shifts every visit by centuries.
"""
from __future__ import annotations

import datetime
import shutil
import sqlite3
import tempfile

from .. import config
from .base import Event, Source, redact

CHROME_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)


def chrome_us(dt: datetime.datetime) -> int:
    return int((dt.astimezone(datetime.timezone.utc) - CHROME_EPOCH).total_seconds() * 1_000_000)


def from_chrome_us(value: int) -> datetime.datetime:
    return CHROME_EPOCH + datetime.timedelta(microseconds=value)


class BrowserHistory(Source):
    name = "browser"

    def available(self) -> bool:
        return any((config.HOME / "Library/Application Support" / p / "History").exists()
                   for _, p in config.BROWSER_PROFILES)

    def fetch(self, day: datetime.date):
        start, end = config.day_bounds(day)
        lo, hi = chrome_us(start), chrome_us(end)
        for label, profile in config.BROWSER_PROFILES:
            src = config.HOME / "Library/Application Support" / profile / "History"
            if not src.exists():
                continue
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            try:
                shutil.copy(src, tmp.name)
                con = sqlite3.connect("file:{}?mode=ro".format(tmp.name), uri=True)
                rows = con.execute(
                    "SELECT v.id, v.visit_time, u.title, u.url, v.visit_duration "
                    "FROM visits v JOIN urls u ON u.id = v.url "
                    "WHERE v.visit_time >= ? AND v.visit_time < ? ORDER BY v.visit_time",
                    (lo, hi)).fetchall()
                con.close()
            except sqlite3.Error:
                continue
            finally:
                try:
                    import os
                    os.unlink(tmp.name)
                except OSError:
                    pass

            for visit_id, visit_time, title, url, duration in rows:
                yield Event.make(
                    from_chrome_us(visit_time), self.name, "visit",
                    "br:{}:{}".format(label, visit_id),
                    duration_s=(duration or 0) / 1_000_000.0,
                    title=redact(title or ""), body=redact(url or ""),
                    meta={"profile": label},
                )
