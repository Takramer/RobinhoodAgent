#!/usr/bin/env python3
"""Objective gate for plan.json — the maker/checker split from loop engineering.

order_plan.py is the maker (writes plan.json). This is the checker: it fails,
loudly and with a non-zero exit, any plan that is structurally broken or unsafe
to hand to autonomous Stage 2 live trading. It encodes only the DETERMINISTIC
hard stops from TRADING_RULES.md + AGENT_TASK_PROMPT.md — the machine-checkable
ones. Trade *quality* is not judged here (that is Stage 1's job); this only
answers "is this plan safe to execute at all."

Usage:
    python3 validate_plan.py                 # check ./plan.json against today
    python3 validate_plan.py --plan p.json   # check another file
    python3 validate_plan.py --today 2026-06-08   # override "today" (testing)
    python3 validate_plan.py --self-test     # run built-in checks, no file

Exit 0 = safe. Exit 1 = one or more violations (printed to stderr).
Warnings never fail the gate — they print to stderr and exit 0.

Wiring options (pick one; none are applied automatically):
  - Manual: run it after Stage 1, before trusting plan.json.
  - Stage 1: append `python3 validate_plan.py || exit 1` to run_session.sh so a
    bad plan never reaches Stage 2.
  - Stage 2: add `Bash(python3 validate_plan.py*)` to run_stage2.sh's
    --allowedTools and have AGENT_TASK_PROMPT.md run it as a Step-1 precondition.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

# Keys order_plan.py is contracted to write. Missing key = schema drift = fail.
REQUIRED_KEYS = [
    "session_date", "mode", "kill_switch_triggered", "kill_switch_reason",
    "vix_blocked", "portfolio_value", "cash_available", "cash_floor",
    "deployable_cash_start", "deployable_cash_remaining", "open_position_count",
    "max_open_positions", "qualified_setups", "positions_to_exit",
    "positions_to_trail",
]
EPS = 0.01  # dollar tolerance for float sums

# Per-setup fields vary; probe the common names rather than assume one schema.
_AMOUNT_FIELDS = ("dollar_amount", "notional", "size", "amount")
_SIDE_FIELDS = ("side", "action", "direction")
_TICKER_FIELDS = ("ticker", "symbol")


def _first(d: dict, names) -> object:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def validate(plan: dict, today: str | None = None) -> tuple[list[str], list[str]]:
    """Return (violations, warnings). Empty violations == safe to execute."""
    v: list[str] = []
    w: list[str] = []

    # 1. Schema — every contracted key present.
    missing = [k for k in REQUIRED_KEYS if k not in plan]
    if missing:
        v.append(f"missing required keys: {missing}")
        return v, w  # everything below assumes the keys exist

    # 2. Staleness — never execute a stale plan (AGENT_TASK_PROMPT Step 1).
    if today is not None and plan["session_date"] != today:
        v.append(f"stale plan: session_date {plan['session_date']} != today {today}")

    # 3. Kill-switch consistency.
    ks = plan["kill_switch_triggered"]
    if not isinstance(ks, bool):
        v.append(f"kill_switch_triggered must be bool, got {type(ks).__name__}")
    elif ks and not plan["kill_switch_reason"]:
        v.append("kill_switch_triggered is true but kill_switch_reason is empty")

    # 4. Money sanity — no negative cash, portfolio must be real.
    if _num(plan["portfolio_value"]) <= 0:
        v.append(f"portfolio_value must be > 0, got {plan['portfolio_value']}")
    for k in ("cash_available", "cash_floor", "deployable_cash_start",
              "deployable_cash_remaining"):
        if _num(plan[k]) < 0:
            v.append(f"{k} must be >= 0, got {plan[k]}")

    # 5. Position-count sanity.
    open_n = _num(plan["open_position_count"])
    max_n = _num(plan["max_open_positions"])
    if open_n > max_n:
        v.append(f"open_position_count {open_n} exceeds max_open_positions {max_n}")

    setups = plan["qualified_setups"]
    exits = plan["positions_to_exit"]
    if not isinstance(setups, list):
        v.append("qualified_setups must be a list")
        setups = []
    if not isinstance(exits, list):
        v.append("positions_to_exit must be a list")
        exits = []

    # 6. Don't plan more entries than open slots.
    free_slots = max_n - open_n
    if len(setups) > free_slots:
        v.append(f"{len(setups)} entries planned but only {free_slots} slots free")

    # 7. Entries: long buys, positive size, total within deployable cash.
    total_entry = 0.0
    for i, s in enumerate(setups):
        if not isinstance(s, dict):
            v.append(f"qualified_setups[{i}] is not an object")
            continue
        if _first(s, _TICKER_FIELDS) is None:
            v.append(f"qualified_setups[{i}] has no ticker/symbol")
        side = _first(s, _SIDE_FIELDS)
        if side is not None and str(side).lower() not in ("buy", "long"):
            v.append(f"qualified_setups[{i}] side {side!r} is not a buy (entries are long buys only)")
        amt = _first(s, _AMOUNT_FIELDS)
        if amt is not None:
            if _num(amt) <= 0:
                v.append(f"qualified_setups[{i}] amount must be > 0, got {amt}")
            else:
                total_entry += _num(amt)
    if total_entry > _num(plan["deployable_cash_start"]) + EPS:
        v.append(f"planned entries ${total_entry:.2f} exceed deployable_cash_start "
                 f"${_num(plan['deployable_cash_start']):.2f}")

    # 8. Exits: sells only.
    for i, e in enumerate(exits):
        if isinstance(e, dict):
            side = _first(e, _SIDE_FIELDS)
            if side is not None and str(side).lower() not in ("sell", "short"):
                v.append(f"positions_to_exit[{i}] side {side!r} is not a sell")

    # 9. Warn (not fail) when entries are planned under a block — Stage 2 skips
    #    them by rule, so it's safe, but worth surfacing.
    if (ks is True or plan["vix_blocked"] is True) and setups:
        w.append(f"{len(setups)} entries listed while kill_switch/vix block is on "
                 "(Stage 2 will skip them; check this is intended)")

    return v, w


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _self_test() -> int:
    base = json.loads(_SAMPLE)
    ok, warn = validate(base, today=base["session_date"])
    assert ok == [], f"clean plan should pass, got {ok}"

    stale, _ = validate(base, today="2099-01-01")
    assert any("stale" in m for m in stale), stale

    bad = json.loads(_SAMPLE)
    bad["kill_switch_triggered"] = True
    bad["kill_switch_reason"] = None
    ks, _ = validate(bad, today=bad["session_date"])
    assert any("kill_switch_reason" in m for m in ks), ks

    neg = json.loads(_SAMPLE)
    neg["deployable_cash_remaining"] = -5
    n, _ = validate(neg, today=neg["session_date"])
    assert any("deployable_cash_remaining" in m for m in n), n

    over = json.loads(_SAMPLE)
    over["deployable_cash_start"] = 40.0
    over["max_open_positions"] = 5
    over["open_position_count"] = 0
    over["qualified_setups"] = [{"ticker": "AAA", "dollar_amount": 30},
                                {"ticker": "BBB", "dollar_amount": 30}]
    o, _ = validate(over, today=over["session_date"])
    assert any("exceed deployable_cash_start" in m for m in o), o

    sell_entry = json.loads(_SAMPLE)
    sell_entry["qualified_setups"] = [{"ticker": "AAA", "side": "sell", "dollar_amount": 5}]
    se, _ = validate(sell_entry, today=sell_entry["session_date"])
    assert any("is not a buy" in m for m in se), se

    drift = json.loads(_SAMPLE)
    del drift["portfolio_value"]
    d, _ = validate(drift, today=drift["session_date"])
    assert any("missing required keys" in m for m in d), d

    print("validate_plan self-test: all checks passed")
    return 0


# Minimal valid plan (mirrors plan.sample.json) for the self-test only.
_SAMPLE = """{
  "session_date": "2026-06-08", "mode": "dry_run",
  "kill_switch_triggered": false, "kill_switch_reason": null,
  "vix_blocked": false, "portfolio_value": 50.0, "cash_available": 50.0,
  "cash_floor": 10.0, "deployable_cash_start": 40.0,
  "deployable_cash_remaining": 40.0, "open_position_count": 0,
  "max_open_positions": 5, "qualified_setups": [], "positions_to_exit": [],
  "positions_to_trail": []
}"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate plan.json before Stage 2 executes it.")
    ap.add_argument("--plan", default="plan.json")
    ap.add_argument("--today", default=None,
                    help="YYYY-MM-DD to check session_date against; default = system date")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    today = args.today or _dt.date.today().isoformat()
    try:
        with open(args.plan) as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"FAIL: cannot load {args.plan}: {e}", file=sys.stderr)
        return 1

    violations, warnings = validate(plan, today=today)
    for warn in warnings:
        print(f"WARN: {warn}", file=sys.stderr)
    if violations:
        for m in violations:
            print(f"FAIL: {m}", file=sys.stderr)
        print(f"plan.json REJECTED ({len(violations)} violation(s)) — do not execute.",
              file=sys.stderr)
        return 1
    print(f"plan.json OK for {today} ({len(plan.get('qualified_setups', []))} entries, "
          f"{len(plan.get('positions_to_exit', []))} exits).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
