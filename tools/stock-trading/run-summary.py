#!/usr/bin/env python3
"""Human-readable summary of stock-trading runs.

Reads logs/trading-log.jsonl (and logs/outcomes.jsonl if present) and
renders a plain-text summary. Filters by date range or experiment id.

Usage:
  python3 tools/stock-trading/run-summary.py
      -> print the most recent run

  python3 tools/stock-trading/run-summary.py --since 2026-04-14
      -> aggregate every run on or after this date (inclusive)

  python3 tools/stock-trading/run-summary.py --experiment exp-002
      -> aggregate every run with matching experiment_id

No color codes, no unicode box drawing — pipe-safe.
stdlib only. No writes.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = REPO_ROOT / "logs" / "trading-log.jsonl"
OUTCOMES_PATH = REPO_ROOT / "logs" / "outcomes.jsonl"


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


def _run_date(run: dict) -> str:
    run_id = run.get("run_id") or ""
    return run_id[:10] if len(run_id) >= 10 else ""


def _truncate(s, width):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= width else s[: width - 1] + "…"


def _latest_outcomes_by_key():
    state_rank = {
        "pending_fill": 0,
        "filled": 1,
        "t0": 2,
        "t1": 3,
        "t5": 4,
        "t20": 5,
        "unfilled_cancelled": 6,
    }
    latest = {}
    for row in _iter_jsonl(OUTCOMES_PATH):
        key = (row.get("run_id"), row.get("ticker"))
        if None in key:
            continue
        rank = state_rank.get(row.get("outcome_state"), -1)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, row)
    return {k: v[1] for k, v in latest.items()}


def _filter_runs(runs, since=None, experiment=None):
    for r in runs:
        if since and _run_date(r) < since:
            continue
        if experiment and r.get("experiment_id") != experiment:
            continue
        yield r


def _render_single_run(run, outcomes):
    lines = []
    hdr = (
        f"Run {run.get('run_id', '?')}   "
        f"exp={run.get('experiment_id', '?')}   "
        f"ranking={run.get('ranking_version', '?')}   "
        f"mode={run.get('mode', '?')}   "
        f"dry_run={run.get('dry_run', False)}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))

    acct = run.get("account_snapshot") or {}
    if acct:
        lines.append(
            f"Account: equity=${acct.get('equity', '?')} cash=${acct.get('cash', '?')} "
            f"day_trades={acct.get('day_trade_count', '?')}"
        )

    sentiment = run.get("market_sentiment") or {}
    if sentiment:
        lines.append(
            f"Fear & Greed: {sentiment.get('fear_greed_score', '?')} "
            f"({sentiment.get('fear_greed_rating', '?')})"
        )

    decisions = run.get("decisions", [])
    if decisions:
        lines.append("")
        lines.append("Decisions:")
        lines.append(
            f"  {'action':<6} {'ticker':<7} {'conf':<7} {'override':<9} rationale"
        )
        for d in decisions:
            override = "yes" if d.get("min_trade_override") else ""
            lines.append(
                f"  {_truncate(d.get('action'), 6):<6} "
                f"{_truncate(d.get('ticker'), 7):<7} "
                f"{_truncate(d.get('confidence'), 7):<7} "
                f"{override:<9} "
                f"{_truncate(d.get('rationale'), 60)}"
            )

    orders = run.get("orders", [])
    if orders:
        lines.append("")
        lines.append("Orders:")
        lines.append(
            f"  {'status':<10} {'ticker':<7} {'side':<5} {'qty':<5} "
            f"{'limit':<10} {'notional':<10} order_id"
        )
        for o in orders:
            lines.append(
                f"  {_truncate(o.get('status'), 10):<10} "
                f"{_truncate(o.get('ticker'), 7):<7} "
                f"{_truncate(o.get('side'), 5):<5} "
                f"{_truncate(o.get('qty'), 5):<5} "
                f"{_truncate(o.get('limit_price'), 10):<10} "
                f"{_truncate(o.get('notional'), 10):<10} "
                f"{_truncate(o.get('order_id'), 24)}"
            )

    overrides = run.get("risk_overrides", [])
    if overrides:
        lines.append("")
        lines.append("Risk overrides:")
        for ro in overrides:
            lines.append(
                f"  {_truncate(ro.get('ticker'), 7):<7} {_truncate(ro.get('reason'), 80)}"
            )

    if run.get("min_trade_override_waived"):
        lines.append("")
        lines.append("NOTE: min_trade_override_waived = true (too-garbage-to-trade waiver)")

    if outcomes:
        run_id = run.get("run_id")
        matched = [(t, row) for (rid, t), row in outcomes.items() if rid == run_id]
        if matched:
            lines.append("")
            lines.append("Outcomes (latest state per decision):")
            lines.append(
                f"  {'ticker':<7} {'state':<20} {'fill':<10} "
                f"{'return_t5':<10} {'return_t20':<10}"
            )
            for ticker, row in sorted(matched):
                ret_t5 = row.get("return_t5_pct")
                ret_t20 = row.get("return_t20_pct")
                lines.append(
                    f"  {_truncate(ticker, 7):<7} "
                    f"{_truncate(row.get('outcome_state'), 20):<20} "
                    f"{_truncate(row.get('fill_price'), 10):<10} "
                    f"{(f'{ret_t5:+.2f}%' if ret_t5 is not None else ''):<10} "
                    f"{(f'{ret_t20:+.2f}%' if ret_t20 is not None else ''):<10}"
                )

    return "\n".join(lines)


def _render_aggregate(runs, outcomes, label):
    runs = list(runs)
    if not runs:
        return f"No runs match {label}."

    total = len(runs)
    decisions_total = 0
    buys = 0
    sells = 0
    holds = 0
    override_buys = 0
    orders_placed = 0
    orders_skipped = 0

    for r in runs:
        for d in r.get("decisions", []):
            decisions_total += 1
            action = (d.get("action") or "").upper()
            if action == "BUY":
                buys += 1
                if d.get("min_trade_override"):
                    override_buys += 1
            elif action == "SELL":
                sells += 1
            elif action == "HOLD":
                holds += 1
        for o in r.get("orders", []):
            status = o.get("status")
            if status == "placed":
                orders_placed += 1
            elif status == "skipped":
                orders_skipped += 1

    lines = [
        f"Aggregate summary — {label}",
        "=" * 60,
        f"Runs: {total}",
        f"Decisions: {decisions_total}  (BUY {buys} / SELL {sells} / HOLD {holds})",
        f"  of which minimum-trade overrides: {override_buys}",
        f"Orders: {orders_placed} placed, {orders_skipped} skipped",
    ]

    if outcomes:
        relevant = [
            row
            for (rid, _), row in outcomes.items()
            if any(rid == r.get("run_id") for r in runs)
        ]
        t5_rows = [r for r in relevant if r.get("return_t5_pct") is not None]
        if t5_rows:
            mean_t5 = sum(r["return_t5_pct"] for r in t5_rows) / len(t5_rows)
            hit_t5 = sum(1 for r in t5_rows if r["return_t5_pct"] > 0) / len(t5_rows)
            lines.append(
                f"T+5 outcomes: n={len(t5_rows)} mean={mean_t5:+.2f}% hit_rate={hit_t5*100:.0f}%"
            )

            by_conf = defaultdict(list)
            for r in t5_rows:
                by_conf[r.get("confidence") or "unknown"].append(r["return_t5_pct"])
            for conf in ("high", "medium", "low", "unknown"):
                vals = by_conf.get(conf)
                if not vals:
                    continue
                mean = sum(vals) / len(vals)
                hit = sum(1 for v in vals if v > 0) / len(vals)
                lines.append(
                    f"  confidence={conf:<8} n={len(vals):<3} "
                    f"mean={mean:+.2f}% hit={hit*100:.0f}%"
                )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--since", help="YYYY-MM-DD — render an aggregate across runs on/after this date")
    group.add_argument("--experiment", help="experiment_id — render an aggregate across runs with matching id")
    args = parser.parse_args()

    runs = list(_iter_jsonl(LOG_PATH))
    outcomes = _latest_outcomes_by_key()

    if args.since:
        filtered = list(_filter_runs(runs, since=args.since))
        print(_render_aggregate(filtered, outcomes, f"since {args.since}"))
        return 0

    if args.experiment:
        filtered = list(_filter_runs(runs, experiment=args.experiment))
        print(_render_aggregate(filtered, outcomes, f"experiment={args.experiment}"))
        return 0

    if not runs:
        print("No runs in logs/trading-log.jsonl yet.")
        return 0

    latest = runs[-1]
    print(_render_single_run(latest, outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
