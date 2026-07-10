"""
news_data.py — Alpaca news → sentiment/flow features for meta-labeling.

Alpaca's news feed (Benzinga-sourced) returns headline/summary/symbols/timestamps
but NO sentiment score, so we derive one with a compact Loughran-McDonald-style
finance lexicon (dependency-free). We expose as-of features that never look ahead:
for a signal on session date D we use only articles dated <= D.

Two entry points:
  * build_cache(cfg, lookback_days)      — fetch + score all watchlist news once,
                                           cache to news_cache.json (re-runnable)
  * news_features_asof(index, ticker, d) — trailing-window features as of date d

Features: news_count_7d, news_count_3d, news_recency_days, news_sentiment_7d.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

_ET = ZoneInfo("America/New_York")
_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
CACHE_PATH = "news_cache.json"

NEWS_FEATURE_NAMES = [
    "news_count_7d", "news_count_3d", "news_recency_days", "news_sentiment_7d",
]
NEWS_DEFAULTS = {
    "news_count_7d": 0.0, "news_count_3d": 0.0,
    "news_recency_days": 30.0, "news_sentiment_7d": 0.0,
}

# ── compact finance sentiment lexicon (Loughran-McDonald flavored) ──
_POS = set("""
beat beats surge surged soar soared jump jumped rally rallied gain gains gained
rise rises rose upgrade upgraded outperform outperformed strong stronger growth
profit profitable record bullish boost boosted exceed exceeded tops topped win
wins winning positive optimistic expand expansion momentum breakthrough approval
approved partnership dividend buyback raise raised upside rebound recover recovery
accelerate robust milestone launch launches opportunity confidence improve improved
improvement success successful leading leader innovative upbeat higher rallies
""".split())
_NEG = set("""
miss misses missed plunge plunged fall falls fell drop dropped decline declined
slump slumped crash crashed downgrade downgraded underperform weak weaker loss
losses lose cut cuts cutting warn warning warned lawsuit sue sued investigation
probe fraud bearish selloff tumble tumbled slide slid concern concerns risk risks
recall delay delayed halt halted bankruptcy default layoff layoffs fired resign
resigned scandal penalty fine fined deficit shortfall disappoint disappointing
disappointed negative pessimistic slowdown slowing headwind headwinds downside
pressure struggle struggling plummet plummeted sink sank crisis threat volatile
volatility uncertainty subpoena delisting lower slashes slashed halts
""".split())


def score_text(text):
    """Net finance sentiment in [-1, 1] from a headline/summary."""
    if not text:
        return 0.0
    words = "".join(c.lower() if (c.isalnum() or c.isspace()) else " " for c in text).split()
    p = sum(1 for w in words if w in _POS)
    n = sum(1 for w in words if w in _NEG)
    if p + n == 0:
        return 0.0
    return (p - n) / (p + n)


def _et_date(iso_utc):
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(_ET).date().isoformat()


def _credentials():
    s = json.load(open(os.path.join(os.path.dirname(__file__), "secrets.json")))
    return s["ALPACA_API_KEY"].strip(), s["ALPACA_API_SECRET"].strip()


def build_cache(cfg, lookback_days=520, cache_path=CACHE_PATH, verbose=True,
                max_pages=2000):
    """Fetch + score all watchlist news over the window. Returns an index:
    {ticker: [(et_date, sentiment), ...] sorted ascending}. Cached to disk;
    reused if the cache already covers the requested start date."""
    path = os.path.join(os.path.dirname(__file__), cache_path)
    start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()

    if os.path.exists(path):
        cached = json.load(open(path))
        if cached.get("start_date", "9999") <= start_date:
            if verbose:
                print(f"  news cache hit ({cached['articles']} articles from {cached['start_date']})")
            return {t: [tuple(x) for x in v] for t, v in cached["by_ticker"].items()}

    key, secret = _credentials()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    symbols = ",".join(cfg["watchlist"])
    start = start_date + "T00:00:00Z"

    by_ticker = {}
    token = None
    pages = 0
    total = 0
    if verbose:
        print(f"  fetching news since {start_date} for {len(cfg['watchlist'])} symbols...")
    while pages < max_pages:
        params = {"symbols": symbols, "start": start, "limit": 50, "sort": "asc"}
        if token:
            params["page_token"] = token
        r = requests.get(_NEWS_URL, params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            time.sleep(2)
            continue
        if not r.ok:
            if verbose:
                print(f"  news fetch stopped: HTTP {r.status_code}")
            break
        d = r.json()
        for a in d.get("news", []):
            date = _et_date(a["created_at"])
            sent = score_text((a.get("headline") or "") + ". " + (a.get("summary") or ""))
            for sym in a.get("symbols", []):
                by_ticker.setdefault(sym, []).append((date, sent))
            total += 1
        pages += 1
        token = d.get("next_page_token")
        if not token:
            break

    for sym in by_ticker:
        by_ticker[sym].sort(key=lambda x: x[0])

    json.dump({"start_date": start_date, "articles": total, "by_ticker": by_ticker},
              open(path, "w"))
    if verbose:
        print(f"  cached {total} articles across {len(by_ticker)} tickers, {pages} pages")
    return by_ticker


def _days_between(d_from, d_to):
    from datetime import date
    return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days


def news_features_asof(index, ticker, asof_date):
    """Trailing-window news features for `ticker` as of `asof_date` (ISO). Uses
    only articles dated <= asof_date (no lookahead)."""
    arts = index.get(ticker)
    if not arts:
        return dict(NEWS_DEFAULTS)
    c7 = c3 = 0
    s7_sum = 0.0
    last_date = None
    for date, sent in arts:
        if date > asof_date:
            break  # sorted ascending — nothing further is known yet
        last_date = date
        age = _days_between(date, asof_date)
        if age <= 6:
            c7 += 1
            s7_sum += sent
        if age <= 2:
            c3 += 1
    return {
        "news_count_7d": float(c7),
        "news_count_3d": float(c3),
        "news_recency_days": float(min(30, _days_between(last_date, asof_date))) if last_date else 30.0,
        "news_sentiment_7d": (s7_sum / c7) if c7 else 0.0,
    }
