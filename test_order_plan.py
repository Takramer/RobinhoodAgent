import unittest
from datetime import datetime, timezone

from order_plan import build_plan

TODAY = datetime.now(timezone.utc).date().isoformat()
NOW = datetime.now(timezone.utc).isoformat()


def base_config():
    return {
        "account": {
            "portfolio_value": 50.0,
            "risk_pct": 0.02,
            "max_position_pct": 0.10,
            "max_position_notional": 10.0,
            "cash_floor_pct": 0.20,
            "daily_loss_limit_pct": 0.03,
            "weekly_loss_limit_pct": 0.05,
            "max_open_positions": 5,
        },
        "exits": {
            "stop_atr_mult": 1.0,
            "target_atr_mult": 2.0,
            "trail_trigger_mult": 1.5,
            "max_hold_sessions": 10,
            "leveraged_max_hold_sessions": 3,
        },
        "leveraged_tickers": ["SQQQ"],
    }


def base_state(**overrides):
    state = {
        "source": "mcp",
        "generated_at": NOW,
        "portfolio_value": 50.0,
        "cash_available": 50.0,
        "open_positions": [],
        "open_position_count": 0,
        "daily_loss_limit_remaining": 1.5,
        "weekly_loss_limit_remaining": 2.5,
        "kill_switch_triggered": False,
        "kill_switch_reason": None,
    }
    state.update(overrides)
    return state


def base_signals(*setups):
    return {
        "session_date": TODAY,
        "spy_regime": "uptrend",
        "strategy_used": "macd",
        "qualified_setups": list(setups),
    }


def setup(ticker="MSFT", direction="bull", entry=100.0, stop=95.0, target=110.0, atr=5.0):
    return {
        "ticker": ticker,
        "direction": direction,
        "score": 4,
        "entry": entry,
        "stop": stop,
        "target": target,
        "atr": atr,
        "legs": [{"name": "test", "pass": True}],
    }


class OrderPlanTests(unittest.TestCase):
    def test_sizes_entry_with_cash_and_position_cap(self):
        plan = build_plan(base_signals(setup()), base_state(), base_config())

        self.assertEqual(len(plan["qualified_setups"]), 1)
        entry = plan["qualified_setups"][0]
        self.assertEqual(entry["ticker"], "MSFT")
        self.assertEqual(entry["dollar_amount"], 10.0)
        self.assertEqual(entry["dollar_risk"], 0.5)
        self.assertEqual(plan["deployable_cash_remaining"], 30.0)

    def test_skips_bear_setup_because_robinhood_mcp_cannot_short(self):
        plan = build_plan(base_signals(setup(direction="bear")), base_state(), base_config())

        self.assertEqual(plan["qualified_setups"], [])
        self.assertIn("MSFT", plan["skip_reasons"])
        self.assertIn("no short selling", plan["skip_reasons"]["MSFT"])

    def test_respects_cash_floor(self):
        plan = build_plan(
            base_signals(setup()),
            base_state(cash_available=10.50),
            base_config(),
        )

        self.assertEqual(plan["qualified_setups"], [])
        self.assertEqual(plan["skip_reasons"]["MSFT"], "cash floor reached")

    def test_respects_max_positions(self):
        state = base_state(
            open_position_count=5,
            open_positions=[
                {"ticker": "AAPL", "qty": 0.1},
                {"ticker": "META", "qty": 0.1},
                {"ticker": "AMZN", "qty": 0.1},
                {"ticker": "GOOGL", "qty": 0.1},
                {"ticker": "ORCL", "qty": 0.1},
            ],
        )
        plan = build_plan(base_signals(setup()), state, base_config())

        self.assertEqual(plan["qualified_setups"], [])
        self.assertEqual(plan["skip_reasons"]["MSFT"], "max open positions reached")

    def test_builds_exit_when_stop_hit(self):
        state = base_state(
            open_positions=[
                {
                    "ticker": "MSFT",
                    "qty": 0.1,
                    "avg_cost": 100.0,
                    "current_price": 94.0,
                    "stop_price": 95.0,
                    "target_price": 110.0,
                    "atr": 5.0,
                    "sessions_held": 2,
                }
            ],
            open_position_count=1,
        )
        plan = build_plan(base_signals(), state, base_config())

        self.assertEqual(len(plan["positions_to_exit"]), 1)
        self.assertEqual(plan["positions_to_exit"][0]["ticker"], "MSFT")
        self.assertIn("stop price hit", plan["positions_to_exit"][0]["reason"])

    def test_trails_stop_to_breakeven_after_profit_trigger(self):
        state = base_state(
            open_positions=[
                {
                    "ticker": "MSFT",
                    "qty": 0.1,
                    "avg_cost": 100.0,
                    "current_price": 108.0,
                    "stop_price": 95.0,
                    "target_price": 112.0,
                    "atr": 5.0,
                    "sessions_held": 2,
                }
            ],
            open_position_count=1,
        )
        plan = build_plan(base_signals(), state, base_config())

        self.assertEqual(len(plan["positions_to_trail"]), 1)
        self.assertEqual(plan["positions_to_trail"][0]["new_stop"], 100.0)
        self.assertEqual(plan["positions_to_trail"][0]["action"], "TRAIL_STOP_TO_BREAKEVEN")

    def test_chandelier_trail_ratchets_above_breakeven_deep_in_profit(self):
        cfg = base_config()
        cfg["exits"]["chandelier_atr_mult"] = 3.0
        # Price far beyond breakeven: chandelier (130 - 3*5 = 115) exceeds
        # both breakeven (100) and the current stop (100), so the stop ratchets
        # up to 115 and the action is the chandelier trail.
        state = base_state(
            open_positions=[
                {
                    "ticker": "MSFT",
                    "qty": 0.1,
                    "avg_cost": 100.0,
                    "current_price": 130.0,
                    "stop_price": 100.0,
                    "target_price": 200.0,
                    "atr": 5.0,
                    "sessions_held": 4,
                }
            ],
            open_position_count=1,
        )
        plan = build_plan(base_signals(), state, cfg)

        self.assertEqual(len(plan["positions_to_trail"]), 1)
        trail = plan["positions_to_trail"][0]
        self.assertEqual(trail["new_stop"], 115.0)
        self.assertEqual(trail["action"], "TRAIL_STOP_CHANDELIER")


    def test_sample_or_stale_state_blocks_new_entries(self):
        plan = build_plan(base_signals(setup()), base_state(source="sample"), base_config())
        self.assertTrue(plan["kill_switch_triggered"])
        self.assertEqual(plan["qualified_setups"], [])

        plan = build_plan(base_signals(setup()),
                          base_state(generated_at="2026-01-01T00:00:00+00:00"),
                          base_config())
        self.assertTrue(plan["kill_switch_triggered"])
        self.assertEqual(plan["qualified_setups"], [])

    def test_stale_signals_block_new_entries(self):
        signals = base_signals(setup())
        signals["session_date"] = "2026-06-08"
        plan = build_plan(signals, base_state(), base_config())
        self.assertTrue(plan["kill_switch_triggered"])
        self.assertEqual(plan["qualified_setups"], [])

    def test_size_multiplier_never_exceeds_caps(self):
        signals = base_signals(setup())
        signals["vol_target_mult"] = 1.5
        plan = build_plan(signals, base_state(), base_config())
        entry = plan["qualified_setups"][0]
        self.assertLessEqual(entry["dollar_amount"], 10.0)  # max_position_notional

    def test_exits_still_execute_under_kill_switch(self):
        state = base_state(
            source="sample",  # forces kill switch
            open_positions=[{
                "ticker": "MSFT", "qty": 0.1, "avg_cost": 100.0,
                "current_price": 94.0, "stop_price": 95.0,
                "target_price": 110.0, "atr": 5.0, "sessions_held": 2,
            }],
            open_position_count=1,
        )
        plan = build_plan(base_signals(setup(ticker="AAPL")), state, base_config())
        self.assertTrue(plan["kill_switch_triggered"])
        self.assertEqual(len(plan["positions_to_exit"]), 1)  # exit not blocked
        self.assertEqual(plan["qualified_setups"], [])


if __name__ == "__main__":
    unittest.main()
