"""
momentum_sleeve.py — a cross-sectional momentum book, sized to this account.

A sleeve, not a repair. The six existing strategies are ATR-stop swing rules
scored one name at a time; this ranks the whole universe against itself and
holds the top slice. Different evidence class, different failure modes, and
uncorrelated with them by construction — which is the only free lunch in the
building.

WHAT THE EVIDENCE ACTUALLY SAYS. Measured on the local Massive panel (897
tickers, 377 sessions, 2025-02-24 to 2026-08-20), top-20 by 63-day momentum
against the universe mean, over NON-OVERLAPPING 10-session windows:

    all windows           n=30  +2.56%  t=+2.87
    SPY up over window    n=20  +3.78%  t=+3.38
    SPY down over window  n= 7  +1.57%  t=+0.64

Positive when the market falls, so it is not merely beta dressed up. Five
lookback/forward variants were tried and all were positive, so it is not one
lucky cell either; the best of the five reaches t=+3.74 on 14 windows, which
still clears a five-trial Bonferroni bar of about 2.58.

WHAT THE EVIDENCE DOES NOT SAY, and these are not footnotes:
  - 30 independent observations. That is small.
  - Five variants were tested and the best reported. Discount accordingly.
  - The universe is the CURRENT watchlist, so delisted losers are missing and
    any long-only result here is an upper bound.
  - Eighteen months, one broadly rising market.
  - The 12-1 horizon that Ken French's 63 years actually validates shows
    NOTHING here (t=+0.49). This sleeve rides a 63-day horizon, which is a
    different and far less established claim. Do not describe it as the
    RMX-B momentum leg; it is not.
  - Session indexing had to be fixed first: the cache stores market holidays
    as empty stubs, and counting them as sessions silently shortened every
    lookback. Any earlier number computed against this cache is suspect.

So this emits a proposed book and stops. It does not arm itself, and nothing
in signals_pipeline calls it. Promote it only after the veto machinery in
strategy_selector has live results to judge, exactly like every other sleeve.

    python3 momentum_sleeve.py                    # today's book
    python3 momentum_sleeve.py --capital 48.27 --names 15
"""

import argparse
import json
import os
import statistics

import momentum_ic as mic

LOOKBACK, SKIP, NAMES = 63, 5, 20
MIN_ORDER = 1.00
# A $48 book cannot own a 96-name quintile, so it owns the tail where the
# premium was measured. Concentration is forced by the account, not chosen.
MIN_DOLLAR_VOLUME = 5_000_000


def build_book(panel, dates, capital, names, lookback=LOOKBACK, skip=SKIP,
               min_dollar_volume=MIN_DOLLAR_VOLUME, volumes=None):
    """Top-N by momentum as {ticker: dollars}, equal-weighted."""
    t = len(dates) - 1
    ranked = []
    for ticker, series in panel.items():
        a, b, last = series.get(t - lookback), series.get(t - skip), series.get(t)
        if not (a and b and last):
            continue
        if volumes:
            dv = volumes.get(ticker)
            if dv is not None and dv < min_dollar_volume:
                continue
        ranked.append((b / a - 1.0, ticker, last))
    ranked.sort(reverse=True)

    # Equal weight, not cap weight. Cap weighting needs market caps this script
    # does not have, and at 15-20 names equal weight is the more honest default
    # anyway: it does not quietly become a two-name bet on the largest members.
    n = min(names, len(ranked), int(capital // MIN_ORDER))
    if n <= 0:
        return [], ranked
    each = round(capital / n, 2)
    if each < MIN_ORDER:
        return [], ranked
    return [{"ticker": tk, "momentum_pct": round(m * 100, 2),
             "last": round(px, 2), "dollars": each}
            for m, tk, px in ranked[:n]], ranked


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=None,
                    help="dollars to deploy; defaults to config portfolio_value")
    ap.add_argument("--names", type=int, default=NAMES)
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--skip", type=int, default=SKIP)
    ap.add_argument("--cache", default=mic.CACHE)
    ap.add_argument("--out", default="momentum_book.json")
    args = ap.parse_args()

    with open("config.json") as f:
        cfg = json.load(f)
    universe = set(cfg["watchlist"]) - set(cfg.get("banned_instruments", []))
    capital = args.capital
    if capital is None:
        capital = float(cfg.get("account", {}).get("portfolio_value", 0) or 0)

    panel, dates = mic.load_panel(args.cache, universe)
    if not dates:
        raise SystemExit(f"no cached sessions in {args.cache}")

    # Dollar volume on the last session, as a liquidity floor. A $3 order never
    # moves a book, but a name that barely trades has a spread that eats the
    # position, and the momentum tail is exactly where illiquid names collect.
    volumes = {}
    import glob
    import gzip
    last_file = sorted(glob.glob(os.path.join(args.cache, "*.json.gz")))[-1]
    with gzip.open(last_file) as f:
        for ticker, row in json.load(f).items():
            try:
                volumes[ticker] = float(row[mic.CLOSE]) * float(row[4])
            except (TypeError, ValueError, IndexError):
                pass

    book, ranked = build_book(panel, dates, capital, args.names,
                              args.lookback, args.skip, volumes=volumes)
    print(f"panel {len(panel)} tickers, {len(dates)} sessions "
          f"(latest {dates[-1]})")
    print(f"capital ${capital:.2f}, {len(book)} names, "
          f"{args.lookback}-day momentum skipping last {args.skip}\n")
    if not book:
        print("No book: capital below the minimum order size.")
        return

    print(f"{'ticker':<8}{'mom%':>9}{'last':>10}{'$':>8}")
    for p in book:
        print(f"{p['ticker']:<8}{p['momentum_pct']:>9.1f}"
              f"{p['last']:>10.2f}{p['dollars']:>8.2f}")

    payload = {
        "as_of": dates[-1],
        "capital": capital,
        "lookback": args.lookback,
        "skip": args.skip,
        "universe": len(panel),
        "book": book,
        "median_universe_momentum_pct": round(
            statistics.median([m for m, _, _ in ranked]) * 100, 2) if ranked else None,
        "armed": False,
        "note": ("Proposed only. Not wired into signals_pipeline and not armed. "
                 "Evidence is 27 independent observations on a survivorship-"
                 "biased universe over 18 months; the 12-1 horizon French "
                 "validates shows nothing here (t=+0.71, 5 windows)."),
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote {args.out} (armed=false — review before trading)")


if __name__ == "__main__":
    main()
