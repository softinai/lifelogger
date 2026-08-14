# Contributing

The most useful contribution is a new source. That is one file in
`lifelog/ingest/`, and nothing else.

## The Source contract

Subclass `Source` from `lifelog/ingest/base.py` and implement two methods:

```python
from .base import Event, Source

class MySource(Source):
    name = "mysource"

    def available(self) -> bool:
        """False when the tool isn't installed. The run skips you, never fails."""
        return SOME_PATH.exists()

    def fetch(self, day):
        """Yield Events for this local day."""
        yield Event.make(when_utc, source=self.name, kind="note",
                         dedupe_key="mysource:{}".format(stable_id),
                         title=..., body=..., duration_s=...)
```

Then add it to the `SOURCES` list in `bin/nightly.py` — that list is the registry.

## The three rules

1. **`dedupe_key` must be stable.** It is what makes re-runs free. The same
   event on the same day must produce the same key forever. Anything with a
   timestamp, a row id that renumbers, or a random component is not stable.
2. **`day` is local time, `ts_utc` is UTC.** Use `Event.make`, which derives
   `day` for you. Deriving it yourself silently shifts every evening event to
   the wrong day.
3. **Degrade, don't fail.** A source that raises must not take down the
   nightly run. Return what you have; `safe_fetch` records the error.

## Never

- Write to a user's diary or notes. Input directories are read-only.
- Touch bullets with `origin='human'`. They are the user's own words and no
  automated path may edit, re-file or delete them.
- Add a runtime dependency. The nightly path runs on macOS's stock
  `/usr/bin/python3` with nothing installed — that is a hard requirement, not
  a preference.
- Send anything off the machine.

## Before you open a PR

```bash
/usr/bin/python3 -m unittest discover -s tests
```

Add a test for your source's `dedupe_key` stability: build the same event
twice and assert one row. If you changed anything date-related, run the suite
under a second timezone too:

```bash
LIFELOG_TZ=America/New_York /usr/bin/python3 -m unittest discover -s tests
```

Read [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md)
first — if a choice looks odd, the reasoning is usually there.
