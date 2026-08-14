# lifelogger

A local-first personal progress tracker.

Captures what you do each day from automatic sources — activity tracking, AI
coding sessions, browser history, git, shell, health data — plus what you write
by hand. Stores it as an append-only event log and generates daily, weekly,
monthly and yearly summaries of skills gained and progress made.

## Principles

- **Local only.** No data leaves the machine. No telemetry, no cloud storage,
  no external API in the automatic path.
- **Append-only.** Raw events are never updated or deleted. Summaries are
  derived and disposable; raw data is not.
- **Idempotent.** Every ingester is safe to re-run over any day, forever.
- **Your writing is yours.** Hand-written notes are never overwritten by
  automation.
- **Degrade, don't fail.** A missing tool or a stopped service produces a
  warning and a partial run, never a blank day.

## Status

Early. This README is a placeholder while the source is prepared for release.

## Stack

Python standard library only — runs on a stock interpreter with no third-party
runtime dependencies. SQLite for storage. A local LLM for classification.

## License

TBD.
