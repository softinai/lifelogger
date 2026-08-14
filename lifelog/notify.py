"""Notifications. Failure to notify never fails the run.

Two channels:
  - macOS banner (terminal-notifier, osascript fallback) — local, full detail
  - ntfy push to the phone — **counts and status only, never content**

The ntfy rule is deliberate. ntfy.sh is a third-party server; a message sent
through it leaves this machine, which principle P1 forbids for personal data.
A push saying "7 bullets, ok" carries no diary content, so it stays inside the
rule. Bullet text, overviews and health numbers are never pushed.
"""
from __future__ import annotations

import shutil
import subprocess
import urllib.error
import urllib.request

from . import config


def send(title: str, message: str, open_path: str = "") -> bool:
    if shutil.which("terminal-notifier"):
        cmd = ["terminal-notifier", "-title", title, "-message", message,
               "-group", "lifelog"]
        if open_path:
            cmd += ["-open", "file://" + open_path.replace(" ", "%20")]
    else:
        script = 'display notification {} with title {}'.format(
            _quote(message), _quote(title))
        cmd = ["osascript", "-e", script]
    try:
        subprocess.run(cmd, capture_output=True, timeout=15)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def _quote(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


SAFE_FIELDS = ("day", "status", "bullets_human", "bullets_ai", "attempts", "period")

_HEADER_SUBS = {"—": "-", "–": "-", "·": "-", "…": "...",
                "⚠️": "!", "⚠": "!"}


def _header_safe(text: str) -> str:
    """HTTP headers must be latin-1. A title containing an em dash raises
    UnicodeEncodeError inside urllib and the push vanishes silently."""
    for bad, good in _HEADER_SUBS.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def push(title: str, facts: dict, priority: str = "default") -> bool:
    """Send a content-free ping to the phone.

    `facts` is filtered to SAFE_FIELDS before sending. Anything else — bullet
    text, overviews, health values, page titles — is dropped rather than
    trusted to the caller, because a leak here is silent and permanent.
    """
    topic = config.local("ntfy_topic")
    if not topic:
        return False

    safe = {k: v for k, v in facts.items() if k in SAFE_FIELDS}
    body = " · ".join("{} {}".format(v, k.replace("bullets_", "")) if "bullets" in k
                      else "{}".format(v) for k, v in safe.items())

    server = config.local("ntfy_server", config.NTFY_SERVER).rstrip("/")
    request = urllib.request.Request(
        "{}/{}".format(server, topic),
        data=body.encode("utf-8"),
        headers={"Title": _header_safe(title), "Priority": priority,
                 "Tags": "chart_with_upwards_trend"},
    )
    token = config.local("ntfy_token")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        urllib.request.urlopen(request, timeout=10)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def announce(title: str, message: str, facts: dict, open_path: str = "",
             priority: str = "default") -> None:
    """Banner locally (full detail) + push to the phone (counts only)."""
    send(title, message, open_path)
    push(title, facts, priority)
