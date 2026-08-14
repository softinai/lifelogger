"""Your hand-written diary — the highest-value source in the system.

Read-only, always. This module never writes to the vault.
Bullets are captured VERBATIM including nesting, indentation and language —
whatever language you write in is stored unchanged. The model may summarise
around them but never rewrites them.

Opt-in: set "obsidian_diary_dir" in config/local.json. Unset, this source
reports unavailable and the nightly run carries on without it.

Expected layout: `<diary dir>/<year>/<MM-Month>.md`, with `DD/MM/YYYY` date
lines inside, newest first.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import subprocess
import time
from typing import List

from .. import config
from .base import Event, Source

DATE_RE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$")

MATERIALISE_WAIT_S = 20.0


class DiaryUnavailable(Exception):
    """The diary should be readable but is not — usually iCloud eviction.

    Raised instead of returning quietly: to the caller, a missing diary is
    indistinguishable from an empty one, and that silence once cost a whole
    day of notes. The nightly run records the error and marks the night
    partial rather than pretending nothing was written.
    """


def materialise(path, wait_s=None):
    """Return a readable path, pulling the file back from iCloud if evicted.

    iCloud Drive evicts file *contents* to save space and leaves a hidden
    `.name.icloud` placeholder; the real path then reports exists() == False.
    `brctl download` asks iCloud to fetch it back. Returns None when the file
    is genuinely absent (no placeholder either) or could not be recovered in
    time.
    """
    if path.exists():
        return path
    placeholder = path.parent / (".{}.icloud".format(path.name))
    if not placeholder.exists():
        return None
    try:
        subprocess.run(["brctl", "download", str(path)],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    deadline = time.time() + (MATERIALISE_WAIT_S if wait_s is None else wait_s)
    while time.time() < deadline:
        if path.exists():
            return path
        time.sleep(0.5)
    return None
BULLET_RE = re.compile(r"^(\s*)[-*+]\s*(.*)$")


def _expand(line: str) -> str:
    return line.replace("\t", "    ")


def parse_day_bullets(text: str, day: datetime.date) -> List[str]:
    """Return top-level bullets for `day`, each keeping its nested children."""
    want = (day.day, day.month, day.year)
    lines = [_expand(l) for l in text.splitlines()]

    start = None
    for i, line in enumerate(lines):
        m = DATE_RE.match(line)
        if m and tuple(int(x) for x in m.groups()) == want:
            start = i + 1
            break
    if start is None:
        return []

    block = []
    for line in lines[start:]:
        if DATE_RE.match(line):
            break
        block.append(line)


    indents = [len(l) - len(l.lstrip()) for l in block
               if BULLET_RE.match(l) and BULLET_RE.match(l).group(2).strip()]
    if not indents:
        return []
    base = min(indents)

    bullets, current = [], None
    for line in block:
        m = BULLET_RE.match(line)
        indent = len(line) - len(line.lstrip())
        if m and not m.group(2).strip():
            continue
        if m and indent <= base:
            if current is not None:
                bullets.append(current)
            current = m.group(2).rstrip()
        elif current is not None and line.strip():

            rel = max(0, indent - base)
            current += "\n" + " " * rel + line.strip()
    if current is not None:
        bullets.append(current)

    return [b for b in (x.strip() for x in bullets) if b]


class ObsidianDiary(Source):
    name = "journal"

    def available(self) -> bool:
        return (config.OBSIDIAN_DIARY_DIR is not None
                and config.OBSIDIAN_DIARY_DIR.exists())

    def fetch(self, day: datetime.date):
        wanted = config.diary_file(day)
        if wanted is None:
            return
        path = materialise(wanted)
        if path is None:
            placeholder = wanted.parent / (".{}.icloud".format(wanted.name))
            if placeholder.exists():
                raise DiaryUnavailable(
                    "diary file for {} is evicted by iCloud and could not be "
                    "pulled back".format(day.isoformat()))
            return
        text = path.read_text(encoding="utf-8", errors="ignore")
        stamp = datetime.datetime(day.year, day.month, day.day, 12, 0, tzinfo=config.TZ)
        for bullet in parse_day_bullets(text, day):
            digest = hashlib.sha256(bullet.encode("utf-8")).hexdigest()[:12]
            yield Event.make(
                stamp, self.name, "note", "jr:{}:{}".format(day.isoformat(), digest),
                title=bullet.splitlines()[0][:200], body=bullet,
                meta={"path": path.name},
            )
