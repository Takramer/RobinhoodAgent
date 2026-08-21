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
import json
import os
import statistics

LOG_PATH = "setup_log.csv"
BACKTEST_PATH = "regime_backtest.json"

# Outcome reasons that represent a real resolved trade. "Stale" rows are closed
# feeds, not results, and must never vote on whether a strategy works.
REAL_OUTCOMES = ("TP", "SL", "Time")

DEFAULTS = {
    # Distinct signal DAYS, not trades. Trades overlap; days approximate the
    # independent market moves actually observed.
    "selector_min_sample": 30,
    # t-statistic on the mean excess return. 2.0 ≈ 95% one-sided confidence that
    # the edge is not zero. Raise it to trade less and be surer.
    "selector_min_t": 2.0,
    # Total resolved rows below which the log cannot judge anything and the
    # static engine map is used instead.
    "selector_cold_start_rows": 200,
    # Allow a pairing backed only by regime_backtest.json when live days are
    # still too few. Set false to require live evidence for everything, which
    # means trading nothing until a pairing somehow accumulates days it can
    # only accumulate by trading.
    "selector_use_backtest": True,
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
            out.append((r.get("spy_regime", ""), r.get("strategy", ""),
                        excess, r.get("signal_date", "")))
    return out


def cluster_stats(pairs):
    """
    Mean excess and a t-statistic that survives overlapping trades.

    Trades are not independent draws. Hundreds fire on the same day across the
    same universe and ride the same market move, so treating each as its own
    observation inflates the sample by an order of magnitude and the t-statistic
    by its square root. That is how the same pairing reads t=+5.5 on one window
    and t=-5.6 on another: neither number was ever real.

    Collapse every trade sharing a signal date into one observation, then test
    the daily series. Effective n becomes distinct days, which is roughly the
    number of independent market moves actually observed.

    `pairs` is an iterable of (signal_date, excess). Returns None when fewer
    than two distinct days are present — a single day cannot have a spread.
    """
    by_day = {}
    for day, excess in pairs:
        by_day.setdefault(day, []).append(excess)
    if len(by_day) < 2:
        return None

    daily = [statistics.mean(v) for v in by_day.values()]
    flat = [e for v in by_day.values() for e in v]
    mean = statistics.mean(daily)
    stderr = statistics.stdev(daily) / (len(daily) ** 0.5)
    return {
        "n": len(flat),
        "n_days": len(daily),
        "mean": round(mean, 4),
        "median": round(statistics.median(daily), 4),
        "stderr": round(stderr, 4),
        "t": round(mean / stderr, 2) if stderr else 0.0,
        "beat_rate": round(100 * sum(1 for v in flat if v > 0) / len(flat), 1),
    }


def score_strategies(results, regime):
    """
    Per-strategy stats for one regime, best mean first.

    Returns dicts of strategy/n/mean/stderr/t/beat_rate. A strategy with fewer
    than two samples has no computable spread and is dropped — one lucky trade
    is not evidence.
    """
    by_strategy = {}
    for r_regime, strategy, excess, day in results:
        if r_regime == regime:
            by_strategy.setdefault(strategy, []).append((day, excess))

    scored = []
    for strategy, pairs in by_strategy.items():
        st = cluster_stats(pairs)
        if st:
            scored.append({"strategy": strategy, **st})
    return sorted(scored, key=lambda s: -s["mean"])


def backtest_qualified(regime, path=BACKTEST_PATH):
    """
    Strategy names this regime may trade on backtest evidence alone.

    Live results cannot bootstrap a pairing the static map never routed: with
    no trades there are no days, with no days the gate never opens, and the
    pairing stays untested forever. regime_backtest.py breaks that by scoring
    every pairing on cached history.

    Only cells marked `qualified` count — positive mean excess in BOTH halves
    of the window, at least 30 distinct days, pooled t >= 2.0. Sign stability
    across an unseen half is a weaker claim than a clean holdout t, and it is
    the reason a pairing promoted here should still be sized small and watched
    on live results, which overrule this the moment they are thick enough.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for key, stats in (data.get("results") or {}).items():
        if not stats or not stats.get("qualified"):
            continue
        name, _, cell_regime = key.partition("|")
        if cell_regime == regime:
            out[name] = stats
    return out


def select(cfg, regime, log_path=LOG_PATH, static_fallback=None,
           backtest_path=BACKTEST_PATH):
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
                if s["n_days"] >= min_n and s["t"] >= min_t]

    # Live evidence is authoritative where it exists. A pairing the live log has
    # already judged thick enough is never rescued by a backtest — a backtest
    # that disagrees with realized fills is the one that is wrong.
    judged = {s["strategy"] for s in ranked if s["n_days"] >= min_n}
    from_backtest = []
    if _cfg(cfg, "selector_use_backtest"):
        for name, stats in backtest_qualified(regime, backtest_path).items():
            if name not in judged and name not in selected:
                from_backtest.append(name)
                report.setdefault("backtest_promoted", []).append({
                    "strategy": name,
                    "mean": stats.get("mean_excess"),
                    "t": stats.get("t"),
                    "days": stats.get("n_days"),
                })

    report["live_selected"] = list(selected)

    # A pairing promoted on backtest while its own live rows lean the other way
    # is the single most dangerous state this module can produce, so say so.
    # 19 days is below the bar to overrule, but it is not nothing, and a silent
    # promotion over contrary live evidence is how a bad strategy gets armed.
    live_by_name = {s["strategy"]: s for s in ranked}
    for name in from_backtest:
        live = live_by_name.get(name)
        if live and live["mean"] < 0:
            report.setdefault("conflicts", []).append({
                "strategy": name,
                "live_mean": live["mean"],
                "live_days": live["n_days"],
                "live_t": live["t"],
            })

    selected = selected + sorted(from_backtest)
    report["selected"] = selected

    if selected:
        parts = [f"{s['strategy']} {s['mean']:+.2f}% "
                 f"({s['n_days']} days, t={s['t']}) live"
                 for s in ranked if s["strategy"] in selected]
        parts += [f"{b['strategy']} {b['mean']:+.2f}% "
                  f"({b['days']} days, t={b['t']}) backtest"
                  for b in report.get("backtest_promoted", [])]
        report["reason"] = "; ".join(parts)
    else:
        best = ranked[0]
        report["reason"] = (
            f"no strategy clears {min_n} days and t>={min_t} in '{regime}'; "
            f"best was {best['strategy']} {best['mean']:+.2f}% "
            f"({best['n_days']} days, t={best['t']})"
        )
    return selected, report


def format_report(report):
    """One-screen summary for session logs."""
    lines = [f"  Strategy selector — regime '{report['regime']}', "
             f"{report['total_resolved']} resolved setups"]
    for s in report["ranked"]:
        # Only live evidence earns a live PASS. A backtest promotion is
        # printed on its own line so a negative live row never reads as
        # approved.
        ok = "PASS" if s["strategy"] in report.get("live_selected", []) else "  --"
        lines.append(
            f"    {ok}  {s['strategy']:<18} n={s['n']:<6} "
            f"days={s['n_days']:<5} mean={s['mean']:+6.2f}%  "
            f"t={s['t']:+6.2f}  beat={s['beat_rate']:.0f}%"
        )
    for b in report.get("backtest_promoted", []):
        lines.append(f"    PASS  {b['strategy']:<18} (backtest) "
                     f"days={b['days']:<5} mean={b['mean']:+6.2f}%  "
                     f"t={b['t']:+6.2f}")
    for c in report.get("conflicts", []):
        lines.append(
            f"    WARN  {c['strategy']} promoted on backtest while live is "
            f"{c['live_mean']:+.2f}% over {c['live_days']} days "
            f"(t={c['live_t']}) — size it small, live overrules at "
            f"{report.get('min_sample')} days")
    lines.append(f"    -> {report['reason']}")
    return "\n".join(lines)


def demo():
    """Self-check: the bar must reject weak, negative, and thin evidence."""
    cfg = {"strategy": {"selector_cold_start_rows": 4}}

    # A clearly positive strategy passes; a clearly negative one does not, even
    # with a far larger sample. Sample size must not launder a negative mean.
    # Distinct days matter now, so give each observation its own date.
    def rows(regime, strategy, vals, start=1):
        return [(regime, strategy, v, f"2026-01-{start + i:02d}")
                for i, v in enumerate(vals)]

    good = rows("choppy", "bollinger", [3.0, 2.0, 4.0, 2.5, 3.5, 2.8])
    bad = rows("choppy", "pead", [-2.0, -3.0, -1.5, -2.5, -2.2, -2.8])
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
    thin = rows("uptrend", "macd", [9.0, 8.0, 10.0])
    st = score_strategies(thin, "uptrend")[0]
    assert st["n_days"] == 3 and st["t"] > 2, st
    assert st["n_days"] < DEFAULTS["selector_min_sample"], "thin sample must fail"

    # A single day has no computable spread and must be dropped outright.
    assert score_strategies(rows("uptrend", "vwap", [5.0]), "uptrend") == []

    # The point of clustering: 500 trades on two days is two observations, not
    # 500, and must not manufacture significance out of one market move.
    same_day = [("uptrend", "x", 1.0 + (i % 3) * 0.01, "2026-02-01")
                for i in range(250)]
    same_day += [("uptrend", "x", 1.0 + (i % 3) * 0.01, "2026-02-02")
                 for i in range(250)]
    st = score_strategies(same_day, "uptrend")[0]
    assert st["n"] == 500 and st["n_days"] == 2, st
    assert st["n_days"] < DEFAULTS["selector_min_sample"], "must fail on days"

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
