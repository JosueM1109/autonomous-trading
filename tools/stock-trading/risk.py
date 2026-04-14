#!/usr/bin/env python3
"""Hard safety rules for the stock-trading skill.

Four modes:
  --snapshot   stdin: account JSON  -> stdout: risk context JSON.
  --validate   stdin: order JSON    -> stdout: approved/rejected JSON.
                 If approved, writes a `pending` reservation for
                 (date, ticker, side) to logs/state.json and (for BUYs)
                 increments `session_deployed` by the reserved notional.
  --commit     stdin: {date, ticker, side} -> stdout: ok/error JSON.
                 Promotes a pending reservation to `submitted` (permanent
                 idempotency marker). For BUYs, also advances
                 `session_deployed_confirmed`. Call this AFTER the order
                 placement succeeds on the broker side.
  --release    stdin: {date, ticker, side} -> stdout: ok/error JSON.
                 Clears a pending reservation and, for BUYs, refunds the
                 reserved notional back out of `session_deployed`. Call
                 this if order placement fails, or at the end of a dry run
                 to unwind the reservation.

State machine per (date, ticker, side):
    (nothing) --validate--> pending --commit--> submitted
                                \
                                 --release--> (nothing, session_deployed refunded)

Idempotency: --validate rejects if the same (date, ticker, side) is either
`submitted` (permanent) or already has a `pending` reservation. In practice
this means a crash between --validate and --commit/--release locks that
(ticker, side) until the date bucket rolls over the next day. This is a
deliberate trade-off against adding TTLs or admin escape hatches.

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
    bucket = state.setdefault(
        date,
        {
            "session_deployed": 0.0,
            "session_deployed_confirmed": 0.0,
            "pending": {},
            "submitted": {},
        },
    )
    # Forward-compat patch for older state.json shapes (pre-cluster-B).
    bucket.setdefault("session_deployed", 0.0)
    bucket.setdefault("session_deployed_confirmed", 0.0)
    bucket.setdefault("pending", {})
    bucket.setdefault("submitted", {})
    return bucket


def _find_pending(bucket: dict, ticker: str, side: str):
    for entry in bucket["pending"].get(ticker, []):
        if entry.get("side") == side:
            return entry
    return None


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
    date = order.get("date")
    ticker_raw = order.get("ticker")
    side_raw = order.get("side")
    if not isinstance(date, str) or not date:
        return {"approved": False, "reason": "missing or invalid date"}
    if not isinstance(ticker_raw, str) or not ticker_raw:
        return {"approved": False, "reason": "missing or invalid ticker"}
    if not isinstance(side_raw, str):
        return {"approved": False, "reason": "missing or invalid side"}

    ticker = ticker_raw.upper()
    side = side_raw.lower()
    if side not in ("buy", "sell"):
        return {"approved": False, "reason": f"unknown side '{side_raw}'"}

    try:
        qty = int(order["qty"])
        limit_price = float(order["limit_price"])
        bid = float(order["bid"])
        ask = float(order["ask"])
        equity = float(order["account_equity"])
        cash = float(order.get("account_cash", equity))
        day_trades = int(order.get("day_trade_count", 0))
    except (KeyError, TypeError, ValueError) as e:
        return {"approved": False, "reason": f"invalid order payload: {e}"}

    existing_position = bool(order.get("existing_position", False))

    if qty <= 0:
        return {"approved": False, "reason": "invalid qty: must be positive integer"}
    if limit_price <= 0:
        return {"approved": False, "reason": "invalid limit_price: must be positive"}
    if bid <= 0 or ask <= 0:
        return {"approved": False, "reason": "invalid quote: bid and ask must be positive"}
    if bid > ask:
        return {"approved": False, "reason": f"invalid quote: bid {bid} > ask {ask}"}

    risk_cfg = cfg["risk"]
    notional = round(qty * limit_price, 2)

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

        submitted_sides = bucket["submitted"].get(ticker, [])
        if side in submitted_sides:
            return {
                "approved": False,
                "reason": f"idempotency: {side.upper()} {ticker} already submitted today",
            }

        if _find_pending(bucket, ticker, side) is not None:
            return {
                "approved": False,
                "reason": f"idempotency: {side.upper()} {ticker} already has a pending reservation — commit or release first",
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

        bucket["pending"].setdefault(ticker, []).append(
            {"side": side, "notional": notional}
        )
        write_state(state)

    return {"approved": True, "reason": None, "reserved_notional": notional}


def _parse_ref(ref: dict):
    date = ref.get("date")
    ticker_raw = ref.get("ticker")
    side_raw = ref.get("side")
    if not isinstance(date, str) or not date:
        return None, None, None, "missing or invalid date"
    if not isinstance(ticker_raw, str) or not ticker_raw:
        return None, None, None, "missing or invalid ticker"
    if not isinstance(side_raw, str):
        return None, None, None, "missing or invalid side"
    side = side_raw.lower()
    if side not in ("buy", "sell"):
        return None, None, None, f"unknown side '{side_raw}'"
    return date, ticker_raw.upper(), side, None


def commit_order(ref: dict) -> dict:
    date, ticker, side, err = _parse_ref(ref)
    if err:
        return {"ok": False, "error": err}

    with state_lock():
        state = read_state()
        bucket = day_bucket(state, date)
        entry = _find_pending(bucket, ticker, side)
        if entry is None:
            return {
                "ok": False,
                "error": f"no pending reservation for {side.upper()} {ticker} on {date}",
            }
        reserved_notional = float(entry.get("notional", 0.0))

        bucket["pending"][ticker] = [
            e for e in bucket["pending"][ticker] if e is not entry
        ]
        if not bucket["pending"][ticker]:
            del bucket["pending"][ticker]

        bucket["submitted"].setdefault(ticker, []).append(side)
        if side == "buy":
            bucket["session_deployed_confirmed"] = round(
                float(bucket["session_deployed_confirmed"]) + reserved_notional,
                2,
            )
        write_state(state)

    return {
        "ok": True,
        "committed": {
            "date": date,
            "ticker": ticker,
            "side": side,
            "notional": reserved_notional,
        },
    }


def release_order(ref: dict) -> dict:
    date, ticker, side, err = _parse_ref(ref)
    if err:
        return {"ok": False, "error": err}

    with state_lock():
        state = read_state()
        bucket = day_bucket(state, date)
        entry = _find_pending(bucket, ticker, side)
        if entry is None:
            return {
                "ok": False,
                "error": f"no pending reservation for {side.upper()} {ticker} on {date}",
            }
        reserved_notional = float(entry.get("notional", 0.0))

        bucket["pending"][ticker] = [
            e for e in bucket["pending"][ticker] if e is not entry
        ]
        if not bucket["pending"][ticker]:
            del bucket["pending"][ticker]

        if side == "buy":
            refunded = max(
                0.0, float(bucket["session_deployed"]) - reserved_notional
            )
            bucket["session_deployed"] = round(refunded, 2)
        write_state(state)

    return {
        "ok": True,
        "released": {
            "date": date,
            "ticker": ticker,
            "side": side,
            "notional": reserved_notional,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", action="store_true")
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--commit", dest="commit_mode", action="store_true")
    mode.add_argument("--release", action="store_true")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {e}"}))
        return 2

    try:
        if args.snapshot:
            cfg = load_config()
            result = snapshot(payload, cfg)
        elif args.validate:
            cfg = load_config()
            result = validate(payload, cfg)
        elif args.commit_mode:
            result = commit_order(payload)
        else:
            result = release_order(payload)
    except (KeyError, TypeError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 2

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
