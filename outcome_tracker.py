"""
outcome_tracker.py — Updates setup_log.csv with trade outcomes

Fetches Alpaca bars for every open setup in the log and walks forward
bar-by-bar to determine if stop, target, or time exit was hit.

P&L is expressed in R-multiples:
    +2.0 R = hit full take-profit target (2× ATR)
    -1.0 R = stopped out (1× ATR loss)
     0.0 R = time exit at breakeven

Every resolved setup is then scored against SPY over the identical holding
window (outcome_pct / spy_pct / excess_pct). R-multiples say whether the stop
and target were hit; excess_pct says whether that was worth more than buying
SPY on the same day and selling it on the same day.

Run this once per day, ideally at 3:15 PM ET before the close,
or after hours. It only modifies rows where outcome_reason = "open".

Usage:
    python3 outcome_tracker.py           # update outcomes + print summary
    python3 outcome_tracker.py --summary # print summary only, no fetch
"""

import argparse
import bisect
import csv
import json
import os
import statistics
import sys
from datetime import datetime, timezone

LOG_PATH    = "setup_log.csv"
CONFIG_PATH = "config.json"


# Columns added after the log already existed. _load_rows backfills them as
# empty so DictWriter never sees a ragged row set.
LATE_FIELDS = ("outcome_pct", "spy_pct", "excess_pct")


def _load_rows():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in LATE_FIELDS:
            r.setdefault(k, "")
    return rows


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


def _spy_closes(oldest_date, verbose=True):
    """Return (sorted_dates, {date: close}) of daily SPY closes covering the log."""
    from rhengine.market_data import fetch_bars_batch

    span = (datetime.now(timezone.utc).date()
            - datetime.fromisoformat(oldest_date).date()).days
    lookback = max(30, min(span + 10, 730))
    res = fetch_bars_batch(["SPY"], interval="1d", lookback_days=lookback,
                           verbose=False)
    bars = res["bars"].get("SPY", [])
    closes = {
        datetime.fromtimestamp(b["time"] / 1000, tz=timezone.utc).date().isoformat():
        b["close"]
        for b in bars
    }
    if verbose and not closes:
        print("outcome_tracker: SPY bars unavailable — skipping benchmark scoring.")
    return sorted(closes), closes


def _close_on_or_before(dates, closes, day):
    """SPY close for `day`, falling back to the last session before it."""
    if day in closes:
        return closes[day]
    i = bisect.bisect_right(dates, day)
    return closes[dates[i - 1]] if i else None


def score_vs_spy(verbose=True):
    """
    Fill outcome_pct / spy_pct / excess_pct on every resolved row.

    excess_pct is the setup's directional return minus SPY's return over the
    exact same holding window. This is the only number that says whether a
    strategy earned anything a plain SPY buy would not have. R-multiples do
    not: a +2R month in a market that rose 4% is a losing month.
    """
    rows = _load_rows()
    todo = [
        r for r in rows
        if r.get("outcome_date") and not r.get("excess_pct")
        and r.get("outcome_reason") not in ("open", "")
    ]
    if not todo:
        if verbose:
            print("outcome_tracker: no rows need benchmark scoring.")
        return

    dates, closes = _spy_closes(min(r["signal_date"] for r in todo), verbose)
    if not closes:
        return

    scored = 0
    for r in todo:
        try:
            entry = float(r["entry_price"])
            exit_ = float(r["outcome_price"])
        except (ValueError, KeyError, TypeError):
            continue
        if entry <= 0:
            continue

        spy_start = _close_on_or_before(dates, closes, r["signal_date"])
        spy_end   = _close_on_or_before(dates, closes, r["outcome_date"])
        if not spy_start or not spy_end:
            continue

        # +1 long, -1 short: a bear setup beats the market when the name falls
        # further than SPY does, so both legs get the same sign flip.
        sign = 1.0 if r.get("direction") == "bull" else -1.0
        stock_pct = (exit_ - entry) / entry * 100.0
        spy_pct   = (spy_end - spy_start) / spy_start * 100.0

        r["outcome_pct"] = round(sign * stock_pct, 4)
        r["spy_pct"]     = round(sign * spy_pct, 4)
        r["excess_pct"]  = round(sign * (stock_pct - spy_pct), 4)
        scored += 1

    _save_rows(rows)
    if verbose:
        print(f"outcome_tracker: benchmark-scored {scored} setup(s) vs SPY.")


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

    # Window must reach back past the OLDEST open row, not a fixed 30 days.
    # With a fixed window any row that ages out before it resolves becomes
    # permanently unresolvable — the fetch returns no post-signal bars, so it
    # stays "open" forever and silently drops out of every summary.
    oldest   = min(r["signal_date"] for r in open_rows)
    span     = (datetime.now(timezone.utc).date()
                - datetime.fromisoformat(oldest).date()).days
    lookback = max(30, min(span + 15, 730))

    result   = fetch_bars_batch(list(ticker_to_rows.keys()), interval=interval,
                                lookback_days=lookback, verbose=False)
    updated  = 0

    for ticker, bars in result["bars"].items():
        for row in ticker_to_rows.get(ticker, []):
            # Only use bars that came AFTER the signal date
            signal_ts = (
                datetime.fromisoformat(row["signal_date"])
                .replace(tzinfo=timezone.utc)
                .timestamp() * 1000
            )
            age = (datetime.now(timezone.utc).date()
                   - datetime.fromisoformat(row["signal_date"]).date()).days

            post = [b for b in bars if b["time"] > signal_ts]
            if not post:
                # No bars at all after the signal. The feed for this ticker
                # stopped; waiting will not produce any. Once the row is past
                # the hold window it is dead data, not an open trade — close it
                # flat so it leaves the open list instead of sitting there
                # forever skewing every count.
                if age > 30:
                    row["outcome_date"]   = row["signal_date"]
                    row["outcome_price"]  = row["entry_price"]
                    row["outcome_pnl_r"]  = 0.0
                    row["outcome_reason"] = "Stale"
                    updated += 1
                continue

            outcome = _resolve_outcome(row, post)
            if not outcome and (len(post) >= 10 or age > 30):
                # Either enough sessions elapsed to force a time exit and the
                # walk still produced nothing, or the row outlived the hold
                # window on a feed that stopped part-way. Both are dead data.
                # Close at the last bar that exists rather than leaving the row
                # open forever.
                last = post[-1]
                try:
                    entry = float(row["entry_price"]); atr = float(row["atr"])
                except (ValueError, KeyError):
                    continue
                if atr:
                    sign = 1.0 if row["direction"] == "bull" else -1.0
                    outcome = (
                        datetime.fromtimestamp(last["time"] / 1000, tz=timezone.utc)
                        .date().isoformat(),
                        last["close"],
                        round(sign * (last["close"] - entry) / atr, 3),
                        "Stale",
                    )
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

    # Benchmark section. Everything above is R-multiples, which are blind to
    # what the market did over the same window. This is the part that decides
    # whether the strategy is worth running instead of holding SPY.
    scored = [r for r in completed if r.get("excess_pct") not in ("", None)]
    if scored:
        def _stats(subset):
            ex = [float(r["excess_pct"]) for r in subset]
            beat = sum(1 for v in ex if v > 0)
            return (len(ex), statistics.mean(ex), statistics.median(ex),
                    beat / len(ex) * 100)

        n, mean_x, med_x, beat_pct = _stats(scored)
        print(f"\n  vs SPY ({n} scored, same-window excess return)")
        print(f"    Mean excess : {mean_x:+.2f}%")
        print(f"    Median      : {med_x:+.2f}%")
        print(f"    Beat SPY    : {beat_pct:.0f}% of setups")

        traded  = [r for r in scored if str(r.get("executed")) == "True"]
        skipped = [r for r in scored if str(r.get("executed")) != "True"]
        if traded and skipped:
            tn, _, tmed, _ = _stats(traded)
            sn, _, smed, _ = _stats(skipped)
            print(f"    Traded      : {tmed:+.2f}% median  (n={tn})")
            print(f"    Skipped     : {smed:+.2f}% median  (n={sn})")
            edge = tmed - smed
            print(f"    Selection   : {edge:+.2f}%  "
                  f"({'picking winners' if edge > 0 else 'picking losers — the filter is backwards'})")

        # Sampling error on the mean. Under ~20 resolved setups the sign of the
        # mean is not evidence of anything; print the band so a good-looking
        # number does not get treated as an edge.
        if n >= 2:
            se = statistics.stdev([float(r["excess_pct"]) for r in scored]) / (n ** 0.5)
            print(f"    Std error   : ±{se:.2f}%  "
                  f"{'(n too small to call)' if n < 20 or abs(mean_x) < 2 * se else '(significant)'}")

    # Open positions summary — oldest first, capped. This runs daily under
    # launchd and the open list is in the thousands; the whole dump would bury
    # session.log.
    if still_open:
        shown = sorted(still_open, key=lambda r: r["signal_date"])[:15]
        print(f"\n  Open setups ({len(still_open)}, oldest {len(shown)}):")
        for r in shown:
            print(f"    {r['signal_date']}  {r['ticker']:6s}  "
                  f"{r['direction']:4s}  {r['score']}/4  entry ${r['entry_price']}")
        if len(still_open) > len(shown):
            print(f"    ... {len(still_open) - len(shown)} more")

    print("─────────────────────────────────────────\n")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true",
                        help="Print summary only, skip outcome fetching")
    parser.add_argument("--score-only", action="store_true",
                        help="Backfill SPY excess-return scoring, skip outcome fetching")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    elif args.score_only:
        score_vs_spy()
        print_summary()
    else:
        update_outcomes()
        score_vs_spy()
        print_summary()
