# RobinhoodAgent — Signal Engine + Dry-Run Order Planner

Python orchestration shell around the shared `rhengine` package (git submodule
`engine/` → [Takramer/AgentTradingLogic](https://github.com/Takramer/AgentTradingLogic)).
The repo produces a dry-run `plan.json` that Claude/Codex can later execute through the
Robinhood MCP server after live trading is explicitly enabled.

Market data and indicator scoring live in the submodule (`rhengine.market_data`,
`rhengine.indicators`). This app owns Stage 1 orchestration (`signals_pipeline.py`,
order planning, validation) and Stage 2 execution prompts.

---

## Setup

```bash
cd /path/to/RobinhoodAgent

# Required: pull the shared engine at the commit this repo pins
git submodule update --init --recursive

# Install dependencies (one time)
pip3 install -r requirements.txt

# Set Alpaca credentials (add these to ~/.zshrc to make permanent)
export ALPACA_API_KEY="your_key_here"
export ALPACA_API_SECRET="your_secret_here"
```

`run_session.sh` and `run_scheduled.sh` put `./engine` on `PYTHONPATH` so imports
resolve to the vendored submodule, not a stray editable install elsewhere.

Without `git submodule update --init`, `engine/` is empty and Stage 1 cannot import
`rhengine` — that is not optional.

---

## Files

| File | What it does |
|---|---|
| `config.json` | Watchlist, risk params, strategy settings, sleeve rules |
| `engine/` | Git submodule → AgentTradingLogic (`rhengine`: market data, indicators, backtest) |
| `signals_pipeline.py` | Stage 1 signal generation: fetch bars, score watchlist, write `signals.json` |
| `portfolio_state.py` | Creates/validates `state.json` for dry-run planning |
| `order_plan.py` | Converts `signals.json` + `state.json` into dry-run `plan.json` |
| `setup_logger.py` / `outcome_tracker.py` / `validate_plan.py` | Setup log, outcome scoring, plan gate |
| `backtest.py` | Thin shim → `rhengine.backtest` (walk-forward OOS validation) |
| `optimize.py` | Local parameter search (IS/OOS); prefer `python -m rhengine.optimize` for the shared optimizer |
| `features.py` / `dataset.py` / `metalabel.py` / `metascore.py` | Meta-labeling ML layer: P(win) model that filters/ranks TA signals |
| `TRADING_RULES.md` | Execution rules for future Claude/Codex Robinhood sessions |
| `ROBINHOOD_MCP.md` | Robinhood MCP tool contract and safety notes |
| `session_prompt.md` | Autonomous live execution prompt (`LIVE_TRADING_ENABLED=true`) |
| `DAILY_OPERATIONS.md` | Current operator timeline (launchd + Stage 2) |
| `run_session.sh` | Runs the current Stage 1 dry-run pipeline |
| `requirements.txt` | `requests`, `numpy`, `scikit-learn`, `joblib`, `scipy` |

---

## Smoke Tests

```bash
# Confirm the engine submodule is present
python3 -c "import rhengine; print(rhengine.__file__)"

# Write production signals for order planning
python3 signals_pipeline.py --write-signals

# Write sample portfolio state for local dry-run testing
python3 portfolio_state.py --write-sample --state state.json

# Build dry-run order plan
python3 order_plan.py

# Run full Stage 1 dry-run pipeline
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

Once `metamodel.joblib` exists, `signals_pipeline.py` attaches a `meta_prob` (P(win)) to
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

`signals_pipeline.py --write-signals` creates `signals.json`.
`order_plan.py` creates `plan.json`. No command above places trades.

---

## How it fits the full architecture

```
Phase 1            → signals_pipeline.py + engine/rhengine = signal calculation
Phase 2 (current)  → portfolio_state.py + order_plan.py = dry-run plan JSON
Phase 3 (guarded)  → Claude/Codex reads plan.json, connects to Robinhood MCP, reviews orders
Phase 4            → launchd schedules Phase 3 every weekday at 9:50 AM ET
```

Live trading is enabled. `session_prompt.md` contains `LIVE_TRADING_ENABLED=true`, and both the
manual session and the scheduled Cowork task place real orders autonomously (no confirmation step).
Set the flag to `false` to return to review-only dry-run. Risk gates (kill switch, VIX block,
daily/weekly loss limits, stop/target/time exits) remain in force in either mode.

For day-to-day ops, prefer `DAILY_OPERATIONS.md`. `PROJECT_GUIDE.md` is archival context
(June 2026 handoff); treat README + Daily Ops as the live operator surface.

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
- Core Stage 1 needs `requests` + the engine submodule; meta-labeling and validation also need numpy / scikit-learn / joblib / scipy
