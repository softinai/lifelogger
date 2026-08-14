"""Apple Health / Apple Watch, via a file the iPhone drops in iCloud Drive.

Accepts two formats so the free path and the paid path both work without a
code change:

1. **Shortcuts (free)** — a plain `key=value` text file, one per line:

       date=2026-08-12
       steps=8432
       active_energy=520
       exercise_minutes=45
       sleep_hours=6.8
       workouts=Swimming 30 min; Strength 45 min

2. **Health Auto Export (paid)** — its JSON export, `{"data": {"metrics": [...],
   "workouts": [...]}}`.

Files are named `lifelog-health-YYYY-MM-DD.*`; the date in the filename or in
the payload decides which day the samples belong to, never the file's mtime —
iCloud rewrites mtimes when it syncs.
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Dict, Iterator

from .. import config
from .base import Event, Source

FILE_RE = re.compile(r"lifelog-health-(\d{4}-\d{2}-\d{2})\.(txt|csv|json)$", re.I)
DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
SUPPORTED = (".txt", ".csv", ".json")


KNOWN = {
    "steps": ("steps", "count"),
    "active_energy": ("active_energy", "kcal"),
    "exercise_minutes": ("exercise_minutes", "min"),
    "stand_hours": ("stand_hours", "count"),
    "sleep_hours": ("sleep_hours", "hours"),
    "resting_hr": ("resting_heart_rate", "bpm"),
    "weight": ("weight", "kg"),
    "distance_km": ("distance", "km"),
    "mindful_minutes": ("mindful_minutes", "min"),
}


def parse_keyvalue(text: str) -> Dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if value and value.lower() not in ("null", "none", "n/a"):
            values[key] = value
    return values


def _number(value: str):
    try:
        return float(re.sub(r"[^0-9.\-]", "", value) or "nan")
    except ValueError:
        return None


def parse_health_auto_export(payload: dict) -> Dict[str, str]:
    """Flatten HAE's JSON into the same key=value shape."""
    values = {}
    data = payload.get("data") or {}
    for metric in data.get("metrics") or []:
        name = (metric.get("name") or "").strip().lower().replace(" ", "_")
        points = metric.get("data") or []
        if not name or not points:
            continue
        total = 0.0
        for point in points:
            qty = point.get("qty", point.get("Avg"))
            if isinstance(qty, (int, float)):
                total += float(qty)
        values[name] = "{:g}".format(total)
    workouts = []
    for workout in data.get("workouts") or []:
        label = workout.get("name") or workout.get("workoutActivityType") or "workout"
        minutes = workout.get("duration")
        workouts.append("{} {:g} min".format(label, float(minutes) / 60.0)
                        if isinstance(minutes, (int, float)) else str(label))
    if workouts:
        values["workouts"] = "; ".join(workouts)
    return values


class AppleHealth(Source):
    name = "health"

    def available(self) -> bool:
        return config.HEALTH_DIR.exists()

    def fetch(self, day: datetime.date) -> Iterator[Event]:
        wanted = day.isoformat()
        stamp = datetime.datetime(day.year, day.month, day.day, 21, 0, tzinfo=config.TZ)

        for path in sorted(config.HEALTH_DIR.iterdir()):
            if path.suffix.lower() not in SUPPORTED or path.name.startswith("."):
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if path.suffix.lower() == ".json":
                try:
                    values = parse_health_auto_export(json.loads(raw))
                except ValueError:
                    continue
            else:
                values = parse_keyvalue(raw)


            in_name = DATE_IN_NAME_RE.search(path.name)
            file_day = in_name.group(1) if in_name else values.get("date", "")[:10]
            if file_day != wanted:
                continue
            values.pop("date", None)

            for key, value in values.items():
                metric, unit = KNOWN.get(key, (key, None))
                if key == "workouts":
                    for entry in (v.strip() for v in value.split(";")):
                        if entry:
                            yield Event.make(
                                stamp, self.name, "workout",
                                "hk:{}:workout:{}".format(wanted, entry[:60]),
                                domain="sport", title=entry[:200],
                                meta={"source_file": path.name})
                    continue
                number = _number(value)
                yield Event.make(
                    stamp, self.name, "metric",
                    "hk:{}:{}".format(wanted, metric),
                    domain="sport",
                    title="{}: {}{}".format(metric.replace("_", " "), value,
                                            " " + unit if unit else ""),
                    duration_s=None,
                    meta={"metric": metric, "value": number, "unit": unit,
                          "source_file": path.name})
