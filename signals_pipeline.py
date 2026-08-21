"""
signals_pipeline.py — Stage 1 signal generation (app-side orchestration)

Fetches the configured watchlist, classifies market regime, runs the shared
engine (rhengine) across it, attaches meta-labeling P(win), and writes
signals.json for order_plan.py. Extracted from indicators.py when the pure
engine moved to the rhengine package (./engine submodule).
"""

import argparse
import json
import os
from datetime import datetime, timezone

from rhengine.indicators import (
    drop_partial_bar,
    classify_spy_regime,
    estimate_vix_regime,
    compute_trend_filter,
    compute_realized_vol,
    compute_vol_target_mult,
    select_strategy_for_regime,
    resolve_thresholds,
    score_all_tickers,
)

def build_signals_payload(cfg, verbose=True):
    """
    Fetch the configured watchlist and build the production signals payload.

    This is the Phase 1 -> Phase 2 handoff object. It intentionally contains
    signal data only; account state, cash checks, and position sizing belong in
    order_plan.py.
    """
    from rhengine.market_data import fetch_bars_batch

    watchlist = cfg["watchlist"]
    spy_ticker = cfg.get("spy_ticker", "SPY")
    interval = cfg["strategy"].get("interval", "1d")
    lookback_days = cfg["strategy"].get("lookback_days", 90)
    min_score = cfg["strategy"].get("min_score", 4)

    tickers_to_fetch = list(dict.fromkeys(watchlist + [spy_ticker]))
    if verbose:
        print(f"Fetching bars for {len(tickers_to_fetch)} configured symbols...")

    result = fetch_bars_batch(
        tickers_to_fetch,
        interval=interval,
        lookback_days=lookback_days,
        verbose=verbose,
    )
    bars_map = dict(result["bars"])
    spy_bars = bars_map.pop(spy_ticker, None)

    if not spy_bars:
        raise RuntimeError(f"{spy_ticker} bars are required to classify market regime")

    # Score on the last completed session, never today's partial bar (see
    # drop_partial_bar). Applies to the watchlist and to SPY (regime + VIX).
    bars_map = {t: drop_partial_bar(b) for t, b in bars_map.items()}
    spy_bars = drop_partial_bar(spy_bars)

    # Drop tickers whose data has gone stale. A feed gap does not look like an
    # error downstream — indicators compute happily on a frozen series and the
    # same setup re-fires at the same price every session until someone reads
    # the log. JHG sat at $51.95 for eleven sessions this way. SPY is the
    # freshness anchor: it is the one symbol guaranteed to be current, so
    # anything trailing it by more than max_bar_staleness_sessions is out.
    max_stale = cfg["strategy"].get("max_bar_staleness_sessions", 3)
    if spy_bars:
        spy_last = spy_bars[-1]["time"]
        fresh, stale = {}, []
        for t, b in bars_map.items():
            if not b:
                stale.append((t, "no bars"))
                continue
            # Bars are daily; count sessions by position in SPY's own series.
            behind = sum(1 for x in spy_bars if x["time"] > b[-1]["time"])
            if behind > max_stale:
                stale.append((t, f"{behind} sessions behind"))
            else:
                fresh[t] = b
        bars_map = fresh
        if stale and verbose:
            names = ", ".join(f"{t} ({why})" for t, why in sorted(stale)[:8])
            more = f" +{len(stale) - 8} more" if len(stale) > 8 else ""
            print(f"  Stale data, skipped {len(stale)}: {names}{more}")

    # Longer SPY history for the 200-day trend filter and realized-vol targeting.
    # The batch fetch is only ~90d; fetch SPY separately and use it everywhere it
    # helps (regime/VIX work with any length >= 55). Falls back to batch SPY.
    try:
        from rhengine.market_data import fetch_bars as _fetch_one
        spy_long = drop_partial_bar(_fetch_one(spy_ticker, interval=interval, lookback_days=380))
        if spy_long and len(spy_long) >= 60:
            spy_bars = spy_long
    except Exception as exc:
        print(f"WARNING: Alpaca long SPY fetch failed ({exc}); trying Massive cache")
        try:
            from rhengine.market_data import fetch_bars_batch_massive
            m_out = fetch_bars_batch_massive([spy_ticker], lookback_days=380,
                                             verbose=False)
            spy_long = drop_partial_bar(m_out.get("bars", {}).get(spy_ticker, []))
            if spy_long and len(spy_long) >= 60:
                spy_bars = spy_long
        except Exception as exc2:
            print(f"WARNING: Massive long SPY fetch also failed ({exc2}); "
                  "trend filter will fail closed on short history")

    spy_regime  = classify_spy_regime(spy_bars)
    vix_regime  = estimate_vix_regime(spy_bars)

    # Market overlays (Sharpe levers): crash-avoidance trend filter + vol targeting.
    trend_filter_on = cfg.get("market_trend_filter", True)
    market_risk_off, spy_sma200 = compute_trend_filter(spy_bars)
    if trend_filter_on and spy_sma200 is None:
        # Fail closed: if the 380d SPY fetch fell back to short history and the
        # 200-day SMA can't be computed, treat the market as risk-off rather than
        # silently disabling the crash-avoidance filter.
        print("WARNING: SPY 200-day SMA unavailable (short history) — "
              "trend filter failing CLOSED, new entries blocked this session")
        market_risk_off = True
    market_risk_off = bool(trend_filter_on and market_risk_off)
    target_vol = cfg.get("account", {}).get("target_annual_vol")
    vol_bounds = tuple(cfg.get("vol_target_bounds", [0.5, 1.5]))
    vol_target_mult, realized_vol = (
        compute_vol_target_mult(spy_bars, target_vol, vol_bounds)
        if target_vol else (1.0, compute_realized_vol(spy_bars))
    )
    # Strategy choice. In "auto" mode this is decided by realized results in
    # setup_log.csv rather than the static regime table: a strategy trades only
    # while its own live excess-return record in this regime clears the sample
    # and t-statistic bar. An empty selection is a real answer and means place
    # no entries — see strategy_selector. Naming a strategy in config bypasses
    # the whole test, which is what manual override is for.
    import strategy_selector

    configured_strategy = cfg["strategy"].get("mode", "auto")
    if configured_strategy == "auto":
        strategies, selector_report = strategy_selector.select(
            cfg, spy_regime,
            static_fallback=select_strategy_for_regime(spy_regime),
        )
    else:
        strategies = [configured_strategy]
        selector_report = {
            "regime": spy_regime, "ranked": [], "selected": strategies,
            "total_resolved": 0, "cold_start": False,
            "reason": f"manual override: strategy.mode={configured_strategy}",
        }

    strategy_used = strategies[0] if strategies else None
    strategy_blocked = not strategies
    thresholds = resolve_thresholds(cfg, strategy_used) if strategy_used else {}

    if verbose:
        print(strategy_selector.format_report(selector_report))

    # Block all new entries in fear_spiking / panic regardless of strategy
    vix_blocked = vix_regime in ("fear_spiking", "panic")

    # Run scoring at min_score=3. Both 3/4 and 4/4 setups qualify for trading;
    # the meta-model P(win) filter in order_plan.py is the real quality gate
    # (purged-OOS backtest: meta-filtered 3/4+4/4 pool beats 4/4-only).
    # Every selected strategy runs, then the pools merge and dedupe by
    # (ticker, direction) keeping the stronger setup. PEAD used to be hardwired
    # on here as a regime-agnostic sleeve; it now earns its place through the
    # same test as everything else, because "always on" and "measured negative
    # in every regime it has traded" cannot both be right.
    run_strategies = [] if (vix_blocked or strategy_blocked) else list(strategies)
    if cfg.get("pead_enabled", True) and "pead" in (selector_report.get("selected") or []):
        if "pead" not in run_strategies:
            run_strategies.append("pead")

    pools = []
    for name in run_strategies:
        pools.extend(score_all_tickers(
            bars_map,
            name,
            resolve_thresholds(cfg, name),
            min_score=3,
            spy_bars=spy_bars,
            bear_regime_only=cfg.get("bear_regime_only", []),
            spy_regime=spy_regime,
        ))

    best = {}
    for s in pools:
        k = (s["ticker"], s["direction"])
        cur = best.get(k)
        if cur is None or (s["score"], s.get("strength", 0)) > (cur["score"], cur.get("strength", 0)):
            best[k] = s
    all_setups = sorted(best.values(),
                        key=lambda s: (s["score"], s.get("weighted_score", 0), s.get("strength", 0.0)),
                        reverse=True)

    qualified_setups = [s for s in all_setups if s["score"] >= min_score]
    # candidates = below the trade gate (logged, not traded). Empty when the gate
    # is at the scoring floor (min_score=3) — 3/4 are now tradeable, meta-gated.
    candidate_setups = [s for s in all_setups if s["score"] < min_score]

    # Meta-labeling: attach P(win) from the trained model (advisory — the model's
    # OOS AUC is modest, so it re-ranks and can optionally filter, but the score
    # gate still governs). No-op if metamodel.joblib / sklearn are absent.
    try:
        from metascore import attach_meta
        attach_meta(qualified_setups, bars_map, spy_regime, vix_regime, cfg)
        attach_meta(candidate_setups, bars_map, spy_regime, vix_regime, cfg)
        # Re-rank qualified so the portfolio prefers higher-P(win) setups first.
        if any(s.get("meta_prob") is not None for s in qualified_setups):
            qualified_setups.sort(
                key=lambda s: (s["score"], s.get("meta_prob") if s.get("meta_prob") is not None else -1,
                               s.get("weighted_score", 0), s.get("strength", 0.0)),
                reverse=True,
            )
    except Exception:
        pass

    # Size multiplier driven by VIX regime — order_plan.py applies this
    vix_size_mult = {
        "calm":          1.0,
        "normal":        1.0,
        "fear_elevated": 0.5,
        "fear_spiking":  0.0,
        "panic":         0.0,
        "unknown":       0.75,
    }.get(vix_regime, 0.75)

    now = datetime.now(timezone.utc)
    return {
        "generated_at":   now.isoformat(),
        "session_date":   now.date().isoformat(),
        "spy_ticker":     spy_ticker,
        "spy_regime":     spy_regime,
        "vix_regime":     vix_regime,
        "vix_size_mult":  vix_size_mult,
        "vix_blocked":    vix_blocked,
        "market_risk_off":   market_risk_off,
        "spy_sma200":        round(spy_sma200, 2) if spy_sma200 else None,
        "realized_vol":      round(realized_vol, 4) if realized_vol else None,
        "vol_target_mult":   round(vol_target_mult, 3),
        "strategy_mode":  configured_strategy,
        "strategy_used":  strategy_used,
        "strategies_run":     run_strategies,
        "strategy_blocked":   strategy_blocked,
        "strategy_selector":  selector_report,
        "thresholds":     thresholds,
        "interval":       interval,
        "lookback_days":  lookback_days,
        "min_score":      min_score,
        "watchlist_count":  len(watchlist),
        "symbols_fetched":  len(result["bars"]),
        "qualified_setups": qualified_setups,
        "candidate_setups": candidate_setups,
        "data_errors":      result["errors"],
    }


def write_signals(output_path="signals.json", config_path="config.json", verbose=True):
    """Build and write signals.json for the order planning step."""
    with open(config_path) as f:
        cfg = json.load(f)

    payload = build_signals_payload(cfg, verbose=verbose)
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    if verbose:
        print(
            f"\nWrote {output_path}: {len(payload['qualified_setups'])} qualifying setup(s), "
            f"SPY regime {payload['spy_regime']}, strategy {payload['strategy_used']}"
        )
    return payload


def run_smoke_test():
    from rhengine.market_data import fetch_bars_batch

    print("Loading config...")
    with open("config.json") as f:
        cfg = json.load(f)

    tickers    = cfg["watchlist"][:5]   # test on first 5 only
    interval   = cfg["strategy"]["interval"]

    print(f"Fetching bars for: {tickers}")
    result   = fetch_bars_batch(tickers + ["SPY"], interval=interval, lookback_days=90)
    bars_map = {t: drop_partial_bar(b) for t, b in result["bars"].items()}
    spy_bars = bars_map.pop("SPY", None)

    regime   = classify_spy_regime(spy_bars) if spy_bars else "choppy"
    strategy = select_strategy_for_regime(regime)
    thresholds = resolve_thresholds(cfg, strategy)
    print(f"\nSPY regime: {regime} -> strategy: {strategy}")

    setups = score_all_tickers(
        bars_map, strategy, thresholds,
        min_score=3,           # lower threshold for smoke test
        spy_bars=spy_bars,
        bear_regime_only=cfg["bear_regime_only"],
        spy_regime=regime,
    )

    if not setups:
        print("\nNo setups found on this sample.")
    else:
        print(f"\n{len(setups)} setup(s) found:\n")
        for s in setups:
            legs_passed = [l["name"] for l in s["legs"] if l["pass"]]
            print(f"  {s['ticker']:6s} {s['direction']:4s}  score={s['score']}/4"
                  f"  entry=${s['entry']:.2f}  stop=${s['stop']:.2f}"
                  f"  target=${s['target']:.2f}")
            print(f"         legs: {', '.join(legs_passed)}")


# ─────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(os.path.dirname(__file__))

    parser = argparse.ArgumentParser(description="Score strategy signals via the shared engine.")
    parser.add_argument(
        "--write-signals",
        action="store_true",
        help="score the full configured watchlist and write signals.json",
    )
    parser.add_argument(
        "--output",
        default="signals.json",
        help="output path for --write-signals (default: signals.json)",
    )
    args = parser.parse_args()

    if args.write_signals:
        write_signals(args.output)
    else:
        run_smoke_test()
