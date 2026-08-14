"""Local HTTP server exposing the log to a browser (D-032, option G).

Design rule: **this module contains no query logic.** It routes, checks the
token, and serialises. Everything it returns comes from `query.py`, the same
layer `ask.py` and the MCP server use. That is what stops a second
implementation from existing (see UI_DESIGN.md §1).

Security, because this process can read the whole diary:
  - binds 127.0.0.1 only, never 0.0.0.0
  - every /api/ request needs a token generated fresh at startup
  - no CORS headers, so a random website cannot call it
  - writes are refused entirely in --read-only mode
  - the access log records route and status, never content
"""
from __future__ import annotations

import json
import mimetypes
import posixpath
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import config, db, query

UI_DIR = config.REPO / "ui"
TOKEN_HEADER = "X-Lifelog-Token"


class ReadOnlyServer(Exception):
    """A write reached a --read-only server. Mapped to HTTP 403."""


FALLBACK_HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lifelog</title>
<style>
  body {{ font: 16px/1.5 -apple-system, sans-serif; max-width: 40rem;
         margin: 3rem auto; padding: 0 1rem; color: #222; }}
  code {{ background: #f2f2f2; padding: .1em .35em; border-radius: 4px; }}
  pre  {{ background: #f8f8f8; padding: 1rem; border-radius: 8px; overflow-x: auto; }}
</style>
<h1>lifelog</h1>
<p>The server is running. This build has no bundled web interface — your log
lives in <code>logs/</code> as plain markdown, and everything the server knows
is available as JSON:</p>
<pre id="stats">loading /api/stats…</pre>
<p>Endpoints: <code>/api/stats</code> · <code>/api/day?day=YYYY-MM-DD</code> ·
<code>/api/search?q=…&amp;mode=semantic</code> ·
<code>/api/domains</code> · <code>/api/metrics</code></p>
<p>Every call needs the token from the URL you were given, as
<code>?t=…</code> or the <code>X-Lifelog-Token</code> header.</p>
<script>
  const t = new URLSearchParams(location.search).get("t");
  fetch("/api/stats", {{headers: {{"X-Lifelog-Token": t}}}})
    .then(r => r.json())
    .then(d => document.getElementById("stats").textContent =
               JSON.stringify(d, null, 2))
    .catch(e => document.getElementById("stats").textContent = String(e));
</script>
"""


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Router(object):
    """Maps a path + query params onto query.py. Additive by design: a new
    endpoint is one line here plus one function there — never two languages."""

    def __init__(self, con: sqlite3.Connection, read_only: bool = False):
        self.con = con
        self.read_only = read_only

    def get(self, path: str, params: dict):
        one = lambda k, d=None: params.get(k, [d])[0]        # noqa: E731

        if path == "/api/stats":
            return query.stats(self.con)
        if path == "/api/day":
            return query.get_day(self.con, one("day"))
        if path == "/api/range":
            return query.get_range(self.con, one("start"), one("end"),
                                   one("domain"), _int(one("limit"), 200))
        if path == "/api/domains":
            return query.domain_summary(self.con, one("start", "0000-01-01"),
                                        one("end", "9999-12-31"))
        if path == "/api/metrics":
            return query.get_metrics(self.con, one("start"), one("end"), one("metric"))
        if path == "/api/search":
            text, mode = one("q", ""), one("mode", "keyword")
            if not text:
                return []
            if mode == "semantic":
                return query.semantic_search(self.con, text, _int(one("limit"), 20))
            return query.search_bullets(self.con, text, _int(one("limit"), 50),
                                        one("origin"))
        if path == "/api/reviews":
            return query.list_reviews(self.con, one("period"))
        if path == "/api/review":
            return query.get_review(self.con, one("period"), one("key"))
        if path == "/api/categories":
            return query.list_categories(self.con)
        if path == "/api/health":
            return query.health(self.con, _int(one("limit"), 20))
        return None

    def post(self, path: str, body: dict):
        if self.read_only:
            raise ReadOnlyServer()
        if path == "/api/bullet/text":
            return query.edit_bullet(self.con, body.get("bullet_id"),
                                     body.get("text", ""))
        if path == "/api/bullet/category":
            return query.set_bullet_category(
                self.con, body.get("bullet_id"), body.get("category_id"))
        if path == "/api/category/new":
            label = (body or {}).get("label", "").strip()
            return query.create_category(self.con, label) if label else {"error": "label required"}
        if path == "/api/category/decide":
            return query.decide_category(self.con, body.get("id"), body.get("action"))
        if path == "/api/note":
            text = (body or {}).get("text", "").strip()
            if not text:
                return {"error": "text is required"}
            return query.add_note(self.con, text, (body or {}).get("day"))
        return None


def make_handler(token: str, read_only: bool) -> Callable:
    class Handler(BaseHTTPRequestHandler):
        server_version = "lifelog"


        def log_message(self, fmt, *args):
            """Route and status only. Never query strings — they carry search
            terms, which are diary content."""
            print("[web] {} {}".format(self.command, self.path.split("?")[0]))

        def _send(self, status: int, payload, content_type="application/json"):
            data = (payload if isinstance(payload, bytes)
                    else json.dumps(payload, ensure_ascii=False, default=str).encode())
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self, params: dict) -> bool:
            supplied = self.headers.get(TOKEN_HEADER) or params.get("t", [None])[0]
            return supplied == token


        def do_GET(self):                                    # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path.startswith("/api/"):
                if not self._authorised(params):
                    return self._send(403, {"error": "bad or missing token"})
                con = db.connect()
                try:
                    result = Router(con, read_only).get(parsed.path, params)
                finally:
                    con.close()
                if result is None:
                    return self._send(404, {"error": "no such endpoint"})
                return self._send(200, result)
            return self._static(parsed.path)

        def do_POST(self):                                   # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if not self._authorised(params):
                return self._send(403, {"error": "bad or missing token"})
            length = _int(self.headers.get("Content-Length"), 0)
            if length > 100_000:
                return self._send(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            con = db.connect()
            try:
                result = Router(con, read_only).post(parsed.path, body)
            except ReadOnlyServer:
                return self._send(403, {"error": "server is read-only"})
            finally:
                con.close()
            if result is None:
                return self._send(404, {"error": "no such endpoint"})
            return self._send(200, result)

        def _static(self, path: str):
            """Serve ui/. Path is normalised against the ui directory so
            ../../etc/passwd cannot escape it."""
            rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
            target = (UI_DIR / (rel or "index.html")).resolve()
            try:
                target.relative_to(UI_DIR.resolve())
            except ValueError:
                return self._send(403, {"error": "forbidden"})
            if not target.is_file():
                if rel in ("", "index.html") and not UI_DIR.is_dir():
                    return self._send(200, FALLBACK_HTML.encode(), "text/html")
                return self._send(404, {"error": "not found"})
            kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), kind)

    return Handler


def serve(port: int, token: str, read_only: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, read_only))
    return server
