"""
momentum_ic.py — does cross-sectional 12-1 momentum predict forward returns here?

This tests a SLEEVE, not a fix to the existing setup strategies. Those six are
ATR-stop swing rules fitted on about a year of data. Cross-sectional momentum is
a different evidence class: Jegadeesh-Titman 1993, and the momentum leg of
AgentTradingLogic's RMX-B returns +2.43%/yr with alpha t=+2.10 on Ken French's
survivorship-free portfolios, 1963-2026.

None of that transfers automatically. French's evidence is for a cap-weighted
top-quintile portfolio of large caps rebalanced monthly. This account holds
$48.27, which at a $1 minimum order buys at most 48 names and realistically
15-20, so whatever runs here is a truncated, concentrated version of the thing
that was tested. The question this script answers is narrower and honest: in
THIS universe over the data actually on disk, does the 12-1 rank carry any
forward information at all? If it does not, the concentrated version cannot
work either and nothing more needs building.

Method — rank information coefficient, which is what the local cache can
support. The Massive cache holds ~377 sessions; 12-1 momentum spends 252 on the
lookback, leaving ~125 testable days, or about six monthly rebalances. Six
portfolio formations is not a backtest. But 125 daily cross-sections of ~900
names each measures the signal directly:

    momentum(t) = close[t-21] / close[t-252] - 1     (skip the last month:
                                                      it carries short-term
                                                      reversal, opposite sign)
    forward(t)  = close[t+21] / close[t]  - 1
    IC(t)       = Spearman rank correlation across names

One IC per day is already the clustered statistic — no overlapping-trade
inflation to undo. Overlapping FORWARD windows still autocorrelate the IC
series, so the reported t is optimistic and is not corrected here; treat a
marginal result as negative.

Survivorship: the universe is config.json's current watchlist. Delisted losers
are absent, which flatters any long-only result. Another reason a marginal
number means no.

    python3 momentum_ic.py
    python3 momentum_ic.py --lookback 126 --skip 21 --forward 10
"""

import argparse
import glob
import gzip
import json
import os
import statistics

CACHE = "massive_cache"
CLOSE, TS = 3, 5   # index into the cached [o, h, l, c, v, time_ms] row


def load_panel(cache_dir=CACHE, universe=None):
    """
    {ticker: {session_index: close}} plus the sorted session list.

    Indices count REAL sessions only. The cache stores market holidays as
    51-byte stubs with no tickers in them, and if those consume index slots
    then "63 sessions ago" silently means 63 slots ago — a lookback short by
    however many holidays it spans, and a book that cannot be built at all when
    the lookback lands exactly on Christmas. Skip empty days before assigning
    an index so the offsets mean what they say.
    """
    files = sorted(glob.glob(os.path.join(cache_dir, "*.json.gz")))
    panel, dates = {}, []
    for path in files:
        try:
            with gzip.open(path) as f:
                day = json.load(f)
        except (OSError, ValueError):
            continue
        if not day:
            continue
        i = len(dates)
        wrote = False
        for ticker, row in day.items():
            if universe and ticker not in universe:
                continue
            try:
                c = float(row[CLOSE])
            except (TypeError, ValueError, IndexError):
                continue
            if c > 0:
                panel.setdefault(ticker, {})[i] = c
                wrote = True
        if wrote:
            dates.append(os.path.basename(path).replace(".json.gz", ""))
    return panel, dates


def spearman(xs, ys):
    """Rank correlation. Ties get average ranks."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def run(panel, dates, lookback, skip, forward, top_n):
    """Daily ICs and top-decile excess over the universe mean."""
    n_days = len(dates)
    ics, spreads, top_slices = [], [], []

    for t in range(lookback, n_days - forward):
        moms, fwds = [], []
        for ticker, series in panel.items():
            a = series.get(t - lookback)
            b = series.get(t - skip)
            c = series.get(t)
            d = series.get(t + forward)
            if not (a and b and c and d):
                continue
            moms.append(b / a - 1.0)
            fwds.append(d / c - 1.0)
        if len(moms) < 50:
            continue

        ics.append(spearman(moms, fwds))

        pairs = sorted(zip(moms, fwds), key=lambda p: -p[0])
        k = max(1, min(top_n, len(pairs) // 10))
        top = statistics.mean(f for _, f in pairs[:k])
        universe = statistics.mean(fwds)
        spreads.append((top - universe) * 100.0)
        top_slices.append(k)

    return ics, spreads, top_slices


def report(label, values, unit=""):
    if len(values) < 2:
        print(f"  {label}: too few observations")
        return None
    mean = statistics.mean(values)
    se = statistics.stdev(values) / (len(values) ** 0.5)
    t = mean / se if se else 0.0
    print(f"  {label:<28} mean={mean:+.4f}{unit}  se={se:.4f}  "
          f"t={t:+.2f}  n={len(values)}")
    return t


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=252)
    ap.add_argument("--skip", type=int, default=21)
    ap.add_argument("--forward", type=int, default=21)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--cache", default=CACHE)
    args = ap.parse_args()

    with open("config.json") as f:
        cfg = json.load(f)
    universe = set(cfg["watchlist"]) - set(cfg.get("banned_instruments", []))

    panel, dates = load_panel(args.cache, universe)
    print(f"panel: {len(panel)} tickers, {len(dates)} sessions "
          f"({dates[0]} -> {dates[-1]})")
    print(f"signal: close[t-{args.skip}]/close[t-{args.lookback}] - 1, "
          f"forward {args.forward} sessions\n")

    ics, spreads, ks = run(panel, dates, args.lookback, args.skip,
                           args.forward, args.top_n)
    if not ics:
        print("Not enough history for that lookback — try a shorter one.")
        return

    t_ic = report("rank IC", ics)
    t_sp = report(f"top-{int(statistics.mean(ks))} vs universe", spreads, "%")
    hit = 100 * sum(1 for v in ics if v > 0) / len(ics)
    print(f"  {'IC > 0 on':<28} {hit:.0f}% of days")

    print()
    if t_ic is not None and t_ic >= 2.0 and t_sp is not None and t_sp >= 2.0:
        print("VERDICT: signal carries forward information in this universe.")
        print("  Caveats that still apply: overlapping forward windows inflate")
        print("  this t, and the universe is survivorship-biased. Treat as a")
        print("  candidate to size small, not a validated sleeve.")
    else:
        print("VERDICT: no usable signal here. Do not build the sleeve.")


if __name__ == "__main__":
    main()
