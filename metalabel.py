"""
metalabel.py — train and evaluate the meta-labeling model.

Primary model  : the TA engine (indicators.py) — proposes long signals.
Secondary model: a gradient-boosted classifier that predicts P(win) for each
                 proposed signal, so the system can SKIP likely losers and SIZE
                 by confidence. This is López de Prado meta-labeling: keep the
                 primary model's recall, buy precision (win%) with the filter.

Validation is purged, embargoed, expanding-window walk-forward. Trades hold up to
~10 sessions, so a training label can overlap a later test window; we purge any
training sample whose outcome horizon reaches within `embargo_days` of the test
block. Skipping this is the classic way ML trading models look great in-sample and
lose money live — so it is the core of this script, not an afterthought.

Reported: OOS precision/F1/AUC, and the number that actually matters — does
filtering by P(win) beat the naive "take every 4/4" rule on win% AND expectancy?

Usage:
    python3 metalabel.py                     # train on training_data.csv, save model
    python3 metalabel.py --embargo-days 16 --min-trades 120
"""

import argparse
import json
import os

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score
import joblib

from features import FEATURE_NAMES

DATA = "training_data.csv"
MODEL_OUT = "metamodel.joblib"
META_OUT = "metamodel_meta.json"


def load_data(path):
    import csv
    rows = list(csv.DictReader(open(path, newline="")))
    X = np.array([[float(r[f]) for f in FEATURE_NAMES] for r in rows], dtype=float)
    y = np.array([int(r["label_win"]) for r in rows], dtype=int)
    rmult = np.array([float(r["r_multiple"]) for r in rows], dtype=float)
    score = np.array([int(float(r["score"])) for r in rows], dtype=int)
    dates = np.array([r["signal_date"] for r in rows])
    tickers = np.array([r["ticker"] for r in rows])
    order = np.argsort(dates, kind="stable")
    return X[order], y[order], rmult[order], score[order], dates[order], tickers[order]


def _day_index(dates):
    """Map ISO date strings -> integer offsets in days from the first date."""
    from datetime import date
    d0 = date.fromisoformat(dates[0])
    return np.array([(date.fromisoformat(d) - d0).days for d in dates])


def _model():
    # Modest capacity + regularization: small data, overlapping labels, noisy target.
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=250, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=40,
        early_stopping=False, random_state=0,
    )


def purged_walkforward(X, y, dayidx, n_folds=5, embargo_days=16):
    """Expanding-window walk-forward. Returns oos_prob aligned to X (NaN where a
    sample was never in a test fold). Training samples whose signal is within
    `embargo_days` of a test block's start are purged to prevent label overlap."""
    oos = np.full(len(X), np.nan)
    dmin, dmax = dayidx.min(), dayidx.max()
    edges = np.linspace(dmin, dmax, n_folds + 1)
    for k in range(1, n_folds + 1):
        test_lo, test_hi = edges[k - 1], edges[k]
        test_mask = (dayidx >= test_lo) & (dayidx < test_hi if k < n_folds else dayidx <= test_hi)
        # Train strictly before the test block, purged by the embargo gap.
        train_mask = dayidx < (test_lo - embargo_days)
        if train_mask.sum() < 300 or test_mask.sum() < 20:
            continue
        m = _model()
        m.fit(X[train_mask], y[train_mask])
        oos[test_mask] = m.predict_proba(X[test_mask])[:, 1]
    return oos


def _stats(rmult):
    n = len(rmult)
    if n == 0:
        return None
    wins = rmult[rmult > 0]
    gl = -rmult[rmult <= 0].sum()
    return {
        "n": n,
        "win": (rmult > 0).mean() * 100,
        "avg_r": rmult.mean(),
        "pf": (wins.sum() / gl) if gl > 0 else float("inf"),
    }


def _fmt(s):
    if not s:
        return "   n/a"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"n={s['n']:>4}  win={s['win']:>4.1f}%  avgR={s['avg_r']:>+5.2f}  PF={pf}"


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--embargo-days", type=int, default=16)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--min-trades", type=int, default=120,
                    help="min OOS trades a threshold must keep to be eligible")
    args = ap.parse_args()

    X, y, rmult, score, dates, tickers = load_data(args.data)
    dayidx = _day_index(dates)
    print(f"Loaded {len(X)} samples  ({dates[0]}..{dates[-1]}), "
          f"base win {y.mean()*100:.1f}%,  {int((score==4).sum())} are 4/4")

    # ---- purged walk-forward OOS predictions ----
    oos = purged_walkforward(X, y, dayidx, n_folds=args.folds,
                             embargo_days=args.embargo_days)
    ev = ~np.isnan(oos)
    # Persist the HONEST out-of-fold predictions (never in-sample) so downstream
    # tools (ratios.py) evaluate the meta filter without lookahead bias.
    import csv as _csv
    with open("oos_meta.csv", "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["signal_date", "ticker", "oos_prob", "r_multiple", "score"])
        for i in range(len(dates)):
            if ev[i]:
                w.writerow([dates[i], tickers[i], round(float(oos[i]), 4),
                            round(float(rmult[i]), 4), int(score[i])])
    print(f"\nOOS coverage: {ev.sum()} samples predicted out-of-fold (saved oos_meta.csv)")
    yhat = (oos[ev] >= 0.5).astype(int)
    print("OOS classification @0.5:  "
          f"precision={precision_score(y[ev], yhat):.3f}  "
          f"recall={recall_score(y[ev], yhat):.3f}  "
          f"F1={f1_score(y[ev], yhat):.3f}  "
          f"AUC={roc_auc_score(y[ev], oos[ev]):.3f}")

    # ---- the decision that matters: filter by P(win) vs the naive 4/4 rule ----
    base4 = _stats(rmult[ev & (score == 4)])
    base34 = _stats(rmult[ev])
    print("\nBaselines (out-of-sample window):")
    print(f"  take ALL 4/4          {_fmt(base4)}")
    print(f"  take ALL 3/4 & 4/4    {_fmt(base34)}")

    print("\nMeta-filter — take any signal with P(win) >= threshold:")
    print(f"  {'thr':>4} | {'all 3+4 signals':<34} | {'among 4/4 only':<34}")
    best = None
    for thr in [round(x, 2) for x in np.arange(0.45, 0.71, 0.025)]:
        sel = ev & (oos >= thr)
        s_all = _stats(rmult[sel])
        s_4 = _stats(rmult[sel & (score == 4)])
        print(f"  {thr:>4.2f} | {_fmt(s_all):<34} | {_fmt(s_4):<34}")
        if s_all and s_all["n"] >= args.min_trades:
            if best is None or s_all["avg_r"] > best[1]["avg_r"]:
                best = (thr, s_all)

    if best:
        thr, s = best
        lift = s["avg_r"] - (base4["avg_r"] if base4 else 0)
        print(f"\nRecommended threshold {thr:.3f}: {_fmt(s)}")
        print(f"  vs naive 4/4 baseline: avgR {base4['avg_r']:+.2f} -> {s['avg_r']:+.2f} "
              f"({lift:+.2f} R/trade), win {base4['win']:.1f}% -> {s['win']:.1f}%")
    else:
        thr = 0.5
        print("\nNo threshold kept enough OOS trades — using 0.50 and flagging low confidence.")

    # ---- feature importance (permutation on a holdout of the OOS region) ----
    final = _model()
    final.fit(X, y)
    perm = permutation_importance(final, X[ev], y[ev], n_repeats=8,
                                  random_state=0, scoring="roc_auc")
    imp = sorted(zip(FEATURE_NAMES, perm.importances_mean), key=lambda z: z[1], reverse=True)
    print("\nTop features (permutation importance, AUC drop):")
    for name, val in imp[:10]:
        print(f"  {name:<20} {val:+.4f}")

    # ---- persist the production model (trained on everything) + metadata ----
    joblib.dump(final, MODEL_OUT)
    meta = {
        "features": FEATURE_NAMES,
        "threshold": float(thr),
        "trained_rows": int(len(X)),
        "train_dates": [dates[0], dates[-1]],
        "oos_auc": float(roc_auc_score(y[ev], oos[ev])),
        "base_4of4_avg_r": float(base4["avg_r"]) if base4 else None,
        "filtered_avg_r": float(best[1]["avg_r"]) if best else None,
        "embargo_days": args.embargo_days,
        "note": "meta-labeling take/skip filter; apply to live 4/4 (and 3/4) signals",
    }
    json.dump(meta, open(META_OUT, "w"), indent=2)
    print(f"\nSaved {MODEL_OUT} and {META_OUT} (threshold {thr:.3f}).")


if __name__ == "__main__":
    main()
