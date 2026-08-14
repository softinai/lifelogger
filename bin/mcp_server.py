#!/usr/bin/env python3
"""MCP server over life.db — lets any MCP client query the log.

Speaks MCP over stdio as plain JSON-RPC 2.0. No SDK, no pip install: the whole
protocol surface needed here is `initialize`, `tools/list` and `tools/call`.
That keeps the zero-dependency rule intact (R-031).

Register with Claude Code:

    claude mcp add lifelog -- /usr/bin/python3 \\
        "<repo>/bin/mcp_server.py"

Or in a client's JSON config:

    {"mcpServers": {"lifelog": {"command": "/usr/bin/python3",
                                "args": ["<repo>/bin/mcp_server.py"]}}}

⚠️  Privacy: this server is local, but the *client* decides where your data
goes. A cloud client sends every returned row to its provider. For a fully
local path use bin/ask.py instead. Run with --read-only to forbid add_note.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import db, query   # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
READ_ONLY = "--read-only" in sys.argv


def tool_list():
    allowed = query.READ_ONLY if READ_ONLY else {t["name"] for t in query.TOOLS}
    return [{"name": t["name"], "description": t["description"],
             "inputSchema": t["parameters"]}
            for t in query.TOOLS if t["name"] in allowed]


def handle(request, con):
    method = request.get("method", "")
    request_id = request.get("id")

    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "lifelog", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        allowed = {t["name"] for t in tool_list()}
        if name not in allowed:
            result = {"content": [{"type": "text",
                                   "text": "tool not available: " + name}],
                      "isError": True}
        else:
            payload = query.call(con, name, arguments)
            is_error = isinstance(payload, dict) and "error" in payload
            result = {"content": [{"type": "text",
                                   "text": json.dumps(payload, ensure_ascii=False,
                                                      indent=2, default=str)}],
                      "isError": is_error}
    elif method in ("ping",):
        result = {}
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "method not found: " + method}}

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    con = db.connect()
    db.init_db(con)
    out = sys.stdout

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(request, con)
        except Exception as exc:                            # noqa: BLE001
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
