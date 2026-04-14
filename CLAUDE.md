# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single Claude Code skill (`stock-trading`) plus its Python safety layer. There is no build system, no package manager, no test suite, and no application server. "Running the code" means invoking the skill inside a Claude Code session — the skill itself drives MCP tools (Alpaca + TradingView) and shells out to two small stdlib Python scripts.

The canonical entry point is the skill file, not any Python module. **Read [.claude/skills/stock-trading/SKILL.md](.claude/skills/stock-trading/SKILL.md) before touching anything else** — it defines the trading phases, token-efficiency rules, exclusion logic, and the paper-trading doctrine that the Python layer only partially enforces.

## Architecture

Three-layer split, strictly enforced:

1. **Reasoning layer — [.claude/skills/stock-trading/SKILL.md](.claude/skills/stock-trading/SKILL.md).** Decides BUY/SELL/HOLD. Runs inside the Claude Code session; no separate process. Pulls dossiers via MCP (`alpaca`, `tradingview`) and one Finnhub HTTP call. No Anthropic API key needed — the "model" is whichever Claude is running the session.
2. **Data layer — MCP servers + HTTP.** Registered at user scope via `claude mcp add`. `alpaca` provides account/positions/quotes/order placement. `tradingview` (package `tradingview-mcp-server`, free, no auth) provides `coin_analysis` (per-ticker TA, no bulk endpoint — Phase 2 fans out) and `smart_volume_scanner` (Phase 1.5 morning screen). Finnhub earnings calendar is called directly via `curl`, not MCP.
3. **Risk + audit layer — [tools/stock-trading/risk.py](tools/stock-trading/risk.py) and [tools/stock-trading/logger.py](tools/stock-trading/logger.py).** Python stdlib only. `risk.py` owns `logs/state.json` via `fcntl.flock` and enforces hard money rules (per-position cap, session cap, min notional, spread cap, PDT block, idempotency). `logger.py` appends one JSONL row per run to `logs/trading-log.jsonl`. These scripts are invoked as subprocesses from the skill — `risk.py --snapshot` once per run, `risk.py --validate` once per order, `logger.py` once at the end.

Thresholds, risk parameters, and the morning-screen config live in [tools/stock-trading/config.json](tools/stock-trading/config.json). **There is no static watchlist** — the candidate list is assembled fresh each run from `smart_volume_scanner` results + any open positions. `config.json` carries `_paper_note` / `_max_position_pct_note` fields that must be tightened before flipping `ALPACA_PAPER=false`.

`risk.py` uses a three-step state machine for every order: `--validate` reserves a pending entry (and, for buys, pre-reserves `session_deployed`), `--commit` promotes it to a permanent `submitted` idempotency marker on successful placement, and `--release` unwinds the reservation on rejection/dry-run/error. **Every `--validate` must be paired with exactly one `--commit` or `--release`** — stale pending entries lock that `(date, ticker, side)` until the date bucket rolls over the next day. Dry runs call `--release` automatically after `--validate`, so they leave `logs/state.json` clean without manual intervention.

## Commands

There is no `make`, `npm`, or `pytest`. The only commands that exist:

```bash
# Run the skill (inside a Claude Code session, not a shell):
#   "run the trading skill"          -- full run, places limit orders on Alpaca
#   "run the trading skill --dry-run" -- phases 0-5 run, no orders, no log

# Python smoke tests (from repo root):
echo '{"date":"2026-04-13","account":{"equity":10000,"cash":5000,"day_trade_count":0,"trading_blocked":false,"account_blocked":false,"pattern_day_trader":false},"positions":[]}' \
  | python3 tools/stock-trading/risk.py --snapshot

# --validate reserves a pending entry. Always pair with --commit OR --release.
echo '{"date":"2026-04-13","ticker":"NVDA","side":"buy","qty":2,"limit_price":425.0,"bid":424.80,"ask":425.20,"account_equity":10000,"account_cash":5000,"day_trade_count":0,"existing_position":false}' \
  | python3 tools/stock-trading/risk.py --validate

echo '{"date":"2026-04-13","ticker":"NVDA","side":"buy"}' \
  | python3 tools/stock-trading/risk.py --commit   # promote pending -> submitted

echo '{"date":"2026-04-13","ticker":"NVDA","side":"buy"}' \
  | python3 tools/stock-trading/risk.py --release  # clear pending, refund session_deployed

# Run the risk.py test suite (stdlib unittest, no deps):
python3 -m unittest tests.test_risk -v

# Reset runtime state (optional — dry runs no longer leave stale state since
# the skill auto-releases pending entries, but this is still safe):
rm -f logs/state.json logs/.state.lock
: > logs/trading-log.jsonl

# MCP connectivity check:
claude mcp list   # expect ✓ Connected on both `alpaca` and `tradingview`
```

Full setup (API keys, MCP registration commands, first-run walkthrough) lives in [tools/stock-trading/SETUP.md](tools/stock-trading/SETUP.md).

## Non-obvious constraints

- **`experiment_id` is the canary for experiment integrity.** `tools/stock-trading/config.json` carries a top-level `experiment_id` field. Any change to the `risk`, `thresholds`, or (future) `ranking` blocks requires bumping `experiment_id` and starting a fresh run of 5 paper sessions. The skill reads `experiment_id` in Phase 0 and passes it through to `logger.py` so every row in `trading-log.jsonl` is tagged. Mid-experiment tweaks silently corrupt the comparable-run dataset the skill exists to produce. The typical daily cadence for the skill is ~10:00 ET, not earlier — running before 10:00 makes the volume-ratio denominator tiny (<3% by 09:40) and widens spreads enough to distort the population of tradeable tickers.
- **Paper trading is the default and the whole repo is tuned for it.** `ALPACA_PAPER=true` in `.env`. Several `config.json` values (`max_position_pct_of_equity`, `max_session_pct_of_cash`, `max_spread_pct_of_midpoint`) are deliberately loose for paper mode and documented with `_*_note` siblings that say what to tighten before going live. Do not edit those without reading the notes and the promotion checklist in SKILL.md.
- **Limit orders only, never market.** `mcp__alpaca__place_stock_order` is always called with `type: "limit"`. This is a non-negotiable rule in the skill.
- **TradingView exchange whitelist is `NASDAQ / NYSE / BIST / EGX / BURSA / HKEX`.** Anything else (including `AMEX` / `NYSEARCA`) silently falls back to the crypto default `KUCOIN` and returns "No data found". That's why the morning screen is restricted to NYSE + NASDAQ.
- **The minimum-trade rule is intentional.** Phase 4 must place at least one BUY per run unless it's a dry run, the account is blocked, or every candidate failed its dossier fetch. A run of all HOLDs has to override the weakest HOLD into a BUY. This exists because the point of the skill is to generate data under real market conditions — see the "Paper-experiment doctrine" in SKILL.md for the reasoning. Don't "fix" this by loosening it to "HOLD if signals are weak".
- **Fan-out discipline matters for token cost.** Phase 1 and Phase 1.5 fire in parallel in a single message; Phase 2 fires all per-ticker calls (two `coin_analysis` per ticker + one batched `get_stock_snapshot` + one Finnhub curl) in a single message after Phase 1.5 returns; Phase 2.5 fires top-5 `WebSearch` calls in parallel. Sequential fan-out across phases wastes the session's token budget.
- **Hard vs soft dependencies for exclusion.** 1D `coin_analysis` and Alpaca snapshot are hard (missing → ticker excluded). 1H `coin_analysis`, Finnhub earnings, Phase 2.5 news search, and Phase 1 Fear & Greed are soft (missing → default value, run continues).
- **`risk.py` rejection is final.** If validation rejects an order, log the reason and skip. Never retry with tweaked qty / price / side.
- **`logs/` is gitignored** (`trading-log.jsonl`, `state.json`, `.state.lock`). Don't commit run artifacts.
