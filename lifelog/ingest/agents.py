"""Coding agents other than Claude Code: Codex, OpenCode, Antigravity.

One file rather than three, because they are the same *kind* of source and
share the session -> Event shape; splitting would triple the boilerplate for no
gain. Each is a separate Source class, so they still fail independently.

Every format here was read from real files on this machine before the parser
was written — none of it is guessed:

  Codex        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
               JSONL. `session_meta` carries id/cwd/git; `response_item` rows
               carry the messages. Directory dates let us skip whole trees
               instead of parsing 613 MB every night.

  OpenCode     ~/.local/share/opencode/storage/session/<project>/<id>.json
               JSON with a generated title and ms-epoch times. (Its
               opencode.db is present but empty — the file store is live.)

  Antigravity  Library/…/Antigravity/User/globalStorage/state.vscdb
               key `antigravityUnifiedStateSync.trajectorySummaries`:
               base64 -> protobuf, containing more base64 -> protobuf with
               field 1 = title, 3 = timestamp, 4 = uuid. Best-effort by
               nature; if the shape ever changes the source reports zero
               rather than crashing the night.
"""
from __future__ import annotations

import base64
import datetime
import json
import re
import sqlite3
from typing import Iterator, Optional, Tuple

from .. import config
from .base import Event, Source, redact

MAX_BODY_PROMPTS = 12


def _session_event(source: str, session_id: str, start: datetime.datetime,
                   end: Optional[datetime.datetime], title: str,
                   prompts, meta: dict) -> Event:
    """Every agent yields the same shape as Claude Code: one event per session."""
    duration = (end - start).total_seconds() if end and end > start else None
    body = " | ".join(p for p in prompts[:MAX_BODY_PROMPTS] if p) or None


    return Event.make(
        start, source, "session",
        "{}:{}:{}".format(source[:2], session_id,
                          start.astimezone(config.TZ).date().isoformat()),
        duration_s=duration, title=(title or "untitled")[:200],
        body=redact(body), meta=meta)


class Codex(Source):
    name = "codex"

    def available(self) -> bool:
        return config.CODEX_SESSIONS.exists()

    def fetch(self, day: datetime.date) -> Iterator[Event]:


        candidates = []
        for offset in (0, 1):
            date = day - datetime.timedelta(days=offset)
            folder = config.CODEX_SESSIONS / "{:04d}/{:02d}/{:02d}".format(
                date.year, date.month, date.day)
            if folder.is_dir():
                candidates.extend(sorted(folder.glob("*.jsonl")))

        for path in candidates:
            meta, prompts = {}, []
            first = last = None
            try:
                handle = path.open(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    stamp = record.get("timestamp")
                    when = _parse_iso(stamp)
                    if when is None:
                        continue
                    local = when.astimezone(config.TZ)
                    if local.date() != day:
                        continue
                    first = when if first is None else min(first, when)
                    last = when if last is None else max(last, when)

                    payload = record.get("payload") or {}
                    if record.get("type") == "session_meta":
                        git = payload.get("git") or {}
                        meta = {"id": payload.get("id"),
                                "cwd": payload.get("cwd"),
                                "branch": git.get("branch"),
                                "source": payload.get("source"),
                                "cli_version": payload.get("cli_version")}
                    elif payload.get("role") == "user":
                        for part in payload.get("content") or []:
                            text = (part or {}).get("text", "")


                            if text and not text.lstrip().startswith("<"):
                                prompts.append(text.strip().replace("\n", " ")[:240])

            if first is None or not prompts:
                continue
            session_id = meta.get("id") or path.stem
            title = (meta.get("cwd") or "").rstrip("/").split("/")[-1] or "codex"
            meta["prompts"] = len(prompts)
            yield _session_event(self.name, session_id, first, last, title,
                                 prompts, meta)


class OpenCode(Source):
    name = "opencode"

    def available(self) -> bool:
        return (config.OPENCODE_STORAGE / "session").is_dir()

    def fetch(self, day: datetime.date) -> Iterator[Event]:
        session_root = config.OPENCODE_STORAGE / "session"
        for path in sorted(session_root.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, ValueError):
                continue
            times = data.get("time") or {}
            start = _from_ms(times.get("created"))
            if start is None or start.astimezone(config.TZ).date() != day:
                continue
            end = _from_ms(times.get("updated"))
            session_id = data.get("id") or path.stem

            messages = list((config.OPENCODE_STORAGE / "message" / session_id).glob("*.json"))
            models = set()
            for message in messages[:60]:
                try:
                    blob = json.loads(message.read_text(encoding="utf-8", errors="ignore"))
                except (OSError, ValueError):
                    continue
                if blob.get("modelID"):
                    models.add(blob["modelID"])

            directory = (data.get("directory") or "").rstrip("/").split("/")[-1]
            yield _session_event(
                self.name, session_id, start, end,
                data.get("title") or directory or "opencode", [],
                {"slug": data.get("slug"), "directory": data.get("directory"),
                 "messages": len(messages), "models": sorted(models),
                 "version": data.get("version")})


_B64_RUN = re.compile(rb"[A-Za-z0-9+/]{24,}={0,2}")


def _varint(buf: bytes, index: int) -> Tuple[int, int]:
    result = shift = 0
    while index < len(buf):
        byte = buf[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


def _protobuf_fields(buf: bytes) -> dict:
    """Minimal protobuf reader: {field_number: value}. Enough to pull a title,
    a timestamp and an id without a schema or a dependency."""
    fields, index = {}, 0
    while index < len(buf):
        tag, index = _varint(buf, index)
        number, wire = tag >> 3, tag & 7
        if wire == 2:
            length, index = _varint(buf, index)
            fields.setdefault(number, buf[index:index + length])
            index += length
        elif wire == 0:
            value, index = _varint(buf, index)
            fields.setdefault(number, value)
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            raise ValueError("unsupported wire type {}".format(wire))
    return fields


def parse_trajectories(blob: str):
    """Yield (title, started_at, session_id) from the base64 protobuf blob."""
    try:
        outer = base64.b64decode(blob + "=" * (-len(blob) % 4))
    except Exception:                                       # noqa: BLE001
        return
    for match in _B64_RUN.finditer(outer):
        chunk = match.group()
        try:
            inner = base64.b64decode(chunk + b"=" * (-len(chunk) % 4))
            fields = _protobuf_fields(inner)
        except Exception:                                   # noqa: BLE001
            continue
        title = fields.get(1)
        stamp = fields.get(3)
        if not isinstance(title, bytes) or not isinstance(stamp, bytes):
            continue
        try:
            seconds = _protobuf_fields(stamp).get(1)
            text = title.decode("utf-8")
        except Exception:                                   # noqa: BLE001
            continue
        if not isinstance(seconds, int) or not 1_500_000_000 < seconds < 4_000_000_000:
            continue
        if not text.strip():
            continue
        identifier = fields.get(4)
        yield (text.strip(),
               datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc),
               identifier.decode("utf-8", "replace") if isinstance(identifier, bytes)
               else "{}".format(seconds))


class Antigravity(Source):
    name = "antigravity"
    KEY = "antigravityUnifiedStateSync.trajectorySummaries"

    def available(self) -> bool:
        return config.ANTIGRAVITY_STATE.exists()

    def fetch(self, day: datetime.date) -> Iterator[Event]:
        import shutil
        import tempfile
        handle = tempfile.NamedTemporaryFile(suffix=".vscdb", delete=False)
        handle.close()
        try:
            shutil.copy(config.ANTIGRAVITY_STATE, handle.name)
            con = sqlite3.connect("file:{}?mode=ro".format(handle.name), uri=True)
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key=?", (self.KEY,)).fetchone()
            con.close()
        except (OSError, sqlite3.Error):
            return
        finally:
            import os
            try:
                os.unlink(handle.name)
            except OSError:
                pass

        if not row or not row[0]:
            return
        blob = row[0]
        if isinstance(blob, bytes):
            blob = blob.decode("utf-8", "ignore")

        for title, started, identifier in parse_trajectories(blob):
            if started.astimezone(config.TZ).date() != day:
                continue
            yield _session_event(self.name, identifier, started, None,
                                 title, [], {"trajectory": identifier})


def _parse_iso(stamp) -> Optional[datetime.datetime]:
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def _from_ms(value) -> Optional[datetime.datetime]:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(value / 1000.0, datetime.timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
