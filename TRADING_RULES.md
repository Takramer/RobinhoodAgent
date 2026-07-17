# Trading Rules

These rules govern Claude/Codex execution after Python has produced `plan.json`.

## Hard stops

- If `plan.json.kill_switch_triggered` is true, place no entry orders.
- If daily or weekly loss limits are exhausted, place no entry orders.
- Never hardcode `account_number`; call Robinhood MCP `get_accounts` each session.
- Never call `place_equity_order` without first calling `review_equity_order`.

## Entry rules

- Only execute entries listed in `plan.json.qualified_setups`.
- Entries are long equity buys only.
- Use `type=market` with `dollar_amount` for fractional-share entries.
- Before each entry:
  - call `get_equity_tradability` for the ticker
  - skip if the ticker is not tradeable
  - skip fractional orders if the ticker is not fractionally tradeable
  - call `review_equity_order`
  - stop if Robinhood returns blocking alerts, halted warnings, or insufficient buying power

## Exit rules

- Execute exits listed in `plan.json.positions_to_exit` before entries.
- Exits are sells of existing long positions only.
- Call `review_equity_order` before each exit.

## Trail rules

- `plan.json.positions_to_trail` is a local stop-management instruction.
- Robinhood MCP order placement for stop updates should only happen after support for persistent stop orders is tested.
- Until then, log trail updates in `decision_log.md` and keep state synced manually or through the next portfolio snapshot.

## Logging

Append every session to `decision_log.md`:

- timestamp
- SPY regime and strategy
- account number suffix only, never full account number
- exits reviewed/placed
- entries reviewed/placed
- skipped tickers and reasons
- Robinhood alerts or errors
- final decision summary
