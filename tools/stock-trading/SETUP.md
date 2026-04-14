# Stock Trading Skill — Setup

One-time setup for the MCP-native trading skill. Market data comes from two MCP servers (Alpaca + TradingView Screener) and one HTTP call (Finnhub earnings calendar); Python only enforces hard risk rules and appends the audit log.

**Paper trading is the default.** You must explicitly set `ALPACA_PAPER=false` in `.env` to go live.

---

## 1. Register the MCP servers

Register both servers at **user scope** via `claude mcp add` — that stores them in `~/.claude.json` and makes them available across projects. Each server runs as a local stdio subprocess.

**Alpaca** (one-time, only if not already registered):

```bash
claude mcp add alpaca --scope user --env ALPACA_API_KEY=... --env ALPACA_SECRET_KEY=... --env ALPACA_PAPER=true -- uvx alpaca-mcp-server
```

**TradingView** ([atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp), PyPI package `tradingview-mcp-server`):

```bash
claude mcp add tradingview --scope user -- /opt/homebrew/bin/uvx --from tradingview-mcp-server tradingview-mcp
```

Notes:
- Use the **full path** to `uvx` (`/opt/homebrew/bin/uvx` on Apple Silicon homebrew, `/home/YOUR_USERNAME/.local/bin/uvx` on Linux). GUI apps and some MCP client shells don't inherit `PATH`, and `uvx` alone will resolve to "command not found" intermittently.
- `uvx --from tradingview-mcp-server tradingview-mcp` pulls the package on first run and caches it. No manual `pip install` needed.
- TradingView MCP is free and takes **no env vars** — no API key, no subscription.

Verify with `claude mcp list` and look for `✓ Connected` on both `alpaca` and `tradingview`. If either says `✗`, check the server logs (`claude mcp get tradingview`) before continuing.

---

## 2. Get API keys

| Service | URL | Notes |
|---|---|---|
| Alpaca | https://alpaca.markets/ | Sign up → create a **paper** account → Dashboard → API Keys. Copy key + secret. Keep live account keys separate. |
| Finnhub | https://finnhub.io/ | Sign up → Dashboard → free API key. The `/calendar/earnings` endpoint is covered on the free tier. |
| TradingView Screener | *(none — free, no auth)* | No signup needed. Data scraped from TradingView public screener endpoints. |

---

## 3. Populate `.env`

Add to the project root `.env`:

```bash
ALPACA_API_KEY=PK_your_paper_key_here
ALPACA_SECRET_KEY=your_paper_secret_here
ALPACA_PAPER=true

FINNHUB_API_KEY=your_finnhub_free_key_here
```

Never commit `.env`. Confirm it is already in `.gitignore` before adding these.

---

## 4. Verify connectivity (read-only smoke tests)

Open a Claude Code session in this repo and run each of these one at a time:

1. **Alpaca** — call `mcp__alpaca__get_account_info`. Expect `{ equity, cash, status: "ACTIVE", ... }`.
2. **TradingView — single-ticker TA.** Call `mcp__tradingview__coin_analysis` with `symbol: "AAPL"`, `exchange: "NASDAQ"`, `timeframe: "1D"`. Expect `rsi.value`, `market_sentiment.buy_sell_signal`, `stock_score`, `grade`, and ~20 other indicator blocks. Note: this tool has **no bulk equivalent** — Phase 2 calls it once per ticker, in parallel. Also note the exchange whitelist is `NASDAQ / NYSE / BIST / EGX / BURSA / HKEX`; anything else (including `AMEX` / `NYSEARCA`) silently falls back to the crypto default `KUCOIN` and returns "No data found". That's why the skill's Phase 1.5 morning screen is restricted to NYSE and NASDAQ.
3. **TradingView — morning screener.** Call `mcp__tradingview__smart_volume_scanner` with `exchange: "NASDAQ"`, `rsi_range: "oversold"`, `min_volume_ratio: 1.5`, `min_price_change: 0`, `limit: 8`. Expect a list of oversold, high-relative-volume candidates. Repeat with `exchange: "NYSE"`. This is the tool that replaces the old static watchlist — there is no `screen_stocks` tool on this server despite what the README suggests.
3. **Finnhub — earnings calendar** (from a terminal in the repo root, with `.env` loaded):
   ```bash
   set -a; source .env; set +a
   curl -sS "https://finnhub.io/api/v1/calendar/earnings?from=$(date -u +%Y-%m-%d)&to=$(date -u -v+3d +%Y-%m-%d 2>/dev/null || date -u -d '+3 days' +%Y-%m-%d)&token=${FINNHUB_API_KEY}" | head -c 400
   ```
   Expect a JSON object with an `earningsCalendar` array. If you get an HTML error page or `{"error":"..."}`, the key is wrong.

4. **Finnhub — company news** (same `.env`, tests the Phase 2.5 call):
   ```bash
   curl -sS "https://finnhub.io/api/v1/company-news?symbol=AAPL&from=$(date -u -v-2d +%Y-%m-%d 2>/dev/null || date -u -d '-2 days' +%Y-%m-%d)&to=$(date -u +%Y-%m-%d)&token=${FINNHUB_API_KEY}" | head -c 400
   ```
   Expect a JSON array of items, each with `headline`, `datetime` (unix epoch seconds), `source`, and `url`. An empty `[]` is OK if AAPL happens to have no news in the last 48 hours — retry with a busier ticker like `NVDA`. If you get a 403, `{"error":"..."}`, or an HTML page, the free tier plan isn't covering this endpoint for your key. Per-run Finnhub call count: 1 earnings + up to 5 company-news = **6 total**, under the 60/minute free-tier limit.

If any of the three fails, fix credentials / MCP config before proceeding — do not run the skill.

---

## 5. Verify the Python safety layer standalone

From the repo root:

```bash
# risk.py --snapshot
echo '{"date":"2026-04-13","account":{"equity":10000,"cash":5000,"day_trade_count":0,"trading_blocked":false,"account_blocked":false,"pattern_day_trader":false},"positions":[]}' \
  | python3 tools/stock-trading/risk.py --snapshot
```

Expected output (keys in any order):
```json
{"ok": true, "abort": false, "max_per_position": 2000.0, "max_session_allocation": 4000.0, "pdt_headroom": 3, "session_deployed_so_far": 0.0, ...}
```

These values follow the frozen `experiment_id: exp-001` paper caps (`0.20 * $10k = $2k` per position, `0.80 * $5k = $4k` per session). If you see `1000.0 / 2500.0` or `2500.0 / 5000.0`, `config.json` has drifted from the experiment baseline — check whether `experiment_id` was bumped and risk caps were changed.

```bash
# risk.py --validate (approved) — reserves a pending entry + session_deployed
echo '{"date":"2026-04-13","ticker":"NVDA","side":"buy","qty":2,"limit_price":425.0,"bid":424.80,"ask":425.20,"account_equity":10000,"account_cash":5000,"day_trade_count":0,"existing_position":false}' \
  | python3 tools/stock-trading/risk.py --validate
```

Expected: `{"approved": true, "reason": null, "reserved_notional": 850.0}`. This writes a `pending` entry to `logs/state.json`. Every validate must be paired with exactly one commit OR release:

```bash
# Happy path: order placed successfully, promote pending -> submitted
echo '{"date":"2026-04-13","ticker":"NVDA","side":"buy"}' \
  | python3 tools/stock-trading/risk.py --commit
# Expected: {"ok": true, "committed": {..., "notional": 850.0}}

# Rollback path: order rejected by broker, refund session_deployed
echo '{"date":"2026-04-13","ticker":"NVDA","side":"buy"}' \
  | python3 tools/stock-trading/risk.py --release
# Expected: {"ok": true, "released": {..., "notional": 850.0}}
```

After the smoke tests, reset state so the first real run starts clean:

```bash
rm -f logs/state.json logs/.state.lock
```

```bash
# logger.py
echo '{"run_id":"setup-check","timestamp":"2026-04-13T09:00:00Z","mode":"paper","dry_run":true,"decisions":[]}' \
  | python3 tools/stock-trading/logger.py
```

Expected: `{"ok": true, "run_id": "setup-check", "path": "logs/trading-log.jsonl"}` and one new line in `logs/trading-log.jsonl`. Delete that line before the first real run:

```bash
: > logs/trading-log.jsonl
```

---

## 6. First dry run

In a Claude Code session, say:

> run the trading skill --dry-run

Expected: account snapshot, morning screen result (N candidates discovered from `smart_volume_scanner` across NYSE + NASDAQ, plus any open positions), per-ticker dossier for every surviving candidate, and a reasoning block per ticker. No orders are placed. `logs/state.json` is NOT touched. The `logs/trading-log.jsonl` file is NOT appended to.

The candidate list is now **dynamic** — there's no fixed watchlist in config. Each run discovers its own oversold/high-relative-volume names via `smart_volume_scanner`, merges with open positions, applies the `screen.min_price` floor client-side, then runs the full dossier fetch. If the screen returns zero hits on a given morning, the run ends cleanly with "no candidates" and no orders.

Review the output. If the screen returned a sensible candidate list, every surviving candidate produced a complete dossier, and every decision reads sensibly, you are ready for a paper run.

---

## 8. Outcome evaluation

After you've made a few paper runs and want to see how the decisions turned out:

```
run the trading skill --evaluate
```

(Or `evaluate trades` / `run the evaluator` — the aliases map to the same mode.) This is a read-only pass that:

- Walks `logs/trading-log.jsonl` to find every placed order.
- Walks `logs/outcomes.jsonl` (created on first run) to get the current state of each decision.
- Fires parallel `mcp__alpaca__get_order_by_id` and `mcp__alpaca__get_stock_bars` calls to advance each decision's state through `pending_fill → filled → t0 → t1 → t5 → t20`.
- Appends new state transitions to `logs/outcomes.jsonl` via `outcomes_reducer.py --append`.
- Prints a plain-text summary: fill slippage, mean T+5 returns, mean returns by confidence label, organic-BUY vs minimum-trade-override-BUY comparison, count of decisions still pending.

Evaluate Mode does NOT place orders, does NOT call `risk.py`, and does NOT touch `trading-log.jsonl`. It's safe to run as often as you like — re-running on the same day is a no-op for any state that can't yet advance.

The standalone reducer is useful for debugging:

```bash
# dump the current work queue
echo '{}' | python3 tools/stock-trading/outcomes_reducer.py --current-state

# append a synthetic row (use only for testing — in normal use, the skill calls this)
echo '{"lines":[{"run_id":"test","ticker":"TEST","outcome_state":"filled"}]}' \
  | python3 tools/stock-trading/outcomes_reducer.py --append
```

For a human-readable view of a past run (without touching Alpaca at all):

```bash
python3 tools/stock-trading/run-summary.py                 # most recent run
python3 tools/stock-trading/run-summary.py --since 2026-04-14
python3 tools/stock-trading/run-summary.py --experiment exp-002
```

## 9. Promotion checklist (before going live)

Do **not** flip `ALPACA_PAPER=false` until:

- [ ] 5 consecutive clean paper runs, each reviewed the same morning
- [ ] Every `logs/trading-log.jsonl` entry reviewed for bad reasoning or missing dossiers
- [ ] No MCP errors in any of those 5 runs
- [ ] `risk.py --validate` has rejected at least one real order (confirm the guardrails fire)
- [ ] You have a plan for what you will do if the first live run places an order you disagree with

Going live is a one-line change in `.env`. Going back to paper is the same one-line change. Use it.
