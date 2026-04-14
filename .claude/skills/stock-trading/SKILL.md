---
name: stock-trading
description: Manually-triggered morning stock/ETF trading routine. Pulls account snapshot and per-ticker dossiers through Alpaca + TradingView Screener MCP servers and a Finnhub earnings HTTP call; reasons through BUY/SELL/HOLD decisions against a fixed strategy; validates each order through a Python risk layer; places limit-only orders on Alpaca; appends a full audit log. Paper trading by default. Trigger phrases — "run the trading skill", "run trading", "trade", "morning trades", "stock trading". Supports --dry-run.
---

# Skill: stock-trading

**Manually triggered.** There is no cron, no launchd, no background process. Josue runs this inside a Claude Code session when he wants it to run. **Typical cadence: one run per trading day at ~10:00 ET.** Running earlier than 10:00 is allowed but introduces well-known data-quality problems — the volume-ratio denominator is tiny (<3% of a session by 09:40), bid/ask spreads are 2–5× wider than later in the morning, and premarket gaps haven't resolved. 10:00 ET is the canonical experiment time.

**Paper trading by default.** Only goes live when `ALPACA_PAPER=false` is set in `.env`. Never place a market order under any circumstance — limit orders only.

## Experiment discipline

The `risk`, `thresholds`, and (future) `ranking` blocks in `tools/stock-trading/config.json` are **frozen for the duration of an experiment**. Each experiment has an `experiment_id` at the top of the config file. Any change to those blocks requires bumping `experiment_id` and starting a fresh run of 5 paper sessions. Mid-experiment tweaks to risk caps or thresholds silently corrupt the data the skill is supposed to produce.

The skill reads `experiment_id` from config in Phase 0 and passes it through to the logger payload as `experiment_id` in Phase 6. Every row in `logs/trading-log.jsonl` carries the experiment id it was generated under, so later analysis can segment cleanly.

## Trigger Phrases
`run the trading skill`, `run trading`, `trade`, `morning trades`, `stock trading`

Add `--dry-run` to any trigger to run steps 1–5 without placing orders or writing the log.

Add `--evaluate` to any trigger (e.g. `run the trading skill --evaluate`) to run **Evaluate Mode** instead of the morning routine — a read-only walk over past decisions that advances their outcome state machine toward T+20. Evaluate Mode does not place orders, does not call `risk.py`, does not touch `trading-log.jsonl`, and does not re-read `config.json`'s risk block. See § Evaluate Mode below. Aliases: `evaluate trades`, `run the evaluator`.

---

## Required MCP Servers

| Server | Provides | Key in `.env` |
|---|---|---|
| `alpaca` | Account snapshot, positions, quotes (bid/ask/last/prev_close/volume), limit order execution | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER` |
| `tradingview` | RSI(14), MACD, Bollinger, 20+ indicators, composite TA recommendation (STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL) — `tradingview-mcp-server` by atilaahmettaner | *(none — free, no auth)* |

Plus one HTTP call each run:

- **Finnhub** — earnings calendar. Called directly via `curl` in Phase 2, not via MCP. Free tier key required in `.env` as `FINNHUB_API_KEY`.

If any required MCP server is not connected, refuse to run and tell Josue which one is missing. See `tools/stock-trading/SETUP.md`.

---

## Environment Variables

| Var | Purpose | Default |
|---|---|---|
| `ALPACA_API_KEY` | Alpaca API key (paper or live) | (required) |
| `ALPACA_SECRET_KEY` | Alpaca API secret | (required) |
| `ALPACA_PAPER` | `true` = paper trading, `false` = live | `true` |
| `FINNHUB_API_KEY` | Finnhub free-tier key — earnings calendar | (required) |

**No Anthropic API key is needed.** Reasoning happens inside the Claude Code session itself.

---

## Token Efficiency Rules
1. **Phase 1 (account + Fear & Greed) fires in parallel with Phase 1.5 (morning screen)** — both are independent. Fire Alpaca account + clock + positions + Fear & Greed curl + `smart_volume_scanner` per exchange all in the same message.
2. **Phase 2 (dossiers) must wait for Phase 1.5** because the candidate list comes from Phase 1.5 output. Once the candidate list exists, fire in one message: two `coin_analysis` calls per ticker (1D and 1H), the single batched Alpaca `get_stock_snapshot`, and the Finnhub earnings curl.
3. **Phase 2.5 (news search) fires after Phase 2 returns** — the top-5-by-volume-ratio list depends on dossier fields. Fire all top-5 Finnhub `/company-news` curls in one parallel bash block.
4. Read `tools/stock-trading/config.json` once at the start — never re-read during reasoning.
5. **Hard vs. soft dependencies for exclusion**: 1D TradingView call and Alpaca snapshot are hard (missing → ticker excluded). 1H TradingView call, Finnhub earnings call, Phase 2.5 news search, and Phase 1 Fear & Greed are all soft (missing → default value, continue run).
6. Keep output terse — no narration of tool calls, just the final summary block (§ Output Format).

---

## Morning Routine

### Phase 0 — Load config + parse flags
- `Read tools/stock-trading/config.json`. Extract `experiment_id`, `screen`, `thresholds`, `risk`, `toggles`.
- Stash `experiment_id` on the run dossier immediately — it rides through to Phase 6's logger payload unchanged.
- **There is no static `watchlist` in config anymore.** The tradable list for the session is assembled in Phase 1.5 from the morning screen + any open positions.
- If the trigger phrase includes `--dry-run`, set `dry_run=true`.
- Compute today's date in `America/New_York` (used as the idempotency key).
- If `toggles.force_eod_close` is `true` AND current NY time is past `toggles.force_eod_close_cutoff_local` (default `15:45`), set `force_eod_close_active=true`.

### Phase 0.5 — TradingView MCP health check (fail-fast)

Before any data fan-out, fire one canary call to confirm TradingView MCP is actually returning stock data (not silently falling back to its `KUCOIN` crypto default):

```
mcp__tradingview__coin_analysis
  symbol: "AAPL"
  exchange: "NASDAQ"
  timeframe: "1D"
```

AAPL on NASDAQ is the documented known-working canary (`SETUP.md` § 4). Do **not** use SPY or QQQ — both list on NYSEARCA, which is not in TradingView's whitelist and silently falls back to KUCOIN.

**The call is considered failed if any of these is true:**
- The MCP returns an error, timeout, or non-200 status.
- The response body contains the string `"No data found"` (the documented KUCOIN fallback symptom).
- `stock_score`, `market_sentiment.buy_sell_signal`, or `rsi.value` is missing from the response.

**On failure: abort the run immediately**, before Phase 1 fires. Emit:

```
TradingView MCP health check failed on AAPL/NASDAQ (<error detail>).
Aborting. Run `claude mcp list` and check that `tradingview` shows ✓ Connected.
```

Do not write to `logs/trading-log.jsonl`, do not touch `logs/state.json`, do not call `risk.py`. This is a fail-fast check — its whole purpose is to stop the run before it produces a silently empty dossier set that would either (a) make Phase 2 hard-exclude every ticker, or (b) trip the minimum-trade rule's "all candidates failed their dossier fetch" exemption, both of which produce uselessly empty runs.

**On success: proceed to Phase 1.** Do not reuse the AAPL dossier as an actual Phase 2 candidate — this call is purely a canary and its payload is discarded.

### Phase 1 — Account snapshot + market sentiment (parallel, one message)
Fire all of these together:

**Alpaca (account):**
- `mcp__alpaca__get_account_info` → equity, cash, `day_trade_count`, `trading_blocked`, `account_blocked`, `pattern_day_trader`
- `mcp__alpaca__get_all_positions` → open positions with entry price + unrealized P&L
- `mcp__alpaca__get_clock` → confirm market open (`is_open`, `next_open`, `next_close`)

**CNN Fear & Greed Index (HTTP, via bash):**
```bash
curl -sS "https://production.dataviz.cnn.io/index/fearandgreed/graphdata" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
  -H "Referer: https://www.cnn.com/markets/fear-and-greed" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); fg=d.get("fear_and_greed",{}); print(json.dumps({"score": fg.get("score"), "rating": fg.get("rating")}))'
```
The endpoint requires both a browser-like `User-Agent` and a matching `Referer` header — without them it returns an empty response. Extract `fear_and_greed.score` (0–100) and `fear_and_greed.rating` (text label: `"extreme fear"` / `"fear"` / `"neutral"` / `"greed"` / `"extreme greed"`).

**If the curl fails** (network error, schema change, empty body), set `fear_greed_score = null` and `fear_greed_rating = null` and continue the run — **do not abort**. Phase 4 must handle the null case by skipping the sentiment adjustment entirely, not by blocking trades.

If `trading_blocked` or `account_blocked` is `true`, abort the run immediately and tell Josue.

**After-hours warning:** If `mcp__alpaca__get_clock` returns `is_open=false`, emit a warning banner at the top of the summary: `⚠️ market is closed — quotes may be stale, spreads wider than RTH, after-hours dry runs will skip most tickers`. Continue the run (dry runs are often intentional after hours), but this sets expectations before the skip list appears.

**Market sentiment interpretation** (feeds into Phase 4):

| Score | Rating | Phase 4 bias |
|---|---|---|
| 0–25 | Extreme Fear | Trade aggressively — oversold tape favors mean-reversion buys. Upgrade marginal BUY setups. |
| 26–45 | Fear | Lean toward buys. The market is cautious; contrarian setups work here. |
| 46–55 | Neutral | No directional bias. Rank on technicals alone. |
| 56–75 | Greed | Be selective. Reduce confidence on marginal setups. |
| 76–100 | Extreme Greed | Market is frothy. Only trade the strongest setups. **Downgrade all "medium" confidence BUYs to "low"** and require higher confluence before promoting a HOLD to BUY under the minimum-trade rule. |

If `fear_greed_score` is null, skip this adjustment entirely and proceed with technicals-only reasoning.

### Phase 1.5 — Morning screen (dynamic candidate discovery)

Goal: build the session's candidate list. There are **two sources**, and both contribute:

1. **All open positions** — pulled from Phase 1's `get_all_positions` result. Always included in the candidate list regardless of screen results, because we need fresh dossiers on them to make SELL / stop-loss / take-profit decisions.
2. **Fresh screen results** — run the TradingView volume scanner once per configured stock exchange with **no RSI filter**, so both mean-reversion (oversold) and momentum/breakout setups surface together. Merge and rank downstream.

**Scanner:** `mcp__tradingview__smart_volume_scanner` — accepts US stock exchanges and lets us cast a wide net on relative volume alone.

Fire one call per exchange in `screen.exchanges` (NYSE and NASDAQ). For each call, pass:
- `exchange` = the exchange
- `rsi_range` = `screen.rsi_range` (default `"any"` — **no RSI filter**, so both oversold reversals and breakout momentum surface in one pass)
- `min_volume_ratio` = `screen.min_volume_ratio` (default 1.5 — this is a *relative* volume multiplier, not an absolute share count)
- `min_price_change` = `screen.min_price_change` (default 0)
- `limit` = `screen.max_candidates` (default 15 per exchange — gives Phase 4 enough names to rank)

Merge the results from both exchanges into a single list. Deduplicate by symbol. Sort by volume ratio descending. Take the top `screen.max_candidates` rows.

**Known limitations of this scanner** (documented in config `_filter_notes`): it does **not** expose market cap, absolute average volume, or a numeric RSI threshold — the only implementable guardrails are exchange, relative volume, and the client-side **price floor** (`screen.min_price`, default $5) applied after Phase 2 dossiers return. That's fine: this is a paper experiment, the goal is candidate breadth, and Phase 4 picks the best of what comes back.

**Candidate assembly:**
```
candidates = dedupe([
  *open_position_tickers,
  *screen_results[: screen.max_candidates]
])
```

If `candidates` is empty (no positions AND no screen hits), the run ends cleanly with a summary noting "no candidates" and no log entry beyond the empty-decisions record.

### Phase 2 — Dossier fetch (parallel, one message)
For every ticker in `candidates`, fire all of these at once:

**TradingView MCP — `coin_analysis`** (`tradingview-mcp-server` by atilaahmettaner — free, no auth, crypto-named but handles stocks):
- Fire **two** `mcp__tradingview__coin_analysis` calls per candidate in parallel — one with `timeframe="1D"` (daily structure) and one with `timeframe="1H"` (intraday entry timing).
- `symbol`, `exchange` (must be one of `NASDAQ` / `NYSE` / `BIST` / `EGX` / `BURSA` / `HKEX` — the server silently falls back to its `KUCOIN` default for any unrecognized value, including `AMEX`/`NYSEARCA`, which causes ETF lookups to fail with "No data found").
- **There is no bulk equivalent** on this build. Fan out across all candidates × 2 timeframes in a single parallel message.
- From the 1D result extract `rsi_14` (from `rsi.value`), `ta_summary` (from `market_sentiment.buy_sell_signal`), `stock_score`, and `grade` — these remain the primary structural signals.
- From the 1H result extract `rsi_1h` (from `rsi.value`) and `ta_summary_1h` (from `market_sentiment.buy_sell_signal`). These feed Phase 4's multi-timeframe confluence check.
- **Response shape (useful fields):**
  - `price_data.current_price`, `.open`, `.change_percent`, `.volume`
  - `rsi.value` (numeric 0–100), `.signal`, `.direction`
  - `macd.macd_line`, `.signal_line`, `.histogram`, `.crossover`
  - `sma.sma10..sma200` + `.signals`
  - `ema.ema10..ema200` + `.signals`
  - `bollinger_bands.upper/middle/lower`, `.width`, `.squeeze`, `.position`
  - `stochastic.k`, `.d`, `.signal`
  - `adx.value`, `.trend_strength`, `.plus_di`, `.minus_di`, `.di_signal`
  - `support_resistance.resistance_1..3`, `support_1..3`, `nearest_*`
  - `market_structure.trend`, `.trend_score`, `.momentum_aligned`, `.candle.*`
  - `market_sentiment.buy_sell_signal` (composite `STRONG_BUY`/`BUY`/`NEUTRAL`/`SELL`/`STRONG_SELL`)
  - `stock_score` (0–100), `grade` (letter grade — `"Avoid"` / `"Weak"` / `"Neutral"` / `"Good"` / `"Strong"` typically), `trend_state`
- Extract into the dossier: `rsi_14` from `rsi.value`, `ta_summary` from `market_sentiment.buy_sell_signal`, `stock_score` from `stock_score`.

**Alpaca:**
- `mcp__alpaca__get_stock_snapshot` · `symbols` = comma-joined `candidates` (one call, batched — do not fan out). Returns `latestQuote` (bid/ask), `latestTrade` (last), `dailyBar` (intraday OHLCV), `prevDailyBar` (prior close + prior volume). Derive:
  - `bid`, `ask`, `last` ← `latestQuote.bp` / `latestQuote.ap` / `latestTrade.p`
  - `premarket_change_pct` ← `(dailyBar.o - prevDailyBar.c) / prevDailyBar.c * 100` (opening gap vs prior close — meaningful from 09:30 ET onward)
  - `volume_ratio` ← **time-adjusted**: `dailyBar.v / (prevDailyBar.v * (minutes_since_open / 390))` where 390 is total RTH minutes (09:30–16:00 ET) and `minutes_since_open = max(1, floor((now_et - 09:30_et).total_seconds() / 60))`. The ratio is **normalized against elapsed session time**: >1 means the ticker is tracking above yesterday's full-day volume on a per-minute basis, >1.5 means it's running clearly hot, regardless of whether it's 10:00 or 15:45. At exactly 09:30 the denominator would be zero — the `max(1, …)` floor prevents division-by-zero and makes the first minute read as "very heavy" which is harmless because Phase 2 is never called before 10:00 in practice.

**Finnhub (HTTP, called via bash — not MCP):**
- Fire one single `curl` request covering the full candidate list (not one per ticker):
  ```bash
  from_date=$(date -u +%Y-%m-%d)
  to_date=$(date -u -v+3d +%Y-%m-%d 2>/dev/null || date -u -d '+3 days' +%Y-%m-%d)
  curl -sS "https://finnhub.io/api/v1/calendar/earnings?from=${from_date}&to=${to_date}&token=${FINNHUB_API_KEY}"
  ```
  Parse the returned `earningsCalendar` array, and for each candidate set `earnings_within_3d = true` if its symbol appears, else `false`. The Finnhub free tier covers this endpoint without 403.

**Client-side price floor (applied after dossiers return):** for each candidate, if `last < screen.min_price` (default $5), mark the ticker with `{"error": "below screen.min_price"}` and exclude it. This is how the $5 penny-stock floor gets enforced since the TradingView scanner can't do it upstream.

Assemble a dossier per ticker:
```
{
  rsi_14, ta_summary, stock_score, grade,      // from 1D coin_analysis
  rsi_1h, ta_summary_1h,                        // from 1H coin_analysis
  premarket_change_pct, volume_ratio,           // from Alpaca snapshot
  earnings_within_3d,                           // from Finnhub
  news_headlines,                               // from Phase 2.5 (top 5 only; [] for others)
  bid, ask, last                                // from Alpaca snapshot
}
```

**Exclusion rule (strict):** if any **1D** field is missing or its call errored (including the price-floor filter above), mark the ticker as `{"error": "..."}` and exclude it from reasoning. The 1H call is a **soft** dependency — if it errors, set `rsi_1h=null`, `ta_summary_1h=null` and keep the ticker in the candidate pool; Phase 4 skips the multi-timeframe confluence check for that ticker rather than vetoing it. Similarly `news_headlines` defaults to `[]` for candidates outside the top-5-by-volume and for candidates whose Phase 2.5 search errored — those are not grounds for exclusion.

### Phase 2.5 — News headline lookup (top 5 by volume ratio)

Goal: catch positive catalysts that justify a weak-technicals BUY, and negative catalysts that should override a strong-technicals BUY into a HOLD.

**Scope:** After Phase 2 dossiers return, rank the surviving candidates by `volume_ratio` descending and take the **top 5**. News calls are bounded, not cheap — don't burn them on the bottom of the list.

**Primary source: Finnhub `/company-news`** (HTTP, called via bash, not MCP). Fire one `curl` per top-5 ticker in a single parallel bash block — up to 5 curls. Finnhub's free tier is 60 calls/minute; with 1 earnings call earlier in Phase 2 + up to 5 news calls here, every run consumes at most 6 Finnhub calls — well under the limit.

```bash
from_date=$(date -u -v-2d +%Y-%m-%d 2>/dev/null || date -u -d '-2 days' +%Y-%m-%d)
to_date=$(date -u +%Y-%m-%d)
curl -sS "https://finnhub.io/api/v1/company-news?symbol=${TICKER}&from=${from_date}&to=${to_date}&token=${FINNHUB_API_KEY}"
```

The response is a JSON array of items shaped `{ "headline": "...", "datetime": 1713100000, "source": "...", "url": "...", "related": "NVDA", ... }`. `datetime` is a unix epoch (seconds, UTC).

**Parse:**
- Drop any item older than 48 hours (`now_epoch - datetime > 172800`). The `from_date` window is already 2 days, so this is belt-and-suspenders — but the endpoint sometimes returns items slightly outside the window.
- Keep the `headline` string; discard url/source unless useful for disambiguation.
- Take up to 3 items after the age filter. Drop anything that's clearly unrelated (sector ETF mentions that happen to tag the ticker, etc.) — this is a judgment call, not a keyword filter.
- Store as `news_headlines: ["headline 1", "headline 2", "headline 3"]` on the dossier.

**For candidates outside the top 5 by volume ratio:** set `news_headlines: []` — no curl fired, not a failure.

**If a curl errors** (network, non-200, malformed JSON, empty array after filtering): set `news_headlines: []` on that ticker and continue. Not grounds for exclusion.

**Manual fallback:** if Finnhub is systematically down (all 5 calls erroring), the documented manual recovery is to re-run Phase 2.5 using Claude Code's built-in `WebSearch` tool with query `"<TICKER> stock news today"` for each top-5 ticker. This is a manual fallback for outage recovery, not a primary code path — it returns noisier, snippet-shaped data that makes the negative-catalyst veto less reliable.

**Phase 4 uses this signal directly:**
- **Clear positive catalyst** (earnings beat, upgrade, FDA approval, major partnership, strong guidance, M&A) → upgrade confidence on a BUY by one notch (low→medium, medium→high). Can also justify a BUY on a technically weak setup.
- **Clear negative catalyst** (earnings miss, downgrade, SEC action, CEO departure, product recall, lawsuit, dilutive offering, major customer loss) → **override to HOLD regardless of technicals**. Negative news is one of the few allowed overrides to the minimum-trade rule: if the only tradeable candidate has clear negative news, pick the next candidate instead.
- **Ambiguous or no news** → no adjustment. Rank on technicals.

Parsing is judgment-based, not keyword-based. "Stock falls on weak guidance" is negative. "Stock rises despite weak guidance" is neutral-to-positive. Read the headline, don't regex it.

### Phase 3 — Risk snapshot (subprocess)
```bash
python3 tools/stock-trading/risk.py --snapshot
```
stdin: `{ "date": "YYYY-MM-DD", "account": {...}, "positions": [...] }` (built from Phase 1 results)

stdout: risk context. Read these fields:
- `abort` — if `true`, stop the whole run and surface `abort_reason` to Josue
- `pdt_headroom` — remaining day trades (-1 = unlimited)
- `max_per_position` — dollar cap per new position
- `max_session_allocation` — total dollars deployable this session
- `session_deployed_so_far` — already-deployed this date
- `blocked_tickers` — symbols with open positions (no pyramiding buys)

### Phase 4 — Reasoning (no tool calls)

> **MINIMUM TRADE RULE**
>
> Every run must place **at least one BUY order** unless one of the following is true:
> (a) `dry_run=true`, (b) `trading_blocked=true` or `account_blocked=true`, (c) **all** candidates failed their dossier fetch (the list is genuinely empty), or (d) **too-garbage-to-trade waiver**: the top-ranked candidate by the Phase 4 ranking heuristic has `stock_score < 40` AND `ta_summary ∈ {SELL, STRONG_SELL}`. When (d) fires, record `min_trade_override_waived: true` on the run dossier with reason `"top candidate below garbage threshold"`, and still emit HOLD for every candidate. This waiver should fire on fewer than 2% of runs — if it fires on more than 5%, the morning screen is broken, not the rule.
>
> If Phase 4 reasoning produces all HOLDs and none of the exemptions above apply, **override the weakest HOLD to a BUY on the highest-ranked candidate by the Phase 4 ranking heuristic** (see below). State clearly in the decision rationale that this is a minimum-trade override — the audit log needs to distinguish "organic BUY based on confluence" from "override BUY because the run would have been empty". Set `min_trade_override: true` on that decision.
>
> **Why this exists:** this is a paper account. The cost of a bad trade is zero. The cost of a run with no trades is a useless data point — the whole purpose of the skill is to observe Claude's live reasoning under real market conditions, and a run full of HOLDs produces nothing to learn from. A weak setup traded with real discipline beats a strong setup that never happened. The waiver exists only for the rare case where "weak setup" becomes "setup so bad it contaminates the data" — a candidate that's both strongly bearish (`SELL`/`STRONG_SELL`) AND low-quality (`stock_score < 40`).

For every ticker with a complete dossier, decide BUY / SELL / HOLD with a short rationale and a confidence label (high / medium / low). There are no hard numeric gates — weigh signals holistically and pick the best available trade.

**BUY — pick the best available candidate from the dossier list.**

There is always a best option. Rank all candidates and BUY the top one (subject to the risk layer's approval in Phase 5). The following are **inputs to reasoning, not pass/fail gates**:

- `rsi_14` (1D) — oversold (<35) is a tailwind for mean-reversion; neutral (40-60) is fine for momentum; overbought (>70) is a headwind and usually means "not this one".
- `rsi_1h` + `ta_summary_1h` — **multi-timeframe confluence check**:
  - **1D and 1H both BUY/STRONG_BUY** → strong confluence, upgrade confidence one notch.
  - **1D BUY, 1H SELL (or vice-versa)** → treat as mixed; note the conflict in the rationale but do not block the trade.
  - **1H STRONG_BUY, 1D NEUTRAL** → acceptable intraday entry; do not block, but label confidence `medium` at most.
  - **1H data missing** (soft dependency, didn't fetch) → skip this check entirely; rank on 1D only.
- `stock_score` and `grade` — high score / "Good"-or-better grade is confluence. Low score / "Avoid" grade is a warning but not a veto.
- `ta_summary` (1D) — `STRONG_BUY`/`BUY` aligns; `NEUTRAL` is acceptable; `SELL`/`STRONG_SELL` is a strong preference against picking that ticker for BUY.
- `volume_ratio` (time-adjusted) — >1 on pace, >1.5 running hot, >2 exceptional. Heavier relative volume = stronger signal regardless of direction.
- `premarket_change_pct` — positive for momentum buys, slightly negative is OK for mean-reversion buys on oversold RSI.
- `news_headlines` — **read the headlines as context, not as regex**:
  - **Clear positive catalyst** (earnings beat, upgrade, FDA approval, major partnership, strong guidance, M&A) → upgrade confidence one notch. Can justify a BUY on a technically weak setup.
  - **Clear negative catalyst** (earnings miss, downgrade, SEC action, CEO departure, product recall, lawsuit, dilutive offering, major customer loss) → **override to HOLD regardless of technicals**, AND pick the next-ranked candidate for the minimum-trade BUY. Negative news is one of the few allowed overrides to the minimum-trade rule — it never forces a BUY on a knife-catch setup.
  - **Empty or ambiguous** → no adjustment.
- `market_sentiment` (Phase 1 Fear & Greed): apply the table-documented bias from Phase 1. Extreme Greed downgrades "medium" BUYs to "low" as a hard rule; Extreme Fear upgrades marginal setups. If the score is null, skip the sentiment adjustment.
- `earnings_within_3d` — true is a soft veto (skip this candidate in favor of others). If it's true on ALL candidates, the minimum-trade rule still fires and you pick the one with the cleanest other signals.
- `blocked_tickers` from risk snapshot — no pyramiding buys. Hard constraint.
- `pdt_headroom` — if 0 (and not unlimited), BUY is blocked for opening new positions. Hard constraint.

**Ranking heuristic (R9 — versioned in config):** the Phase 4 ranking formula is sourced from `config.json` → `ranking.weights`. Version `v1` uses `stock_score` (35%) + alignment of `ta_summary`/`ta_summary_1h` with intended direction (30%) + `volume_ratio` (20%) + `rsi_14` suitability for the setup type (10%) + news tone (5%, with negative catalysts acting as a hard veto rather than a weighted deduction). Any change to these weights requires bumping `experiment_id` and `ranking.version` in the same edit. The skill must include `ranking_version: "<value from config>"` in the Phase 6 logger payload so downstream analysis can segment decisions by ranking generation.

Pick the top-ranked non-blocked, non-earnings, non-negative-news candidate. If every candidate has earnings within 3 days, pick the one with the cleanest structural read and state the earnings risk in the rationale.

**A weak setup is still tradeable on a paper account.** The goal of this phase is to exercise judgment under uncertainty, not to sit out.

**SELL — hard rules fire on their own; soft rules need 2-of-3 confluence.**

**Hard rules (any one is sufficient):**
- `ta_summary` is `STRONG_SELL` on an existing position. Single-signal STRONG_SELL is decisive.
- Existing position down ≥ `thresholds.stop_loss_pct` (default 5%) from entry.
- Existing position up ≥ `thresholds.take_profit_pct` (default 8%) from entry.
- `force_eod_close_active` is true — see the override note below.

**Soft rules (require 2-of-3 to fire together before emitting SELL on technical deterioration alone):**
- `ta_summary` is `SELL` (not STRONG_SELL).
- `stock_score` dropped below `thresholds.stock_score_min` (default 55) since entry AND `ta_summary` is `NEUTRAL` or worse.
- RSI(14) above `thresholds.rsi_overbought` (default 70).

**Rationale for the soft split (R8):** any one of those three conditions can fire on a single-day technical wobble and trigger a premature exit. Requiring two of the three to align demands actual confluence — e.g. overbought RSI *and* a deteriorating composite score, or a SELL summary *and* an overbought reading. The hard rules still fire on their own because stop-loss, take-profit, and STRONG_SELL are categorically different from "technicals look a bit soft today".

Log every SELL decision with the triggering rule(s) in the rationale — a reader of `trading-log.jsonl` should be able to see "2-of-3: SELL summary + RSI overbought" or "hard: take-profit hit" directly.

**HOLD — now valid only in these narrow cases:**
- The ticker's dossier fetch failed (missing fields, provider error, below price floor). This is functionally the same as SKIP; it stays labeled HOLD for the audit log.
- An existing position is already at take-profit OR stop-loss threshold AND `force_eod_close_active` is false — the SELL rules above would fire regardless, so a "HOLD at threshold" is really only valid for a narrow window where the position is right at the edge but hasn't crossed it.
- PDT block fires: `day_trade_count >= thresholds.pdt_max_day_trades` AND `equity < risk.pdt_threshold_equity`, which blocks new opening trades (but not closing ones). In this case every BUY decision collapses to HOLD because the account literally cannot open new day trades, and the minimum-trade rule does NOT override — it respects the PDT block as a hard account-level constraint.

"Signals are mixed", "confidence would be low", and "the setup isn't great" are **no longer valid HOLD reasons**. If Phase 4 wants to HOLD for one of those reasons, it must instead go to BUY on the strongest candidate and call out the weakness in the rationale.

**Override: `force_eod_close_active` is true**
→ For every open position, emit a SELL decision regardless of other signals. Rationale: `force_eod_close cutoff passed`. Normal risk validation still applies.

> **Paper-experiment doctrine:** a trade placed beats no trade placed. The risk layer (`risk.py`) still enforces all the hard money rules — 10% per-position cap, 50% session cap, $50 minimum notional, 0.5% spread cap, PDT block, duplicate-order block — so "always trade" cannot actually blow up the account. It just guarantees the experiment produces data every time it runs.

### Phase 5 — Execution (per non-HOLD decision)
For each BUY/SELL, in sequence:

1. **Compute limit price:**
   - Buy: `min(ask, midpoint * 1.001)`
   - Sell: `max(bid, midpoint * 0.999)`
   where `midpoint = (bid + ask) / 2`.

2. **Compute qty:**
   - Buy: `floor(max_per_position / limit_price)`; if `qty * limit_price < 50`, skip with reason "notional below $50 minimum".
   - Sell: use the full position qty (or the qty Josue confirms if partial).

3. **Validate (reserves a pending entry in state.json):**
   ```bash
   python3 tools/stock-trading/risk.py --validate
   ```
   stdin (example):
   ```json
   {
     "date": "2026-04-13",
     "ticker": "NVDA",
     "side": "buy",
     "qty": 2,
     "limit_price": 425.50,
     "bid": 425.20,
     "ask": 425.80,
     "account_equity": 10000.00,
     "account_cash": 5000.00,
     "day_trade_count": 1,
     "existing_position": false
   }
   ```
   If `approved: false`, log the reason in `risk_overrides` and skip this ticker. **Do not retry with different parameters.**

   If `approved: true`, the response carries a `reserved_notional` field and `risk.py` has written a `pending` entry in `logs/state.json`. This reservation must be either **committed** (step 5, after a successful order placement) or **released** (on dry-run, on Alpaca rejection, or on any other skip path). Leaving a stale `pending` entry locks that `(date, ticker, side)` until the date bucket rolls over the next day.

4. **Place order (if approved and not `dry_run`):**
   `mcp__alpaca__place_stock_order`
   - `symbol`, `side` (`buy`/`sell`), `qty`
   - `type`: **`limit`** (never `market`)
   - `time_in_force`: `day`
   - `limit_price`: the computed value

   Record the returned order id or the placement error.

5. **Commit or release the pending reservation:**
   - On **dry-run**: always release — the reservation is immediately unwound so dry-runs leave `state.json` looking like the run never happened. No Alpaca call is made.
     ```bash
     python3 tools/stock-trading/risk.py --release
     ```
     stdin: `{"date": "2026-04-13", "ticker": "NVDA", "side": "buy"}`
   - On **successful order placement**: commit — the pending entry is promoted to `submitted` (permanent idempotency), and `session_deployed_confirmed` advances for buys.
     ```bash
     python3 tools/stock-trading/risk.py --commit
     ```
     stdin: `{"date": "2026-04-13", "ticker": "NVDA", "side": "buy"}`
   - On **Alpaca rejection / placement error**: release — the reservation is unwound and `session_deployed` is refunded, so the session cap can be spent elsewhere.
     ```bash
     python3 tools/stock-trading/risk.py --release
     ```
     Same stdin shape. Log the placement error in the run dossier's `orders` section.

   **Never skip step 5.** If you validated a ticker, you must resolve the pending entry — either via `--commit` after a successful placement or via `--release` otherwise.

### Phase 6 — Log (subprocess, skipped if dry run)
```bash
python3 tools/stock-trading/logger.py
```
stdin: one JSON object with the full run dossier:
```json
{
  "run_id": "<ISO8601 UTC>",
  "timestamp": "<ISO8601 UTC>",
  "experiment_id": "exp-002",
  "ranking_version": "v1",
  "min_trade_override_waived": false,
  "mode": "paper" | "live",
  "dry_run": false,
  "account_snapshot": { ... },
  "market_sentiment": { "fear_greed_score": 42, "fear_greed_rating": "fear", "phase4_bias": "lean toward buys" },
  "screened": { "candidates": ["NVDA","AMD","..."], "open_positions": [], "final": ["NVDA","AMD"], "dropped_below_price_floor": [] },
  "dossiers": { "NVDA": { "rsi_14": 28.4, "ta_summary": "BUY", "stock_score": 72, "grade": "Good", "rsi_1h": 35.1, "ta_summary_1h": "BUY", "premarket_change_pct": 0.8, "volume_ratio": 1.7, "earnings_within_3d": false, "news_headlines": ["NVDA raises FY guidance", "Analyst upgrade to buy"], "bid": 424.8, "ask": 425.2, "last": 425.0 }, "MSFT": { "error": "..." } },
  "decisions": [ { "ticker", "action", "confidence", "rationale", "min_trade_override": false }, ... ],
  "orders": [ { "ticker", "status", "order_id?", "side", "qty", "limit_price", "notional" }, ... ],
  "risk_overrides": [ { "ticker", "reason" }, ... ]
}
```

### Phase 7 — Summary (print to Josue)
See Output Format below.

---

## Dry-Run Mode

If the trigger includes `--dry-run`:
- Phases 0–5 run in full, including `risk.py --validate` calls (so you see what *would* have been approved).
- **Phase 5 step 4** (the `mcp__alpaca__place_stock_order` call) is skipped.
- **Phase 5 step 5** calls `risk.py --release` instead of `--commit`, so every pending reservation written during `--validate` is immediately unwound. At the end of a dry run, `logs/state.json` looks like the run never happened — no stale pending entries, no inflated `session_deployed`. The old "`rm -f logs/state.json` after every dry run" ritual is no longer required.
- Phase 6 (logger.py) is skipped entirely — the log is NOT touched.
- Phase 7 summary shows a "DRY RUN — nothing placed" banner.

**Dry-run after hours is largely decorative.** IEX bid/ask spreads balloon to 5–10% outside RTH, which trips the 0.5% spread cap on every ticker and skips the entire watchlist. Pre-market volume and intraday fields are also thin or absent. Use after-hours dry runs to verify MCP connectivity and dossier shape — not to evaluate the reasoning layer. For real reasoning validation, dry-run between 09:30 and 16:00 ET.

---

## Hard Safety Rules (enforced by `risk.py`)
- **Max 20% of equity per position** — paper trading; tighten to 10% before going live.
- **Max 80% of cash deployed per session** — paper trading; tighten to 50% before going live.
- Minimum $50 notional per order.
- PDT block: if equity < $25,000 and `day_trade_count >= 3`, new opening trades rejected.
- **Bid/ask spread > 1.5% of midpoint → rejected** — paper trading; tighten to 0.3% before going live. 1.5% is deliberately loose so the minimum-trade rule isn't silently defeated by wide-spread tickers on the morning screen.
- Duplicate `(date, ticker, side)` → rejected (idempotent).
- Alpaca `trading_blocked` or `account_blocked` → abort entire run.

None of these are judgment calls. If `risk.py --validate` rejects an order, skip it. Never retry with tweaked parameters. The three "paper trading" values above all need to be reset before flipping `ALPACA_PAPER=false` — both in `tools/stock-trading/config.json` (where they are documented with `_paper_note` fields adjacent to each value) and in the "Promotion Checklist" below.

---

## Output Format

```
Stock Trading — [YYYY-MM-DD HH:MM] [TZ]  ·  [paper|live]  ·  run [run_id]

Account
  Equity $X.XX  ·  Cash $X.XX  ·  PDT headroom N/3
  Positions: [ticker qty @ entry (+/- pct), ...]  (or "none")
  Fear & Greed: [score]/100 ([rating])  (or "unavailable" if null)

Screened
  [N] candidates from morning screen  ·  [M] open positions  ·  [K] total after dedupe + price floor
  [comma-separated tickers that made it into Phase 2]

Decisions
  BUY   TICKER  qty @ $price limit   confidence  — rationale
  SELL  TICKER  qty @ $price limit   confidence  — rationale
  HOLD  TICKER                        confidence  — rationale
  SKIP  TICKER                                    — excluded: <reason>

Orders
  placed    TICKER  side  qty @ $price   ($notional)
  skipped   TICKER — risk.py: <reason>

Session deployed: $X.XX / $cap.XX
Log: logs/trading-log.jsonl
```

When `--dry-run`:
```
Orders (DRY RUN — nothing placed)
  would place  TICKER  side  qty @ $price
```

Omit empty sections. Keep it scannable.

---

## Promotion Checklist — Paper → Live

Do NOT flip `ALPACA_PAPER=false` in `.env` until:

- [ ] 5 consecutive clean paper runs, each reviewed the same morning
- [ ] Every `logs/trading-log.jsonl` entry from those runs read end-to-end
- [ ] No MCP errors in any of those 5 runs
- [ ] `risk.py --validate` has rejected at least one real order (proof the guardrails fire)
- [ ] Plan in place for what to do if the first live run places an order Josue disagrees with

Going live is a one-line change. Going back to paper is the same one-line change. Use it.

---

## Evaluate Mode

Evaluate Mode is how the experiment measures itself. It walks the morning routine's audit log, pulls actual fill prices and post-fill closes from Alpaca, and builds an outcomes timeline per decision that can later be aggregated into hit rates, mean returns, and confidence-label calibration curves.

**Trigger:** `run the trading skill --evaluate` (or `evaluate trades` / `run the evaluator`).

**What Evaluate Mode does NOT do:**
- Does not place orders.
- Does not call `risk.py` in any mode.
- Does not read `config.json`'s `risk` or `thresholds` or `ranking` blocks.
- Does not append to `trading-log.jsonl`.
- Does not require market hours — it only touches the Alpaca orders and bars endpoints, both of which are available around the clock.

**Outcomes state machine (per `(run_id, ticker)`):**

```
pending_fill --> filled --> t0 --> t1 --> t5 --> t20
     |
     +-> unfilled_cancelled (terminal — limit order cancelled/expired without filling)
```

- `pending_fill`: the order was placed but we have not yet confirmed a fill. Default state for any placed decision with no outcomes rows yet.
- `filled`: Alpaca confirms `filled_avg_price` and `filled_at`. Compute `fill_slippage_pct` = (`fill_price` − `limit_price`) / `limit_price` × 100.
- `t0`: close price on the fill date. Compute `return_t0_pct` = (`close_t0` − `fill_price`) / `fill_price` × 100.
- `t1/t5/t20`: close price 1/5/20 trading days after the fill date. Returns computed the same way, all vs `fill_price`.
- `unfilled_cancelled`: if Alpaca reports `status in {cancelled, expired, rejected}` without a fill, emit this terminal row. No returns are computed.

Each state transition becomes one new line in `logs/outcomes.jsonl`. The file is append-only; later states for the same `(run_id, ticker)` never overwrite earlier rows, they append alongside. The reducer (`outcomes_reducer.py`) picks the most-advanced state when building the work queue on the next run.

**Phases:**

### E0 — load work queue

```bash
python3 tools/stock-trading/outcomes_reducer.py --current-state
```

stdin: `{"log_path": "logs/trading-log.jsonl", "outcomes_path": "logs/outcomes.jsonl"}` (both defaults, can omit).

stdout: `{"decisions": [ { run_id, ticker, order_id, experiment_id, ranking_version, confidence, min_trade_override, side, qty, limit_price, notional, current_state, next_state, fill_price, filled_at }, ... ]}`

If `decisions` is empty, print `Nothing to evaluate — every placed decision is at a terminal state.` and end the run. Do not proceed to E1–E4.

### E1 — fetch order fills (parallel, one message)

For every decision with `next_state == "filled"`, fire `mcp__alpaca__get_order_by_id` in parallel. Each returns the order record with fields like `status`, `filled_avg_price`, `filled_at`, `filled_qty`.

Map each response:
- `status == "filled"` and `filled_avg_price` present → emit an outcomes row with `outcome_state: "filled"`, `fill_price: <filled_avg_price>`, `fill_qty: <filled_qty>`, `filled_at: <filled_at>`, `fill_slippage_pct: <computed>`, `prior_state: "pending_fill"`.
- `status in {canceled, expired, rejected}` → emit an outcomes row with `outcome_state: "unfilled_cancelled"`, `prior_state: "pending_fill"`. Terminal.
- `status in {new, accepted, partially_filled, pending_new, ...}` → do nothing this pass; the decision stays at `pending_fill` and will be retried on the next evaluate run.
- MCP error or missing order → do nothing, leave at `pending_fill`, note in the E4 summary.

### E2 — fetch historical bars (parallel, one message)

For every decision whose `next_state` is `t0` / `t1` / `t5` / `t20`, fire `mcp__alpaca__get_stock_bars` in parallel. Batch per ticker: one call per ticker covering the range from the fill date through `next_state_target_date + 5 calendar days` of headroom, at `timeframe: "1Day"`. The wider-than-strictly-needed window lets a single call satisfy multiple state advances if the decision has been dormant for days or weeks.

From the returned bars:
- `t0`: close of the first trading day that matches `fill_date`. Usually the same calendar day as `filled_at`.
- `t1`: close 1 trading day after `t0`. Skip weekends/holidays — the bar itself already does this; use the second bar in the returned sequence.
- `t5`: close 5 trading days after `t0`. Use the 6th bar in the sequence (index 5).
- `t20`: close 20 trading days after `t0`. Use the 21st bar (index 20).

For each state where the required bar is actually present in the response, emit one outcomes row with the appropriate fields (`close_tN`, `return_tN_pct`, `tN_date`, `outcome_state: "tN"`, `prior_state: <previous>`). If the bar isn't yet available (e.g. the 20th post-fill trading day is still in the future), skip that state — it will advance on a later evaluate run.

**Important:** Evaluate Mode can advance multiple states in one pass if enough time has elapsed. Example: a run from 40 calendar days ago that was last evaluated 30 days ago is now at `t0` in outcomes.jsonl. In one E2 call, `t1`, `t5`, and `t20` can all be computed from the same bar response. Emit all three rows in E3, one per state.

### E3 — append to outcomes.jsonl

```bash
python3 tools/stock-trading/outcomes_reducer.py --append
```

stdin: `{"lines": [ { row1 }, { row2 }, ... ]}`

The reducer appends each line with `fcntl.flock` coordination, same crash-safety story as `logger.py`.

### E4 — summary

Print a plain-text summary to Josue. Sections:

- **Work queue processed:** counts by `next_state` attempted (e.g. "pending_fill → filled: 3 advanced, 1 stuck") and by `outcome_state` after this pass.
- **Outcomes so far (all runs):**
  - Mean `fill_slippage_pct`.
  - Mean `return_t5_pct` across all decisions that have reached `t5` or later.
  - Mean `return_t5_pct` split by `min_trade_override: true` vs `false` — this is the primary experimental measurement (does the minimum-trade rule produce worse outcomes than organic picks?).
  - Mean `return_t5_pct` split by `confidence: high` / `medium` / `low`, with hit rate (% positive).
  - Count of decisions that terminated as `unfilled_cancelled`.
- **Still pending:** count of decisions still at `pending_fill` and at each intermediate state, with the earliest `run_id` in each bucket so Josue knows how old the stale-est one is.

Plain text only — no color codes, no unicode box drawing. Output should be pipe-safe so it can be redirected to a file.

---

## Files

| Path | Purpose |
|---|---|
| `tools/stock-trading/config.json` | Watchlist, strategy thresholds, `ranking` weights, `experiment_id`, `force_eod_close` toggle |
| `tools/stock-trading/risk.py` | Hard safety rules. `--snapshot` / `--validate` / `--commit` / `--release` modes. Owns `logs/state.json`. |
| `tools/stock-trading/logger.py` | Append-only JSONL writer with `fcntl.flock` crash safety |
| `tools/stock-trading/outcomes_reducer.py` | Evaluate Mode reducer — `--current-state` emits the work queue, `--append` writes new outcomes rows |
| `tools/stock-trading/run-summary.py` | Human-readable run report generator (`--since`, `--experiment` filters) |
| `tools/stock-trading/SETUP.md` | One-time MCP registration + API keys + first-run verification |
| `.claude/skills/stock-trading/SKILL.md` | This file |
| `logs/trading-log.jsonl` | Append-only run log (gitignored) |
| `logs/outcomes.jsonl` | Append-only per-decision outcomes timeline (gitignored) |
| `logs/state.json` | Per-day idempotency + session-deployed tracker (gitignored) |
| `tests/test_risk.py` | Unittest suite for `risk.py` — run via `python3 -m unittest tests.test_risk -v` |
