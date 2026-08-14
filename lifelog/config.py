"""Paths and settings. Every tunable lives here, nothing is hardcoded elsewhere.

Stdlib only, Python 3.9+ — the nightly path must run on a stock macOS
/usr/bin/python3 with zero installs (sellability requirement R-031).
"""
from __future__ import annotations

import datetime
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
HOME = pathlib.Path.home()


DATA_DIR = REPO / "data"
DB_PATH = DATA_DIR / "life.db"
LOGS_DIR = REPO / "logs"
CONFIG_DIR = REPO / "config"
REGISTRY_PATH = CONFIG_DIR / "domain_registry.json"


LOCAL_CONFIG = CONFIG_DIR / "local.json"


def local(key: str, default=None):
    import json
    try:
        return json.loads(LOCAL_CONFIG.read_text(encoding="utf-8")).get(key, default)
    except (OSError, ValueError):
        return default


def _path(key: str, default=None):
    """A single path from local.json. `~` is expanded; unset returns default."""
    value = local(key)
    return pathlib.Path(value).expanduser() if value else default


def _paths(key: str, default=None):
    """A list of paths from local.json. Accepts a bare string for convenience."""
    value = local(key)
    if not value:
        return list(default or [])
    if isinstance(value, str):
        value = [value]
    return [pathlib.Path(v).expanduser() for v in value]


OBSIDIAN_VAULT = _path("obsidian_vault")
OBSIDIAN_DIARY_DIR = _path(
    "obsidian_diary_dir",
    (OBSIDIAN_VAULT / "Diary") if OBSIDIAN_VAULT else None)

OBSIDIAN_GENERATED_LINK = (OBSIDIAN_VAULT / "Generated") if OBSIDIAN_VAULT else None

CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
OPENCODE_STORAGE = HOME / ".local/share/opencode/storage"
ANTIGRAVITY_STATE = (HOME / "Library/Application Support/Antigravity/User"
                     / "globalStorage" / "state.vscdb")


ICLOUD_DRIVE = HOME / "Library/Mobile Documents/com~apple~CloudDocs"
HEALTH_DIR = _path("health_dir", ICLOUD_DRIVE / "LifelogHealth")
DOWNLOADS = _path("downloads_dir", HOME / "Downloads")


GIT_SCAN_ROOTS = _paths("git_scan_roots",
                        [p for p in (HOME / "Projects", HOME / "Developer",
                                     HOME / "code", HOME / "src", HOME / "repos")
                         if p.exists()])

BROWSER_PROFILES = [
    ("edge", "Microsoft Edge/Default"),
    ("edge-p1", "Microsoft Edge/Profile 1"),
    ("chrome", "Google/Chrome/Default"),
]

AW_BASE = "http://localhost:5600"
OLLAMA_BASE = "http://localhost:11434"


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _resolve_tz():
    """The timezone that defines a "day".

    Order: config/local.json "timezone" -> $LIFELOG_TZ -> the system zone.
    This is not cosmetic: `day` is local-time, so the wrong zone silently
    files every evening event under the wrong day. Set it to where you
    actually live. There is a test for the boundary; keep it passing.
    """
    for name in (local("timezone"), os.environ.get("LIFELOG_TZ")):
        if name and (zone := _zone(name)):
            return zone
    try:
        return _zone(os.readlink("/etc/localtime").split("zoneinfo/", 1)[1])\
            or datetime.datetime.now().astimezone().tzinfo
    except (OSError, IndexError):
        return datetime.datetime.now().astimezone().tzinfo


TZ = _resolve_tz()
MODEL = os.environ.get("LIFELOG_MODEL", "qwen2.5:7b")
PROMPT_VERSION = "v2"
MODEL_ATTEMPTS = 3
MODEL_TIMEOUT_S = 240
DOMAIN_CAP = 15
MIN_BULLETS = 5


MIN_DWELL_S = 120.0
MIN_APP_S = 300.0
ROLLUP_MIN_DAYS = 2
BODY_TRUNCATE = 2000
TITLE_TRUNCATE = 500
MIN_FREE_GB = 5.0


MARK_HUMAN = "\U0001F9CD"
MARK_AI = "\U0001F916"

NTFY_SERVER = "https://ntfy.sh"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def month_file(day: datetime.date) -> pathlib.Path:
    """logs/2026/2026-08.md

    Deliberately NOT `08-August.md`: that is what the owner's own diary file is
    called, and two identically-named files in one Obsidian vault make the quick
    switcher ambiguous — easy to edit the generated one by mistake.
    """
    return LOGS_DIR / str(day.year) / "{:04d}-{:02d}.md".format(day.year, day.month)


def diary_file(day: datetime.date):
    """Your hand-written month file, same naming convention as the output.

    Returns None when no diary directory is configured.
    """
    if OBSIDIAN_DIARY_DIR is None:
        return None
    return OBSIDIAN_DIARY_DIR / str(day.year) / "{:02d}-{}.md".format(
        day.month, MONTH_NAMES[day.month - 1]
    )


def parse_day(text: str, today: "datetime.date" = None) -> "datetime.date":
    """Accepts `today`, `yesterday` or YYYY-MM-DD.

    The keywords exist because they are what people actually type, and an
    unparsed value used to surface as a raw ValueError traceback.
    """
    if today is None:
        today = datetime.datetime.now(TZ).date()
    key = (text or "").strip().lower()
    if key == "today":
        return today
    if key == "yesterday":
        return today - datetime.timedelta(days=1)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            "--day expects YYYY-MM-DD, 'today' or 'yesterday'; got {!r}".format(text))


def day_bounds(day: datetime.date):
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=TZ)
    return start, start + datetime.timedelta(days=1)


def utc_iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
