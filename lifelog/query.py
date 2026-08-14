"""The read-only query layer over `life.db`.

One implementation, three consumers:
  - bin/ask.py      local Q&A with Ollama tool-calling  (fully local)
  - bin/mcp_server.py  MCP protocol for external clients (Claude Code, P4 app)
  - the Tauri app at P4

Every function returns plain dicts/lists so it can be JSON-serialised without
adaptation. Nothing here writes; `add_note` is the single deliberate exception
and it is not exposed read-only clients.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import Dict, List, Optional

from . import bullets as bullets_mod
from . import classify, config, db, embed

MAX_ROWS = 200


def _rows(cursor) -> List[Dict]:
    return [dict(row) for row in cursor.fetchall()]


def search_bullets(con: sqlite3.Connection, text: str, limit: int = 30,
                   origin: Optional[str] = None) -> List[Dict]:
    """Keyword search across everything logged. The workhorse."""
    sql = ("SELECT day, category_id, origin, text FROM bullets "
           "WHERE status='current' AND text LIKE ?")
    params: List = ["%{}%".format(text)]
    if origin in ("human", "ai"):
        sql += " AND origin=?"
        params.append(origin)
    sql += " ORDER BY day DESC LIMIT ?"
    params.append(min(limit, MAX_ROWS))
    return _rows(con.execute(sql, params))


def semantic_search(con: sqlite3.Connection, text: str, limit: int = 10) -> List[Dict]:
    """Meaning-based search. Finds 'gym' when you ask about 'workout'."""
    return embed.semantic_search(con, text, limit)


def get_day(con: sqlite3.Connection, day: str) -> Dict:
    """Everything recorded for one date."""
    return {
        "day": day,
        "overview": db.latest_overview(con, "day", day),
        "bullets": [{"id": r["id"], "origin": r["origin"],
                     "domain": classify.canonical(con, r["category_id"]),
                     "text": r["text"]}
                    for r in bullets_mod.for_day(con, day)],
        "sources": db.source_counts(con, day),
    }


def get_range(con: sqlite3.Connection, start: str, end: str,
              domain: Optional[str] = None, limit: int = 100) -> List[Dict]:
    sql = ("SELECT day, category_id, origin, text FROM bullets "
           "WHERE status='current' AND day BETWEEN ? AND ?")
    params: List = [start, end]
    if domain:
        sql += " AND category_id=?"
        params.append(domain)
    sql += " ORDER BY day DESC LIMIT ?"
    params.append(min(limit, MAX_ROWS))
    return _rows(con.execute(sql, params))


def get_review(con: sqlite3.Connection, period: str, key: str) -> Dict:
    return {"period": period, "key": key,
            "overview": db.latest_overview(con, period, key)}


def list_reviews(con: sqlite3.Connection, period: Optional[str] = None) -> List[Dict]:
    """Each review carries its real date span. "2026-W32" means nothing to a
    human reading a dashboard; "3 Aug - 9 Aug" does."""
    from . import rollup
    sql = ("SELECT period, period_key, max(generated_at) generated_at FROM digests "
           "WHERE period != 'day'")
    params: List = []
    if period:
        sql += " AND period=?"
        params.append(period)
    sql += " GROUP BY period, period_key ORDER BY period_key DESC"
    out = _rows(con.execute(sql, params))
    for row in out:
        try:
            days = rollup.period_days(row["period"], row["period_key"])
            row["start"], row["end"] = days[0], days[-1]
        except Exception:                                   # noqa: BLE001
            row["start"] = row["end"] = None
    return out


def get_metrics(con: sqlite3.Connection, start: str, end: str,
                metric: Optional[str] = None) -> List[Dict]:
    sql = "SELECT day, domain, metric, value, unit FROM metrics WHERE day BETWEEN ? AND ?"
    params: List = [start, end]
    if metric:
        sql += " AND metric=?"
        params.append(metric)
    sql += " ORDER BY day DESC LIMIT ?"
    params.append(MAX_ROWS)
    return _rows(con.execute(sql, params))


def domain_summary(con: sqlite3.Connection, start: str = "0000-01-01",
                   end: str = "9999-12-31") -> List[Dict]:
    """How much has been logged per domain — the 'where is my progress' answer."""
    label_of = classify.labels(con)
    counts: Dict[str, Dict] = {}
    for row in con.execute(
            "SELECT category_id, origin, count(*) n, min(day) first, max(day) last "
            "FROM bullets WHERE status='current' AND day BETWEEN ? AND ? "
            "GROUP BY category_id, origin", (start, end)):
        key = classify.canonical(con, row["category_id"]) or "other"
        slot = counts.setdefault(key, {"domain": key,
                                       "label": label_of.get(key, key),
                                       "bullets": 0, "mine": 0, "model": 0,
                                       "first": row["first"], "last": row["last"]})
        slot["bullets"] += row["n"]
        slot["mine" if row["origin"] == "human" else "model"] += row["n"]
        slot["first"] = min(slot["first"], row["first"])
        slot["last"] = max(slot["last"], row["last"])
    return sorted(counts.values(), key=lambda d: -d["bullets"])


def stats(con: sqlite3.Connection) -> Dict:
    days = [r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM bullets WHERE status='current' ORDER BY day")]
    origins = {r["origin"]: r["n"] for r in con.execute(
        "SELECT origin, count(*) n FROM bullets WHERE status='current' GROUP BY origin")}
    return {
        "days_logged": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "bullets_mine": origins.get("human", 0),
        "bullets_model": origins.get("ai", 0),
        "events": con.execute("SELECT count(*) n FROM events").fetchone()["n"],
        "domains": [d["label"] for d in domain_summary(con)],
    }


def health(con: sqlite3.Connection, limit: int = 10) -> List[Dict]:
    return _rows(con.execute(
        "SELECT day, job, status, attempts FROM runs "
        "ORDER BY started_at DESC LIMIT ?", (limit,)))


def list_categories(con: sqlite3.Connection) -> List[Dict]:
    """Every domain the owner can file a bullet under, plus pending proposals."""
    return [{"id": r["id"], "label": r["label"], "status": r["status"],
             "description": r["description"]}
            for r in con.execute(
                "SELECT id,label,status,description FROM categories "
                "WHERE status IN ('active','proposed') ORDER BY status DESC, label")]


def set_bullet_category(con: sqlite3.Connection, bullet_id: int,
                        category_id: str) -> Dict:
    """The owner re-files a bullet. Stamped so automation never moves it again
    (D-018): improved rules may fix the model's guesses, never the owner's."""
    bullets_mod.set_category_by_owner(con, int(bullet_id), category_id)
    return {"ok": True, "bullet_id": int(bullet_id), "category_id": category_id}


def edit_bullet(con: sqlite3.Connection, bullet_id: int, text: str) -> Dict:
    """The owner corrects a bullet. Never destructive: a new row is inserted
    and the old one superseded, so every version survives (D-018). An edited
    AI bullet keeps origin='ai' but is stamped edited_by='human', which is what
    stops the next nightly run from overwriting the correction."""
    text = (text or "").strip()
    if not text:
        return {"error": "text is required"}
    try:
        new_id = bullets_mod.edit(con, int(bullet_id), text, edited_by="human")
    except bullets_mod.HumanBulletProtected as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "id": new_id, "text": text}


def create_category(con: sqlite3.Connection, label: str) -> Dict:
    """A domain the owner invents themselves is active immediately — they do not
    need their own approval."""
    key = classify.slug(label)
    existing = con.execute("SELECT id FROM categories WHERE id=?", (key,)).fetchone()
    if not existing:
        con.execute(
            "INSERT INTO categories(id,label,aliases,status,created_by,created_at,approved_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, label.strip()[:60], "[]", "active", "human",
             config.now_utc_iso(), config.now_utc_iso()))
        con.commit()
    else:
        classify.approve(con, key)
    return {"ok": True, "id": key, "label": label.strip()[:60]}


def decide_category(con: sqlite3.Connection, category_id: str,
                    action: str) -> Dict:
    """Approve or reject a domain the model proposed (R-024)."""
    if action == "approve":
        classify.approve(con, category_id)
    elif action == "reject":
        con.execute("UPDATE categories SET status='archived' WHERE id=?", (category_id,))
        con.commit()
    else:
        return {"error": "action must be approve or reject"}
    return {"ok": True, "id": category_id, "action": action}


def add_note(con: sqlite3.Connection, text: str, day: Optional[str] = None) -> Dict:
    """Add a bullet in the owner's own voice — origin='human', so nothing
    automated can ever alter it afterwards."""
    day = day or datetime.datetime.now(config.TZ).date().isoformat()
    category = classify.rule_match(con, text)
    bullet_id = bullets_mod.add_human(con, day, text, category)
    return {"added": bullet_id is not None, "day": day,
            "domain": category, "text": text}


TOOLS = [
    {"name": "search_bullets",
     "description": "Search everything the owner has logged, by keyword. Use this "
                    "first for any 'when did I', 'have I ever', 'what did I learn "
                    "about X' question.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "description": "keyword or phrase"},
         "limit": {"type": "integer", "description": "max results, default 30"},
         "origin": {"type": "string", "enum": ["human", "ai"],
                    "description": "'human' = the owner's own words only"}},
         "required": ["text"]}},

    {"name": "semantic_search",
     "description": "Search by MEANING rather than exact words. Use this when "
                    "search_bullets returns nothing, or when the question is "
                    "conceptual ('when did I feel stuck', 'anything about "
                    "fitness') and the owner may have used different wording.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"},
         "limit": {"type": "integer"}}, "required": ["text"]}},

    {"name": "get_day",
     "description": "Everything logged on one date: overview, bullets, source counts.",
     "parameters": {"type": "object", "properties": {
         "day": {"type": "string", "description": "YYYY-MM-DD"}}, "required": ["day"]}},

    {"name": "get_range",
     "description": "All bullets between two dates, optionally one domain only.",
     "parameters": {"type": "object", "properties": {
         "start": {"type": "string"}, "end": {"type": "string"},
         "domain": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["start", "end"]}},

    {"name": "get_review",
     "description": "A generated weekly, monthly or yearly review.",
     "parameters": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["week", "month", "year"]},
         "key": {"type": "string", "description": "2026-W33, 2026-08 or 2026"}},
         "required": ["period", "key"]}},

    {"name": "list_reviews",
     "description": "Which reviews exist.",
     "parameters": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["week", "month", "year"]}}}},

    {"name": "domain_summary",
     "description": "How many bullets per life domain, and the date span of each. "
                    "Use for 'where am I making progress' questions.",
     "parameters": {"type": "object", "properties": {
         "start": {"type": "string"}, "end": {"type": "string"}}}},

    {"name": "get_metrics",
     "description": "Daily numbers: bullets_total, active_hours, event counts.",
     "parameters": {"type": "object", "properties": {
         "start": {"type": "string"}, "end": {"type": "string"},
         "metric": {"type": "string"}}, "required": ["start", "end"]}},

    {"name": "stats",
     "description": "Overall totals: days logged, bullet counts, domains, date span.",
     "parameters": {"type": "object", "properties": {}}},

    {"name": "health",
     "description": "Recent nightly job runs and whether they succeeded.",
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer"}}}},

    {"name": "add_note",
     "description": "Record something the owner did that no automatic source can "
                    "see. Stored as their own words and never altered afterwards.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"},
         "day": {"type": "string", "description": "YYYY-MM-DD, default today"}},
         "required": ["text"]}},
]

READ_ONLY = {t["name"] for t in TOOLS} - {"add_note"}

DISPATCH = {
    "search_bullets": search_bullets,
    "semantic_search": semantic_search,
    "get_day": get_day,
    "get_range": get_range,
    "get_review": get_review,
    "list_reviews": list_reviews,
    "domain_summary": domain_summary,
    "get_metrics": get_metrics,
    "stats": stats,
    "health": health,
    "add_note": add_note,
    "list_categories": list_categories,
}


def call(con: sqlite3.Connection, name: str, arguments: dict):
    """Dispatch a tool call. Unknown tools and bad arguments return an error
    dict rather than raising — a model will routinely get both wrong."""
    function = DISPATCH.get(name)
    if function is None:
        return {"error": "unknown tool: {}".format(name)}
    try:
        return function(con, **(arguments or {}))
    except TypeError as exc:
        return {"error": "bad arguments for {}: {}".format(name, exc)}
    except sqlite3.Error as exc:
        return {"error": "query failed: {}".format(exc)}
