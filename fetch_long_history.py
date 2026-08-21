"""
fetch_long_history.py — full-history daily bars for the watchlist, cached locally.

The Massive cache holds about 64 sessions and AgentTradingLogic's yahoo_cache
holds 274. Neither can test a cross-sectional momentum sleeve: 12-1 momentum
spends 252 sessions on the lookback alone, leaving 22 days of signal, which is
not a backtest. Yahoo's chart endpoint serves full history for free with no key,
so the constraint was never the data.

Adapted from AgentTradingLogic/scripts/fetch_history.py — same endpoint, same
symbol translation, same adjusted-close handling.

SURVIVORSHIP WARNING, stated because it changes what the output means: the
universe here is config.json's CURRENT watchlist. Names that were in the index
and then dropped out, went bankrupt, or were acquired are absent, and they are
disproportionately the losers. Any backtest over this cache is an UPPER bound
on what the strategy would have earned. AgentTradingLogic keeps a
point-in-time membership file (sp500_pit_raw.csv) that fixes this; it is not on
this machine. Treat a marginal result here as a negative result.

    python3 fetch_long_history.py              # fetch what is missing
    python3 fetch_long_history.py --force      # re-fetch everything
"""

import argparse
import gzip
import json
import os
import time
import ssl
import urllib.error
import urllib.request

OUT_DIR = "long_history"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
         "?period1=0&period2=9999999999&interval=1d&events=div%2Csplit")


def _ssl_context():
    """
    A context with real root certificates.

    The python.org macOS framework build ships without a CA bundle wired into
    OpenSSL, so every HTTPS fetch dies on CERTIFICATE_VERIFY_FAILED. certifi
    carries the bundle; fall back to the default context if it is absent rather
    than disabling verification, which would make this script a way to silently
    accept a forged Yahoo.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = _ssl_context()


def yahoo_symbol(t):
    """Index notation to Yahoo notation (BRK.B -> BRK-B)."""
    return t.replace(".", "-").strip().upper()


def fetch_one(symbol, timeout=30, retries=4):
    """
    Full daily history as the engine's bar dicts, or None.

    Yahoo answers a burst with 429 and stays annoyed for a while, so back off
    exponentially rather than marching through the rest of the universe turning
    every remaining ticker into a recorded "miss". A run that logs 900 failures
    in ninety seconds looks like bad symbols and is really one rate limit.
    """
    req = urllib.request.Request(CHART.format(sym=yahoo_symbol(symbol)),
                                 headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                payload = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == retries - 1:
                raise
            time.sleep(min(60, 5 * 2 ** attempt))
    else:
        return None
    res = (payload.get("chart") or {}).get("result")
    if not res:
        return None
    res = res[0]
    ts = res.get("timestamp")
    if not ts:
        return None
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")

    bars = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        # Split/dividend adjustment matters more here than anywhere else in this
        # project. A 12-month momentum rank computed on raw closes reads a 4:1
        # split as a -75% return and shorts the best performer in the universe.
        # Scale OHLC by the adjclose/close ratio so the whole bar is consistent.
        a = adj[i] if adj and adj[i] is not None else c
        k = a / c if c else 1.0
        o, h, l = q["open"][i], q["high"][i], q["low"][i]
        bars.append({
            "time": int(t) * 1000,
            "open": (o * k) if o is not None else a,
            "high": (h * k) if h is not None else a,
            "low": (l * k) if l is not None else a,
            "close": a,
            "raw_close": c,
            "volume": q["volume"][i] or 0,
        })
    return bars or None


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--extra", default="SPY")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    with open("config.json") as f:
        cfg = json.load(f)

    universe = list(dict.fromkeys(
        cfg["watchlist"] + [t for t in args.extra.split(",") if t]))
    banned = set(cfg.get("banned_instruments", []))
    universe = [t for t in universe if t not in banned]

    ok = skip = miss = 0
    misses = []
    for i, ticker in enumerate(universe, 1):
        path = os.path.join(OUT_DIR, f"{ticker}.json.gz")
        if os.path.exists(path) and not args.force:
            skip += 1
            continue
        try:
            bars = fetch_one(ticker)
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, ValueError, KeyError, IndexError) as e:
            bars, err = None, str(e)[:60]
        else:
            err = "no data"
        if not bars:
            miss += 1
            misses.append(f"{ticker}: {err}")
        else:
            with gzip.open(path, "wt") as f:
                json.dump({"ticker": ticker, "fetched": time.strftime("%Y-%m-%d"),
                           "bars": bars}, f)
            ok += 1
        if i % 50 == 0:
            print(f"  {i}/{len(universe)}  ok={ok} skip={skip} miss={miss}",
                  flush=True)
        time.sleep(args.sleep)

    with open(os.path.join(OUT_DIR, "_coverage.json"), "w") as f:
        json.dump({"universe": len(universe), "ok": ok, "skipped": skip,
                   "missing": misses}, f, indent=2)
    print(f"done: {ok} fetched, {skip} cached, {miss} missing -> {OUT_DIR}/")
    if misses:
        print("  missing:", ", ".join(m.split(":")[0] for m in misses[:15]))


if __name__ == "__main__":
    main()
