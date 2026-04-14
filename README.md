# autonomous-trading

A Claude Code skill that runs a morning stock/ETF trading routine end-to-end: pulls an account snapshot and per-ticker dossiers through MCP, reasons through BUY/SELL/HOLD against a fixed strategy, validates every order through a Python risk layer, places limit-only orders on Alpaca, and appends a full audit log.

**Paper trading by default.** Live trading is a one-line change in `.env` that is gated by a five-run promotion checklist. Nothing in this repo places a market order under any circumstance — the skill uses limit orders only.

---

## What it is (and isn't)

**What it is:** a manually-triggered morning routine. Josue opens a Claude Code session, types `run the trading skill`, and Claude walks through seven phases — account snapshot, market sentiment, morning screen, dossier fetch, news lookup, reasoning, execution, log. The reasoning happens inside the Claude Code session itself; the Python layer only enforces hard money rules and appends the audit log. **Typical cadence: one run per trading day at ~10:00 ET.** Running earlier makes the volume-ratio denominator tiny (<3% of a session by 09:40) and widens spreads — 10:00 is the canonical experiment time.

**What it isn't:** a bot, a cron job, a background service, or an autonomous agent. There is no launchd entry, no systemd unit, no GitHub Action, no Anthropic API key in the repo. If Josue doesn't open a session and type a trigger phrase, nothing runs. If Claude is offline, nothing runs. This is a deliberate design choice — the point of the skill is to exercise Claude's live reasoning under real market conditions with a human in the loop, not to automate trading.

The candidate list is **dynamic**: each run discovers its own tickers via a TradingView volume scanner across NYSE + NASDAQ, merges in any open positions, and fans out per-ticker dossier calls in parallel. There is no static watchlist.

---

## Architecture at a glance

```
                                   ┌─────────────────────────────┐
                                   │    Claude Code session       │
                                   │  (reasoning + orchestration) │
                                   └──────────────┬──────────────┘
                                                  │
               ┌──────────────────────────────────┼──────────────────────────────────┐
               │                                  │                                  │
               ▼                                  ▼                                  ▼
      ┌────────────────┐                 ┌────────────────┐                 ┌────────────────┐
      │   alpaca MCP    │                 │ tradingview MCP │                 │   HTTP (curl)  │
      │  account /      │                 │ coin_analysis    │                 │  Finnhub       │
      │  positions /    │                 │ smart_volume_   │                 │  earnings cal  │
      │  quotes /       │                 │ scanner          │                 │  + CNN Fear &  │
      │  place_order    │                 │                  │                 │  Greed index   │
      └────────────────┘                 └────────────────┘                 └────────────────┘
               │                                  │                                  │
               └──────────────────────────────────┼──────────────────────────────────┘
                                                  │
                                                  ▼
                                     ┌────────────────────────────┐
                                     │  risk.py   (subprocess)    │
                                     │  --snapshot  / --validate  │
                                     │  owns logs/state.json      │
                                     └──────────────┬─────────────┘
                                                    │
                                                    ▼
                                     ┌────────────────────────────┐
                                     │  logger.py  (subprocess)   │
                                     │  appends one JSONL row     │
                                     │  → logs/trading-log.jsonl  │
                                     └────────────────────────────┘
```

Three layers, strictly separated:

| Layer | Lives in | Responsibility |
|---|---|---|
| Reasoning | [.claude/skills/stock-trading/SKILL.md](.claude/skills/stock-trading/SKILL.md) | Phase orchestration, candidate ranking, BUY/SELL/HOLD decisions, limit price math, output formatting. Runs inside the Claude Code session. |
| Data | MCP servers (`alpaca`, `tradingview`) + two HTTP calls (Finnhub earnings, CNN Fear & Greed) | All market data in and all order placement out. No data is fetched anywhere else. |
| Risk + audit | [tools/stock-trading/risk.py](tools/stock-trading/risk.py), [tools/stock-trading/logger.py](tools/stock-trading/logger.py) | Hard money rules (per-position cap, session cap, min notional, spread cap, PDT block, idempotency) and append-only JSONL audit log. Python stdlib only, invoked as subprocesses. |

---

## The seven phases

Phase definitions are authoritative in [.claude/skills/stock-trading/SKILL.md](.claude/skills/stock-trading/SKILL.md); this is a summary.

| Phase | What happens | Fires in parallel with |
|---|---|---|
| 0 | Read `config.json`, parse flags, compute NY date, check `force_eod_close` cutoff | — |
| 1 | Alpaca account / positions / clock + CNN Fear & Greed curl | Phase 1.5 |
| 1.5 | `smart_volume_scanner` once per exchange (NYSE, NASDAQ), merge with open positions, dedupe, take top N | Phase 1 |
| 2 | Per-ticker: two `coin_analysis` calls (1D + 1H) + one batched Alpaca `get_stock_snapshot` + one Finnhub earnings curl | internally parallel |
| 2.5 | `WebSearch` for top-5-by-volume-ratio candidates, extract 2–3 recent headlines per ticker | internally parallel |
| 3 | `risk.py --snapshot` — returns PDT headroom, per-position cap, session cap, blocked tickers | — |
| 4 | Reasoning, no tool calls — rank candidates, apply the minimum-trade rule, pick BUY/SELL/HOLD | — |
| 5 | Per non-HOLD decision: compute limit price, compute qty, `risk.py --validate`, place limit order | sequential |
| 6 | `logger.py` — append one JSONL row (skipped on `--dry-run`) | — |
| 7 | Print summary to Josue | — |

The minimum-trade rule is the most important thing to understand: **every run must place at least one BUY unless it's a dry run, the account is blocked, or every candidate failed its dossier fetch.** A run full of HOLDs is a useless data point — it produces nothing to learn from — so Phase 4 will override the weakest HOLD into a BUY on the top-ranked non-blocked candidate if reasoning would otherwise produce zero trades. The audit log distinguishes "organic BUY" from "minimum-trade override BUY" in the rationale field. See the doctrine note in SKILL.md for the full reasoning.

---

## Repository layout

```
autonomous-trading/
├── README.md                               ← this file
├── CLAUDE.md                               ← orientation for future Claude Code sessions
├── .gitignore                              ← .env, logs, __pycache__
├── .claude/
│   └── skills/
│       └── stock-trading/
│           └── SKILL.md                    ← the skill: phases, rules, output format
├── tools/
│   └── stock-trading/
│       ├── SETUP.md                        ← MCP registration + API keys + smoke tests
│       ├── config.json                     ← screen / thresholds / risk / toggles
│       ├── risk.py                         ← --snapshot / --validate
│       └── logger.py                       ← JSONL writer
└── logs/                                   ← gitignored runtime state
    ├── trading-log.jsonl                   ← append-only audit log
    ├── state.json                          ← per-day idempotency + session-deployed tracker
    └── .state.lock                         ← fcntl.flock coordination file
```

---

## Setup

Full walkthrough is in [tools/stock-trading/SETUP.md](tools/stock-trading/SETUP.md). The short version:

1. **Register the MCP servers at user scope:**
   ```bash
   claude mcp add alpaca --scope user \
     --env ALPACA_API_KEY=... \
     --env ALPACA_SECRET_KEY=... \
     --env ALPACA_PAPER=true \
     -- uvx alpaca-mcp-server

   claude mcp add tradingview --scope user \
     -- /opt/homebrew/bin/uvx --from tradingview-mcp-server tradingview-mcp
   ```
   Use the **full path** to `uvx` — GUI shells don't inherit `PATH`. Verify with `claude mcp list` and look for `✓ Connected` on both.

2. **Get API keys:**
   - [Alpaca](https://alpaca.markets/) — create a paper account, copy key + secret.
   - [Finnhub](https://finnhub.io/) — free-tier key; the `/calendar/earnings` endpoint is covered.
   - TradingView — no key needed, no signup, free.

3. **Populate `.env` at the repo root:**
   ```
   ALPACA_API_KEY=PK_your_paper_key_here
   ALPACA_SECRET_KEY=your_paper_secret_here
   ALPACA_PAPER=true
   FINNHUB_API_KEY=your_finnhub_free_key_here
   ```
   `.env` is gitignored — confirm before adding.

4. **Smoke-test the Python layer** (from the repo root):
   ```bash
   echo '{"date":"2026-04-13","account":{"equity":10000,"cash":5000,"day_trade_count":0,"trading_blocked":false,"account_blocked":false,"pattern_day_trader":false},"positions":[]}' \
     | python3 tools/stock-trading/risk.py --snapshot
   ```
   Expect a JSON object with `"ok": true`, `"abort": false`, a `max_per_position`, and a `max_session_allocation`.

5. **First dry run** (in a Claude Code session, in this repo):
   > run the trading skill --dry-run

   Expected: account snapshot, morning screen with N candidates, per-ticker dossiers, a reasoning block, and a "DRY RUN — nothing placed" banner. `logs/state.json` **is** mutated under dry run (same code path as a real run), so delete it before the first real run of the day:
   ```bash
   rm -f logs/state.json logs/.state.lock
   ```

---

## Running the skill

In a Claude Code session, inside this repo:

| Trigger | Behavior |
|---|---|
| `run the trading skill` | Full run. Places limit orders on Alpaca (paper or live per `ALPACA_PAPER`). Appends one row to `logs/trading-log.jsonl`. |
| `run trading` / `trade` / `morning trades` / `stock trading` | Same as above — all recognized triggers. |
| `run the trading skill --dry-run` | Phases 0–5 run, including `risk.py --validate`. **Does NOT** place orders. **Does NOT** append to the log. **Does** mutate `logs/state.json` (because `--validate` is the same code path). Reset state before the first real run of the day. |

After-hours dry runs are largely decorative: IEX bid/ask spreads balloon to 5–10% outside regular trading hours, which trips the 0.5% spread cap on every ticker and skips the whole list. Use after-hours dry runs to verify MCP connectivity and dossier shape — not to evaluate reasoning. For real reasoning validation, dry-run between 09:30 and 16:00 ET.

---

## Configuration

Everything strategy-related lives in [tools/stock-trading/config.json](tools/stock-trading/config.json). There is no environment-variable override layer and no CLI flag other than `--dry-run`.

```json
{
  "experiment_id": "exp-001",
  "screen": {
    "max_candidates": 15,
    "min_price": 5.00,
    "exchanges": ["NYSE", "NASDAQ"],
    "rsi_range": "any",
    "min_volume_ratio": 1.5,
    "min_price_change": 0
  },
  "thresholds": {
    "rsi_oversold": 35,
    "rsi_overbought": 70,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.08,
    "volume_ratio_min": 1.5,
    "earnings_blackout_days": 3,
    "stock_score_min": 55
  },
  "risk": {
    "max_position_pct_of_equity": 0.20,
    "max_session_pct_of_cash": 0.80,
    "min_notional_usd": 50,
    "max_spread_pct_of_midpoint": 0.015,
    "pdt_threshold_equity": 25000,
    "pdt_max_day_trades": 3
  },
  "toggles": {
    "force_eod_close": false,
    "force_eod_close_cutoff_local": "15:45",
    "force_eod_close_timezone": "America/New_York"
  }
}
```

**`experiment_id` is frozen for the duration of an experiment.** Any change to the `risk` or `thresholds` blocks requires bumping this field and restarting a fresh 5-run paper cycle. See `CLAUDE.md` § Non-obvious constraints for the full discipline.

**The `risk` values are deliberately loose for paper mode.** Each paper-mode value has a `_*_note` sibling in the JSON describing the tighter value to use before going live (e.g. `max_position_pct_of_equity` tightens from 0.20 → 0.10, `max_spread_pct_of_midpoint` tightens from 0.015 → 0.003). Don't touch these without reading the promotion checklist.

**There is no static watchlist.** The candidate list is assembled fresh each run from the morning screen plus any open positions. If you want to force-include a ticker, open a position in it first.

---

## Hard safety rules (enforced by `risk.py`)

None of these are judgment calls. If `risk.py --validate` rejects an order, the skill logs the reason and skips the ticker. **It never retries with tweaked parameters.**

| Rule | Paper value | Live value (before promotion) |
|---|---|---|
| Max per-position size | 20% of equity | 10% of equity |
| Max session allocation | 80% of cash | 50% of cash |
| Min notional per order | $50 | $50 |
| Max bid/ask spread | 1.5% of midpoint | 0.3% of midpoint |
| PDT block | equity < $25k and `day_trade_count` ≥ 3 | same |
| Idempotency | reject duplicate `(date, ticker, side)` within same day | same |
| Account-level abort | Alpaca `trading_blocked` or `account_blocked` → abort run | same |

The spread cap is deliberately loose in paper mode (1.5%) so the minimum-trade rule isn't silently defeated by wide-spread tickers on the morning screen. Tightening to 0.3% before going live is non-negotiable.

---

## Audit log

Every non-dry run appends one JSON object to `logs/trading-log.jsonl`. Each row contains the full run dossier — account snapshot, market sentiment, screened candidates, per-ticker dossiers (including error reasons for excluded tickers), decisions with rationales and confidence labels, orders placed (or skip reasons), and any risk overrides.

```json
{
  "run_id": "2026-04-14T13:32:08Z",
  "mode": "paper",
  "dry_run": false,
  "account_snapshot": { ... },
  "market_sentiment": { "fear_greed_score": 42, "fear_greed_rating": "fear", "phase4_bias": "lean toward buys" },
  "screened": { "candidates": ["NVDA","AMD","..."], "final": ["NVDA","AMD"], ... },
  "dossiers": { "NVDA": { "rsi_14": 28.4, "ta_summary": "BUY", ... }, "MSFT": { "error": "..." } },
  "decisions": [ { "ticker", "action", "confidence", "rationale" }, ... ],
  "orders": [ { "ticker", "status", "order_id", "side", "qty", "limit_price", "notional" }, ... ],
  "risk_overrides": [ { "ticker", "reason" }, ... ]
}
```

The log is append-only and coordinated by `fcntl.flock` so concurrent writers cannot interleave partial lines. It is gitignored — do not commit run artifacts.

---

## Promotion checklist — paper → live

Do **not** flip `ALPACA_PAPER=false` in `.env` until all of the following are true:

- [ ] Five consecutive clean paper runs, each reviewed the same morning
- [ ] Every `logs/trading-log.jsonl` entry from those runs read end-to-end
- [ ] Zero MCP errors across those five runs
- [ ] `risk.py --validate` has rejected at least one real order (proof the guardrails actually fire)
- [ ] The `risk` block in `config.json` has been tightened to live values (see `_*_note` fields)
- [ ] You have a plan for what to do if the first live run places an order you disagree with

Going live is a one-line change. Going back to paper is the same one-line change. Use it freely.

---

## Non-obvious things worth knowing

- **`risk.py --validate` mutates `logs/state.json` even under `--dry-run`.** Same code path. After a dry-run experiment, `rm -f logs/state.json` before the first real run or idempotency will reject repeat orders.
- **TradingView `coin_analysis` has no bulk endpoint.** Phase 2 fans out one call per ticker per timeframe, in parallel, in a single message. Don't serialize.
- **TradingView exchange whitelist is `NASDAQ / NYSE / BIST / EGX / BURSA / HKEX`.** Anything else (including `AMEX` / `NYSEARCA`) silently falls back to the server's `KUCOIN` default and returns "No data found". That's why the morning screen is restricted to NYSE + NASDAQ.
- **Limit orders only, always.** `mcp__alpaca__place_stock_order` is always called with `type: "limit"`. Buy limit = `min(ask, midpoint × 1.001)`. Sell limit = `max(bid, midpoint × 0.999)`.
- **Fear & Greed is a soft dependency.** If the CNN curl fails, the sentiment adjustment is skipped, not blocked.
- **Reasoning lives in Markdown, not code.** The phase logic, the minimum-trade rule, the exclusion rules, and the ranking heuristic are all in [.claude/skills/stock-trading/SKILL.md](.claude/skills/stock-trading/SKILL.md). If you want to change behavior, edit the skill — not `risk.py`.

---

## Contributing / editing

This is a single-operator repo. If you fork it:

1. Read [.claude/skills/stock-trading/SKILL.md](.claude/skills/stock-trading/SKILL.md) end-to-end first.
2. Read [CLAUDE.md](CLAUDE.md) for the orientation future Claude sessions will see.
3. Treat the three layers as separate: reasoning changes go in the skill, risk-rule changes go in `risk.py` and `config.json`, log-format changes go in `logger.py`. Don't mix.
4. Never add a code path that places a market order. Never add a code path that bypasses `risk.py --validate`. Never add a code path that retries a rejected order with different parameters.
5. Paper trading is the default. Live mode is a one-line `.env` flip gated by the promotion checklist — not a config option to expose.

---

## License

No license specified. This is a personal trading tool; treat it as all-rights-reserved unless you hear otherwise from the author.
