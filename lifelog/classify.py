"""Domains — discovered, not fixed (R-022/R-023/R-024, D-015).

Two stages:
  1. The seed registry holds known domains. Free, deterministic.
  2. The model may propose a new domain for a bullet that fits none. A proposal
     is stored `status='proposed'` and is NEVER shown as fact until approved.

Cap: the active set stays small (default 15). Over cap, new proposals wait
rather than sprawling. Approving a domain is a one-line edit or `approve()`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Dict, List, Optional

from . import config

SEED = {
    "ai_eng":    ("AI Engineering", ["ai", "ml", "llm", "agent", "claude", "codex", "prompt"]),
    "it_dev":    ("IT / Dev", ["git", "terminal", "infra", "docker", "sql", "deploy", "api"]),
    "career":    ("Career", ["job", "interview", "cv", "resume", "hh", "linkedin", "offer"]),
    "finance":   ("Finance", ["money", "market", "invest", "budget", "tradingview", "bank"]),
    "sport":     ("Sport / Health", ["sport", "gym", "swim", "swimming", "run", "running",
                                     "workout", "exercise", "exercises", "fitness", "health",
                                     "steps", "deadlift", "press", "dumbbell"]),
    "spiritual": ("Spiritual / Mindfulness", ["meditation", "prayer", "gratitude"]),
    "psych":     ("Psychology", ["habit", "mindset", "mood", "bias", "law"]),
    "language":  ("Language", ["english", "spanish", "duolingo", "vocabulary"]),
    "learning":  ("Learning / Reading", ["book", "course", "article", "paper", "doc"]),
    "other":     ("Other", []),
}


def sync_seed(con: sqlite3.Connection) -> None:
    for key, (label, aliases) in SEED.items():
        con.execute(
            "INSERT OR IGNORE INTO categories"
            "(id,label,aliases,status,created_by,created_at,approved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, label, json.dumps(aliases), "active", "seed",
             config.now_utc_iso(), config.now_utc_iso()))
    con.commit()


def active(con: sqlite3.Connection) -> List[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM categories WHERE status='active' ORDER BY id").fetchall()


def proposed(con: sqlite3.Connection) -> List[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM categories WHERE status='proposed' ORDER BY created_at").fetchall()


def labels(con: sqlite3.Connection) -> Dict[str, str]:
    return {row["id"]: row["label"] for row in
            con.execute("SELECT id,label FROM categories")}


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:32] or "other"


def resolve(con: sqlite3.Connection, label: str) -> Optional[str]:
    """Map a model-emitted label onto a category id, if one already exists."""
    label_l = label.strip().lower()
    for row in con.execute("SELECT id,label,aliases FROM categories"):
        if row["label"].lower() == label_l or row["id"] == slug(label):
            return row["id"]
        for alias in json.loads(row["aliases"] or "[]"):
            if alias and alias in label_l:
                return row["id"]
    return None


def rule_match(con: sqlite3.Connection, text: str) -> Optional[str]:
    """Stage 1: cheap keyword hit, on WORD BOUNDARIES.

    Substring matching was actively harmful: "…more money for killing them" in
    a note about Goodhart's Law filed it under Finance. Longest alias wins, so
    a specific term beats a generic one.
    """


    if not text:
        return None
    head = text.splitlines()[0]
    rules = [(row["id"], json.loads(row["aliases"] or "[]"))
             for row in con.execute(
                 "SELECT id,aliases FROM categories WHERE status='active'")
             if row["id"] != "other"]


    for scope in (head, text):
        low = scope.lower()
        best = None
        for category_id, aliases in rules:
            for alias in aliases:
                if alias and re.search(
                        r"(?<!\w){}(?!\w)".format(re.escape(alias)), low):
                    if best is None or len(alias) > best[1]:
                        best = (category_id, len(alias))
        if best:
            return best[0]
    return None


def propose_category(con: sqlite3.Connection, label: str, reason: str = "") -> Optional[str]:
    """Stage 2 fallback. Never becomes visible until the owner approves it."""
    key = slug(label)
    if con.execute("SELECT 1 FROM categories WHERE id=?", (key,)).fetchone():
        return key
    n_active = len(active(con)) + len(proposed(con))
    if n_active >= config.DOMAIN_CAP:
        return None
    con.execute(
        "INSERT INTO categories(id,label,aliases,description,status,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (key, label.strip()[:60], "[]", reason[:200], "proposed", "ai", config.now_utc_iso()))
    con.commit()
    return key


def approve(con: sqlite3.Connection, category_id: str) -> bool:
    """Returns False when no such category exists, so callers can say so
    instead of reporting success for a typo."""
    cur = con.execute(
        "UPDATE categories SET status='active', approved_at=? WHERE id=?",
        (config.now_utc_iso(), category_id))
    con.commit()
    return cur.rowcount > 0


def merge(con: sqlite3.Connection, source_id: str, target_id: str) -> None:
    """Shrink the set. Bullets keep pointing at the old id; rendering follows
    `merged_into`, so old entries relabel without rewriting any markdown."""
    con.execute("UPDATE categories SET status='merged', merged_into=? WHERE id=?",
                (target_id, source_id))
    con.commit()


def canonical(con: sqlite3.Connection, category_id: Optional[str]) -> Optional[str]:
    seen = set()
    while category_id and category_id not in seen:
        seen.add(category_id)
        row = con.execute("SELECT merged_into FROM categories WHERE id=?",
                          (category_id,)).fetchone()
        if not row or not row["merged_into"]:
            return category_id
        category_id = row["merged_into"]
    return category_id
