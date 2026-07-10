"""
metascore.py — live application of the trained meta-labeling model.

Loads metamodel.joblib (if present) and attaches a `meta_prob` = P(win) to each
setup, computed from the same lookahead-free features used in training. Designed
to DEGRADE GRACEFULLY: if the model file or sklearn is missing, it is a no-op and
the rest of the pipeline runs unchanged.

Given the model's current OOS AUC (~0.53), `meta_prob` is used as an ADVISORY
ranking/sizing input, not a hard gate — see attach_meta / order_plan.
"""

import json
import os

_MODEL = None
_META = None
_LOADED = False


def _load():
    global _MODEL, _META, _LOADED
    if _LOADED:
        return _MODEL, _META
    _LOADED = True
    here = os.path.dirname(os.path.abspath(__file__))
    model_path = os.path.join(here, "metamodel.joblib")
    meta_path = os.path.join(here, "metamodel_meta.json")
    if os.path.exists(model_path) and os.path.exists(meta_path):
        try:
            import joblib
            _MODEL = joblib.load(model_path)
            _META = json.load(open(meta_path))
        except Exception:
            _MODEL, _META = None, None
    return _MODEL, _META


def threshold():
    _, meta = _load()
    return meta.get("threshold") if meta else None


def score_setup(hist, setup, regime, vix_regime, is_leveraged):
    """Return P(win) for one setup, or None if the model is unavailable."""
    model, _ = _load()
    if model is None:
        return None
    try:
        import numpy as np
        from features import compute_features, feature_vector
        feats = compute_features(hist, setup, regime, vix_regime, is_leveraged)
        if feats is None:
            return None
        x = np.array([feature_vector(feats)], dtype=float)
        return round(float(model.predict_proba(x)[0, 1]), 4)
    except Exception:
        return None


def attach_meta(setups, bars_map, regime, vix_regime, cfg):
    """Mutate each setup in-place, adding `meta_prob` (or None). `bars_map` must be
    the SAME (partial-bar-trimmed) bars used for scoring, so bars_map[ticker][-1]
    is the signal bar. Returns the count of setups scored."""
    model, _ = _load()
    if model is None:
        for s in setups:
            s["meta_prob"] = None
        return 0
    leveraged = set(cfg.get("leveraged_tickers", []))
    scored = 0
    for s in setups:
        # The model is trained on LONG setups only; scoring a bear signal is
        # out-of-distribution and meaningless. Bears are never traded anyway.
        hist = bars_map.get(s["ticker"])
        if s.get("direction") != "bull" or not hist:
            s["meta_prob"] = None
            continue
        prob = score_setup(hist, s, regime, vix_regime, s["ticker"] in leveraged)
        s["meta_prob"] = prob
        scored += prob is not None
    return scored
