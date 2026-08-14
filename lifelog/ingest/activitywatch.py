"""ActivityWatch — per-app focus time. Fills the gap browser history can't:
VS Code, terminal, Obsidian, Zoom.

Note: this Mac's AW history begins 2026-08-12. Earlier days legitimately have
no app data — that is missing history, not a bug.
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request

from .. import config
from .base import Event, Source, redact


def _get(url: str, timeout: int = 5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


class ActivityWatch(Source):
    name = "activitywatch"

    def available(self) -> bool:
        try:
            _get(config.AW_BASE + "/api/0/info", timeout=3)
            return True
        except Exception:                                  # noqa: BLE001
            return False

    def fetch(self, day: datetime.date):
        start, end = config.day_bounds(day)
        buckets = _get(config.AW_BASE + "/api/0/buckets/", timeout=5)
        for bucket_id in buckets:
            if "window" not in bucket_id and "afk" not in bucket_id:
                continue
            kind = "afk" if "afk" in bucket_id else "app_use"
            url = "{}/api/0/buckets/{}/events?start={}&end={}&limit=-1".format(
                config.AW_BASE, bucket_id,
                urllib.parse.quote(config.utc_iso(start)),
                urllib.parse.quote(config.utc_iso(end)))
            try:
                events = _get(url, timeout=15)
            except Exception:                              # noqa: BLE001
                continue
            for event in events:
                stamp = event.get("timestamp")
                if not stamp:
                    continue
                try:
                    when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                data = event.get("data") or {}
                yield Event.make(
                    when, self.name, kind,
                    "aw:{}:{}".format(bucket_id, event.get("id")),
                    duration_s=event.get("duration", 0.0),
                    title=redact(data.get("app") or data.get("status") or "unknown"),
                    body=redact(data.get("title")),
                )
