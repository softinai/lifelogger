"""Semantic search over bullets — embeddings without a SQLite extension.

`sqlite-vec` is not an option here: macOS's /usr/bin/python3 is compiled
without `enable_load_extension`, and using it would mean giving up the
stdlib-only runtime that lets this run on any Mac with zero installs (D-020).

So vectors live in an ordinary BLOB column and the similarity scan runs in
Python. At this scale that is not a compromise: 768 floats per bullet, a few
thousand bullets, one pass of dot products. Revisit only if it ever gets slow.

Model: nomic-embed-text via Ollama. Its task prefixes matter — documents are
embedded as `search_document:` and queries as `search_query:`; skipping them
measurably degrades retrieval.
"""
from __future__ import annotations

import array
import json
import math
import sqlite3
import urllib.request
from typing import List, Optional, Tuple

from . import config

EMBED_MODEL = "nomic-embed-text"
BATCH = 32


def embed_texts(texts: List[str], prefix: str = "search_document") -> List[List[float]]:
    """Ollama /api/embed. Returns [] on any failure — search degrades to
    keyword, it never breaks the nightly run."""
    if not texts:
        return []
    payload = json.dumps({
        "model": EMBED_MODEL,
        "input": ["{}: {}".format(prefix, t[:2000]) for t in texts],
    }).encode("utf-8")
    request = urllib.request.Request(
        config.OLLAMA_BASE + "/api/embed", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response).get("embeddings") or []
    except Exception:                                       # noqa: BLE001
        return []


def _pack(vector: List[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def _norm(vector) -> float:
    return math.sqrt(sum(x * x for x in vector)) or 1.0


def index_new(con: sqlite3.Connection, limit: Optional[int] = None) -> int:
    """Embed every current bullet that has no vector yet. Idempotent."""
    sql = ("SELECT b.id, b.text FROM bullets b "
           "LEFT JOIN embeddings e ON e.bullet_id = b.id "
           "WHERE b.status='current' AND e.bullet_id IS NULL ORDER BY b.id")
    if limit:
        sql += " LIMIT {}".format(int(limit))
    pending = con.execute(sql).fetchall()
    if not pending:
        return 0

    written = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        vectors = embed_texts([row["text"] for row in chunk])
        if len(vectors) != len(chunk):
            break
        con.executemany(
            "INSERT OR REPLACE INTO embeddings(bullet_id,model,dim,vec,created_at) "
            "VALUES (?,?,?,?,?)",
            [(row["id"], EMBED_MODEL, len(vec), _pack(vec), config.now_utc_iso())
             for row, vec in zip(chunk, vectors)])
        written += len(chunk)
    con.commit()
    return written


def semantic_search(con: sqlite3.Connection, text: str, limit: int = 10,
                    min_score: float = 0.45) -> List[dict]:
    """Meaning-based search. Falls back to nothing (not an error) if the
    embedding model is unavailable."""
    query_vectors = embed_texts([text], prefix="search_query")
    if not query_vectors:
        return []
    query = array.array("f", query_vectors[0])
    query_norm = _norm(query)

    scored: List[Tuple[float, sqlite3.Row]] = []
    for row in con.execute(
            "SELECT b.day, b.origin, b.category_id, b.text, e.vec "
            "FROM embeddings e JOIN bullets b ON b.id = e.bullet_id "
            "WHERE b.status='current'"):
        vector = _unpack(row["vec"])
        if len(vector) != len(query):
            continue
        dot = sum(a * b for a, b in zip(query, vector))
        score = dot / (query_norm * _norm(vector))
        if score >= min_score:
            scored.append((score, row))

    scored.sort(key=lambda pair: -pair[0])
    return [{"day": row["day"], "origin": row["origin"],
             "domain": row["category_id"], "text": row["text"],
             "score": round(score, 3)}
            for score, row in scored[:limit]]


def coverage(con: sqlite3.Connection) -> dict:
    total = con.execute(
        "SELECT count(*) n FROM bullets WHERE status='current'").fetchone()["n"]
    done = con.execute(
        "SELECT count(*) n FROM embeddings e JOIN bullets b ON b.id=e.bullet_id "
        "WHERE b.status='current'").fetchone()["n"]
    return {"bullets": total, "embedded": done, "model": EMBED_MODEL}
