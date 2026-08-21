"""
setup_logger.py — Appends all 3/4 and 4/4 setups from signals.json to setup_log.csv

Called automatically at end of run_session.sh after indicators.py and order_plan.py.
Skips setups already logged for the same session date (idempotent — safe to re-run).

Columns:
    id               Unique key: date_ticker_direction_score
    signal_date      Date of signal (YYYY-MM-DD)
    signal_time_utc  Exact timestamp from signals.json
    ticker           Stock symbol
    strategy         Strategy that produced the signal
    score            3 or 4 (legs passed)
    direction        bull or bear
    legs_passed      Pipe-separated names of passing legs
    entry_price      Price at signal time
    stop             Stop loss price (1× ATR below/above entry)
    target           Take profit price (2× ATR)
    atr              14-bar ATR at signal time
    spy_regime       uptrend / downtrend / choppy
    vix_regime       calm / normal / fear_elevated / fear_spiking / panic
    vix_size_mult    Position size multiplier from VIX (1.0 / 0.5 / 0.0)
    executed         True if this setup appeared in plan.json qualified_setups
    outcome_date     Filled by outcome_tracker.py when resolved
    outcome_price    Price at outcome (TP/SL hit or time exit close)
    outcome_pnl_r    P&L in R-multiples (1.0 = hit 1× ATR profit)
    outcome_reason   TP / SL / Time / open
    outcome_pct      Directional % return of the setup, entry to outcome
    spy_pct          SPY % return over the identical window, same sign
    excess_pct       outcome_pct - spy_pct — the only number that says whether
                     this setup beat simply holding SPY over the same days
"""

import csv
import json
import os
import sys

LOG_PATH     = "setup_log.csv"
SIGNALS_PATH = "signals.json"
PLAN_PATH    = "plan.json"

FIELDS = [
    "id", "signal_date", "signal_time_utc", "ticker", "strategy",
    "score", "direction", "legs_passed",
    "entry_price", "stop", "target", "atr",
    "spy_regime", "vix_regime", "vix_size_mult",
    "executed",
    "outcome_date", "outcome_price", "outcome_pnl_r", "outcome_reason",
    "outcome_pct", "spy_pct", "excess_pct",
]


def _load_existing_ids():
    if not os.path.exists(LOG_PATH):
        return set()
    with open(LOG_PATH, newline="") as f:
        return {row["id"] for row in csv.DictReader(f)}


def _make_id(signal_date, ticker, direction, score):
    return f"{signal_date}_{ticker}_{direction}_{score}"


def append_setups(signals, plan):
    """
    Read signals (from signals.json) and plan (from plan.json),
    append any new 3/4 or 4/4 setups to setup_log.csv.
    Returns number of rows written.
    """
    existing_ids = _load_existing_ids()

    # Tickers that were actually entered today (for the 'executed' flag)
    executed_tickers = {
        e["ticker"]
        for e in plan.get("qualified_setups", [])
        if e.get("action") == "ENTER"
    }

    # Combine candidate (3/4) and qualified (4/4) — both are now in signals.json
    all_setups = (
        signals.get("qualified_setups", []) +
        signals.get("candidate_setups", [])
    )

    new_rows = []
    for setup in all_setups:
        ticker    = str(setup.get("ticker", "")).upper()
        direction = setup.get("direction", "")
        score     = setup.get("score", 0)
        date      = signals.get("session_date", "")
        row_id    = _make_id(date, ticker, direction, score)

        if row_id in existing_ids:
            continue  # already logged this session/ticker/direction

        legs_passed = "|".join(
            l["name"] for l in setup.get("legs", []) if l.get("pass")
        )

        new_rows.append({
            "id":              row_id,
            "signal_date":     date,
            "signal_time_utc": signals.get("generated_at", ""),
            "ticker":          ticker,
            "strategy":        setup.get("strategy", signals.get("strategy_used", "")),
            "score":           score,
            "direction":       direction,
            "legs_passed":     legs_passed,
            "entry_price":     setup.get("entry", ""),
            "stop":            setup.get("stop", ""),
            "target":          setup.get("target", ""),
            "atr":             setup.get("atr", ""),
            "spy_regime":      signals.get("spy_regime", ""),
            "vix_regime":      signals.get("vix_regime", ""),
            "vix_size_mult":   signals.get("vix_size_mult", ""),
            "executed":        ticker in executed_tickers and direction == "bull",
            "outcome_date":    "",
            "outcome_price":   "",
            "outcome_pnl_r":   "",
            "outcome_reason":  "open",
            "outcome_pct":     "",
            "spy_pct":         "",
            "excess_pct":      "",
        })

    if not new_rows:
        print("setup_logger: no new setups to log.")
        return 0

    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    qualified = sum(1 for r in new_rows if r["score"] == 4)
    candidates = sum(1 for r in new_rows if r["score"] == 3)
    print(
        f"setup_logger: logged {len(new_rows)} setup(s) → "
        f"{qualified} qualified (4/4), {candidates} candidate (3/4)"
    )
    return len(new_rows)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if not os.path.exists(SIGNALS_PATH):
        print(f"ERROR: {SIGNALS_PATH} not found — run indicators.py first.")
        sys.exit(1)

    with open(SIGNALS_PATH) as f:
        signals = json.load(f)

    plan = {}
    if os.path.exists(PLAN_PATH):
        with open(PLAN_PATH) as f:
            plan = json.load(f)

    append_setups(signals, plan)
