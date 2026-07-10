"""
optimize.py — parameter search for the Nydex strategies, ranked by out-of-sample
expectancy (avg R per trade), NOT win rate or R:R alone.

Win rate and reward:risk trade off against each other; their product (avg R) is
what compounds the account, so that is the objective. Guardrails reject lucky
spikes: a minimum trade count in EACH of the in-sample / out-of-sample halves, a
profit-factor floor, and a positive OOS result (consistency, not just IS).

Method: fetch bars ONCE, split history by date (older 60% in-sample, newer 40%
out-of-sample), evaluate configs in memory. Each strategy is searched in two
stages of coordinate descent — structure first, then exits — to keep the grid
small and legible rather than a giant joint sweep that invites overfitting. Each
strategy is evaluated only on the regime(s) it is actually routed to.

Run:
    python3 optimize.py                 # all three: macd, bollinger, residual
    python3 optimize.py --strategy bollinger
"""

import argparse
import copy
import itertools
import json
import os

from rhengine.backtest import load_bars, run_backtest_on, _stats, _iso_date


# strategy -> (regime(s) it is evaluated on, live min_score, structure axes, exit axes)
GRIDS = {
    "macd": {
        "regime": "uptrend",
        "min_score": 4,
        "structure": {
            "secondLeg": ["sub_zero", "hist_rising"],
            "crossLookback": [1, 2, 3],
            "rsiLow": [45, 50, 40], "rsiHigh": [75, 70, 80],  # paired below
        },
        "structure_pairs": [("rsiLow", "rsiHigh", [(45, 75), (50, 70), (40, 80)])],
        "exits": {"stopAtrMult": [1.5, 2.0, 2.5], "targetAtrMult": [2.0, 2.5, 3.0, 3.5]},
    },
    "bollinger": {
        "regime": "choppy",
        "min_score": 3,   # mean-reversion in choppy gives fewer 4/4 — use 3 for a stable sample
        "structure": {
            "rsiLow": [30, 35, 40],     # oversold trigger for bull
            "adxMax": [25, 30, 35],     # hard trend gate
            "volMult": [1.0, 1.1, 1.2],
        },
        "structure_pairs": [],
        # target stays the mid-band; targetAtrMult is only the fallback, so sweep the stop
        "exits": {"stopAtrMult": [1.25, 1.5, 2.0, 2.5], "targetAtrMult": [1.5]},
    },
    "residualmomentum": {
        "regime": ["uptrend", "downtrend"],
        "min_score": 4,
        "structure": {
            "minResidualRA": [0.10, 0.15, 0.20, 0.25],
            "volMult": [1.0, 1.1],
        },
        "structure_pairs": [],
        "exits": {"stopAtrMult": [1.5, 2.0, 2.5], "targetAtrMult": [2.0, 2.5, 3.0, 3.5]},
    },
}


def split_cutoff(spy_bars, frac=0.6):
    dates = sorted(_iso_date(b["time"]) for b in spy_bars)
    return dates[int(len(dates) * frac)]


def eval_config(base_cfg, strategy, overrides, bars_map, spy_bars, cutoff,
                min_score, regime):
    cfg = copy.deepcopy(base_cfg)
    cfg["strategy"].setdefault("thresholds", {}).setdefault(strategy, {})
    cfg["strategy"]["thresholds"][strategy].update(overrides)
    is_tr = run_backtest_on(cfg, bars_map, spy_bars, min_score=min_score,
                            forced_strategy=strategy, regime_only=regime, date_hi=cutoff)
    oos_tr = run_backtest_on(cfg, bars_map, spy_bars, min_score=min_score,
                             forced_strategy=strategy, regime_only=regime, date_lo=cutoff)
    return _stats(is_tr), _stats(oos_tr)


def _combos(axes, pairs):
    """Cartesian product of independent axes, honoring any tied pairs (e.g. rsiLow
    must move with rsiHigh)."""
    paired_keys = {k for (a, b, _) in pairs for k in (a, b)}
    indep = {k: v for k, v in axes.items() if k not in paired_keys}
    keys = list(indep)
    base = [dict(zip(keys, vals)) for vals in itertools.product(*(indep[k] for k in keys))] or [{}]
    out = []
    for combo in base:
        seeds = [dict(combo)]
        for (a, b, options) in pairs:
            seeds = [{**s, a: av, b: bv} for s in seeds for (av, bv) in options]
        out.extend(seeds)
    return out


def _fmt(st):
    if not st:
        return f"{'—':>5} {'—':>5} {'—':>6} {'—':>6}"
    pf = "inf" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
    return f"{st['n']:>5} {st['win_rate']*100:>4.0f}% {st['avg_r']:>+6.2f} {pf:>6}"


def _label(ov):
    return " ".join(f"{k}={v}" for k, v in ov.items())


def rank(rows, min_is_n, min_oos_n, min_pf):
    keep = [r for r in rows if r["is"] and r["oos"]
            and r["is"]["n"] >= min_is_n and r["oos"]["n"] >= min_oos_n
            and r["oos"]["profit_factor"] >= min_pf and r["oos"]["avg_r"] > 0]
    keep.sort(key=lambda r: r["oos"]["avg_r"], reverse=True)
    return keep


def print_table(title, rows, top=10):
    print(f"\n{title}")
    print(f"  {'config':<46} | {'IS   n  win  avgR   PF':>23} | {'OOS  n  win  avgR   PF':>23}")
    print("  " + "-" * 98)
    for r in rows[:top]:
        print(f"  {r['label']:<46} | {_fmt(r['is'])} | {_fmt(r['oos'])}")


def optimize_strategy(name, cfg, bars_map, spy_bars, cutoff,
                      min_is_n, min_oos_n, min_pf, min_score_override=None):
    spec = GRIDS[name]
    regime = spec["regime"]
    min_score = min_score_override or spec["min_score"]
    tset = cfg["strategy"].get("thresholds", {}).get(name, {})

    print("\n" + "#" * 70)
    reg_txt = regime if isinstance(regime, str) else "+".join(regime)
    print(f"# OPTIMIZE {name}   (regime={reg_txt}, min_score={min_score})")
    print("#" * 70)

    # ---- Stage A: structure, exits fixed at current config ----
    base_exit = {k: tset.get(k) for k in spec["exits"] if k in tset}
    struct_combos = _combos(spec["structure"], spec["structure_pairs"])
    stage_a = []
    for combo in struct_combos:
        ov = {**base_exit, **combo}
        iss, oos = eval_config(cfg, name, ov, bars_map, spy_bars, cutoff, min_score, regime)
        stage_a.append({"label": _label(combo), "overrides": ov, "is": iss, "oos": oos})
    ranked_a = rank(stage_a, min_is_n, min_oos_n, min_pf)
    print_table("STAGE A — structure (exits fixed at current):", ranked_a)
    if not ranked_a:
        print(f"  none cleared guardrails (IS n>={min_is_n}, OOS n>={min_oos_n}, "
              f"OOS PF>={min_pf}); showing best-effort by OOS avg R:")
        stage_a.sort(key=lambda r: (r["oos"]["avg_r"] if r["oos"] else -9), reverse=True)
        print_table("  (guardrails relaxed)", stage_a)
    best_struct = (ranked_a[0] if ranked_a else stage_a[0])["overrides"]

    # ---- Stage B: exits, structure fixed at Stage A winner ----
    exit_combos = _combos(spec["exits"], [])
    stage_b = []
    for combo in exit_combos:
        ov = {**best_struct, **combo}
        iss, oos = eval_config(cfg, name, ov, bars_map, spy_bars, cutoff, min_score, regime)
        stage_b.append({"label": _label(combo), "overrides": ov, "is": iss, "oos": oos})
    ranked_b = rank(stage_b, min_is_n, min_oos_n, min_pf)
    print_table("STAGE B — exits (structure fixed at Stage A winner):", ranked_b)

    pool = ranked_b or ranked_a
    if not pool:
        print(f"\n>> {name}: no config cleared guardrails — sample too thin to tune with confidence.")
        return None
    best = pool[0]
    print(f"\n>> RECOMMENDED {name} (best OOS expectancy within guardrails):")
    print("   " + json.dumps(best["overrides"]))
    print(f"   IS : {_fmt(best['is'])}    OOS: {_fmt(best['oos'])}   (n win% avgR PF)")
    return best["overrides"]


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Optimize strategies for OOS expectancy.")
    ap.add_argument("--strategy", default="all",
                    help="macd | bollinger | residualmomentum | all")
    ap.add_argument("--lookback-days", type=int, default=500)
    ap.add_argument("--min-score", type=int, default=None, help="override the per-strategy default")
    ap.add_argument("--min-is-n", type=int, default=20)
    ap.add_argument("--min-oos-n", type=int, default=12)
    ap.add_argument("--min-pf", type=float, default=1.1)
    ap.add_argument("--split-frac", type=float, default=0.6)
    args = ap.parse_args()

    with open("config.json") as f:
        cfg = json.load(f)

    bars_map, spy_bars = load_bars(cfg, lookback_days=args.lookback_days)
    cutoff = split_cutoff(spy_bars, frac=args.split_frac)
    print(f"In-sample < {cutoff} <= out-of-sample   ({len(bars_map)} symbols)")

    targets = (["macd", "bollinger", "residualmomentum"]
               if args.strategy == "all" else [args.strategy])
    recs = {}
    for name in targets:
        if name not in GRIDS:
            print(f"(no grid for {name!r})")
            continue
        recs[name] = optimize_strategy(name, cfg, bars_map, spy_bars, cutoff,
                                       args.min_is_n, args.min_oos_n, args.min_pf,
                                       min_score_override=args.min_score)

    print("\n" + "=" * 70)
    print("SUMMARY — recommended overrides (apply to config.json thresholds):")
    for name, rec in recs.items():
        print(f"  {name}: {json.dumps(rec) if rec else 'no confident improvement'}")


if __name__ == "__main__":
    main()
