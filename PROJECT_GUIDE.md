# RobinhoodAgent — Project Guide & Handoff Document
**Owner:** Thomas Kramer | **Last Updated:** June 2026 | **Status:** ARCHIVAL (stale in places)

> **Operator note (2026-10):** This handoff is historical. Live setup and Stage 1
> commands live in `README.md` and `DAILY_OPERATIONS.md`. Signal math and market
> data are **not** top-level `market_data.py` / `indicators.py` — those were
> extracted to the `engine/` git submodule (`rhengine`). Stage 1 scoring is
> `signals_pipeline.py`. After clone: `git submodule update --init --recursive`.

---

## 1. What This Project Is

An autonomous trading agent that executes real-money swing trades through Thomas's Robinhood account
using the same signal logic powering his Nydex web app. No GUI. No human clicking during market hours.
Claude Code serves as the orchestration brain — it reads Python-generated signal output, connects to
the Robinhood MCP server, makes execution decisions, and logs its reasoning. Python handles all
deterministic calculation. Claude handles all judgment.

**Target metric:** 1.5 Sharpe ratio (the risk-adjusted return measure used by professional funds —
a 1.5 Sharpe means for every unit of risk taken, the strategy earns 1.5 units of return annually).
Realistic first-year target is 0.8–1.0 Sharpe while the system is being validated.

**Validation environment:** $50 live Robinhood account. This is intentional — small capital tests
the execution plumbing, signal quality, and workflow without meaningful dollar risk. Real deployment
scales once the system is validated over multiple weeks of live dry-run sessions.

---

## 2. How It Relates to Nydex

Nydex (`/Users/thomaskramer/Downloads/Claude/nydex/`) is Thomas's React/Vite web app that:
- Scans a watchlist of stocks against five technical indicator strategies
- Scores each ticker from 0–4 (one point per passing strategy leg)
- Runs backtests with 1% risk sizing and 2:1 reward-to-risk ratios
- Provides a paper trading interface via Alpaca Markets
- Displays VIX regime context and sector performance

RobinhoodAgent is the **execution layer** that automates what Nydex currently shows visually.
The signal math lives in `engine/rhengine` (AgentTradingLogic submodule) — a faithful Python
port of `indicators.js`. Same five strategies, same leg definitions, same scoring thresholds.
This app's `signals_pipeline.py` orchestrates fetches/scoring and writes `signals.json`.

**Two-system relationship:**
```
Nydex (React/Vite)          RobinhoodAgent (Python + Claude Code)
─────────────────────        ─────────────────────────────────────
Signal visualization    →    Signal calculation (signals_pipeline + rhengine)
Alpaca paper trading    →    Robinhood live trading (MCP)
Manual human execution  →    Autonomous Claude Code execution
Browser-based GUI       →    Terminal + decision log (no GUI)
```

Neither system replaces the other. Nydex remains the research and analysis layer.
RobinhoodAgent is the live execution layer.

---

## 3. Architecture Overview

### Two-Stage Pipeline (Critical)

The pipeline is split into two distinct stages. Stage 1 is pure Python — no AI, no Robinhood
connection. Stage 2 is Claude Code — judgment, MCP connection, execution.

```
STAGE 1 — Python (run_session.sh)
──────────────────────────────────
signals_pipeline.py --write-signals
    → Fetches bars via rhengine.market_data (Alpaca / Massive per config)
    → Classifies SPY regime (uptrend / downtrend / choppy) in rhengine.indicators
    → Selects strategy based on regime
    → Scores every ticker against that strategy's 4-leg system
    → Filters to qualified setups
    → Writes: signals.json

portfolio_state.py --validate   (or --write-sample for dry runs)
    → Validates state.json against required schema
    → (Live state must come from Stage 2 — see note below)

order_plan.py
    → Reads signals.json + state.json + config.json
    → Applies kill switches, cash floor, position limits
    → Calculates ATR-based position sizing
    → Writes: plan.json
(+ setup_logger.py, outcome_tracker.py, validate_plan.py — see run_session.sh)

STAGE 2 — Claude Code (manual or via launchd)
──────────────────────────────────────────────
Reads: session_prompt.md, TRADING_RULES.md, ROBINHOOD_MCP.md, plan.json
    → Calls get_accounts → gets account_number (never hardcoded)
    → Calls get_portfolio + get_equity_positions → gets LIVE state from Robinhood
    → Updates state.json with real MCP data
    → If LIVE_TRADING_ENABLED=true:
        · Processes exits first (review → place)
        · Processes entries (tradability → review → place)
    → Appends session summary to decision_log.md
```

**Why two stages:** Python is deterministic, fast, and auditable. Claude Code handles MCP
connections and contextual judgment. You don't use an LLM to do arithmetic, and you can't
use a Python script to authenticate with a live broker API.

**Critical:** `portfolio_state.py` does NOT connect to Robinhood. It validates and generates
schema-compliant state.json files for dry-run testing. The live account snapshot always comes
from Claude Code calling the Robinhood MCP tools in Stage 2.

---

## 4. The Five Nydex Strategies (Signal Engine)

All strategies score 0–4. Setups scoring **3 or 4** qualify, then the meta-model
filters them by P(win) ≥ `meta_filter_threshold` (see §6). Each leg is boolean.
Entry, stop, and target are ATR-based (Average True Range = average daily price movement over 14 days).

### Exit Parameters (per-strategy — set in each `thresholds` block)
| Parameter | Value | Reasoning |
|---|---|---|
| Stop loss | 1.5 × ATR below entry (`stopAtrMult`) | 1× sat inside daily noise and got clipped; 1.5× is the floor of the researched 1.5–2.5× range |
| Take profit (trend) | 2.5 × ATR above entry (`targetAtrMult`) | vwap / macd / consolidation / residual |
| Take profit (Bollinger) | mid-band (20-SMA) | Mean reversion targets the mean — hit far more often than the opposite band, higher win rate. Falls back to `targetAtrMult` (1.5×) if the band is on the wrong side of entry |
| Breakeven trail trigger | 1.5 × ATR profit (`trail_trigger_mult`) | Move stop to entry once the position is working |
| Chandelier trail | price − 3.0 × ATR (`chandelier_atr_mult`), ratchet-up only | Once deep in profit, locks gains and lets trend winners run; never lowers the stop |
| Max hold (equities) | 10 sessions | Time stop — prevents dead money in a loser |
| Max hold (leveraged ETFs) | 3 sessions | Daily rebalancing decay destroys value beyond this |

Sizing is stop-based (`dollar_risk / stop_pct`), so widening the stop automatically
shrinks position notional — risk-neutral, just fewer noise stop-outs.

---

### Strategy 1: VWAP Confluence (`"vwap"`)
**Regime:** Uptrend, VIX < 20.

VWAP (Volume Weighted Average Price) is the institutional benchmark price for the day. A stock
reclaiming VWAP with a volume surge signals institutions stepping back in.

| Leg | Bull | Bear |
|---|---|---|
| 1 | Price crossed above VWAP | Price crossed below VWAP |
| 2 | EMA9 > EMA21 > EMA50, price > EMA9 | EMA9 < EMA21 < EMA50, price < EMA9 |
| 3 | Volume > 1.15× 20-bar average | Same |
| 4 | RSI dipped below 50 then crossed back above | RSI crossed below 50 from above |

**Thresholds:** `volMult: 1.15, rsiLow: 50, rsiHigh: 70`

---

### Strategy 2: Bollinger Band Mean Reversion (`"bollinger"`)
**Regime:** Choppy/ranging.

Bollinger Bands are a 20-bar moving average ± 2 standard deviations. When price closes outside
and re-enters, the move is exhausted and likely to snap back toward the middle.

| Leg | Bull | Bear |
|---|---|---|
| 1 | Prior bar closed below lower BB | Prior bar closed above upper BB |
| 2 | Current bar closes back inside BB | Current bar closes back inside BB |
| 3 | RSI < 35 on prior bar (oversold) | RSI > 65 on prior bar (overbought) |
| 4 | Volume > 1.10× 20-bar average | Same |

**Thresholds:** `volMult: 1.10, rsiLow: 35, rsiHigh: 65, adxMax: 35`
(`adxMax` is the hard trend gate — skip mean reversion when ADX exceeds it.
Backtest-tuned from 30 → 35: OOS avg R +0.22 → +0.28, PF 1.66 → 1.99.)

---

### Strategy 3: MACD Trend Continuation (`"macd"`)
**Regime:** (deprecated from auto-routing — see §7.) Backtesting showed no
out-of-sample edge in uptrends; kept available and parameterized (`macdFast`,
`macdSlow`, `macdSignal`, `crossLookback`, `secondLeg`, `emaFilter`) for manual
use and further research, but the uptrend slot now routes to Residual Momentum.

MACD measures momentum by comparing a fast EMA (12-bar) to a slow EMA (26-bar). Expanding
histogram means momentum is strengthening.

| Leg | Bull | Bear |
|---|---|---|
| 1 | MACD crossed above signal line within last 3 bars | MACD crossed below signal line |
| 2 | Histogram expanding in trade direction | Histogram expanding negative |
| 3 | Price above EMA50 | Price below EMA50 |
| 4 | RSI 45–75 | RSI 25–55 |

**Thresholds:** `volMult: 1.10, rsiLow: 45, rsiHigh: 75`

---

### Strategy 4: Consolidation Breakout (`"consolidation"`)
**Regime:** Low-volatility squeeze.

BB width measures price compression. ADX confirms ranging. Volume on the breakout bar
confirms the move is real, not a fakeout.

| Leg | Bull | Bear |
|---|---|---|
| 1 | BB width (upper−lower / mid) < 6% | Same |
| 2 | ADX < 25 | Same |
| 3 | 20-bar price range < 8% of price | Same |
| 4 | Closes above range high, volume > 1.30× average | Closes below range low |

**Thresholds:** `bbWidth: 0.06, adxMax: 25, rangeWidth: 0.08, volMult: 1.30`

---

### Strategy 6: Post-Earnings-Announcement Drift (`"pead"`)
**Always-on event sleeve (not regime-routed).** Bernard & Thomas (1989): stocks
drift in the direction of an earnings surprise for weeks. Price-based
implementation — detects the earnings event as the most recent volume-confirmed
large move (`detect_earnings_event`) and enters the drift. Runs alongside the
regime strategy; the two pools are merged/deduped in `build_signals_payload`.
Legs: Earnings Surprise (volume-confirmed gap) · Drift Intact · Above EMA20 · RSI.
Strength = surprise size in ATR. PEAD *features* (`pead_days_since`, `pead_ret_atr`)
are in the meta-model (`features.INCLUDE_PEAD=True`), so PEAD is meta-gated on the
same footing as every sleeve. **Honest finding:** on the only contiguous real-volume
window available (~94 days / ~30 signal days), the price-proxy PEAD backtests
NEGATIVE (avg R ≈ −0.24, PF 0.53) — the proxy fires on any big volume move, not
just earnings, and a short single-regime window doesn't capture multi-week drift.
The meta-model correctly filters most PEAD, keeping only high-conviction setups.
**Upgrade path (what would make it real):** a true earnings calendar + SUE (actual
vs consensus EPS) from a fundamentals feed, and a longer history to validate on.

### Strategy 5: Residual Momentum (`"residualmomentum"`)
**Regime:** All, especially uptrend. Requires SPY bars.

Based on Kakushadze §3.7. OLS regression of stock returns against SPY extracts beta. The
residual — what the stock earned beyond what beta predicts — is risk-adjusted by its own
standard deviation. A consistently positive risk-adjusted residual = genuine alpha.

| Leg | Bull | Bear |
|---|---|---|
| 1 | Risk-adjusted residual > +0.15 vs SPY | Residual < −0.15 |
| 2 | Price above EMA21 | Price below EMA21 |
| 3 | EMA21 above EMA50 | EMA21 below EMA50 |
| 4 | RSI 50–75 | RSI 25–50 |

**Thresholds:** `minResidualRA: 0.15, volMult: 1.10`

---

## 5. Config Thresholds Schema

Thresholds are stored per-strategy in `config.json`. Each strategy has its own block with
only the keys that strategy uses. The `resolve_thresholds(cfg, strategy)` function in
`rhengine.indicators` reads the correct block and falls back to a flat format for compatibility.

```json
"thresholds": {
  "vwap":             { "volMult": 1.15, "rsiLow": 50, "rsiHigh": 70, "vwapAnchorLookback": 30, "stopAtrMult": 1.5, "targetAtrMult": 2.5 },
  "bollinger":        { "volMult": 1.10, "rsiLow": 35, "rsiHigh": 65, "adxMax": 35, "stopAtrMult": 1.5, "targetAtrMult": 1.5 },
  "macd":             { "volMult": 1.10, "rsiLow": 45, "rsiHigh": 75, "stopAtrMult": 1.5, "targetAtrMult": 2.5 },
  "consolidation":    { "bbWidth": 0.06, "adxMax": 25, "rangeWidth": 0.08, "volMult": 1.30, "stopAtrMult": 1.5, "targetAtrMult": 2.5 },
  "residualmomentum": { "minResidualRA": 0.15, "volMult": 1.10, "stopAtrMult": 1.5, "targetAtrMult": 2.5 }
}
```

Signal keys drive leg pass/fail; `stopAtrMult` / `targetAtrMult` drive exits;
`vwapAnchorLookback` is the swing-pivot window for anchored VWAP. Tune these with
`backtest.py` before deploying — never on the live account blind.

---

## 6. Risk Management Parameters

| Parameter | Value | Reasoning |
|---|---|---|
| Risk per trade | 2.0% ($1.00) | 1% gives $0.50 — too small to validate at $50 |
| Max position notional | $10 (10%) | Prevents over-concentration |
| Cash floor | $10 (20%) | Always liquid; deployable cash = cash_available − $10 |
| Max open positions | 5 | Edge declines beyond 5 positions |
| Daily loss limit | 3% ($1.50) | Hit → no new entries today |
| Weekly loss limit | 5% ($2.50) | Hit → no new entries until Monday |
| Min qualifying score | 3/4 + meta P(win) ≥ 0.53 | Gate lowered from 4/4: purged-OOS backtest showed the 3/4+4/4 pool filtered by the meta-model beats 4/4-only (avg R +0.03 → +0.13). The meta filter (`meta_filter_threshold` in config) is the real quality floor now — a 4/4 with low P(win) is skipped, a strong 3/4 is taken |
| Order type | market + dollar_amount | Fractional shares require market orders on Robinhood |
| Minimum order notional | $1.00 | Hardcoded in order_plan.py — no sub-dollar orders |

### Position Sizing Math
```python
dollar_risk    = portfolio_value × risk_pct           # $50 × 0.02 = $1.00
stop_pct       = atr / entry_price                    # e.g. $8.20 / $421 = 1.95%
raw_notional   = dollar_risk / stop_pct               # $1.00 / 0.0195 = $51.28
dollar_amount  = min(raw_notional,                    # capped at $10 at this account size
                     max_position_notional,
                     deployable_cash)
actual_risk    = dollar_amount × stop_pct             # $10 × 0.0195 = $0.195
```
At $50 account size, ATR-based sizing almost always exceeds the $10 cap. `dollar_amount`
will equal $10 on nearly every trade. Actual dollar risk will be well below $1. This is
expected — the system is being validated, not optimized for dollar returns.

---

## 7. Market Regime System

Every session begins with SPY regime classification before any signals are evaluated.

### SPY Trend Regime (EMA stack on daily bars)
| Regime | Condition | Strategy | Inverse ETFs |
|---|---|---|---|
| Uptrend | Price > EMA21 > EMA50 | Residual Momentum | ❌ Blocked |
| Downtrend | Price < EMA21 < EMA50 | Residual Momentum | ✅ Allowed |
| Choppy | Mixed / crossing | Bollinger | ❌ Blocked |

**Uptrend was re-routed from MACD to Residual Momentum (2026-07-02)** after a
walk-forward backtest (`optimize.py`) found MACD has no out-of-sample edge in
uptrends under any parameterization (OOS avg R ≈ −0.14 to −0.22, PF < 1), whereas
Residual Momentum is consistently positive IS and OOS (OOS avg R ≈ +0.10, PF ≈ 1.2)
on a large sample. MACD is retained and fully parameterized for manual use.

### VIX Volatility Regime
| VIX Level | Label | Action |
|---|---|---|
| < 15 | Calm | Full size |
| 15–20 | Normal | Full size |
| 20–25 | Fear Elevated | 0.5× size |
| 25–30 | Fear Spiking | No new entries |
| > 30 | Panic | No new entries; review open longs |

**Sharpe mechanism:** VIX gating is one lever. Two more were added (both in
`build_signals_payload` → consumed by `order_plan`):
- **200-day trend filter** (`market_trend_filter`): no new entries when SPY closes
  below its 200-day SMA (`market_risk_off`). Crash-avoidance — drawdown clusters
  are what wreck Sharpe. Exits are never blocked.
- **Volatility targeting** (`account.target_annual_vol`, default 0.15): a continuous
  size multiplier `vol_target_mult = clip(target / realized_vol, [0.5,1.5])` applied
  alongside the VIX multiplier. The most reliable Sharpe lever — lean into calm,
  cut exposure when realized vol spikes.

**Reality check:** run `validation.py` — net of costs and multiple-testing the
edge's Deflated Sharpe is marginal. Forward performance will trail the backtest;
live closed trades in `setup_log.csv` are the real scorecard.

---

## 8. Inverse / Leveraged ETF Rules

Robinhood agentic MCP does not support short selling. Inverse ETFs provide bear-side exposure.

**Inverse ETFs in watchlist:** `VXX, SPXS, SQQQ, TZA, FAZ, LABD, SRTY, SDOW, YANG`

| Rule | Detail |
|---|---|
| Entry | Only in SPY Downtrend regime — blocked in Uptrend and Choppy |
| Max hold | 3 sessions (not 10) — daily rebalancing decay is destructive longer |
| VXX | Tracks VIX futures, not equities. Only in Fear Elevated+ regimes |
| 3× products | ATR is 3× underlying. Small notionals from sizing math are correct |
| Bear setups on equities | Skipped with reason: "no short selling via Robinhood MCP" |

---

## 9. LIVE_TRADING_ENABLED — Now On (Autonomous)

Live order placement is controlled by a flag in `session_prompt.md`:

```
LIVE_TRADING_ENABLED=true    ← current: places real orders autonomously
LIVE_TRADING_ENABLED=false   ← dry-run only, review_equity_order without place
```

The system is now in autonomous live mode. Both the manual chat session
(`session_prompt.md`) and the scheduled Cowork task (`AGENT_TASK_PROMPT.md`)
call `review_equity_order` → `place_equity_order` with no human confirmation
step. There is no queue file and no "execute queue" reply.

**The risk gates that keep autonomy survivable are NOT the trading gate and
remain fully in force** — do not remove them:
- `kill_switch_triggered` (daily −3% / weekly −5% realized loss) → blocks new entries
- `vix_blocked` (Fear Spiking / Panic regime) → blocks new entries
- Stop-loss / take-profit / time-stop exits always execute
- Cash floor, max positions, sector caps, 3/4 min score + meta P(win) gate (enforced in Stage 1)

To return to dry-run, set `LIVE_TRADING_ENABLED=false` in `session_prompt.md`.

---

## 10. Robinhood MCP — Tool Reference

Server: `https://agent.robinhood.com/mcp/trading`
Auth: OAuth (Max OAuth, token expires every 7 days — needs refresh logic in Phase 4)

### Account / Portfolio
| Tool | Notes |
|---|---|
| `get_accounts` | **Call first every session.** Read `account_number` from response — never hardcode |
| `get_portfolio` | Portfolio value, buying power, asset-class breakdown |
| `get_equity_positions` | Open positions with avg cost and quantity |
| `get_equity_orders` | Order history. Filter `placed_agent: "agentic"` for Claude-only orders |
| `get_equity_quotes` | Real-time quotes. Max 20 symbols — data degrades above 20 |

### Order Execution
| Tool | Notes |
|---|---|
| `get_equity_tradability` | Check tradeability + fractional eligibility. Max 10 symbols/call |
| `review_equity_order` | **Mandatory pre-step.** Simulates order, returns blocking alerts |
| `place_equity_order` | Real money. Only call after non-blocking review output |
| `cancel_equity_order` | Cancels open order by UUID |

### Supported Order Types
`market`, `limit`, `stop_market`, `stop_limit`
Fractional shares: `type=market` + `dollar_amount` only. Limit orders = whole shares.

### Watchlist
`get_watchlists`, `get_watchlist_items`, `add_to_watchlist`, `update_watchlist`,
`follow_list`, `unfollow_list`, `get_popular_lists`

### Options (Rolling Out — Ignore Until Phase 5+)
`get_option_chains`, `get_option_instruments`, `get_option_quotes`,
`get_option_positions`, `get_option_orders`, `review_option_order`,
`place_option_order`, `cancel_option_order`
These tools are live on the MCP server but no options strategy exists yet.
Do not call them until a dedicated options sleeve is built.

---

## 11. Critical Production Gotchas

### 1. Fractional Order Rate Limit
Separate from the general API rate limit. After ~10–15 fractional orders, Robinhood returns
HTTP 429 and blocks the bucket. Mitigation: delay between orders, spread rebalances across
days, or use whole-share limit orders where position size allows.

### 3. No Short Selling
`side: "sell"` closes an existing long only — cannot open a short. Inverse ETFs are the
bear-side solution. Bear setups on equities are skipped in `order_plan.py` with a logged reason.

### 4. OAuth Token Expiry
7-day lifetime. Phase 4 launchd scheduling must include a token refresh check. Pattern:
check expiry at session start, refresh inline if expiring within 24 hours.

### 5. account_number Must Be Fetched
Call `get_accounts` every session. Never hardcode. A hardcoded or wrong account number will
silently route to the wrong account or fail.

### 6. Trail Rules Are Log-Only (Currently)
`positions_to_trail` in `plan.json` is a local stop-management instruction. Persistent stop
orders via MCP have not been tested yet. Until they are: log trail updates in `decision_log.md`
and sync state manually on the next portfolio snapshot.

---

## 12. Implementation Phases

### Phase 1 — Signal Engine ✅ COMPLETE
`engine/` (rhengine submodule), `signals_pipeline.py`, `config.json`, `secrets.json`, `requirements.txt`
- Market data + scorers live in `engine/rhengine` (AgentTradingLogic); app orchestrates via `signals_pipeline.py`
- Alpaca IEX / Massive bar fetch for any ticker list
- All 5 Nydex strategies ported to Python (faithful to `indicators.js`)
- SPY regime classifier and strategy selector
- Per-strategy threshold resolution via `resolve_thresholds()`
- `build_signals_payload()` and `write_signals()` for pipeline handoff
- `score_all_tickers()` batch entry point with regime gating

**Test:** `git submodule update --init` → `python3 signals_pipeline.py --write-signals`

---

### Phase 2 — Portfolio State + Order Planning ✅ COMPLETE
`portfolio_state.py`, `order_plan.py`, `test_order_plan.py`

**`portfolio_state.py`** — Schema validator and sample generator. Does not connect to
Robinhood. Live state comes from Claude Code in Stage 2.
- `--write-sample` → generates state.json from config.json values for dry-run testing
- `--validate` → validates state.json field types and consistency

**`order_plan.py`** — Full order planning pipeline. No Robinhood connection.
- Reads `signals.json` + `state.json` + `config.json` → writes `plan.json`
- ATR-based sizing with triple-cap (raw, max_position_notional, deployable_cash)
- Exit detection: stop hit, target hit, max hold sessions
- Trail detection: breakeven trigger at 1.5× ATR profit
- Kill switch, cash floor, max positions, bear-setup rejection all enforced
- `mode: "dry_run"` baked into output — no live flag in Python

**`test_order_plan.py`** — 6 unit tests covering all core business rules.

**Test:** `python3 -m unittest test_order_plan.py -v`

---

### Phase 3 — Claude Code Integration ✅ COMPLETE
`.claude/settings.json`, `session_prompt.md`, `TRADING_RULES.md`, `ROBINHOOD_MCP.md`

**`.claude/settings.json`** — Robinhood MCP registered for Claude Code:
```json
{ "mcpServers": { "robinhood": { "url": "https://agent.robinhood.com/mcp/trading" } } }
```

**`session_prompt.md`** — Runtime instruction for Claude Code. Key elements:
- `LIVE_TRADING_ENABLED=true` at the top — autonomous live execution (set to `false` for dry-run)
- 9-step workflow: read plan → get accounts → verify positions → exits → entries → log
- Dry-run restrictions explicit: no `place_equity_order` until flag is true

**`TRADING_RULES.md`** — Hard stop rules, entry rules, exit rules, trail rules, logging format.

**`ROBINHOOD_MCP.md`** — Full tool reference for Claude Code sessions.

---

### Phase 4 — Scheduling ⚠️ IN PROGRESS
`run_session.sh` ✅ | `robinhoodagent.plist` ⬜

**`run_session.sh`** — Full Stage 1 pipeline:
```bash
python3 signals_pipeline.py --write-signals
# validates or generates state.json
python3 portfolio_state.py --validate (or --write-sample)
python3 order_plan.py
python3 setup_logger.py
python3 outcome_tracker.py   # non-fatal
python3 validate_plan.py
# prints: Stage 1 complete; plan.json ready for Stage 2
```
Ends with a print, not a Claude Code invocation — launchd plist schedules Stage 2.

**`robinhoodagent.plist`** — Still to build. macOS launchd config that fires
`run_session.sh` every weekday at 9:50 AM ET (20 min after open).
Load with: `launchctl load ~/Library/LaunchAgents/robinhoodagent.plist`

---

### Phase 5 — Sleeve Registry + Options
`sleeves.json`, `decision_log.md` (auto-created by Claude Code on first session)

Sleeve registry enables multiple parallel strategies with target weight allocation.
Weights auto-renormalize when fewer than all sleeves are active.

```json
{
  "sleeves": [
    {"name": "nydex_technical", "weight": 0.60, "status": "live",    "strategy": "auto"},
    {"name": "momentum_quant",  "weight": 0.40, "status": "planned", "strategy": "momentum_12_1"}
  ]
}
```

Options sleeve possible once options tools are tested and an options strategy is designed.

---

## 13. First Live Claude Code Session — Exact Workflow

When running Claude Code manually for the first time (before launchd is set up):

```bash
# Step 1 — Run Stage 1 pipeline
cd ~/Downloads/Claude/RobinhoodAgent
bash run_session.sh

# Step 2 — Review the plan
cat plan.json

# Step 3 — Open Claude Code in the project directory
claude
```

Claude Code will read `session_prompt.md` (which references `plan.json`), connect to
Robinhood MCP, call `get_accounts`, verify positions, and — because
`LIVE_TRADING_ENABLED=true` — place real orders after each `review_equity_order`
passes. No confirmation prompt.

**To pause live trading:** Set `LIVE_TRADING_ENABLED=false` in `session_prompt.md`.
The session then runs review-only (simulation) without placing orders. The risk
gates (kill switch, VIX block, loss limits) apply in either mode and are separate
from this flag — leave them in place.

---

## 14. File Reference — Current State

```
RobinhoodAgent/
├── config.json             ← Watchlist, risk params, per-strategy thresholds, ETF rules
├── secrets.json            ← Alpaca API keys (DO NOT SHARE)
├── requirements.txt        ← requests, numpy, scikit-learn, joblib, scipy
├── engine/                 ← git submodule → Takramer/AgentTradingLogic (rhengine)
│   └── rhengine/           ← market_data, indicators, backtest (shared source of truth)
│
├── signals_pipeline.py     ← Stage 1 orchestration: fetch + score → signals.json
├── portfolio_state.py      ← state.json schema validator and sample generator
│                              (does NOT connect to Robinhood)
├── order_plan.py           ← signals + state → plan.json (no Robinhood connection)
├── setup_logger.py         ← appends setups to setup_log.csv
├── outcome_tracker.py      ← scores past setups (offline)
├── validate_plan.py        ← Stage 1 gate before Stage 2
├── test_order_plan.py      ← unit tests for order_plan.py business logic
├── backtest.py             ← shim → rhengine.backtest
├── optimize.py             ← local OOS parameter search (prefer rhengine.optimize)
├── validation.py           ← Probabilistic + Deflated Sharpe (Bailey/LdP)
│
│   Meta-labeling layer (López de Prado) — secondary ML model that predicts
│   P(win) per signal, to filter/rank the TA engine's proposals:
├── features.py             ← lookahead-free signal-time features
├── dataset.py              ← walk history → training_data.csv (features + triple-barrier label)
├── metalabel.py            ← purged walk-forward train + eval → metamodel.joblib
├── metascore.py            ← live P(win) scoring; attaches meta_prob to setups (graceful no-op if model absent)
├── news_data.py            ← Alpaca news + lexicon sentiment/flow features (as-of, no lookahead).
│                              TESTED, did NOT improve OOS AUC — OFF by default
│                              (features.INCLUDE_NEWS=False); kept for FinBERT follow-up
│
├── .claude/
│   └── settings.json       ← Robinhood MCP config for Claude Code
├── session_prompt.md       ← Claude Code runtime instruction (LIVE_TRADING_ENABLED=true)
├── TRADING_RULES.md        ← Hard stops, entry/exit rules, logging format
├── ROBINHOOD_MCP.md        ← Full Robinhood MCP tool reference
├── DAILY_OPERATIONS.md     ← Current operator timeline (prefer this over this guide)
│
├── run_session.sh          ← Stage 1 pipeline script (Python only)
│
├── signals.json            ← Output of signals_pipeline.py (overwritten each session)
├── state.json              ← Portfolio state (sample or MCP-sourced)
├── plan.json               ← Output of order_plan.py (overwritten each session)
├── state.sample.json       ← Reference sample state
│
├── README.md               ← Quick setup reference (canonical)
└── PROJECT_GUIDE.md        ← This file (archival handoff)

Ops / scheduling (see DAILY_OPERATIONS.md):
├── robinhoodagent.plist
└── robinhoodagent.stage2.plist

Auto-created by Claude Code on first session:
└── decision_log.md         ← Append-only session audit trail
```

---

## 15. Live Run Results (June 8, 2026)

The pipeline ran end-to-end on real data for the first time on June 8, 2026 at 21:13 UTC.

```
SPY regime:      choppy
Strategy used:   bollinger
Tickers fetched: 61 (60 watchlist + SPY)
Data errors:     0
Qualified setups (4/4): 0
```

Alpaca connection confirmed working. Regime classification working. 4/4 Bollinger filter
found no setups — correct behavior on a choppy market day. A less selective system would
have forced trades here and given up edge. No setups = no trades = no unnecessary losses.

---

## 16. Nydex Source Reference

| Python / package | Ported from / role |
|---|---|
| `engine/rhengine/market_data.py` | Nydex `src/api.js` (shared AgentTradingLogic package) |
| `engine/rhengine/indicators.py` | Nydex `src/indicators.js` (shared package) |
| `signals_pipeline.py` | App-side Stage 1 orchestration (was top-level indicators writer) |
| `config.json` (thresholds) | Nydex `src/strategies.js` (+ engine `strategies.json`) |

If Nydex / rhengine signal logic is updated, keep the submodule pin and app orchestration in sync.
Strategy keys used in both codebases: `vwap`, `bollinger`, `macd`, `consolidation`, `residualmomentum`

---

## 17. Pre-Live-Trade Checklist

- [x] Engine submodule: `git submodule update --init` → `engine/rhengine` importable
- [x] Signal scoring: `python3 signals_pipeline.py --write-signals` writes signals.json
- [x] Order planning: `python3 order_plan.py` writes valid plan.json
- [x] Unit tests: `python3 -m unittest test_order_plan.py -v` all pass
- [x] Pipeline script: `bash run_session.sh` runs without errors
- [ ] First Claude Code session: dry-run with Robinhood MCP (read-only tools only)
- [ ] Second+ Claude Code sessions: confirm state.json reflects real MCP data
- [ ] `LIVE_TRADING_ENABLED=true`: only after dry-run sessions validate cleanly
- [ ] Fractional rate limit: do not exceed ~10 fractional orders per session
- [ ] OAuth token refresh: implement before launchd scheduling (7-day expiry)
- [ ] `robinhoodagent.plist`: build and load for fully autonomous scheduling

---

*This document reflects the state of the project as of June 2026.
It is the authoritative handoff reference for any new Claude or Claude Code session
continuing this build. Read this file before reading any other file in the project.*
