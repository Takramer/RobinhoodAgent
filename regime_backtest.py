"""
regime_backtest.py — which strategy earns its slot in which SPY regime.

strategy_selector answers this from live results, but it can only judge a pairing
that has already traded. bollinger has never run in an uptrend, so live data
cannot clear it and never will: the static regime map only ever routed it to
choppy, so it can never accumulate the sample the selector demands. That is a
deadlock, and this script is how a pairing gets its first evidence.

Data comes from AgentTradingLogic's yahoo_cache — 519 tickers already on disk —
so a sweep costs no API calls and no rate limit. Costs default to 30 bps round
trip, which is that repo's single source of truth; its 2026-07 audit found the
previous engine's headline result was an artifact of running at zero.

Excess return against SPY over the identical holding window is the reported
number, not R-multiples, so the output is directly comparable to setup_log.csv
and to what strategy_selector gates on.

    python3 regime_backtest.py                    # every strategy x regime
    python3 regime_backtest.py --strategy bollinger
"""

import argparse
import glob
import gzip
import json
import os
import statistics
import sys

ATL_CACHE = "/Users/thomaskramer/Downloads/Claude/AgentTradingLogic/yahoo_cache"
OUT_PATH = "regime_backtest.json"

# AgentTradingLogic config.json -> costs.round_trip_bps. Do not lower this
# without executed-fill evidence; the audit that killed the previous engine
# turned on exactly this number being zero.
COST_BPS = 30.0

REGIMES = ("uptrend", "choppy", "downtrend")
STRATEGIES = ("bollinger", "residualmomentum", "pead",
              "vwap", "macd", "consolidation")


def load_cache(cache_dir=ATL_CACHE, spy_ticker="SPY"):
    """{ticker: bars} from the on-disk Yahoo cache, plus SPY split out."""
    bars_map = {}
    for path in sorted(glob.glob(os.path.join(cache_dir, "*.json.gz"))):
        ticker = os.path.basename(path).replace(".json.gz", "")
        try:
            with gzip.open(path) as f:
                bars = json.load(f).get("bars") or []
        except (OSError, ValueError):
            continue
        if bars:
            bars_map[ticker] = bars
    spy = bars_map.pop(spy_ticker, None)
    return bars_map, spy


def _spy_closes(spy_bars):
    """date -> close, plus the sorted dates, for benchmark lookups."""
    import datetime as dt
    closes = {
        dt.datetime.fromtimestamp(b["time"] / 1000, dt.timezone.utc)
        .date().isoformat(): b["close"]
        for b in spy_bars
    }
    return sorted(closes), closes


def _spy_on_or_before(dates, closes, day):
    import bisect
    if day in closes:
        return closes[day]
    i = bisect.bisect_right(dates, day)
    return closes[dates[i - 1]] if i else None


def excess_of(trade, dates, closes, spy_bars):
    """
    Trade return minus SPY's return over the same holding window, in percent.

    The backtest reports r_multiple, which is scaled by ATR and says nothing
    about the market. Recompute from entry and exit prices so the number means
    the same thing here as it does in setup_log.csv.
    """
    import datetime as dt
    entry, exit_ = trade["entry"], trade["exit_price"]
    if not entry:
        return None
    start = trade["signal_date"]
    # sessions_held is in trading sessions; walk SPY's own series rather than
    # guessing a calendar offset.
    i = next((k for k, d in enumerate(dates) if d >= start), None)
    if i is None:
        return None
    j = min(i + max(1, trade.get("sessions_held", 1)), len(dates) - 1)
    spy_start = _spy_on_or_before(dates, closes, dates[i])
    spy_end = _spy_on_or_before(dates, closes, dates[j])
    if not spy_start or not spy_end:
        return None
    return (exit_ - entry) / entry * 100.0 - (spy_end - spy_start) / spy_start * 100.0


def summarize(trades, dates, closes, spy_bars):
    """
    Clustered by signal date — see strategy_selector.cluster_stats.

    Thousands of trades fired across one universe over 274 sessions are not
    thousands of independent observations. Reporting a raw t here would produce
    the same phantom significance the live log did, in the opposite direction.
    """
    from strategy_selector import cluster_stats

    pairs = []
    for t in trades:
        e = excess_of(t, dates, closes, spy_bars)
        if e is not None:
            pairs.append((t["signal_date"], e))
    st = cluster_stats(pairs)
    if not st:
        return None
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return {
        "n": st["n"],
        "n_days": st["n_days"],
        "mean_excess": st["mean"],
        "median_excess": st["median"],
        "stderr": st["stderr"],
        "t": st["t"],
        "beat_rate": st["beat_rate"],
        "avg_r": round(statistics.mean(rs), 4) if rs else None,
        "win_rate": round(100 * sum(1 for v in rs if v > 0) / len(rs), 1) if rs else None,
    }


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(os.getcwd(), "engine"))

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default=None, help="one strategy, or all")
    ap.add_argument("--regime", default=None, help="one regime, or all")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS)
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--split", default="2026-01-01",
                    help="date splitting in-sample from the holdout")
    args = ap.parse_args()

    from rhengine.backtest import run_backtest_on

    with open("config.json") as f:
        cfg = json.load(f)

    print(f"Loading cached bars from {ATL_CACHE} ...")
    bars_map, spy_bars = load_cache()
    if not spy_bars:
        raise SystemExit("SPY missing from the cache — cannot compute excess return.")
    print(f"  {len(bars_map)} tickers, {len(spy_bars)} SPY bars, "
          f"cost {args.cost_bps:.0f} bps round trip")

    dates, closes = _spy_closes(spy_bars)
    strategies = [args.strategy] if args.strategy else list(STRATEGIES)
    regimes = [args.regime] if args.regime else list(REGIMES)

    results = {}
    print(f"\n{'strategy':<18}{'regime':<11}{'n':>7}{'days':>6}{'mean%':>9}"
          f"{'t':>8}{'beat%':>7}{'avgR':>8}{'WR%':>7}")
    print("-" * 81)
    for strategy in strategies:
        for regime in regimes:
            def run(lo=None, hi=None):
                return summarize(run_backtest_on(
                    cfg, bars_map, spy_bars,
                    min_score=args.min_score,
                    forced_strategy=strategy,
                    regime_only=regime,
                    cost_bps=args.cost_bps,
                    date_lo=lo, date_hi=hi,
                ), dates, closes, spy_bars)

            stats = run()
            if stats:
                # A pooled t-statistic over 18 cells is a multiple-comparison
                # trap: at t=2 roughly one of them clears by luck alone. Split
                # the window and demand the same sign on both sides. Sign
                # stability across an unseen half is weaker evidence than a
                # clean holdout t, and it is stated as such — but it is the one
                # check that a curve fitted to the whole window cannot pass.
                is_, oos = run(hi=args.split), run(args.split, None)
                stats["in_sample"] = is_
                stats["out_sample"] = oos
                stats["both_halves_positive"] = bool(
                    is_ and oos
                    and is_["mean_excess"] > 0 and oos["mean_excess"] > 0
                )
                stats["qualified"] = bool(
                    stats["both_halves_positive"]
                    and stats["n_days"] >= 30
                    and stats["t"] >= 2.0
                )
            results[f"{strategy}|{regime}"] = stats
            if not stats:
                print(f"{strategy:<18}{regime:<11}{'-':>7}   (too few trades)")
                continue
            print(f"{strategy:<18}{regime:<11}{stats['n']:>7}"
                  f"{stats['n_days']:>6}{stats['mean_excess']:>9.2f}"
                  f"{stats['t']:>8.2f}{stats['beat_rate']:>7.0f}"
                  f"{stats['avg_r']:>8.2f}{stats['win_rate']:>7.0f}")

    payload = {
        "source": ATL_CACHE,
        "tickers": len(bars_map),
        "spy_bars": len(spy_bars),
        "window": [dates[0], dates[-1]],
        "cost_bps": args.cost_bps,
        "min_score": args.min_score,
        "results": results,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote {args.out}")

    passing = [k for k, v in results.items() if v and v.get("qualified")]
    print(f"\nQualified (both halves positive, >=30 days, pooled t>=2.0): "
          f"{passing or 'NONE'}")
    for k, v in results.items():
        if v and v.get("both_halves_positive") and not v.get("qualified"):
            print(f"  near miss: {k} pooled t={v['t']} days={v['n_days']}")


if __name__ == "__main__":
    main()
