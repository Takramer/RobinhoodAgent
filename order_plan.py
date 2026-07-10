"""
order_plan.py - deterministic order planning for RobinhoodAgent.

Reads signals.json + state.json + config.json and writes plan.json.
This script never connects to Robinhood and never places trades. It only builds
the execution plan that a Claude/Codex MCP session can review and execute later.
"""

import argparse
import json
import os
from datetime import datetime, timezone


MIN_ORDER_NOTIONAL = 1.00


def load_json(path):
    with open(path) as f:
        return json.load(f)


def dump_json(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_ticker(position):
    return str(position.get("ticker") or position.get("symbol") or "").upper()


def _position_current_price(position):
    price = _num(position.get("current_price"))
    if price > 0:
        return price

    qty = _num(position.get("qty") or position.get("quantity"))
    current_value = _num(position.get("current_value") or position.get("market_value"))
    if qty > 0 and current_value > 0:
        return current_value / qty
    return _num(position.get("avg_cost") or position.get("average_cost"))


def _is_leveraged(ticker, cfg):
    return ticker in set(cfg.get("leveraged_tickers", []))


def _max_hold_for(ticker, cfg):
    exits = cfg.get("exits", {})
    if _is_leveraged(ticker, cfg):
        return int(exits.get("leveraged_max_hold_sessions", 3))
    return int(exits.get("max_hold_sessions", 10))


def _build_exit_and_trail_actions(state, cfg):
    exits = []
    trails = []
    warnings = []
    exit_seen = set()

    trail_mult = _num(cfg.get("exits", {}).get("trail_trigger_mult"), 1.5)
    chandelier_mult = _num(cfg.get("exits", {}).get("chandelier_atr_mult"), 3.0)

    for position in state.get("open_positions", []):
        ticker = _position_ticker(position)
        if not ticker:
            continue

        current_price = _position_current_price(position)
        avg_cost = _num(position.get("avg_cost") or position.get("average_cost"))
        _qty = _num(position.get("qty") or position.get("quantity"))
        _mv  = _num(position.get("current_value") or position.get("market_value"))
        if _num(position.get("current_price")) <= 0 and not (_qty > 0 and _mv > 0):
            # Price fell back to avg_cost — stop/target checks below are blind.
            warnings.append(f"{ticker}: current price unknown (avg_cost fallback) — "
                            "stop/target checks unreliable, verify with live quotes in Stage 2")
        stop_price = _num(position.get("stop_price"))
        target_price = _num(position.get("target_price"))
        atr = _num(position.get("atr"))
        sessions_held = int(_num(position.get("sessions_held"), 0))
        max_hold = _max_hold_for(ticker, cfg)

        reasons = []
        if current_price > 0 and stop_price > 0 and current_price <= stop_price:
            reasons.append("stop price hit")
        if current_price > 0 and target_price > 0 and current_price >= target_price:
            reasons.append("target price hit")
        if sessions_held >= max_hold:
            reasons.append(f"max hold reached ({sessions_held}/{max_hold} sessions)")

        if reasons:
            exits.append({
                "ticker": ticker,
                "action": "EXIT",
                "side": "sell",
                "order_type": "market",
                "qty": position.get("qty") or position.get("quantity"),
                "current_price": round(current_price, 4) if current_price else None,
                "reason": "; ".join(reasons),
            })
            exit_seen.add(ticker)
            continue

        if avg_cost > 0 and atr > 0 and current_price >= avg_cost + atr * trail_mult:
            # Ratchet the stop up (never down) to the higher of breakeven and a
            # Chandelier level (price - chandelier_mult*ATR). Early in the move
            # breakeven binds; deep in a trend the Chandelier locks in gains and
            # lets winners run — beats a static target on trend strategies.
            breakeven  = avg_cost
            chandelier = current_price - atr * chandelier_mult
            new_stop   = max(stop_price, breakeven, chandelier)
            if new_stop > stop_price:
                at_breakeven = new_stop <= breakeven + 1e-9
                trails.append({
                    "ticker": ticker,
                    "action": "TRAIL_STOP_TO_BREAKEVEN" if at_breakeven else "TRAIL_STOP_CHANDELIER",
                    "current_stop": round(stop_price, 4) if stop_price else None,
                    "new_stop": round(new_stop, 4),
                    "current_price": round(current_price, 4) if current_price else None,
                    "reason": (
                        f"profit exceeded {trail_mult} ATR trail trigger"
                        if at_breakeven
                        else f"chandelier trail {chandelier_mult} ATR below high"
                    ),
                })

    return exits, trails, exit_seen, warnings


def _entry_notional(setup, portfolio_value, deployable_cash, cfg):
    account_cfg = cfg.get("account", {})
    risk_pct = _num(account_cfg.get("risk_pct"), 0.02)
    max_position_notional = _num(
        account_cfg.get("max_position_notional"),
        portfolio_value * _num(account_cfg.get("max_position_pct"), 0.10),
    )

    entry = _num(setup.get("entry") or setup.get("price"))
    stop = _num(setup.get("stop"))
    atr = _num(setup.get("atr"))

    if entry <= 0 or stop <= 0:
        return None, "missing entry or stop"

    stop_distance = abs(entry - stop)
    stop_pct = stop_distance / entry if entry else 0
    if stop_pct <= 0:
        return None, "invalid stop distance"

    dollar_risk_target = portfolio_value * risk_pct
    raw_notional = dollar_risk_target / stop_pct
    dollar_amount = min(raw_notional, max_position_notional, deployable_cash)

    if dollar_amount < MIN_ORDER_NOTIONAL:
        return None, "deployable cash below $1 minimum order notional"

    actual_risk = dollar_amount * stop_pct
    return {
        "entry_price": round(entry, 4),
        "dollar_amount": round(dollar_amount, 2),
        "stop_price": round(stop, 4),
        "target_price": round(_num(setup.get("target")), 4),
        "dollar_risk": round(actual_risk, 4),
        "risk_pct_of_portfolio": round(actual_risk / portfolio_value, 6) if portfolio_value else 0,
        "atr": round(atr, 4),
        "raw_notional": round(raw_notional, 2),
        "max_position_notional": round(max_position_notional, 2),
    }, None


def _get_sector(ticker, cfg):
    return cfg.get("sector_map", {}).get(ticker.upper(), "unknown")


def build_plan(signals, state, cfg):
    account_cfg = cfg.get("account", {})
    portfolio_value = _num(state.get("portfolio_value"), _num(account_cfg.get("portfolio_value"), 50.0))
    cash_available = _num(state.get("cash_available"))
    cash_floor = portfolio_value * _num(account_cfg.get("cash_floor_pct"), 0.20)
    deployable_cash = max(0.0, cash_available - cash_floor)
    max_positions = int(account_cfg.get("max_open_positions", 5))

    # VIX regime size multiplier from signals (calculated in indicators.py)
    vix_size_mult = _num(signals.get("vix_size_mult"), 1.0)
    vix_regime    = signals.get("vix_regime", "unknown")
    max_per_sector = int(cfg.get("max_per_sector", 2))

    # Market overlays (Sharpe levers): crash-avoidance trend filter blocks new
    # entries; vol targeting scales size continuously. Exits are never affected.
    market_risk_off = bool(signals.get("market_risk_off"))
    vol_target_mult = _num(signals.get("vol_target_mult"), 1.0)

    # Optional meta-labeling filter: skip entries whose model P(win) is below this.
    # Off by default (None) — the model is advisory until its OOS edge is stronger.
    meta_filter = cfg.get("meta_filter_threshold")

    open_positions = state.get("open_positions", [])
    held_tickers = {_position_ticker(p) for p in open_positions if _position_ticker(p)}
    open_count = int(state.get("open_position_count", len(held_tickers)))

    positions_to_exit, positions_to_trail, exit_seen, warnings = _build_exit_and_trail_actions(state, cfg)

    kill_switch = bool(state.get("kill_switch_triggered"))
    kill_reason = state.get("kill_switch_reason")
    if _num(state.get("daily_loss_limit_remaining"), 1) <= 0:
        kill_switch = True
        kill_reason = "daily loss limit reached"
    if _num(state.get("weekly_loss_limit_remaining"), 1) <= 0:
        kill_switch = True
        kill_reason = "weekly loss limit reached"

    # Fail safe on stale inputs. The loss-limit math above is only as real as
    # state.json — a sample or old snapshot must never authorize new risk.
    # Stage 2 writes live MCP state each session; ≤5 days covers weekends/holidays.
    state_source = str(state.get("source", "")).lower()
    state_age_days = None
    try:
        state_age_days = (datetime.now(timezone.utc)
                          - datetime.fromisoformat(str(state.get("generated_at")))).days
    except (TypeError, ValueError):
        pass
    if state_source not in ("mcp", "live"):
        kill_switch = True
        kill_reason = (f"portfolio state source is '{state_source or 'missing'}', not live MCP data "
                       "— new entries blocked until Stage 2 writes state.json from Robinhood")
    elif state_age_days is None or state_age_days > 5:
        kill_switch = True
        kill_reason = f"portfolio state is stale ({state_age_days if state_age_days is not None else 'unknown'} days old) — new entries blocked"

    today_utc = datetime.now(timezone.utc).date().isoformat()
    if str(signals.get("session_date")) != today_utc:
        kill_switch = True
        kill_reason = (f"signals dated {signals.get('session_date')} but today is {today_utc} "
                       "— stale plan, no new entries")

    qualified_entries = []
    skip_reasons = {}
    # Do not credit planned exits back as free slots — an exit can be blocked at
    # review time, and a filled paired entry would then exceed max_positions.
    remaining_slots = max(0, max_positions - open_count)
    remaining_cash = deployable_cash

    # Track sector counts: start with already-held positions
    sector_counts = {}
    for p in open_positions:
        t = _position_ticker(p)
        if t and t not in exit_seen:
            s = _get_sector(t, cfg)
            sector_counts[s] = sector_counts.get(s, 0) + 1

    for setup in signals.get("qualified_setups", []):
        ticker = str(setup.get("ticker", "")).upper()
        if not ticker:
            continue

        if kill_switch:
            skip_reasons[ticker] = kill_reason or "kill switch triggered"
            continue
        if market_risk_off and ticker not in set(cfg.get("bear_regime_only", [])):
            # Inverse ETFs (bear_regime_only) are exempt: they are the designed
            # bear-side vehicle, and downtrends nearly always coincide with
            # SPY < 200-SMA — blocking them here made them unreachable.
            skip_reasons[ticker] = "market risk-off (SPY below 200-day SMA) — no new entries"
            continue
        if vix_size_mult == 0.0:
            skip_reasons[ticker] = f"vix regime '{vix_regime}' blocks all new entries"
            continue
        if setup.get("direction") != "bull":
            skip_reasons[ticker] = "bear setup — no short selling via Robinhood MCP"
            continue
        if ticker in held_tickers and ticker not in exit_seen:
            skip_reasons[ticker] = "already held"
            continue
        if remaining_slots <= 0:
            skip_reasons[ticker] = "max open positions reached"
            continue
        if remaining_cash < MIN_ORDER_NOTIONAL:
            skip_reasons[ticker] = "cash floor reached"
            continue

        # Meta-model filter. When a threshold is configured (i.e. the score gate
        # is relaxed to 3/4), a setup must clear it — and must HAVE a probability.
        # A missing meta_prob means the model couldn't vet it, so at a lowered gate
        # we skip rather than trade an unvetted 3/4. The model now includes PEAD
        # features, so PEAD is gated on the same footing as every other sleeve.
        meta_prob = setup.get("meta_prob")
        if meta_filter is not None:
            if meta_prob is None:
                skip_reasons[ticker] = "no meta_prob (model unavailable) — skipped under meta gate"
                continue
            if meta_prob < meta_filter:
                skip_reasons[ticker] = f"meta_prob {meta_prob:.2f} < filter {meta_filter}"
                continue

        # Sector concentration check
        sector = _get_sector(ticker, cfg)
        if sector_counts.get(sector, 0) >= max_per_sector:
            skip_reasons[ticker] = f"sector cap reached ({sector}: {sector_counts[sector]}/{max_per_sector})"
            continue

        sizing, skip_reason = _entry_notional(setup, portfolio_value, remaining_cash, cfg)
        if skip_reason:
            skip_reasons[ticker] = skip_reason
            continue

        # Apply VIX regime multiplier AND the continuous vol-target multiplier.
        # Re-cap after scaling: vol_target_mult can be >1 and must never push a
        # position past the notional cap or the remaining deployable cash.
        size_mult = vix_size_mult * vol_target_mult
        adjusted_notional = round(min(sizing["dollar_amount"] * size_mult,
                                      sizing["max_position_notional"],
                                      remaining_cash), 2)
        if adjusted_notional < MIN_ORDER_NOTIONAL:
            skip_reasons[ticker] = f"notional after size multiplier ({size_mult:.2f}x) below $1 minimum"
            continue
        sizing["dollar_amount"]    = adjusted_notional
        sizing["vix_size_mult"]    = vix_size_mult
        sizing["vol_target_mult"]  = vol_target_mult
        sizing["dollar_risk"]      = round(sizing["dollar_risk"] * size_mult, 4)

        qualified_entries.append({
            "ticker":       ticker,
            "direction":    setup.get("direction"),
            "score":        setup.get("score"),
            "sector":       sector,
            "strategy":     setup.get("strategy", signals.get("strategy_used")),
            "action":       "ENTER",
            "side":         "buy",
            "order_type":   "market",
            "time_in_force":"gfd",
            "meta_prob":    meta_prob,
            **sizing,
            "legs": setup.get("legs", []),
        })
        remaining_slots -= 1
        remaining_cash  -= adjusted_notional
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "session_date":  signals.get("session_date"),
        "mode":          "live" if account_cfg.get("autonomous") else "dry_run",
        "spy_regime":    signals.get("spy_regime"),
        "vix_regime":    vix_regime,
        "vix_size_mult": vix_size_mult,
        "vix_blocked":   signals.get("vix_blocked", False),
        "market_risk_off":  market_risk_off,
        "vol_target_mult":  vol_target_mult,
        "realized_vol":     signals.get("realized_vol"),
        "strategy_used": signals.get("strategy_used"),
        "size_multiplier": round(vix_size_mult * vol_target_mult, 3),
        "portfolio_value": round(portfolio_value, 2),
        "cash_available": round(cash_available, 2),
        "cash_floor": round(cash_floor, 2),
        "deployable_cash_start": round(deployable_cash, 2),
        "deployable_cash_remaining": round(max(0.0, remaining_cash), 2),
        "open_position_count": open_count,
        "max_open_positions": max_positions,
        "kill_switch_triggered": kill_switch,
        "kill_switch_reason": kill_reason,
        "qualified_setups": qualified_entries,
        "positions_to_exit": positions_to_exit,
        "positions_to_trail": positions_to_trail,
        "skip_reasons": skip_reasons,
        "warnings": warnings,
        "source_files": {
            "signals": "signals.json",
            "state": "state.json",
            "config": "config.json",
        },
    }


def main():
    os.chdir(os.path.dirname(__file__))

    parser = argparse.ArgumentParser(description="Build dry-run order plan from signals and portfolio state.")
    parser.add_argument("--signals", default="signals.json")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output", default="plan.json")
    args = parser.parse_args()

    signals = load_json(args.signals)
    state = load_json(args.state)
    cfg = load_json(args.config)

    # A retrained meta-model ships its calibrated threshold in metamodel_meta.json;
    # prefer it over the config value so retrains apply without a config edit.
    if cfg.get("meta_filter_threshold") is not None and os.path.exists("metamodel_meta.json"):
        try:
            model_thr = load_json("metamodel_meta.json").get("threshold")
            if model_thr is not None:
                cfg["meta_filter_threshold"] = float(model_thr)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    plan = build_plan(signals, state, cfg)
    dump_json(args.output, plan)

    print(
        f"Wrote {args.output}: {len(plan['qualified_setups'])} entries, "
        f"{len(plan['positions_to_exit'])} exits, {len(plan['positions_to_trail'])} trails, "
        f"{len(plan['skip_reasons'])} skipped"
    )


if __name__ == "__main__":
    main()
