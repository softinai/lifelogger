"""Bullets — the editable core of the log (R-025, R-026, D-018).

Rules enforced here, not by convention:
  1. An edit never destroys anything: it inserts a new row and marks the old
     one `superseded`. `UPDATE bullets SET text=...` never happens.
  2. No automated path may modify a bullet with origin='human'. Attempting it
     raises HumanBulletProtected. The owner knows better than the model what
     they did.
  3. Regenerating a day supersedes only the AI bullets. Human bullets survive
     every re-run, forever.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Iterable, List, Optional

from . import config


class HumanBulletProtected(Exception):
    """Raised when automation tries to touch a bullet the owner wrote."""


def _key(day: str, origin: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return "{}:{}:{}".format(origin, day, digest)


def add_human(con: sqlite3.Connection, day: str, text: str,
              category_id: Optional[str] = None,
              edited_by: str = "human") -> Optional[int]:
    """Store the owner's own bullet VERBATIM. Idempotent on identical text.

    `edited_by` records where it came from: 'diary' for bullets imported from
    the Obsidian diary, 'human' for ones added directly (ask.py, the app).
    The distinction matters for reconcile_diary().
    """
    key = _key(day, "human", text)
    cur = con.execute(
        "INSERT OR IGNORE INTO bullets"
        "(day,category_id,text,origin,status,dedupe_key,created_at,edited_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (day, category_id, text, "human", "current", key,
         config.now_utc_iso(), edited_by))
    con.commit()
    return cur.lastrowid if cur.rowcount else None


def reconcile_diary(con: sqlite3.Connection, day: str,
                    current_texts: Iterable[str]) -> int:
    """Retire diary-imported bullets whose text is no longer in the diary.

    Without this, editing a line in the diary — or improving the parser, which
    is how this was found — leaves the old version behind and the day shows the
    bullet twice.

    This is not automation overriding the owner: the diary IS the owner, and
    the old row is superseded, never deleted. Bullets added directly
    (`edited_by='human'`) are untouched, because they were never in the diary.
    """
    keep = set(current_texts)
    stale = [row["id"] for row in con.execute(
        "SELECT id, text FROM bullets WHERE day=? AND origin='human' "
        "AND status='current' AND edited_by='diary'", (day,))
        if row["text"] not in keep]
    for bullet_id in stale:
        con.execute("UPDATE bullets SET status='superseded' WHERE id=?", (bullet_id,))
    con.commit()
    return len(stale)


def propose(con: sqlite3.Connection, day: str, category_id: Optional[str], text: str,
            evidence: Optional[list] = None, confidence: Optional[float] = None) -> Optional[int]:
    key = _key(day, "ai", text)
    cur = con.execute(
        "INSERT OR IGNORE INTO bullets"
        "(day,category_id,text,origin,status,confidence,evidence,dedupe_key,created_at,edited_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (day, category_id, text, "ai", "current", confidence,
         json.dumps(evidence or []), key, config.now_utc_iso(), "ai"))
    con.commit()
    return cur.lastrowid if cur.rowcount else None


def reclassify(con: sqlite3.Connection, bullet_id: int, category_id: Optional[str]) -> None:
    """Move a bullet to a different domain WITHOUT touching its text.

    Permitted on human bullets only while the owner has not set the category
    themselves (`edited_by != 'human-category'`). Their words are immutable; which
    heading they sit under is filing, and filing rules improve over time.
    """
    row = con.execute("SELECT origin, edited_by FROM bullets WHERE id=?",
                      (bullet_id,)).fetchone()
    if row is None:
        return
    if row["edited_by"] == "human-category":
        return
    con.execute("UPDATE bullets SET category_id=? WHERE id=?", (category_id, bullet_id))
    con.commit()


def set_category_by_owner(con: sqlite3.Connection, bullet_id: int, category_id: str) -> None:
    """The owner files a bullet by hand. Automation stops touching it after this."""
    con.execute("UPDATE bullets SET category_id=?, edited_by='human-category' WHERE id=?",
                (category_id, bullet_id))
    con.commit()


def supersede_ai_for_day(con: sqlite3.Connection, day: str) -> int:
    """Clear the previous AI pass before regenerating.

    Human bullets are untouched, and so is any AI bullet the owner has since
    corrected (`edited_by='human'`) — otherwise tonight's run would silently
    throw away their fix, which is the whole reason editing exists.
    """
    cur = con.execute(
        "UPDATE bullets SET status='superseded' "
        "WHERE day=? AND origin='ai' AND status='current' "
        "AND COALESCE(edited_by,'') != 'human'", (day,))
    con.commit()
    return cur.rowcount


def edit(con: sqlite3.Connection, bullet_id: int, new_text: str, edited_by: str) -> int:
    """Edit = insert new row + supersede old. Never mutates text in place."""
    row = con.execute("SELECT * FROM bullets WHERE id=?", (bullet_id,)).fetchone()
    if row is None:
        raise ValueError("no bullet {}".format(bullet_id))
    if row["origin"] == "human" and edited_by != "human":
        raise HumanBulletProtected(
            "bullet {} was written by the owner; automation may not edit it".format(bullet_id))
    cur = con.execute(
        "INSERT INTO bullets"
        "(day,category_id,text,origin,status,confidence,evidence,dedupe_key,created_at,edited_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (row["day"], row["category_id"], new_text, row["origin"], "current",
         row["confidence"], row["evidence"], _key(row["day"], row["origin"], new_text),
         config.now_utc_iso(), edited_by))
    new_id = cur.lastrowid
    con.execute("UPDATE bullets SET status='superseded', superseded_by=? WHERE id=?",
                (new_id, bullet_id))
    con.commit()
    return new_id


def reject(con: sqlite3.Connection, bullet_id: int, by: str) -> None:
    row = con.execute("SELECT origin FROM bullets WHERE id=?", (bullet_id,)).fetchone()
    if row and row["origin"] == "human" and by != "human":
        raise HumanBulletProtected("automation may not reject the owner's bullet")
    con.execute("UPDATE bullets SET status='rejected' WHERE id=?", (bullet_id,))
    con.commit()


def for_day(con: sqlite3.Connection, day: str) -> List[sqlite3.Row]:
    """Current bullets, owner's first — their words lead the entry."""
    return con.execute(
        "SELECT * FROM bullets WHERE day=? AND status='current' "
        "ORDER BY CASE origin WHEN 'human' THEN 0 ELSE 1 END, id", (day,)).fetchall()


def history(con: sqlite3.Connection, bullet_id: int) -> List[sqlite3.Row]:
    """Every version of a bullet, oldest first. Nothing is ever lost."""
    chain, cur_id = [], bullet_id
    while cur_id:
        row = con.execute("SELECT * FROM bullets WHERE id=?", (cur_id,)).fetchone()
        if row is None:
            break
        chain.append(row)
        cur_id = row["superseded_by"]
    return chain
