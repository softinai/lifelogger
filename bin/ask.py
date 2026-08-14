#!/usr/bin/env python3
"""Ask questions about your own log. Fully local — Ollama + life.db, nothing leaves.

    ./bin/ask.py "when did I first work on n8n?"
    ./bin/ask.py "compare my sport activity in the first and second week of August"
    ./bin/ask.py "what did I learn about Goodhart's Law?"
    ./bin/ask.py --note "ran 5k this morning"
    ./bin/ask.py            # interactive

The model may only read; `add_note` is reachable through --note, never by the
model deciding to write on its own.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import config, db, models, query   # noqa: E402

SYSTEM = """You answer questions about one person's personal progress log.

You have tools that read their database. Always call a tool before answering -
never guess, never answer from memory.

If search_bullets returns nothing, do NOT conclude the log is empty. Try
semantic_search with the same question, or search_bullets again with a synonym -
they may have written 'gym' where you searched 'workout'. Only after a second
attempt finds nothing should you say there is no record of it.

Bullets with origin 'human' are their own words and are authoritative. Bullets with
origin 'ai' were generated from machine activity and can be wrong; where they
conflict, trust the human ones.

Answer in a few sentences. Cite dates. No praise, no filler.

FACTS ABOUT THIS LOG - use these, never guess a date range:
- today is {today}
- the log covers {first} to {last} ({days} days logged)
- domains in use: {domains}
When a question has no explicit dates, cover the whole log span above."""

MAX_STEPS = 5


def ollama_chat(messages, tools, model_name):
    payload = json.dumps({
        "model": model_name, "stream": False,
        "messages": messages,
        "tools": [{"type": "function", "function": t} for t in tools],
        "options": {"temperature": 0.1},
    }).encode("utf-8")
    request = urllib.request.Request(
        config.OLLAMA_BASE + "/api/chat", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=config.MODEL_TIMEOUT_S) as response:
        return json.load(response).get("message", {})


def build_system(con) -> str:
    """A 7B model will happily invent a 2023 date range and report 'no data'.
    Handing it the real span up front removes that whole failure mode."""
    facts = query.stats(con)
    import datetime
    return SYSTEM.format(
        today=datetime.datetime.now(config.TZ).date().isoformat(),
        first=facts["first_day"] or "-", last=facts["last_day"] or "-",
        days=facts["days_logged"],
        domains=", ".join(facts["domains"]) or "none yet")


def answer(con, question: str, model_name: str, verbose: bool = False) -> str:
    tools = [t for t in query.TOOLS if t["name"] in query.READ_ONLY]
    messages = [{"role": "system", "content": build_system(con)},
                {"role": "user", "content": question}]

    for _ in range(MAX_STEPS):
        message = ollama_chat(messages, tools, model_name)
        calls = message.get("tool_calls") or []
        if not calls:
            return (message.get("content") or "").strip() or "(no answer)"
        messages.append(message)
        for call in calls:
            function = call.get("function", {})
            name = function.get("name", "")
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if name not in query.READ_ONLY:
                result = {"error": "tool {} is not permitted here".format(name)}
            else:
                result = query.call(con, name, arguments)
            if verbose:
                print("  → {}({}) → {} rows".format(
                    name, json.dumps(arguments, ensure_ascii=False)[:80],
                    len(result) if isinstance(result, list) else 1), file=sys.stderr)
            messages.append({"role": "tool", "name": name,
                             "content": json.dumps(result, ensure_ascii=False,
                                                   default=str)[:6000]})
    return "(gave up after {} tool steps)".format(MAX_STEPS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="*")
    parser.add_argument("--model", default=config.MODEL)
    parser.add_argument("--note", help="record your own bullet, no model involved")
    parser.add_argument("--day", help="YYYY-MM-DD for --note")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show which tools were called")
    args = parser.parse_args()

    con = db.connect()
    db.init_db(con)

    if args.note:
        result = query.add_note(con, args.note, args.day)
        print("{} [{}] {}".format(
            "added" if result["added"] else "already present",
            result["domain"] or "unfiled", result["day"]))
        return 0

    model = models.get_model(args.model)
    models.assert_local(model)
    if not model.health():
        print("Ollama is not responding at {}".format(config.OLLAMA_BASE),
              file=sys.stderr)
        return 2

    if args.question:
        print(answer(con, " ".join(args.question), args.model, args.verbose))
        return 0

    print("Ask about your log. Empty line or Ctrl-D to quit.\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            break
        print(answer(con, question, args.model, args.verbose), "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
