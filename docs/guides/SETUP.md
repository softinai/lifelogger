# Setup guide

Every command in this guide has been run, in this order, on a clean machine
profile. Expected output is shown so you know what "working" looks like.

## What you need

| Requirement | Check with | Notes |
|---|---|---|
| macOS with stock Python 3.9+ | `/usr/bin/python3 --version` | Ships with macOS. No pip, no venv, nothing to install |
| [Ollama](https://ollama.com) | `curl -s http://localhost:11434/api/version` | Powers summaries, Q&A and search |
| ~6 GB disk for models | — | 4.7 GB `qwen2.5:7b` + 0.3 GB `nomic-embed-text` |
| Optional: [ActivityWatch](https://activitywatch.net) | `curl -s http://localhost:5600` | App and window time. Skipped if absent |

Pull the two models once:

```bash
ollama pull qwen2.5:7b        # writes the summaries (swap via LIFELOG_MODEL)
ollama pull nomic-embed-text  # powers semantic search
```

## 1. Clone and take a dry look

```bash
git clone https://github.com/softinai/lifelogger.git
cd lifelogger
./bin/nightly.py --day yesterday --dry-run --no-notify
```

The dry run prints the entry it *would* write and touches no files. Expected
tail:

```
[gather] journal=0/0  ... activitywatch=876/876  git=0/0  downloads=0/0
...
[done] 2026-08-14=ok
```

Sources showing `0/0` are simply absent on your machine — that is normal, the
run continues without them. If everything is `0/0` the day is marked `failed`
but still explained; add sources below.

## 2. First real run

```bash
./bin/nightly.sh
```

This builds yesterday with retries, then any due rollups. Your log appears at:

```
logs/<year>/<YYYY-MM>.md   daily entries, newest first
logs/Summary.md            weekly / monthly / yearly reviews
logs/Dashboard.md          streaks, sparklines, domain charts
```

Open them in anything that reads markdown — Obsidian, VS Code, `less`.

## 3. Point it at your life (`config/local.json`)

Create `config/local.json` (gitignored — it never leaves your machine). All
keys optional:

```json
{
  "timezone": "Europe/Berlin",
  "obsidian_diary_dir": "~/MyJournal",
  "git_scan_roots": ["~/code", "~/work"]
}
```

- **`timezone`** — what defines a "day". Defaults to the system timezone.
  Matters: an evening event files under the wrong day in the wrong zone.
- **`obsidian_diary_dir`** — your hand-written diary, **opt-in and read-only**.
  Unset, the tool never goes near your notes. Layout it expects:

  ```
  ~/MyJournal/2026/08-August.md     ← <dir>/<year>/<MM-Month>.md
  ```

  with entries like:

  ```markdown
  14/08/2026
  - tried a new pasta recipe
      - carbonara, no cream
  ```

  Bullets are stored verbatim — nesting, punctuation and language preserved —
  and the model is forbidden (in code, with tests) from rewriting them.
- **`git_scan_roots`** — where your repos live; found one or two levels below
  each root. Defaults to whichever of `~/Projects`, `~/Developer`, `~/code`,
  `~/src`, `~/repos` exist.

Re-run a day after config changes:

```bash
./bin/nightly.py --day 2026-08-14 --force --no-notify
```

Expected: your diary lines appear under 🧍, model bullets under 🤖:

```
[gather] journal=1/1  ...
[write] logs/2026/2026-08.md  (bullets: 1 mine, 4 model)
```

## 4. Talk to your log

```bash
./bin/ask.py "when did I cook something?"
./bin/ask.py --note "shipped the new ingester"    # add a bullet by hand
./bin/ask.py                                      # interactive session
```

Answers come from your database through the local model; asking about the
pasta entry above returns the date and the detail. `--note` bullets are
`origin='human'`: no automated step can ever edit or delete them.

## 5. Browse it

```bash
./bin/serve.py
```

Prints `Life Log  →  http://127.0.0.1:<port>/?t=<token>` and opens it.
Loopback only, token regenerated each start — nothing on your network can
read it. `--read-only` refuses writes (HTTP 403), `--no-open` just prints
the URL, `--port N` pins the port. The JSON API lives under `/api/`
(`/api/stats`, `/api/day?day=…`, `/api/search?q=…&mode=semantic`).

## 6. Run it nightly (launchd)

```bash
sed -e "s|\$HOME/PATH/TO/lifelogger|$PWD|" \
    -e "s|Region/City|$(readlink /etc/localtime | sed 's|.*zoneinfo/||')|" \
    config/com.lifelog.nightly.plist.example \
    > ~/Library/LaunchAgents/com.lifelog.nightly.plist
launchctl load ~/Library/LaunchAgents/com.lifelog.nightly.plist
```

Runs at **00:01** for the day that just ended, with 3 retries. Check on it:

```bash
launchctl list | grep lifelog
tail -20 /tmp/lifelog.out.log
```

Undo with `launchctl unload` of the same path.

## 7. Reviews and domains

Weekly, monthly and yearly reviews generate themselves when a period closes
(Sunday → week, last day of month → month, 31 Dec → year). Force one anytime:

```bash
./bin/rollup.py --period week --key 2026-W33 --force
```

Bullets are filed into domains (Sport, Career, AI Engineering, …). The model
may *propose* a new domain but can never create one silently — proposals wait
for you:

```bash
./bin/approve.py --list              # see proposals and active domains
./bin/approve.py <id>                # approve
./bin/approve.py --reject <id>       # or fold into another: --merge <from> <into>
```

## 8. Query from an MCP client (optional)

```bash
claude mcp add lifelog -- /usr/bin/python3 /ABSOLUTE/PATH/TO/lifelogger/bin/mcp_server.py
```

Exposes 11 read tools (`search_bullets`, `semantic_search`, `get_day`,
`stats`, …) over stdio to Claude Code or any MCP client. Local process, no
network, same read-only guarantees.

## Backfill and repair

Everything is idempotent — re-running any day is always safe and never
duplicates:

```bash
./bin/nightly.py --backfill 7          # rebuild the last 7 days
./bin/nightly.py --day 2026-08-11 --force
```

## When something goes wrong

| Symptom | Meaning | Fix |
|---|---|---|
| `[warn] model: ollama unreachable` | Ollama not running | `ollama serve`, or open the app. The night still writes; re-run with `--force` for prose |
| `Run partial` banner in the month file | A source or the model failed | The banner names the cause. Data already captured is kept |
| `Run failed — no events captured` | No sources found anything | Configure `local.json`, or check the services above |
| `--day expects YYYY-MM-DD` | Typo in the date | `today`, `yesterday`, or an ISO date |
| A day looks wrong | Wrong timezone at capture time | Set `timezone`, then `--day <day> --force` |
| `journal: DiaryUnavailable … evicted by iCloud` | iCloud offloaded your diary's contents to save disk | The run already asked iCloud for the file (`brctl download`) and will pick it up on retry or re-run. To stop it recurring: Finder → right-click the diary folder → *Keep Downloaded* |

Every failure is written into the log itself — a bad night produces a note
explaining what broke and the exact command to redo it, never a blank page.
