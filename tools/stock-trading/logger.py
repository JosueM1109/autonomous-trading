#!/usr/bin/env python3
"""Append-only JSONL run log writer for the stock-trading skill.

Reads one run object as JSON on stdin, appends it as a single line to
logs/trading-log.jsonl, and prints {"ok": true, "run_id": ..., "path": ...}
to stdout. Uses fcntl.flock for crash safety so concurrent writers cannot
interleave partial lines. stdlib only.
"""

import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = REPO_ROOT / "logs" / "trading-log.jsonl"


def main() -> int:
    try:
        run = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {e}"}))
        return 2

    if not isinstance(run, dict):
        print(json.dumps({"ok": False, "error": "run payload must be a JSON object"}))
        return 2

    run.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
    line = json.dumps(run, separators=(",", ":"), sort_keys=True)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    print(
        json.dumps(
            {
                "ok": True,
                "run_id": run.get("run_id"),
                "path": str(LOG_PATH.relative_to(REPO_ROOT)),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
