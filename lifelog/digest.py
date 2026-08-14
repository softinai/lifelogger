"""Turn a day of events into an overview + AI bullets.

The owner's own bullets never pass through the model. They go straight from
`events(source='journal')` into `bullets(origin='human')`, verbatim — Russian
exercise names and all. The model sees them only as context so the overview
can mention them, and is told explicitly not to reproduce them.
"""
from __future__ import annotations

import re
import sqlite3
from typing import List, Tuple

from . import bullets as bullets_mod
from . import classify, config, db, models

SYSTEM = """You write one person's daily progress log. You receive raw signals from their Mac.

Output EXACTLY this structure and nothing else:

## OVERVIEW
<2 to 4 sentences on what the day was about. Plain, factual, no praise, no filler.>

## BULLETS
- [Domain] One achievement, lesson or milestone. 1-3 sentences maximum.
- [Domain] Another one.

Rules:
- Write AT LEAST {min_bullets} bullets, up to 12. The input below covers a whole day of
  work: coding sessions, pages read, apps used, commits, files. Two bullets is a
  failure - it means you summarised the summary. Go through each input section in
  turn and pull out what was actually learned or achieved in it.
- Be specific. "Worked on AI tooling" is useless in a year. "Fixed the markitdown
  MCP server failing on PDF input" is the kind of sentence worth re-reading.
- Prefer these domains: {domains}.
- If something genuinely fits none of them, write [New: Name] - but only for a real,
  recurring theme, never for a one-off.
- One bullet = one thing. Never a paragraph. Never more than 3 sentences.
- **Opening something is not doing something.** A repository, a document or a
  page that was merely open is NOT an achievement. Report work only where the
  input shows real evidence of it: a git commit, a coding-agent session with
  actual prompts, a file downloaded, or the owner's own diary. If all you have
  is a window title or a page visit, say nothing about it.
- Never write that they "worked on", "developed", "enhanced" or "read" something
  when the only evidence is that it appeared on screen. Prefer writing fewer
  bullets over inventing activity.
- Do NOT invent anything that is not in the input.
- Do NOT repeat the owner's own diary bullets; they are already recorded separately.
- Skip noise: idle browsing, tab titles with no substance, routine app switching.
- English. No emoji. No closing summary."""


def build_prompt(con: sqlite3.Connection, day: str) -> Tuple[str, str]:
    rows = db.events_for_day(con, day)
    by_source = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)

    parts = []

    sessions = by_source.get("claude_code", [])
    if sessions:
        parts.append("EVIDENCE — coding-agent sessions ({} today), real work:".format(len(sessions)))
        for row in sessions[:30]:
            parts.append("  - {}: {}".format(row["title"], (row["body"] or "")[:400]))


    visits = by_source.get("browser", [])
    if visits:
        dwell = {}
        for row in visits:
            key = (row["title"] or "").strip()[:80]
            if not key:
                continue
            slot = dwell.setdefault(key, {"secs": 0.0, "url": row["body"] or "", "n": 0})
            slot["secs"] += row["duration_s"] or 0.0
            slot["n"] += 1
        ranked = sorted(dwell.items(), key=lambda kv: -kv[1]["secs"])
        kept = [(t, d) for t, d in ranked if d["secs"] >= config.MIN_DWELL_S]
        parts.append(
            "Web pages that held attention (>= {}s; {} of {} unique titles):".format(
                int(config.MIN_DWELL_S), len(kept), len(ranked)))
        for title, d in kept[:35]:
            parts.append("  - {} [{} min] — {}".format(
                title, max(1, int(d["secs"] // 60)), d["url"][:80]))
        if not kept:
            parts.append("  (nothing held attention long enough to count)")

    apps = {}
    for row in by_source.get("activitywatch", []):
        if row["kind"] != "app_use":
            continue
        apps[row["title"]] = apps.get(row["title"], 0.0) + (row["duration_s"] or 0)
    apps = {a: s for a, s in apps.items() if s >= config.MIN_APP_S}
    if apps:
        parts.append("App focus time (minutes; apps under {} min omitted):".format(
            int(config.MIN_APP_S // 60)))
        for app, secs in sorted(apps.items(), key=lambda kv: -kv[1])[:12]:
            parts.append("  - {}: {}".format(app, int(secs // 60)))

    commits = by_source.get("git", [])
    if commits:
        parts.append("EVIDENCE — git commits (real, delivered work):")
        parts.extend("  - {}: {}".format(r["title"], r["body"]) for r in commits[:20])

    files = by_source.get("downloads", [])
    if files:
        parts.append("EVIDENCE — files downloaded: " + ", ".join(r["title"] for r in files[:15]))

    manual = by_source.get("journal", [])
    if manual:
        parts.append("The owner's own diary bullets (CONTEXT ONLY — do not reproduce these):")
        parts.extend("  - {}".format((r["body"] or "").replace("\n", " / ")[:300])
                     for r in manual)

    if not parts:
        parts.append("(no signals captured for this day)")

    domain_list = ", ".join(row["label"] for row in classify.active(con))
    system = SYSTEM.format(domains=domain_list, min_bullets=config.MIN_BULLETS)
    return system, "DAY {}\n\n".format(day) + "\n".join(parts)


def _signal_is_rich(con: sqlite3.Connection, day: str) -> bool:
    """Only push for more bullets when there is genuinely more to say."""
    counts = db.source_counts(con, day)
    return (counts.get("claude_code", 0) + counts.get("git", 0) >= 2
            or counts.get("browser", 0) >= 20
            or sum(counts.values()) >= 50)


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "into",
    "about", "including", "your", "you", "his", "her", "their", "have", "has",
    "had", "not", "but", "are", "its", "it's", "new", "also", "some", "more",
    "using", "used", "use", "made", "make", "get", "got", "one", "two", "day",
    "today", "learned", "learn", "explored", "explore", "worked", "work",
    "studied", "study", "reviewed", "review", "did", "done",
}
ECHO_THRESHOLD = 0.6
ECHO_MIN_SHARED = 2


def _tokens(text: str) -> set:
    """Content words, crudely stemmed. Good enough to spot an echo."""
    words = re.findall(r"[\w']+", (text or "").lower(), re.UNICODE)
    out = set()
    for word in words:
        if len(word) < 3 or word in STOPWORDS or word.isdigit():
            continue
        out.add(word[:-1] if word.endswith("s") and len(word) > 4 else word)
    return out


def echoes_human(ai_text: str, human_texts) -> bool:
    """True if an AI bullet is just restating something the owner already wrote.

    The model was told not to reproduce diary bullets and did it anyway — it
    translated a diary entry written in another language into English and
    re-published it as its own observation. A prompt is not an enforcement mechanism, so this is checked
    in code.

    Comparison is line by line, not against the whole block. The owner's
    bullets are nested — "Replied to a recruiter in email" is a child
    three levels down — and measuring against the whole block dilutes any real
    match to nothing.
    """
    ai_tokens = _tokens(ai_text)
    if not ai_tokens:
        return False
    for human in human_texts:
        candidates = [line for line in (human or "").splitlines() if line.strip()]
        candidates.append(human)
        for candidate in candidates:
            human_tokens = _tokens(candidate)
            if len(human_tokens) < 3:
                continue
            shared = ai_tokens & human_tokens
            if len(shared) < ECHO_MIN_SHARED:
                continue
            overlap = len(shared) / float(min(len(ai_tokens), len(human_tokens)))
            if overlap >= ECHO_THRESHOLD:
                return True
    return False


BULLET_PATTERNS = [
    re.compile(r"^\s*[-*]\s*\[([^\]]{2,40})\]\s*:?\s*(.+?)\s*$"),
    re.compile(r"^\s*[-*]\s*\*\*([^*]{2,40})\*\*\s*:?\s*(.+?)\s*$"),
    re.compile(r"^\s*[-*]\s*([A-Z][\w /&+-]{2,38}):\s+(.+?)\s*$"),
]
PLAIN_BULLET_RE = re.compile(r"^\s*[-*]\s+(.{4,})$")


def parse(raw: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Returns (overview, [(domain_label, text), ...]). Untagged bullets are kept
    with an empty label so a formatting slip never discards real content."""
    if "## BULLETS" in raw:
        head, tail = raw.split("## BULLETS", 1)
        overview = head.replace("## OVERVIEW", "").strip()
    else:
        overview, tail = "", raw
    if overview.startswith("#"):
        overview = overview.lstrip("#").strip()

    parsed = []
    for line in tail.splitlines():
        for pattern in BULLET_PATTERNS:
            match = pattern.match(line)
            if match:
                parsed.append((match.group(1).strip(), match.group(2).strip()))
                break
        else:
            plain = PLAIN_BULLET_RE.match(line)
            if plain and not plain.group(1).startswith("["):
                parsed.append(("", plain.group(1).strip()))
    return overview, parsed


def store_human_bullets(con: sqlite3.Connection, day: str) -> int:
    """Verbatim, straight from the diary. The model is not involved.

    Existing bullets are re-filed if the rules have improved since, but their
    text is never touched and a category the owner set by hand is never moved.
    """
    count = 0
    texts = []
    for row in db.events_for_day(con, day, source="journal"):
        text = row["body"] or row["title"] or ""
        if not text.strip():
            continue
        texts.append(text)
        category = classify.rule_match(con, text)
        new_id = bullets_mod.add_human(con, day, text, category, edited_by="diary")
        if new_id is not None:
            count += 1
        else:
            existing = con.execute(
                "SELECT id, edited_by FROM bullets "
                "WHERE day=? AND origin='human' AND status='current' AND text=?",
                (day, text)).fetchone()
            if existing:
                bullets_mod.reclassify(con, existing["id"], category)


                if existing["edited_by"] != "diary":
                    con.execute("UPDATE bullets SET edited_by='diary' WHERE id=?",
                                (existing["id"],))
                    con.commit()


    live = _live_diary_texts(day)
    if live is not None:
        bullets_mod.reconcile_diary(con, day, live)
    return count


def _live_diary_texts(day: str):
    """Current diary bullets straight from the file. None if unreadable, so a
    missing vault never causes bullets to be retired."""
    from .ingest.obsidian import ObsidianDiary
    import datetime
    source = ObsidianDiary()
    if not source.available():
        return None
    events, error = source.safe_fetch(datetime.date.fromisoformat(day))
    if error:
        return None
    return [e.body for e in events if e.body]


def generate(con: sqlite3.Connection, day: str, model=None):
    """Returns (status, attempts, error). status: ok | partial."""
    model = model or models.get_model()
    system, user = build_prompt(con, day)
    raw, attempts, error = models.complete_with_retries(model, system, user)

    if raw is None:
        return "partial", attempts, error

    human_texts = [r["text"] for r in bullets_mod.for_day(con, day)
                   if r["origin"] == "human"]

    def clean(text):
        """Parse, then drop anything that merely restates the owner's entries.
        Filtering BEFORE the count check matters: otherwise a retry is judged
        on bullets that are about to be thrown away."""
        head, items = parse(text)
        keep = [(label, body) for label, body in items
                if not echoes_human(body, human_texts)]
        return head, keep, len(items) - len(keep)

    overview, parsed, dropped = clean(raw)
    if not parsed:
        return "partial", attempts, "model returned no usable bullets"


    if len(parsed) < config.MIN_BULLETS and _signal_is_rich(con, day):
        nudge = (user + "\n\nYour previous answer had only {} usable bullets. That "
                 "is too few for this much activity, and do not restate anything "
                 "from their own diary. Re-read every section above and produce at "
                 "least {} distinct, specific bullets.".format(
                     len(parsed), config.MIN_BULLETS))
        retry_raw, retry_attempts, _ = models.complete_with_retries(
            model, system, nudge, attempts=1)
        attempts += retry_attempts
        if retry_raw:
            retry_overview, retry_parsed, retry_dropped = clean(retry_raw)
            if len(retry_parsed) > len(parsed):
                overview = retry_overview or overview
                parsed, dropped = retry_parsed, retry_dropped

    bullets_mod.supersede_ai_for_day(con, day)
    for label, text in parsed:
        if label.lower().startswith("new:"):
            category = classify.propose_category(
                con, label.split(":", 1)[1].strip(), reason="proposed on " + day)
        elif label:
            category = classify.resolve(con, label) or classify.rule_match(con, text)
        else:
            category = classify.rule_match(con, text)
        bullets_mod.propose(con, day, category, text)

    db.save_digest(con, "day", day, model.id, overview)
    if dropped:
        print("[info] dropped {} model bullet(s) echoing your own".format(dropped))
    return "ok", attempts, None
