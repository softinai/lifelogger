"""SQLite store. `life.db` is canonical; markdown is a rendering of it (D-001).

Invariants:
  - `events` is append-only. Never UPDATE, never DELETE.
  - Re-running any ingester over any day is free: dedupe_key + INSERT OR IGNORE.
  - Editing a bullet inserts a new row and supersedes the old one (D-018).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  ts_utc      TEXT NOT NULL,
  day         TEXT NOT NULL,
  source      TEXT NOT NULL,
  kind        TEXT NOT NULL,
  domain      TEXT,
  duration_s  REAL,
  title       TEXT,
  body        TEXT,
  meta        TEXT,
  dedupe_key  TEXT NOT NULL UNIQUE,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_day    ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, day);

CREATE TABLE IF NOT EXISTS categories (
  id          TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  aliases     TEXT,
  description TEXT,
  status      TEXT NOT NULL,          -- active | proposed | merged | archived
  created_by  TEXT NOT NULL,          -- seed | ai | human
  created_at  TEXT NOT NULL,
  approved_at TEXT,
  merged_into TEXT
);

CREATE TABLE IF NOT EXISTS bullets (
  id            INTEGER PRIMARY KEY,
  day           TEXT NOT NULL,
  category_id   TEXT,
  text          TEXT NOT NULL,
  origin        TEXT NOT NULL,        -- human | ai
  status        TEXT NOT NULL,        -- current | superseded | rejected
  confidence    REAL,
  evidence      TEXT,
  dedupe_key    TEXT UNIQUE,
  created_at    TEXT NOT NULL,
  superseded_by INTEGER,
  edited_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bullets_day ON bullets(day, status);

CREATE TABLE IF NOT EXISTS digests (
  period       TEXT NOT NULL,
  period_key   TEXT NOT NULL,
  model        TEXT NOT NULL,
  prompt_ver   TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  overview     TEXT NOT NULL,
  PRIMARY KEY (period, period_key, generated_at)
);

CREATE TABLE IF NOT EXISTS metrics (
  day    TEXT NOT NULL,
  domain TEXT NOT NULL,
  metric TEXT NOT NULL,
  value  REAL NOT NULL,
  unit   TEXT,
  PRIMARY KEY (day, domain, metric)
);

CREATE TABLE IF NOT EXISTS embeddings (
  bullet_id  INTEGER PRIMARY KEY REFERENCES bullets(id),
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  started_at  TEXT PRIMARY KEY,
  finished_at TEXT,
  job         TEXT NOT NULL,
  day         TEXT,
  status      TEXT NOT NULL,          -- ok | partial | failed
  attempts    INTEGER DEFAULT 1,
  detail      TEXT
);
"""


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def insert_events(con: sqlite3.Connection, events: Iterable) -> int:
    """Returns how many rows were ACTUALLY inserted (duplicates are free)."""
    rows = []
    for e in events:
        rows.append((
            e.ts_utc, e.day, e.source, e.kind, e.domain, e.duration_s,
            (e.title or "")[:config.TITLE_TRUNCATE] or None,
            (e.body or "")[:config.BODY_TRUNCATE] or None,
            json.dumps(e.meta, ensure_ascii=False) if e.meta else None,
            e.dedupe_key, config.now_utc_iso(),
        ))
    if not rows:
        return 0
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO events "
        "(ts_utc,day,source,kind,domain,duration_s,title,body,meta,dedupe_key,ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return con.total_changes - before


def events_for_day(con: sqlite3.Connection, day: str, source: Optional[str] = None) -> List[sqlite3.Row]:
    if source:
        return con.execute(
            "SELECT * FROM events WHERE day=? AND source=? ORDER BY ts_utc", (day, source)).fetchall()
    return con.execute("SELECT * FROM events WHERE day=? ORDER BY ts_utc", (day,)).fetchall()


def source_counts(con: sqlite3.Connection, day: str) -> dict:
    return {r["source"]: r["n"] for r in con.execute(
        "SELECT source, count(*) n FROM events WHERE day=? GROUP BY source", (day,))}


def start_run(con: sqlite3.Connection, job: str, day: str) -> str:
    started = config.now_utc_iso()
    con.execute("INSERT OR REPLACE INTO runs(started_at,job,day,status) VALUES (?,?,?,?)",
                (started, job, day, "running"))
    con.commit()
    return started


def finish_run(con: sqlite3.Connection, started_at: str, status: str,
               attempts: int, detail: dict) -> None:
    con.execute(
        "UPDATE runs SET finished_at=?, status=?, attempts=?, detail=? WHERE started_at=?",
        (config.now_utc_iso(), status, attempts,
         json.dumps(detail, ensure_ascii=False), started_at))
    con.commit()


def last_run_for_day(con: sqlite3.Connection, day: str) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM runs WHERE day=? ORDER BY started_at DESC LIMIT 1", (day,)).fetchone()


def recent_failures(con: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM runs WHERE status IN ('failed','partial') "
        "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()


def save_digest(con: sqlite3.Connection, period: str, key: str, model: str,
                overview: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO digests(period,period_key,model,prompt_ver,generated_at,overview) "
        "VALUES (?,?,?,?,?,?)",
        (period, key, model, config.PROMPT_VERSION, config.now_utc_iso(), overview))
    con.commit()


def latest_overview(con: sqlite3.Connection, period: str, key: str) -> Optional[str]:
    row = con.execute(
        "SELECT overview FROM digests WHERE period=? AND period_key=? "
        "ORDER BY generated_at DESC LIMIT 1", (period, key)).fetchone()
    return row["overview"] if row else None
