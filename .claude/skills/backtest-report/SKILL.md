---
name: backtest-report
description: Run the RobinhoodAgent walk-forward backtest and report results in the standard table (win rate, avg R, expectancy, profit factor, Sharpe, $ P/L for a given portfolio size). Use when the user says "backtest", "retrain", "retrain given portfolio $X", "how did the strat do", or asks for win/loss / sharpe / RR breakdowns.
---

# Backtest Report

Run `backtest.py` (and `optimize.py` when tuning) and always deliver the same report format, sized to a dollar portfolio. No exploratory rambling — run, tabulate, then one honest read of the numbers.

## Inputs (take from the user's message; use defaults otherwise)
- **Portfolio size**: default `account.size` from `config.json`, else $4,000.
- **Strategy**: default auto-regime (all). `--strategy <name>` if the user names one.
- **Lookback**: default 400 days (500 for optimize runs).
- **Min score**: default 3.

## Run
```
python3 backtest.py --csv backtest_trades.csv [--strategy X] [--lookback-days N] [--min-score N] [--cost-bps N]
```
- Always pass `--csv` so per-trade rows exist for the dollar math.
- If the user asked to *retrain/tune* (not just measure): run `optimize.py` first, apply only parameter changes that pass its in-sample/out-of-sample gates (`--min-pf`, `--min-oos-n`), then re-run `backtest.py` to report the AFTER numbers. Never report in-sample-only results as improvements.
- Risk-per-trade for dollar conversion: use the live sizing rule from `config.json` / `TRADING_RULES.md` (vol-targeted); if ambiguous, 1% of portfolio per R and say so.

## Report — always this table

| Metric | Value |
|---|---|
| Trades (n) | |
| Win rate | |
| Avg win R / Avg loss R | |
| Expectancy (R per trade) | |
| Profit factor | |
| Max drawdown (R and $) | |
| Sharpe (per-trade, annualized if computable) | |
| Total P/L in $ (at $X portfolio, Y% risk/trade) | |
| Avg hold (sessions) | |

Then, only if relevant:
- Score-tier breakdown (3/4 vs 4/4, weighted tiers) — one small table.
- BEFORE vs AFTER table when parameters changed.

## Honesty rules (non-negotiable)
- State n. If n < 30 for any cell, flag it as statistically weak — don't sell the number.
- These are walk-forward but still historical: one line noting overfit risk whenever a tuning "improved" results, citing the OOS gate it passed.
- Costs: if `--cost-bps 0`, say results exclude slippage/costs.
- Never let a backtest result trigger live-trading changes to `plan.json`/`config.json` without saying exactly what would change and why.

## Close with one recommendation
One sentence: keep / change / kill, and the single highest-leverage next experiment. No option menus — pick.
