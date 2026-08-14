#!/usr/bin/env python3
"""Open the log in a browser. Local only, nothing leaves the Mac.

    ./bin/serve.py                 # pick a free port, open the browser
    ./bin/serve.py --port 8765
    ./bin/serve.py --read-only     # refuse writes
    ./bin/serve.py --no-open       # just print the URL

Bound to 127.0.0.1 and protected by a token regenerated every start, so no
other machine — and no website you happen to have open — can read it.
"""
from __future__ import annotations

import argparse
import pathlib
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import db, web   # noqa: E402


def watch_parent(interval: float = 2.0) -> None:
    """Exit if whoever launched us goes away.

    The Tauri shell kills this process on a clean window close, but a crash or
    a force-quit never runs that handler — and a stray server holding the diary
    open is both a leak and a surprise. On Unix an orphan is re-parented to
    init, so a PPID of 1 means the launcher is gone.
    """


    if not os.environ.get("LIFELOG_TOKEN") or os.getppid() == 1:
        return

    def loop():
        while True:
            time.sleep(interval)
            if os.getppid() == 1:
                os._exit(0)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


def free_port(preferred: int = 0) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="default: any free port")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    con = db.connect()
    db.init_db(con)
    con.close()

    port = free_port(args.port)


    token = os.environ.get("LIFELOG_TOKEN") or secrets.token_urlsafe(16)
    url = "http://127.0.0.1:{}/?t={}".format(port, token)

    watch_parent()
    server = web.serve(port, token, args.read_only)
    print("Life Log  →  {}".format(url), flush=True)
    print("           read-only: {} · Ctrl-C to stop".format(bool(args.read_only)),
          flush=True)

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
