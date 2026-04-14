#!/usr/bin/env python3
"""Pure-function reducer for the stock-trading Evaluate Mode.

Two modes:
  --current-state  stdin: { "log_path": "...", "outcomes_path": "..." }
                   stdout: { "decisions": [ { run_id, ticker, order_id,
                             experiment_id, confidence, min_trade_override,
                             limit_price, current_state, next_state, ... } ] }
                   Walks logs/trading-log.jsonl to find every placed order,
                   walks logs/outcomes.jsonl to get the latest state per
                   (run_id, ticker), and emits the work queue of decisions
                   whose state can still advance. Terminal states
                   (`t20`, `unfilled_cancelled`) are omitted.

  --append         stdin: { "lines": [ { run_id, ticker, outcome_state, ... }, ... ] }
                   stdout: { "ok": true, "appended": N, "path": "..." }
                   Appends each line to logs/outcomes.jsonl atomically
                   (fcntl.flock), same crash-safety discipline as logger.py.

The skill's Evaluate Mode calls --current-state once at E0 to build the
work queue, fans out MCP calls in E1/E2 to fetch order fills and bar
closes, then calls --append once in E3 with all the new outcomes rows.

stdlib only. No HTTP, no Alpaca access, no .env parsing — the reducer
never reaches the network. All data in/out is via stdin/stdout JSON.
"""

import argparse
import fcntl
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = REPO_ROOT / "logs" / "trading-log.jsonl"
DEFAULT_OUTCOMES_PATH = REPO_ROOT / "logs" / "outcomes.jsonl"

STATE_PROGRESSION = ["pending_fill", "filled", "t0", "t1", "t5", "t20"]
TERMINAL_STATES = {"t20", "unfilled_cancelled"}


def _resolve(payload: dict, key: str, default: Path) -> Path:
    raw = payload.get(key)
    if not raw:
        return default
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _collect_placed_decisions(log_path: Path) -> list:
    """Return (run_id, ticker) → context for every placed, non-dry-run order."""
    placed = []
    for run in _iter_jsonl(log_path):
        if run.get("dry_run"):
            continue
        run_id = run.get("run_id")
        if not run_id:
            continue
        experiment_id = run.get("experiment_id")
        ranking_version = run.get("ranking_version")
        decisions_by_ticker = {
            d.get("ticker"): d for d in run.get("decisions", []) if d.get("ticker")
        }
        for order in run.get("orders", []):
            if order.get("status") != "placed":
                continue
            order_id = order.get("order_id")
            ticker = order.get("ticker")
            if not order_id or not ticker:
                continue
            decision = decisions_by_ticker.get(ticker, {})
            placed.append(
                {
                    "run_id": run_id,
                    "ticker": ticker,
                    "order_id": order_id,
                    "experiment_id": experiment_id,
                    "ranking_version": ranking_version,
                    "confidence": decision.get("confidence"),
                    "min_trade_override": bool(decision.get("min_trade_override", False)),
                    "side": order.get("side"),
                    "qty": order.get("qty"),
                    "limit_price": order.get("limit_price"),
                    "notional": order.get("notional"),
                }
            )
    return placed


def _latest_states(outcomes_path: Path) -> dict:
    """Return { (run_id, ticker): latest outcomes row } by most-advanced state."""
    latest = {}
    for row in _iter_jsonl(outcomes_path):
        run_id = row.get("run_id")
        ticker = row.get("ticker")
        state = row.get("outcome_state")
        if not run_id or not ticker:
            continue
        if state not in STATE_PROGRESSION and state not in TERMINAL_STATES:
            continue
        key = (run_id, ticker)
        current = latest.get(key)
        if current is None:
            latest[key] = row
            continue
        # Advance only — later rows with higher state index win.
        if _state_rank(state) > _state_rank(current.get("outcome_state")):
            latest[key] = row
    return latest


def _state_rank(state):
    if state == "unfilled_cancelled":
        return len(STATE_PROGRESSION)  # equivalent-to-terminal
    try:
        return STATE_PROGRESSION.index(state)
    except ValueError:
        return -1


def _next_state(current: str):
    if current in TERMINAL_STATES:
        return None
    try:
        idx = STATE_PROGRESSION.index(current)
    except ValueError:
        return STATE_PROGRESSION[1]  # nothing recorded yet -> fetch `filled`
    if idx + 1 >= len(STATE_PROGRESSION):
        return None
    return STATE_PROGRESSION[idx + 1]


def current_state(payload: dict) -> dict:
    log_path = _resolve(payload, "log_path", DEFAULT_LOG_PATH)
    outcomes_path = _resolve(payload, "outcomes_path", DEFAULT_OUTCOMES_PATH)

    placed = _collect_placed_decisions(log_path)
    latest = _latest_states(outcomes_path)

    out = []
    for ctx in placed:
        key = (ctx["run_id"], ctx["ticker"])
        latest_row = latest.get(key)
        current = latest_row.get("outcome_state") if latest_row else "pending_fill"
        nxt = _next_state(current)
        if nxt is None:
            continue
        out.append(
            {
                **ctx,
                "current_state": current,
                "next_state": nxt,
                "fill_price": latest_row.get("fill_price") if latest_row else None,
                "filled_at": latest_row.get("filled_at") if latest_row else None,
            }
        )
    return {"decisions": out}


def append_lines(payload: dict) -> dict:
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return {"ok": False, "error": "`lines` must be a JSON array"}
    for line in lines:
        if not isinstance(line, dict):
            return {"ok": False, "error": "each entry in `lines` must be a JSON object"}
        if not line.get("run_id") or not line.get("ticker") or not line.get("outcome_state"):
            return {
                "ok": False,
                "error": "each entry in `lines` requires run_id, ticker, outcome_state",
            }

    DEFAULT_OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    appended = 0
    with DEFAULT_OUTCOMES_PATH.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for line in lines:
                f.write(json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n")
                appended += 1
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return {
        "ok": True,
        "appended": appended,
        "path": str(DEFAULT_OUTCOMES_PATH.relative_to(REPO_ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--current-state", dest="current", action="store_true")
    mode.add_argument("--append", action="store_true")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {e}"}))
        return 2

    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "error": "stdin payload must be a JSON object"}))
        return 2

    try:
        result = current_state(payload) if args.current else append_lines(payload)
    except (KeyError, TypeError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 2

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
