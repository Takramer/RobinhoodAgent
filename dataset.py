"""
dataset.py — build the meta-labeling training set.

Walks history exactly like backtest.py (same no-lookahead scoring, same regime
routing the live system uses), and for every qualifying LONG signal writes:
    signal-time features  +  triple-barrier outcome (label + r_multiple)

This is the training table for metalabel.py. The primary model (the TA engine)
proposes the trades; the meta-model will later learn which of them to take.

Label: `label_win` = 1 if the trade's R-multiple > 0 else 0 (the meta-label).
`r_multiple` is kept too so the trainer can score by expectancy, not just accuracy.

Usage:
    python3 dataset.py                       # min_score 3, 500d -> training_data.csv
    python3 dataset.py --min-score 3 --lookback-days 700 --out training_data.csv
"""

import argparse
import csv
import os

from rhengine.backtest import load_bars, simulate_long, _iso_date, _max_hold, WARMUP_BARS
from rhengine.indicators import (
    score_ticker, classify_spy_regime, estimate_vix_regime,
    select_strategy_for_regime, resolve_thresholds,
)
import features as _feat
from features import compute_features, FEATURE_NAMES
from news_data import build_cache, news_features_asof


def build_dataset(cfg, bars_map, spy_bars, min_score=3, news_days=520):
    mode = cfg["strategy"].get("mode", "auto")
    bear_only = set(cfg.get("bear_regime_only", []))
    leveraged = set(cfg.get("leveraged_tickers", []))
    spy_index = {b["time"]: idx for idx, b in enumerate(spy_bars)}

    # News index only when the news features are enabled (they are off by default
    # because they didn't improve OOS AUC — see features.INCLUDE_NEWS).
    news_index = {}
    if _feat.INCLUDE_NEWS:
        try:
            news_index = build_cache(cfg, lookback_days=news_days)
        except Exception as e:
            print(f"  (news unavailable: {e} — using neutral news features)")

    rows = []
    for ticker, bars in bars_map.items():
        n = len(bars)
        if n < WARMUP_BARS + 2:
            continue
        for t in range(WARMUP_BARS, n - 1):
            spy_idx = spy_index.get(bars[t]["time"])
            if spy_idx is None or spy_idx < WARMUP_BARS:
                continue
            spy_hist = spy_bars[: spy_idx + 1]
            regime = classify_spy_regime(spy_hist)
            vix_regime = estimate_vix_regime(spy_hist)
            strategy = (select_strategy_for_regime(regime)
                        if mode == "auto" else mode)

            if ticker in bear_only and regime != "downtrend":
                continue

            hist = bars[: t + 1]
            sig_date = _iso_date(bars[t]["time"])
            news = (news_features_asof(news_index, ticker, sig_date)
                    if _feat.INCLUDE_NEWS else None)

            # Score the regime strategy plus the always-on PEAD sleeve, so the
            # meta-model trains on both and can rank them on the same footing.
            strat_list = [strategy]
            if cfg.get("pead_enabled", True):
                strat_list.append("pead")

            for strat in strat_list:
                setups = score_ticker(ticker, hist, strat, resolve_thresholds(cfg, strat), spy_hist)
                for s in (setups or []):
                    if s["direction"] != "bull" or s["score"] < min_score:
                        continue
                    feats = compute_features(hist, s, regime, vix_regime,
                                             ticker in leveraged, news=news)
                    if feats is None:
                        continue
                    sim = simulate_long(bars, t, s["stop"], s["target"],
                                        _max_hold(strat, ticker, cfg))
                    if sim is None:
                        continue
                    reason, exit_price, r, held = sim
                    row = {
                        "signal_date": sig_date,
                        "ticker": ticker,
                        "strategy": strat,
                        **feats,
                        "hold_sessions": held,
                        "r_multiple": round(r, 4),
                        "label_win": 1 if r > 0 else 0,
                    }
                    rows.append(row)

    rows.sort(key=lambda x: x["signal_date"])
    return rows


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Build meta-labeling training set.")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--lookback-days", type=int, default=500)
    ap.add_argument("--out", default="training_data.csv")
    args = ap.parse_args()

    import json
    with open("config.json") as f:
        cfg = json.load(f)

    bars_map, spy_bars = load_bars(cfg, lookback_days=args.lookback_days)
    rows = build_dataset(cfg, bars_map, spy_bars, min_score=args.min_score)
    if not rows:
        print("No samples generated.")
        return

    cols = (["signal_date", "ticker", "strategy"] + FEATURE_NAMES
            + ["hold_sessions", "r_multiple", "label_win"])
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    wins = sum(r["label_win"] for r in rows)
    print(f"Wrote {len(rows)} samples to {args.out}  "
          f"(win rate {wins/len(rows)*100:.1f}%, "
          f"dates {rows[0]['signal_date']}..{rows[-1]['signal_date']})")


if __name__ == "__main__":
    main()
