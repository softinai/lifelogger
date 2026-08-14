"""The Source contract. One file per source in this package, nothing else.

Every ingester must be pure and idempotent: running it twice over the same
day inserts the same rows once, because `dedupe_key` is stable.
"""
from __future__ import annotations

import datetime
from typing import Optional

from .. import config


class Event(object):
    __slots__ = ("ts_utc", "day", "source", "kind", "domain",
                 "duration_s", "title", "body", "meta", "dedupe_key")

    def __init__(self, ts_utc, day, source, kind, dedupe_key,
                 domain=None, duration_s=None, title=None, body=None, meta=None):
        self.ts_utc = ts_utc
        self.day = day
        self.source = source
        self.kind = kind
        self.dedupe_key = dedupe_key
        self.domain = domain
        self.duration_s = duration_s
        self.title = title
        self.body = body
        self.meta = meta

    @classmethod
    def make(cls, dt: datetime.datetime, source: str, kind: str, dedupe_key: str, **kw):
        """`day` is derived in LOCAL time. East of UTC, an evening UTC
        timestamp belongs to the NEXT local day; getting this wrong silently
        shifts every evening event. There is a test for the boundary."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        local = dt.astimezone(config.TZ)
        if not dedupe_key:
            raise ValueError("dedupe_key is required — it is what makes re-runs free")
        return cls(ts_utc=config.utc_iso(dt), day=local.date().isoformat(),
                   source=source, kind=kind, dedupe_key=dedupe_key, **kw)


class Source(object):
    """Subclass and implement. Failure must never abort the nightly run."""
    name = "unnamed"

    def available(self) -> bool:
        return True

    def fetch(self, day: datetime.date):
        raise NotImplementedError

    def safe_fetch(self, day: datetime.date):
        """Returns (events, error). Degrade, don't fail."""
        if not self.available():
            return [], "unavailable"
        try:
            return list(self.fetch(day)), None
        except Exception as exc:                      # noqa: BLE001 - deliberate
            return [], "{}: {}".format(type(exc).__name__, exc)


SECRET_HINTS = ("token", "secret", "api_key", "apikey", "password", "passwd",
                "bearer", "authorization", "-----BEGIN")


def redact(text: Optional[str]) -> Optional[str]:
    """Strip anything that smells like a credential before it reaches the DB."""
    if not text:
        return text
    low = text.lower()
    for hint in SECRET_HINTS:
        if hint in low:
            return "[redacted — possible secret]"
    return text
