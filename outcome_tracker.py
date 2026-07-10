"""
outcome_tracker.py — Updates setup_log.csv with trade outcomes

Fetches Alpaca bars for every open setup in the log and walks forward
bar-by-bar to determine if stop, target, or time exit was hit.

P&L is expressed in R-multiples:
    +2.0 R = hit full take-profit target (2× ATR)
    -1.0 R = stopped out (1× ATR loss)
     0.0 R = time exit at breakeven

Run this once per day, ideally at 3:15 PM ET before the close,
or after hours. It only modifies rows where outcome_reason = "open".

Usage:
    python3 outcome_tracker.py           # update outcomes + print summary
    python3 outcome_tracker.py --summary # print summary only, no fetch
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

LOG_PATH    = "setup_log.csv"
CONFIG_PATH = "config.json"


def _load_rows():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _save_rows(rows):
    if not rows:
        return
    with open(LOG_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def _resolve_outcome(row, bars_after_signal):
    """
    Walk post-signal bars and return (outcome_date, outcome_price, pnl_r, reason).
    Returns None if still open and fewer than 10 bars available.
    """
    try:
        entry     = float(row["entry_price"])
        stop      = float(row["stop"])
        target    = float(row["target"])
        atr       = float(row["atr"])
        direction = row["direction"]
    except (ValueError, KeyError):
        return None

    if atr == 0:
        return None

    max_hold = 10  # sessions — matches exits.max_hold_sessions in config

    for j, b in enumerate(bars_after_signal[:max_hold]):
        bar_date = datetime.fromtimestamp(
            b["time"] / 1000, tz=timezone.utc
        ).date().isoformat()

        if direction == "bull":
            if b["high"] >= target:
                pnl_r = (target - entry) / atr
                return bar_date, target, round(pnl_r, 3), "TP"
            if b["low"] <= stop:
                pnl_r = (stop - entry) / atr
                return bar_date, stop, round(pnl_r, 3), "SL"
        else:  # bear
            if b["low"] <= target:
                pnl_r = (entry - target) / atr
                return bar_date, target, round(pnl_r, 3), "TP"
            if b["high"] >= stop:
                pnl_r = (entry - stop) / atr
                return bar_date, stop, round(pnl_r, 3), "SL"

        # Time exit on session 10
        if j == max_hold - 1:
            close_price = b["close"]
            pnl_r = (
                (close_price - entry) / atr if direction == "bull"
                else (entry - close_price) / atr
            )
            return bar_date, close_price, round(pnl_r, 3), "Time"

    return None  # still open — not enough bars yet


def update_outcomes(verbose=True):
    rows = _load_rows()
    if not rows:
        print("outcome_tracker: setup_log.csv is empty or missing.")
        return

    today = datetime.now(timezone.utc).date().isoformat()
    open_rows = [
        r for r in rows
        if r.get("outcome_reason") == "open"
        and r.get("signal_date", "") < today
    ]

    if not open_rows:
        if verbose:
            print("outcome_tracker: no open setups to update.")
        return

    # Group tickers to minimise Alpaca API calls
    ticker_to_rows = {}
    for r in open_rows:
        t = r["ticker"]
        ticker_to_rows.setdefault(t, []).append(r)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    interval = cfg["strategy"].get("interval", "1d")

    # Import here so outcome_tracker can run standalone
    from rhengine.market_data import fetch_bars_batch

    if verbose:
        print(
            f"outcome_tracker: resolving {len(open_rows)} open setup(s) "
            f"across {len(ticker_to_rows)} ticker(s)..."
        )

    result   = fetch_bars_batch(list(ticker_to_rows.keys()), interval=interval,
                                lookback_days=30, verbose=False)
    updated  = 0

    for ticker, bars in result["bars"].items():
        for row in ticker_to_rows.get(ticker, []):
            # Only use bars that came AFTER the signal date
            signal_ts = (
                datetime.fromisoformat(row["signal_date"])
                .replace(tzinfo=timezone.utc)
                .timestamp() * 1000
            )
            post = [b for b in bars if b["time"] > signal_ts]
            if not post:
                continue

            outcome = _resolve_outcome(row, post)
            if outcome:
                bar_date, price, pnl_r, reason = outcome
                row["outcome_date"]   = bar_date
                row["outcome_price"]  = round(price, 4)
                row["outcome_pnl_r"]  = pnl_r
                row["outcome_reason"] = reason
                updated += 1

    _save_rows(rows)
    if verbose:
        print(f"outcome_tracker: resolved {updated} outcome(s).")


def print_summary():
    rows = _load_rows()
    if not rows:
        print("No data in setup_log.csv yet.")
        return

    completed = [r for r in rows if r.get("outcome_reason") not in ("open", "")]
    still_open = [r for r in rows if r.get("outcome_reason") == "open"]

    print("\n─────────────────────────────────────────")
    print(f"  SETUP LOG SUMMARY  ({len(rows)} total signals)")
    print("─────────────────────────────────────────")
    print(f"  Completed : {len(completed)}")
    print(f"  Open      : {len(still_open)}")

    if not completed:
        print("  (no completed setups yet)")
        return

    # Win rate and avg R by score
    for score in ("4", "3"):
        subset = [r for r in completed if str(r.get("score")) == score]
        if not subset:
            continue
        wins   = [r for r in subset if float(r.get("outcome_pnl_r", 0)) > 0]
        losses = [r for r in subset if float(r.get("outcome_pnl_r", 0)) <= 0]
        avg_r  = sum(float(r["outcome_pnl_r"]) for r in subset) / len(subset)
        wr     = len(wins) / len(subset) * 100
        print(
            f"\n  Score {score}/4 ({len(subset)} trades)"
            f"  WR: {wr:.0f}%  Avg R: {avg_r:+.2f}"
            f"  W:{len(wins)} L:{len(losses)}"
        )

    # By strategy
    strategies = {r["strategy"] for r in completed}
    if len(strategies) > 1:
        print("\n  By strategy:")
        for strat in sorted(strategies):
            subset = [r for r in completed if r.get("strategy") == strat]
            wins   = sum(1 for r in subset if float(r.get("outcome_pnl_r", 0)) > 0)
            avg_r  = sum(float(r["outcome_pnl_r"]) for r in subset) / len(subset)
            print(f"    {strat:20s} {len(subset):3d} trades  "
                  f"WR: {wins/len(subset)*100:.0f}%  Avg R: {avg_r:+.2f}")

    # Open positions summary
    if still_open:
        print(f"\n  Open setups ({len(still_open)}):")
        for r in still_open:
            print(f"    {r['signal_date']}  {r['ticker']:6s}  "
                  f"{r['direction']:4s}  {r['score']}/4  entry ${r['entry_price']}")

    print("─────────────────────────────────────────\n")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true",
                        help="Print summary only, skip outcome fetching")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        update_outcomes()
        print_summary()
