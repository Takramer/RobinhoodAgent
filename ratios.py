"""
ratios.py — portfolio performance ratios scaled to the configured account size.

Reads the contiguous backtest (backtest_trades.csv) and joins meta-model P(win)
(from training_data.csv features) so it can report BOTH the raw signal pool and
the live 3/4+meta-filter gate. Every trade is sized exactly as order_plan would at
the configured portfolio value, so dollar figures are realistic (the position cap
usually binds, so risk/trade is well under the 2% target — that is shown).

Per-trade ratios are strategy properties (valid regardless of universe size). The
annual dollar estimate models the actual 5-slot portfolio, NOT the full signal
population, so it is a rough projection with stated assumptions.
"""

import csv
import json
import math
import os
from datetime import date

import numpy as np
from scipy.stats import norm, skew, kurtosis

CFG = json.load(open("config.json"))
PV = CFG["account"]["portfolio_value"]
RISK_PCT = CFG["account"]["risk_pct"]
MAX_NOTIONAL = CFG["account"].get("max_position_notional", PV * CFG["account"]["max_position_pct"])
MAX_POS = CFG["account"]["max_open_positions"]
COST_BPS = 5.0
META_THR = CFG.get("meta_filter_threshold", 0.53)


def _size(entry, stop):
    """Replicate order_plan sizing → (notional, risk_dollars) for one trade."""
    stop_pct = (entry - stop) / entry if entry > 0 else 0
    if stop_pct <= 0:
        return 0.0, 0.0
    notional = min(PV * RISK_PCT / stop_pct, MAX_NOTIONAL)
    return notional, notional * stop_pct


def load_trades():
    trades = list(csv.DictReader(open("backtest_trades.csv")))
    # meta_prob from the PURGED OUT-OF-SAMPLE predictions (oos_meta.csv), never the
    # in-sample model — otherwise the filter looks fake-good (it would select the
    # very trades the model was fit to classify). This is the honest gate.
    meta = {}
    if os.path.exists("oos_meta.csv"):
        for r in csv.DictReader(open("oos_meta.csv")):
            meta[(r["signal_date"], r["ticker"])] = float(r["oos_prob"])
    out = []
    for t in trades:
        entry, stop, r = float(t["entry"]), float(t["stop"]), float(t["r_multiple"])
        notional, risk_d = _size(entry, stop)
        if risk_d <= 0:
            continue
        cost_r = 2.0 * (COST_BPS / 10000.0) / ((entry - stop) / entry)
        out.append({
            "date": t["signal_date"], "ticker": t["ticker"],
            "score": int(t["score"]), "reason": t["exit_reason"],
            "r": r, "r_net": r - cost_r,
            "risk_d": risk_d, "notional": notional,
            "pnl_d": r * risk_d, "pnl_d_net": (r - cost_r) * risk_d,
            "meta": meta.get((t["signal_date"], t["ticker"])),
        })
    return out


def stats(rows, net=False):
    if not rows:
        return None
    rk = "r_net" if net else "r"
    pk = "pnl_d_net" if net else "pnl_d"
    R = np.array([x[rk] for x in rows])
    P = np.array([x[pk] for x in rows])
    risk = np.array([x["risk_d"] for x in rows])
    wins, losses = R[R > 0], R[R <= 0]
    gross_w, gross_l = P[P > 0].sum(), -P[P <= 0].sum()
    span_days = (date.fromisoformat(max(x["date"] for x in rows)) -
                 date.fromisoformat(min(x["date"] for x in rows))).days or 1
    years = span_days / 365.25
    # portfolio-level annual estimate: MAX_POS slots, ~avg hold turnover
    avg_hold = np.mean([10] * len(rows))  # exits capped at 10; conservative
    port_trades_yr = MAX_POS * (252 / avg_hold)
    return {
        "n": len(rows),
        "win": (R > 0).mean() * 100, "loss": (R <= 0).mean() * 100,
        "wl_ratio": len(wins) / max(len(losses), 1),
        "avg_win_r": wins.mean() if len(wins) else 0,
        "avg_loss_r": losses.mean() if len(losses) else 0,
        "payoff": (wins.mean() / -losses.mean()) if len(losses) and losses.mean() != 0 else float("inf"),
        "pf": gross_w / gross_l if gross_l > 0 else float("inf"),
        "exp_r": R.mean(), "exp_d": P.mean(),
        "avg_risk_d": risk.mean(),
        "avg_win_d": P[P > 0].mean() if (P > 0).any() else 0,
        "avg_loss_d": P[P <= 0].mean() if (P <= 0).any() else 0,
        "port_trades_yr": port_trades_yr,
        "annual_d": P.mean() * port_trades_yr,
        "annual_pct": P.mean() * port_trades_yr / PV * 100,
        "sharpe": _daily_sharpe(rows, net),
    }


def _daily_sharpe(rows, net):
    pk = "pnl_d_net" if net else "pnl_d"
    by_date = {}
    for x in rows:
        by_date.setdefault(x["date"], []).append(x[pk])
    daily = np.array([np.mean(v) for _, v in sorted(by_date.items())])
    if len(daily) < 3 or daily.std(ddof=1) == 0:
        return 0.0
    return daily.mean() / daily.std(ddof=1) * math.sqrt(252)


def exit_breakdown(rows, net=False):
    pk = "pnl_d_net" if net else "pnl_d"
    out = {}
    for reason in ("TP", "SL", "Time"):
        sub = [x for x in rows if x["reason"] == reason]
        if sub:
            out[reason] = (len(sub), sum(x[pk] for x in sub))
    return out


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    trades = load_trades()
    have_meta = any(t["meta"] is not None for t in trades)
    gated = [t for t in trades if t["meta"] is not None and t["meta"] >= META_THR] if have_meta else []

    print(f"Account ${PV:,.0f} | risk {RISK_PCT*100:.0f}% (${PV*RISK_PCT:,.0f}) | "
          f"max position ${MAX_NOTIONAL:,.0f} | {MAX_POS} slots | cost {COST_BPS}bps")
    print(f"Backtest: {len(trades)} signals, {min(t['date'] for t in trades)}..{max(t['date'] for t in trades)}, "
          f"60 large-caps (contiguous, Alpaca)\n")

    def show(name, rows):
        g, n = stats(rows, net=False), stats(rows, net=True)
        if not g:
            print(f"[{name}] no trades"); return
        print(f"===== {name}  (n={g['n']}) =====")
        print(f"  {'metric':<26}{'gross':>14}{'net (5bps)':>16}")
        print(f"  {'win rate':<26}{g['win']:>13.1f}%{n['win']:>15.1f}%")
        print(f"  {'loss rate':<26}{g['loss']:>13.1f}%{n['loss']:>15.1f}%")
        print(f"  {'win:loss count':<26}{g['wl_ratio']:>14.2f}{n['wl_ratio']:>16.2f}")
        print(f"  {'avg win / avg loss (R)':<26}{g['avg_win_r']:>7.2f}/{g['avg_loss_r']:<6.2f}{n['avg_win_r']:>9.2f}/{n['avg_loss_r']:<6.2f}")
        print(f"  {'payoff ratio':<26}{g['payoff']:>14.2f}{n['payoff']:>16.2f}")
        print(f"  {'profit factor ($)':<26}{g['pf']:>14.2f}{n['pf']:>16.2f}")
        print(f"  {'expectancy (R/trade)':<26}{g['exp_r']:>+14.3f}{n['exp_r']:>+16.3f}")
        print(f"  {'avg risk / trade ($)':<26}{g['avg_risk_d']:>13.2f}{n['avg_risk_d']:>15.2f}")
        print(f"  {'expectancy ($/trade)':<26}{g['exp_d']:>+13.2f}{n['exp_d']:>+15.2f}")
        print(f"  {'avg win / avg loss ($)':<26}{g['avg_win_d']:>+6.1f}/{g['avg_loss_d']:<6.1f}{n['avg_win_d']:>+8.1f}/{n['avg_loss_d']:<6.1f}")
        print(f"  {'~annual P&L ($, 5-slot)':<26}{g['annual_d']:>+13.0f}{n['annual_d']:>+15.0f}")
        print(f"  {'~annual return (%)':<26}{g['annual_pct']:>+13.1f}%{n['annual_pct']:>+14.1f}%")
        print(f"  {'Sharpe (annualized)*':<26}{g['sharpe']:>+14.2f}{n['sharpe']:>+16.2f}")
        eb = exit_breakdown(rows)
        print(f"  exits: " + " ".join(f"{k} {v[0]} (${v[1]:+,.0f})" for k, v in eb.items()))
        print()

    show("ALL 3/4+4/4 signals", trades)
    if have_meta:
        show(f"LIVE GATE: meta P(win) >= {META_THR}", gated)
    else:
        print("(meta model unavailable — showing raw pool only)")
    print("* Sharpe is daily-aggregated (removes cross-sectional overlap) but still an")
    print("  UPPER bound (10-day holds overlap); annual $ assumes a full 5-slot book at")
    print("  ~126 trades/yr and no compounding. Net = after 5bps round-trip cost.")


if __name__ == "__main__":
    main()
