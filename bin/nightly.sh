#!/bin/zsh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="/usr/bin/python3"
ATTEMPTS=3
DELAY=60

cd "$REPO" || exit 1

daily_ok=1
for attempt in $(seq 1 $ATTEMPTS); do
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') daily attempt ${attempt}/${ATTEMPTS} ==="
  if [ $# -eq 0 ]; then set -- --backfill 3 --force; fi
  if "$PY" bin/nightly.py "$@"; then
    echo "=== daily ok on attempt ${attempt} ==="
    daily_ok=0
    break
  fi
  echo "=== daily attempt ${attempt} failed ===" >&2
  [ "$attempt" -lt "$ATTEMPTS" ] && sleep $((DELAY * attempt))
done

if [ "$daily_ok" -ne 0 ]; then
  echo "=== all ${ATTEMPTS} daily attempts failed ===" >&2
  if command -v terminal-notifier >/dev/null 2>&1; then
    terminal-notifier -title "Life Log FAILED" \
      -message "All ${ATTEMPTS} attempts failed. See /tmp/lifelog.err.log" -group lifelog
  fi
fi

echo "=== $(date '+%H:%M:%S') rollups ==="
"$PY" bin/rollup.py "$@" || echo "=== rollup step failed ===" >&2

exit $daily_ok
