"""
validation.py — is the backtested edge real, or an artifact of trying many things?

Reports three numbers on a per-trade R series (from backtest --csv or the training
set):

  * Sharpe (per-trade and annualized) — the raw risk-adjusted edge.
  * PSR  — Probabilistic Sharpe Ratio: P(true Sharpe > 0) given sample size, skew,
           and kurtosis. Answers "is this distinguishable from zero at all?"
  * DSR  — Deflated Sharpe Ratio (Bailey & López de Prado): PSR against the Sharpe
           you'd EXPECT as the best of N independent trials under the null. We ran
           ~dozens of optimizations on this one window, so the honest benchmark is
           not zero — it's the max you'd get by luck from that many tries. DSR is
           the number that predicts forward performance.

Rule of thumb: DSR > 0.95 = the edge is probably real; < 0.9 = likely overfit.
This is a mirror, not a booster — expect it to be humbling on ~500 days of data.

Usage:
    python3 validation.py                       # reads backtest_trades.csv
    python3 validation.py --data training_data.csv --trials 30 --cost-bps 5
"""

import argparse
import csv
import math
import os
from datetime import date

import numpy as np
from scipy.stats import norm, skew, kurtosis

_GAMMA = 0.5772156649015329  # Euler-Mascheroni


def _load(path, cost_bps=0.0, aggregate_daily=True):
    """Load per-trade R. If aggregate_daily, collapse same-day trades to their mean
    (one observation per signal date) — this removes the cross-sectional overlap
    that otherwise inflates Sharpe/DSR by treating correlated same-day trades as
    independent. Residual time-overlap (multi-day holds) still inflates somewhat."""
    rows = list(csv.DictReader(open(path, newline="")))
    by_date = {}
    per_trade = []
    for row in rows:
        val = float(row["r_multiple"])
        if cost_bps and "entry" in row and "stop" in row:
            e, s = float(row["entry"]), float(row["stop"])
            sp = (e - s) / e if e > 0 else 0
            if sp > 0:
                val -= 2.0 * (cost_bps / 10000.0) / sp
        per_trade.append(val)
        by_date.setdefault(row["signal_date"], []).append(val)
    if aggregate_daily:
        dates = sorted(by_date)
        r = np.array([sum(by_date[d]) / len(by_date[d]) for d in dates], float)
        return r, dates, len(per_trade)
    return np.array(per_trade, float), [row["signal_date"] for row in rows], len(per_trade)


def _trades_per_year(dates):
    d0, d1 = date.fromisoformat(min(dates)), date.fromisoformat(max(dates))
    years = max((d1 - d0).days / 365.25, 1e-6)
    return len(dates) / years


def analyze(r, dates, trials=1):
    n = len(r)
    mu, sd = r.mean(), r.std(ddof=1)
    sr = mu / sd if sd > 0 else 0.0            # per-trade Sharpe
    g3 = float(skew(r))
    g4 = float(kurtosis(r, fisher=False))       # non-excess kurtosis
    denom = math.sqrt(max(1e-9, 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2))

    psr0 = norm.cdf(sr * math.sqrt(n - 1) / denom)

    # Expected max Sharpe from `trials` independent tries under the null.
    var_sr = (1.0 / (n - 1)) * (1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    if trials > 1 and var_sr > 0:
        z1 = norm.ppf(1 - 1.0 / trials)
        z2 = norm.ppf(1 - 1.0 / (trials * math.e))
        sr_star = math.sqrt(var_sr) * ((1 - _GAMMA) * z1 + _GAMMA * z2)
    else:
        sr_star = 0.0
    dsr = norm.cdf((sr - sr_star) * math.sqrt(n - 1) / denom)

    tpy = _trades_per_year(dates)
    return {
        "n": n, "mean_r": mu, "std_r": sd,
        "sr_trade": sr, "sr_annual": sr * math.sqrt(tpy),
        "trades_per_year": tpy, "skew": g3, "kurt": g4,
        "psr0": psr0, "sr_star": sr_star, "sr_star_annual": sr_star * math.sqrt(tpy),
        "dsr": dsr, "trials": trials,
    }


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="backtest_trades.csv",
                    help="CSV with r_multiple + signal_date (backtest --csv output)")
    ap.add_argument("--trials", type=int, default=25,
                    help="number of strategy/param configs tried on this data (for DSR)")
    ap.add_argument("--cost-bps", type=float, default=0.0)
    args = ap.parse_args()

    if not os.path.exists(args.data):
        print(f"{args.data} not found. Generate it with:\n"
              f"  python3 backtest.py --lookback-days 500 --min-score 3 --csv {args.data}")
        return

    r, dates, n_trades = _load(args.data, cost_bps=args.cost_bps, aggregate_daily=True)
    s = analyze(r, dates, trials=args.trials)

    print(f"\n{n_trades} trades collapsed to {s['n']} daily observations "
          f"({min(dates)}..{max(dates)})  cost={args.cost_bps}bps")
    print("(daily aggregation removes cross-sectional overlap; multi-day holds still")
    print(" leave some time-overlap, so the annualized figures remain an UPPER bound)")
    print(f"\nMean daily R: {s['mean_r']:+.3f}   Std: {s['std_r']:.3f}   "
          f"skew {s['skew']:+.2f}  kurt {s['kurt']:.1f}")
    print(f"Sharpe: {s['sr_annual']:+.2f} annualized ({s['sr_trade']:+.3f}/day)")
    print(f"PSR (P[true Sharpe > 0]):              {s['psr0']*100:5.1f}%")
    print(f"Luck bar (max Sharpe from {s['trials']} trials): {s['sr_star_annual']:+.2f} annualized")
    print(f"DSR (P[edge survives {s['trials']} trials]):  {s['dsr']*100:5.1f}%")
    verdict = ("LIKELY REAL" if s["dsr"] > 0.95 else
               "PLAUSIBLE" if s["dsr"] > 0.90 else
               "LIKELY OVERFIT / not distinguishable from luck")
    print(f"Verdict: {verdict}")
    print("(DSR>0.95 real · 0.90-0.95 plausible · <0.90 treat backtest as unreliable)\n")


if __name__ == "__main__":
    main()
