# RobinhoodAgent — Signal Engine + Dry-Run Order Planner

Python port of the Nydex signal scoring system, wired to Alpaca for market data.
The repo now produces a dry-run `plan.json` that Claude/Codex can later execute through the
Robinhood MCP server after live trading is explicitly enabled.

---

## Setup

```bash
cd /Users/thomaskramer/Downloads/Claude/RobinhoodAgent

# Install dependencies (one time)
pip3 install -r requirements.txt

# Set Alpaca credentials (add these to ~/.zshrc to make permanent)
export ALPACA_API_KEY="your_key_here"
export ALPACA_API_SECRET="your_secret_here"
```

---

## Files

| File | What it does |
|---|---|
| `config.json` | Watchlist, risk params, strategy settings, sleeve rules |
| `market_data.py` | Fetches OHLCV bars. Two sources via `data_source` in config: `massive` (Polygon/Massive REST grouped-daily, real consolidated SIP volume, disk-cached in `massive_cache/`) or `alpaca` (IEX, ~2-3% of volume). Massive is current. |
| `indicators.py` | Scores tickers against all 5 Nydex strategies, classifies SPY regime |
| `portfolio_state.py` | Creates/validates `state.json` for dry-run planning |
| `order_plan.py` | Converts `signals.json` + `state.json` into dry-run `plan.json` |
| `backtest.py` | Walk-forward historical backtest — validate tuning out-of-sample before live |
| `optimize.py` | Parameter search ranked by out-of-sample expectancy (IS/OOS split + guardrails) |
| `features.py` / `dataset.py` / `metalabel.py` / `metascore.py` | Meta-labeling ML layer: P(win) model that filters/ranks TA signals |
| `TRADING_RULES.md` | Execution rules for future Claude/Codex Robinhood sessions |
| `ROBINHOOD_MCP.md` | Robinhood MCP tool contract and safety notes |
| `session_prompt.md` | Autonomous live execution prompt (`LIVE_TRADING_ENABLED=true`) |
| `run_session.sh` | Runs the current dry-run pipeline |
| `requirements.txt` | Python dependencies (just `requests`) |

---

## Smoke Tests

```bash
# Test Alpaca connection
python3 market_data.py

# Test signal scoring (uses first 5 watchlist tickers + SPY)
python3 indicators.py

# Write production signals for Phase 2 order planning
python3 indicators.py --write-signals

# Write sample portfolio state for local dry-run testing
python3 portfolio_state.py --write-sample --state state.json

# Build dry-run order plan
python3 order_plan.py

# Run full dry-run pipeline
./run_session.sh

# Run unit tests
python3 -m unittest test_order_plan.py

# Backtest the strategies out-of-sample (signal-edge stats by score/strategy)
python3 backtest.py --lookback-days 400 --min-score 3

# Retrain the meta-labeling model (P(win) filter). Needs numpy + scikit-learn.
python3 dataset.py --min-score 3 --lookback-days 500   # -> training_data.csv
python3 metalabel.py                                    # -> metamodel.joblib + report

# Honesty check: is the edge real or overfit? (Probabilistic + Deflated Sharpe)
python3 backtest.py --lookback-days 500 --min-score 3 --csv backtest_trades.csv
python3 validation.py --trials 25 --cost-bps 5
```

Once `metamodel.joblib` exists, `indicators.py` attaches a `meta_prob` (P(win)) to
each setup and ranks by it. It's advisory by default; set `"meta_filter_threshold"`
in config.json to also skip entries below a probability.

## Data source (real volume)

`config.json` `"data_source": "massive"` uses Polygon/Massive REST grouped-daily —
real consolidated (SIP) volume, one call per trading day for the whole market,
cached in `massive_cache/`. Free tier is 5 calls/min, so the first backfill of a
window is slow but resumable (re-run any fetch; it skips cached days and grabs
recent-first). SPY's 200-day trend context still comes from Alpaca (prices only).
Set `"data_source": "alpaca"` to revert. Watchlist is the full S&P 500 + inverse ETFs.

NOTE: the meta-model was trained on Alpaca data; retrain (`dataset.py` → `metalabel.py`)
on Massive once the cache is deep enough, or its `vol_ratio` feature is mis-scaled.

The first two commands print results and exit. `--write-signals` creates `signals.json`.
`order_plan.py` creates `plan.json`. No command above places trades.

---

## How it fits the full architecture

```
Phase 1            → market_data.py + indicators.py = signal calculation
Phase 2 (current)  → portfolio_state.py + order_plan.py = dry-run plan JSON
Phase 3 (guarded)  → Claude/Codex reads plan.json, connects to Robinhood MCP, reviews orders
Phase 4            → launchd schedules Phase 3 every weekday at 9:50 AM ET
```

Live trading is enabled. `session_prompt.md` contains `LIVE_TRADING_ENABLED=true`, and both the
manual session and the scheduled Cowork task place real orders autonomously (no confirmation step).
Set the flag to `false` to return to review-only dry-run. Risk gates (kill switch, VIX block,
daily/weekly loss limits, stop/target/time exits) remain in force in either mode.

---

## Strategy Keys (match Nydex exactly)

| Key | Strategy |
|---|---|
| `vwap` | VWAP Confluence |
| `bollinger` | Bollinger Band Mean Reversion |
| `macd` | MACD Trend Continuation |
| `consolidation` | Consolidation Breakout |
| `residualmomentum` | Residual Momentum (requires SPY bars) |

`"mode": "auto"` in config.json lets SPY regime pick the strategy automatically.

---

## Notes

- `BRK-B` removed from watchlist — Alpaca IEX uses `BRK.B`, add it back manually if needed
- Inverse ETFs (`SPXS`, `SQQQ`, etc.) only score when SPY regime is `"downtrend"`
- Leveraged ETFs cap at 3-session hold (configured in `exits.leveraged_max_hold_sessions`)
- No third-party math libraries — stdlib only except `requests`
