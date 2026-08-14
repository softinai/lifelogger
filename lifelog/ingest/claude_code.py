"""Claude Code sessions — ~/.claude/projects/**/*.jsonl

One event per session, not per message: the log wants "what did I work on",
not a transcript. Malformed lines are counted and skipped, never fatal.
"""
from __future__ import annotations

import datetime
import json
from typing import Dict

from .. import config
from .base import Event, Source, redact


def _text_of(message) -> str:
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text")
    return ""


class ClaudeCode(Source):
    name = "claude_code"

    def available(self) -> bool:
        return config.CLAUDE_PROJECTS.exists()

    def fetch(self, day: datetime.date):
        sessions: Dict[str, dict] = {}
        for path in config.CLAUDE_PROJECTS.rglob("*.jsonl"):
            try:
                handle = path.open(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    stamp = rec.get("timestamp")
                    if not stamp:
                        continue
                    try:
                        when = datetime.datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")).astimezone(config.TZ)
                    except ValueError:
                        continue
                    if when.date() != day:
                        continue


                    sid = "{}|{}".format(rec.get("sessionId") or path.stem,
                                         when.date().isoformat())
                    slot = sessions.setdefault(sid, {
                        "first": when, "last": when, "prompts": [],
                        "project": path.parent.name, "cwd": rec.get("cwd"),
                        "messages": 0,
                    })
                    slot["messages"] += 1
                    slot["first"] = min(slot["first"], when)
                    slot["last"] = max(slot["last"], when)
                    if rec.get("type") == "user":
                        text = _text_of(rec.get("message")).strip().replace("\n", " ")
                        if text and not text.startswith("<"):
                            slot["prompts"].append(text[:240])

        for sid, slot in sessions.items():
            if not slot["prompts"]:
                continue
            yield Event.make(
                slot["first"], self.name, "session", "cc:{}".format(sid),

                duration_s=(slot["last"] - slot["first"]).total_seconds(),
                title=slot["project"].replace("-", "/")[:200],
                body=redact(" | ".join(slot["prompts"][:12])),
                meta={"messages": slot["messages"], "prompts": len(slot["prompts"]),
                      "cwd": slot.get("cwd")},
            )
