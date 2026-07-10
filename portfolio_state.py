"""
portfolio_state.py - portfolio state schema helper for RobinhoodAgent.

The live Robinhood account snapshot must be collected through the Robinhood MCP
server. This script validates or creates state.json once that MCP data is
available, and can write a safe sample state for local dry-run testing.

It does not place trades.
"""

import argparse
import json
import os
from datetime import datetime, timezone


REQUIRED_FIELDS = {
    "portfolio_value": (int, float),
    "cash_available": (int, float),
    "open_positions": list,
    "open_position_count": int,
    "daily_loss_limit_remaining": (int, float),
    "weekly_loss_limit_remaining": (int, float),
    "kill_switch_triggered": bool,
}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def dump_json(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def build_sample_state(config):
    account = config.get("account", {})
    portfolio_value = float(account.get("portfolio_value", 50.0))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "sample",
        "portfolio_value": portfolio_value,
        "cash_available": portfolio_value,
        "open_positions": [],
        "open_position_count": 0,
        "todays_realized_pnl": 0.0,
        "weekly_realized_pnl": 0.0,
        "daily_loss_limit_remaining": round(
            portfolio_value * float(account.get("daily_loss_limit_pct", 0.03)),
            2,
        ),
        "weekly_loss_limit_remaining": round(
            portfolio_value * float(account.get("weekly_loss_limit_pct", 0.05)),
            2,
        ),
        "kill_switch_triggered": False,
        "kill_switch_reason": None,
        "vix_regime": "unknown",
        "size_multiplier": 1.0,
    }


def validate_state(state):
    errors = []
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in state:
            errors.append(f"missing required field: {field}")
            continue
        if not isinstance(state[field], expected_type):
            errors.append(f"{field} must be {expected_type}, got {type(state[field]).__name__}")

    positions = state.get("open_positions", [])
    if isinstance(positions, list):
        for idx, position in enumerate(positions):
            ticker = position.get("ticker") or position.get("symbol")
            qty = position.get("qty") or position.get("quantity")
            if not ticker:
                errors.append(f"open_positions[{idx}] missing ticker")
            if qty is None:
                errors.append(f"open_positions[{idx}] missing qty")

    if state.get("open_position_count") != len(positions):
        errors.append("open_position_count does not match open_positions length")

    return errors


def main():
    os.chdir(os.path.dirname(__file__))

    parser = argparse.ArgumentParser(description="Create or validate RobinhoodAgent portfolio state JSON.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--write-sample", action="store_true", help="write a dry-run sample state.json")
    parser.add_argument("--validate", action="store_true", help="validate the selected state JSON")
    args = parser.parse_args()

    if args.write_sample:
        config = load_json(args.config)
        state = build_sample_state(config)
        dump_json(args.state, state)
        print(f"Wrote sample state to {args.state}")
        return

    if args.validate:
        state = load_json(args.state)
        errors = validate_state(state)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            raise SystemExit(1)
        print(f"{args.state} is valid")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
