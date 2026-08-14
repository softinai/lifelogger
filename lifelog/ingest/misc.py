"""Small sources that don't warrant a file each: git commits and Downloads."""
from __future__ import annotations

import datetime
import subprocess

from .. import config
from .base import Event, Source, redact


class GitCommits(Source):
    name = "git"

    def available(self) -> bool:
        return any(root.exists() for root in config.GIT_SCAN_ROOTS)

    def fetch(self, day: datetime.date):
        start, end = config.day_bounds(day)
        for root in config.GIT_SCAN_ROOTS:
            if not root.exists():
                continue


            found = set(root.glob("*/.git")) | set(root.glob("*/*/.git"))
            for repo in sorted(path.parent for path in found):
                try:
                    out = subprocess.run(
                        ["git", "-C", str(repo), "log",
                         "--since", start.isoformat(), "--until", end.isoformat(),
                         "--pretty=format:%H%x1f%aI%x1f%s"],
                        capture_output=True, text=True, timeout=20).stdout
                except (subprocess.SubprocessError, OSError):
                    continue
                for line in out.splitlines():
                    parts = line.split("\x1f")
                    if len(parts) != 3:
                        continue
                    sha, when, subject = parts
                    try:
                        stamp = datetime.datetime.fromisoformat(when)
                    except ValueError:
                        continue
                    yield Event.make(
                        stamp, self.name, "commit", "git:{}:{}".format(repo.name, sha[:12]),
                        title=repo.name, body=redact(subject),
                        meta={"sha": sha[:12]},
                    )


class Downloads(Source):
    name = "downloads"

    def available(self) -> bool:
        return config.DOWNLOADS.exists()

    def fetch(self, day: datetime.date):
        for path in config.DOWNLOADS.iterdir():
            if path.name.startswith("."):
                continue
            try:
                mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, config.TZ)
            except OSError:
                continue
            if mtime.date() != day:
                continue
            yield Event.make(
                mtime, self.name, "file", "dl:{}:{}".format(day.isoformat(), path.name),
                title=path.name[:200],
                meta={"bytes": path.stat().st_size},
            )
