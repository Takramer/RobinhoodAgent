"""
features.py — signal-time feature extraction for the meta-labeling model.

Every feature here is computed ONLY from bars up to and including the signal bar
(`hist = bars[:t+1]`) and SPY history up to the same date. Nothing may look ahead
— leakage is the fastest way to build a meta-model that backtests beautifully and
loses money live. The label (win/loss) comes separately from the triple-barrier
simulation in dataset.py.

`compute_features` returns a flat dict of numeric features; `FEATURE_NAMES` fixes
their order so the trained model and live scoring stay aligned.
"""

from rhengine.indicators import (
    calc_ema, calc_rsi, calc_bb, calc_macd, calc_atr, calc_adx,
    detect_earnings_event,
)
from news_data import NEWS_FEATURE_NAMES, NEWS_DEFAULTS

# PEAD event features let the meta-model recognise (and fairly rank) post-earnings
# drift setups instead of scoring them blind. OFF until the model is retrained on
# adequate data WITH these features (a 24-feature model on a 22-feature-trained
# .joblib would crash live scoring). The PEAD *strategy* runs regardless; while
# these features are off, PEAD is exempted from the meta-filter in order_plan so
# the anomaly can still trade. Flip True, then retrain (dataset.py + metalabel.py).
INCLUDE_PEAD = True
PEAD_FEATURE_NAMES = ["pead_days_since", "pead_ret_atr"]

# News features were tested (Alpaca news + lexicon sentiment + flow) and did NOT
# improve OOS AUC (0.530 -> 0.532, noise), so they are OFF in the production model.
# The infra is kept for future work (FinBERT sentiment / structured earnings
# events are the promising directions). Flip to True to re-include them — but then
# metascore.py must be wired to fetch live news, or live scoring will feed the
# model neutral defaults and mismatch training.
INCLUDE_NEWS = False

# Fixed feature order — the saved model depends on this. Append only; don't reorder.
_TA_FEATURES = [
    "score", "weighted_score", "strength",
    "rsi", "adx", "atr_pct",
    "px_vs_ema9_atr", "px_vs_ema21_atr", "px_vs_ema50_atr", "ema21_vs_ema50_atr",
    "macd_hist_atr", "bb_pctb",
    "vol_ratio", "ret_5", "ret_20", "dist_to_hi20_atr", "dist_to_lo20_atr",
    "vix_ordinal", "is_uptrend", "is_downtrend", "is_choppy", "is_leveraged",
]
FEATURE_NAMES = (_TA_FEATURES + (PEAD_FEATURE_NAMES if INCLUDE_PEAD else [])
                 + (NEWS_FEATURE_NAMES if INCLUDE_NEWS else []))

_VIX_ORDINAL = {
    "calm": 0, "normal": 1, "fear_elevated": 2,
    "fear_spiking": 3, "panic": 4, "unknown": 1,
}


def _safe_div(a, b, default=0.0):
    return a / b if (b not in (0, 0.0, None) and a is not None) else default


def compute_features(hist, setup, regime, vix_regime, is_leveraged, news=None):
    """
    hist         : list of bars up to and including the signal bar (no lookahead)
    setup        : the setup dict from score_ticker (score/weighted_score/strength)
    regime       : SPY trend regime string at signal time
    vix_regime   : VIX regime string at signal time
    is_leveraged : bool
    news         : dict of NEWS_FEATURE_NAMES -> float (as-of the signal date), or
                   None to use neutral defaults (graceful when news is unavailable)

    Returns a dict of FEATURE_NAMES -> float, or None if core indicators are
    undefined (too little history).
    """
    if len(hist) < 55:
        return None

    closes = [b["close"] for b in hist]
    highs  = [b["high"]  for b in hist]
    lows   = [b["low"]   for b in hist]
    vols   = [b["volume"] for b in hist]
    i = len(hist) - 1
    price = closes[i]
    if price <= 0:
        return None

    ema9  = calc_ema(closes, 9)[i]
    ema21 = calc_ema(closes, 21)[i]
    ema50 = calc_ema(closes, 50)[i]
    rsi   = calc_rsi(closes)[i]
    atr   = calc_atr(hist)
    bb    = calc_bb(closes, 20, 2)[i]
    macd  = calc_macd(closes)
    hist_macd = macd["hist"][i]
    adx   = calc_adx(hist[-60:], 14)

    if None in (ema9, ema21, ema50, rsi, bb, hist_macd) or atr <= 0:
        return None

    avg_vol = (sum(vols[max(0, i - 20):i]) / len(vols[max(0, i - 20):i])) if i > 0 else vols[i]
    hi20 = max(highs[i - 20:i]) if i >= 20 else max(highs[:i + 1])
    lo20 = min(lows[i - 20:i]) if i >= 20 else min(lows[:i + 1])
    bb_range = bb["upper"] - bb["lower"]

    feats = {
        "score":            float(setup.get("score", 0)),
        "weighted_score":   float(setup.get("weighted_score", 0)),
        "strength":         float(setup.get("strength", 0.0)),
        "rsi":              float(rsi),
        "adx":              float(adx) if adx is not None else 20.0,
        "atr_pct":          _safe_div(atr, price),
        "px_vs_ema9_atr":   _safe_div(price - ema9, atr),
        "px_vs_ema21_atr":  _safe_div(price - ema21, atr),
        "px_vs_ema50_atr":  _safe_div(price - ema50, atr),
        "ema21_vs_ema50_atr": _safe_div(ema21 - ema50, atr),
        "macd_hist_atr":    _safe_div(hist_macd, atr),
        "bb_pctb":          _safe_div(price - bb["lower"], bb_range, 0.5),
        "vol_ratio":        _safe_div(vols[i], avg_vol, 1.0),
        "ret_5":            _safe_div(price - closes[i - 5], closes[i - 5]) if i >= 5 else 0.0,
        "ret_20":           _safe_div(price - closes[i - 20], closes[i - 20]) if i >= 20 else 0.0,
        "dist_to_hi20_atr": _safe_div(hi20 - price, atr),
        "dist_to_lo20_atr": _safe_div(price - lo20, atr),
        "vix_ordinal":      float(_VIX_ORDINAL.get(vix_regime, 1)),
        "is_uptrend":       1.0 if regime == "uptrend" else 0.0,
        "is_downtrend":     1.0 if regime == "downtrend" else 0.0,
        "is_choppy":        1.0 if regime == "choppy" else 0.0,
        "is_leveraged":     1.0 if is_leveraged else 0.0,
    }
    if INCLUDE_PEAD:
        ev = detect_earnings_event(hist)
        feats["pead_days_since"] = float(ev["days_since"]) if ev else 30.0
        feats["pead_ret_atr"] = float(ev["ret_atr"]) if ev else 0.0

    if INCLUDE_NEWS:
        news = news or NEWS_DEFAULTS
        for name in NEWS_FEATURE_NAMES:
            feats[name] = float(news.get(name, NEWS_DEFAULTS[name]))
    return feats


def feature_vector(feats):
    """Ordered list of feature values matching FEATURE_NAMES."""
    return [feats[name] for name in FEATURE_NAMES]
