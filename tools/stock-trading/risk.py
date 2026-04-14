#!/usr/bin/env python3
"""Hard safety rules for the stock-trading skill.

Two modes:
  --snapshot   stdin: account JSON  → stdout: risk context JSON
  --validate   stdin: order JSON    → stdout: approved/rejected JSON

Owns logs/state.json exclusively. Uses fcntl.flock for atomic
read-modify-write so concurrent invocations cannot corrupt state.
stdlib only.
"""

import argparse
import fcntl
import json
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
STATE_PATH = REPO_ROOT / "logs" / "state.json"
LOCK_PATH = REPO_ROOT / "logs" / ".state.lock"


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return json.load(f)


@contextmanager
def state_lock():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open() as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def write_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_PATH)


def day_bucket(state: dict, date: str) -> dict:
    return state.setdefault(date, {"session_deployed": 0.0, "submitted": {}})


def snapshot(account_json: dict, cfg: dict) -> dict:
    date = account_json["date"]
    account = account_json["account"]
    positions = account_json.get("positions", [])

    equity = float(account["equity"])
    cash = float(account["cash"])
    day_trades = int(account.get("day_trade_count", 0))
    trading_blocked = bool(account.get("trading_blocked", False))
    account_blocked = bool(account.get("account_blocked", False))

    risk_cfg = cfg["risk"]

    if trading_blocked or account_blocked:
        reason = "trading_blocked" if trading_blocked else "account_blocked"
        return {
            "ok": False,
            "abort": True,
            "abort_reason": f"Alpaca returned {reason}=true — aborting run",
            "pdt_headroom": 0,
            "max_session_allocation": 0.0,
            "max_per_position": 0.0,
            "session_deployed_so_far": 0.0,
            "blocked_tickers": [],
            "blocked_reasons": {},
        }

    max_per_position = round(equity * risk_cfg["max_position_pct_of_equity"], 2)
    max_session_allocation = round(cash * risk_cfg["max_session_pct_of_cash"], 2)

    if equity < risk_cfg["pdt_threshold_equity"]:
        pdt_headroom = max(0, risk_cfg["pdt_max_day_trades"] - day_trades)
    else:
        pdt_headroom = -1  # unlimited

    blocked_reasons = {
        p["symbol"]: "already holding position — no pyramiding" for p in positions
    }

    with state_lock():
        state = read_state()
        deployed = float(day_bucket(state, date)["session_deployed"])

    return {
        "ok": True,
        "abort": False,
        "abort_reason": None,
        "pdt_headroom": pdt_headroom,
        "max_session_allocation": max_session_allocation,
        "max_per_position": max_per_position,
        "session_deployed_so_far": round(deployed, 2),
        "blocked_tickers": sorted(blocked_reasons.keys()),
        "blocked_reasons": blocked_reasons,
    }


def validate(order: dict, cfg: dict) -> dict:
    date = order["date"]
    ticker = order["ticker"].upper()
    side = order["side"].lower()
    qty = int(order["qty"])
    limit_price = float(order["limit_price"])
    bid = float(order["bid"])
    ask = float(order["ask"])
    equity = float(order["account_equity"])
    cash = float(order.get("account_cash", equity))
    day_trades = int(order.get("day_trade_count", 0))
    existing_position = bool(order.get("existing_position", False))

    risk_cfg = cfg["risk"]
    notional = round(qty * limit_price, 2)

    if side not in ("buy", "sell"):
        return {"approved": False, "reason": f"unknown side '{order['side']}'"}

    if notional < risk_cfg["min_notional_usd"]:
        return {
            "approved": False,
            "reason": f"notional ${notional:.2f} below ${risk_cfg['min_notional_usd']} minimum",
        }

    max_per_position = equity * risk_cfg["max_position_pct_of_equity"]
    position_pct = risk_cfg["max_position_pct_of_equity"] * 100
    if notional > max_per_position + 1e-6:
        return {
            "approved": False,
            "reason": f"notional ${notional:.2f} exceeds per-position cap ${max_per_position:.2f} ({position_pct:.0f}% of equity)",
        }

    # Tighten to 0.3% before going live — 1.5% is paper-trading only.
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return {"approved": False, "reason": "invalid quote: midpoint <= 0"}
    spread_pct = (ask - bid) / midpoint
    if spread_pct > risk_cfg["max_spread_pct_of_midpoint"] + 1e-9:
        return {
            "approved": False,
            "reason": f"bid/ask spread {spread_pct*100:.2f}% exceeds {risk_cfg['max_spread_pct_of_midpoint']*100:.2f}% midpoint cap",
        }

    opening_trade = side == "buy" and not existing_position
    if (
        opening_trade
        and equity < risk_cfg["pdt_threshold_equity"]
        and day_trades >= risk_cfg["pdt_max_day_trades"]
    ):
        return {
            "approved": False,
            "reason": f"PDT block: day_trade_count {day_trades} >= {risk_cfg['pdt_max_day_trades']} with equity ${equity:.2f} < ${risk_cfg['pdt_threshold_equity']}",
        }

    with state_lock():
        state = read_state()
        bucket = day_bucket(state, date)
        submitted = bucket["submitted"].get(ticker, [])

        if side in submitted:
            return {
                "approved": False,
                "reason": f"idempotency: {side.upper()} {ticker} already submitted today",
            }

        max_session_allocation = cash * risk_cfg["max_session_pct_of_cash"]
        session_pct = risk_cfg["max_session_pct_of_cash"] * 100
        if side == "buy":
            projected = float(bucket["session_deployed"]) + notional
            if projected > max_session_allocation + 1e-6:
                return {
                    "approved": False,
                    "reason": f"session cap: deploying ${notional:.2f} would push total to ${projected:.2f}, over ${max_session_allocation:.2f} ({session_pct:.0f}% of cash)",
                }
            bucket["session_deployed"] = round(projected, 2)

        bucket["submitted"][ticker] = submitted + [side]
        write_state(state)

    return {"approved": True, "reason": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", action="store_true")
    mode.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {e}"}))
        return 2

    cfg = load_config()

    try:
        result = snapshot(payload, cfg) if args.snapshot else validate(payload, cfg)
    except (KeyError, TypeError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 2

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
