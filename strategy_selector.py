"""
strategy_selector.py — pick strategies from realized results, not a static map.

`select_strategy_for_regime` in the engine is a fixed regime→strategy table
written from backtests. Backtests are a forecast; setup_log.csv is the record of
what actually happened. This module reads that record and lets a strategy trade
only while its own live results in the current regime say it earns more than
holding SPY over the same days.

The test is deliberately one-sided. A strategy is guessed innocent by nobody: it
must clear a minimum sample AND put the lower bound of its mean excess return
above zero before it may place an order. "No strategy qualifies" is a valid and
frequent answer, and it means trade nothing — a flat day costs a spread, a bad
strategy costs the account.

Excess return, not R-multiples, is the input. R says the target was hit before
the stop. It cannot say the money would have done better sitting in SPY, and
that is the only comparison that decides whether running this at all is worth
the risk.

Cold start: with too little history to judge, fall back to the static engine map
so a fresh install is not frozen out forever.
"""

import csv
import os
import statistics

LOG_PATH = "setup_log.csv"

# Outcome reasons that represent a real resolved trade. "Stale" rows are closed
# feeds, not results, and must never vote on whether a strategy works.
REAL_OUTCOMES = ("TP", "SL", "Time")

DEFAULTS = {
    # Below this many resolved setups a strategy has not earned an opinion.
    "selector_min_sample": 30,
    # t-statistic on the mean excess return. 2.0 ≈ 95% one-sided confidence that
    # the edge is not zero. Raise it to trade less and be surer.
    "selector_min_t": 2.0,
    # Total resolved rows below which the log cannot judge anything and the
    # static engine map is used instead.
    "selector_cold_start_rows": 200,
}


def _cfg(cfg, key):
    return cfg.get("strategy", {}).get(key, DEFAULTS[key])


def load_results(log_path=LOG_PATH):
    """Resolved setups carrying a benchmark score, as (regime, strategy, excess)."""
    if not os.path.exists(log_path):
        return []
    out = []
    with open(log_path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("outcome_reason") not in REAL_OUTCOMES:
                continue
            try:
                excess = float(r["excess_pct"])
            except (ValueError, KeyError, TypeError):
                continue
            out.append((r.get("spy_regime", ""), r.get("strategy", ""), excess))
    return out


def score_strategies(results, regime):
    """
    Per-strategy stats for one regime, best mean first.

    Returns dicts of strategy/n/mean/stderr/t/beat_rate. A strategy with fewer
    than two samples has no computable spread and is dropped — one lucky trade
    is not evidence.
    """
    by_strategy = {}
    for r_regime, strategy, excess in results:
        if r_regime == regime:
            by_strategy.setdefault(strategy, []).append(excess)

    scored = []
    for strategy, vals in by_strategy.items():
        if len(vals) < 2:
            continue
        mean = statistics.mean(vals)
        stderr = statistics.stdev(vals) / (len(vals) ** 0.5)
        scored.append({
            "strategy": strategy,
            "n": len(vals),
            "mean": round(mean, 4),
            "stderr": round(stderr, 4),
            # Guard the degenerate case where every outcome was identical.
            "t": round(mean / stderr, 2) if stderr else 0.0,
            "beat_rate": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1),
        })
    return sorted(scored, key=lambda s: -s["mean"])


def select(cfg, regime, log_path=LOG_PATH, static_fallback=None):
    """
    Choose the strategies allowed to trade this session.

    Returns (strategies, report). `strategies` is ranked best-first and is empty
    when nothing clears the bar — the caller must treat that as "place no
    entries", not as "use the default".
    """
    min_n = _cfg(cfg, "selector_min_sample")
    min_t = _cfg(cfg, "selector_min_t")
    cold = _cfg(cfg, "selector_cold_start_rows")

    results = load_results(log_path)
    report = {
        "regime": regime,
        "total_resolved": len(results),
        "min_sample": min_n,
        "min_t": min_t,
        "cold_start": False,
        "ranked": [],
        "selected": [],
        "reason": "",
    }

    if len(results) < cold:
        report["cold_start"] = True
        report["reason"] = (
            f"only {len(results)} resolved setups (need {cold}) — "
            f"falling back to the static regime map"
        )
        report["selected"] = [static_fallback] if static_fallback else []
        return list(report["selected"]), report

    ranked = score_strategies(results, regime)
    report["ranked"] = ranked

    if not ranked:
        report["reason"] = f"no resolved history for regime '{regime}'"
        return [], report

    selected = [s["strategy"] for s in ranked
                if s["n"] >= min_n and s["t"] >= min_t]
    report["selected"] = selected

    if selected:
        report["reason"] = "; ".join(
            f"{s['strategy']} +{s['mean']:.2f}% (n={s['n']}, t={s['t']})"
            for s in ranked if s["strategy"] in selected
        )
    else:
        best = ranked[0]
        report["reason"] = (
            f"no strategy clears n>={min_n} and t>={min_t} in '{regime}'; "
            f"best was {best['strategy']} {best['mean']:+.2f}% "
            f"(n={best['n']}, t={best['t']})"
        )
    return selected, report


def format_report(report):
    """One-screen summary for session logs."""
    lines = [f"  Strategy selector — regime '{report['regime']}', "
             f"{report['total_resolved']} resolved setups"]
    for s in report["ranked"]:
        ok = "PASS" if s["strategy"] in report["selected"] else "  --"
        lines.append(
            f"    {ok}  {s['strategy']:<18} n={s['n']:<6} "
            f"mean={s['mean']:+6.2f}%  t={s['t']:+6.2f}  beat={s['beat_rate']:.0f}%"
        )
    lines.append(f"    -> {report['reason']}")
    return "\n".join(lines)


def demo():
    """Self-check: the bar must reject weak, negative, and thin evidence."""
    cfg = {"strategy": {"selector_cold_start_rows": 4}}

    # A clearly positive strategy passes; a clearly negative one does not, even
    # with a far larger sample. Sample size must not launder a negative mean.
    good = [("choppy", "bollinger", v) for v in [3.0, 2.0, 4.0, 2.5, 3.5, 2.8]]
    bad = [("choppy", "pead", v) for v in [-2.0, -3.0, -1.5, -2.5, -2.2, -2.8] * 5]
    cfg["strategy"]["selector_min_sample"] = 5
    picked, rep = select(cfg, "choppy", log_path="/nonexistent")
    assert rep["cold_start"] and picked == [], "missing log must cold-start"

    scored = score_strategies(good + bad, "choppy")
    assert scored[0]["strategy"] == "bollinger", scored
    assert scored[0]["t"] > 2, scored
    assert scored[1]["t"] < -2, scored

    # Regime isolation: choppy results must not vote on an uptrend session.
    assert score_strategies(good + bad, "uptrend") == []

    # Thin evidence is rejected on sample size even when the mean looks great.
    thin = [("uptrend", "macd", v) for v in [9.0, 8.0, 10.0]]
    s = score_strategies(thin, "uptrend")[0]
    assert s["n"] == 3 and s["t"] > 2, s
    assert s["n"] < DEFAULTS["selector_min_sample"], "thin sample must fail the gate"

    # A single sample has no computable spread and must be dropped outright.
    assert score_strategies([("uptrend", "vwap", 5.0)], "uptrend") == []

    print("strategy_selector: self-check passed")


if __name__ == "__main__":
    import json
    import sys

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if "--demo" in sys.argv:
        demo()
        raise SystemExit(0)

    with open("config.json") as f:
        cfg = json.load(f)
    regime = sys.argv[1] if len(sys.argv) > 1 else "uptrend"
    picked, report = select(cfg, regime)
    print(format_report(report))
    print(f"\nselected: {picked or 'NONE — place no entries'}")
