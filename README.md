# lifelogger

Turns what you already do on your Mac into a daily record of what you actually
learned. **Local-first: nothing leaves the machine.**

Reads your browser history, coding-agent sessions, git commits, ActivityWatch
and your own diary → stores raw events in SQLite → a **local** LLM writes the
day's bullets → markdown you can read in Obsidian, plus weekly, monthly and
yearly reviews.

```bash
./bin/nightly.sh                              # build yesterday
./bin/ask.py "when did I last work on Claude Code?"   # ask your own log
./bin/serve.py                                # read it in a browser
```

The rest of the surface: `bin/rollup.py` (weekly / monthly / yearly reviews,
chained after the nightly run) · `bin/approve.py` (approve or merge a proposed
domain) · `bin/mcp_server.py` (query the log from any MCP client, stdio, no
SDK).

## Why it's built this way

- **Your words are never altered.** Bullets you write are `origin='human'` and
  no automated path can edit, re-file or delete them. Enforced in code, with
  tests — not by prompt instructions.
- **Raw events are append-only.** Summaries are disposable: you can regenerate
  three years of reviews with a better model whenever one arrives.
- **Idempotent.** Every ingester is safe to re-run over any day, forever.
- **Degrade, don't fail.** A missing tool or a stopped service produces a
  warning and a partial run, never a blank day.
- **Zero dependencies.** Runs on macOS's stock `/usr/bin/python3`. No venv, no
  pip, no Docker.

## Sources

Obsidian diary (opt-in) · Claude Code / Codex / OpenCode / Antigravity
sessions · browser history (Chrome, Edge) · git commits · ActivityWatch ·
Apple Health · downloads. Each is one file in `lifelog/ingest/`; any source
that is absent on your machine is skipped, never fatal.

## Requirements

- macOS with the stock `/usr/bin/python3` (3.9+)
- [Ollama](https://ollama.com) running locally for summarisation and search
  (`LIFELOG_MODEL` selects the model, default `qwen2.5:7b`)
- Optional: [ActivityWatch](https://activitywatch.net) for app/window time

## Setup

```bash
ollama pull qwen2.5:7b && ollama pull nomic-embed-text
git clone https://github.com/softinai/lifelogger.git && cd lifelogger
./bin/nightly.py --day yesterday --dry-run   # see what a night would produce
./bin/nightly.sh                             # build yesterday for real
```

The full walkthrough — every command tested, expected output shown, scheduling,
troubleshooting: **[docs/guides/SETUP.md](docs/guides/SETUP.md)**.

Everything machine-specific goes in `config/local.json` (gitignored). All of it
is optional:

```json
{
  "timezone": "Europe/Berlin",
  "obsidian_diary_dir": "~/Documents/MyVault/Journal",
  "git_scan_roots": ["~/code", "~/work"]
}
```

- **`timezone`** — what defines a "day". Defaults to your system timezone.
- **`obsidian_diary_dir`** — your hand-written diary, read-only. **Opt-in:**
  leave it out and the tool never goes near your notes. Expects
  `<dir>/<year>/<MM-Month>.md` with `DD/MM/YYYY` date lines inside.
- **`git_scan_roots`** — where your repositories live; repos are found one or
  two levels below each root. Defaults to whichever of `~/Projects`,
  `~/Developer`, `~/code`, `~/src`, `~/repos` exist.

`day` is local-time while `ts_utc` is UTC — the wrong zone silently files every
evening event under the wrong day, so there is a test for that boundary.

To run it nightly, copy `config/com.lifelog.nightly.plist.example` to
`~/Library/LaunchAgents/`, replace the placeholders, and `launchctl load` it.

## Tests

```bash
/usr/bin/python3 -m unittest discover -s tests
```

106 cases, stdlib only, no network. They cover the things that are expensive to
get wrong: idempotent re-runs, timezone day boundaries, the `origin='human'`
guarantee, echo suppression, and the loopback server's auth.

## Adding a source

One file in `lifelog/ingest/`, one interface. See [CONTRIBUTING.md](CONTRIBUTING.md).


## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
