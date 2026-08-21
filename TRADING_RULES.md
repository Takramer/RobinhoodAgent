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
  - call `get_equity_price_book` and skip if the spread is wider than 50 bps or
    either side of the book is empty
  - call `review_equity_order`
  - stop if Robinhood returns blocking alerts, halted warnings, or insufficient buying power
- Skip any ticker listed in `plan.json.earnings_blackout`. A report inside the
  next three sessions is a coin flip the setup did not price in. PEAD is not
  affected — it trades the days after a report, not before one.

## Liquidity rule

The order book is a safety check, not a signal. Its only job is to stop a market
order going into a broken or absent quote. Do not read depth for direction, and
do not size against it — on S&P 500 and midcap names at this account size, top of
book is orders of magnitude deeper than any order this system places.

## Exit rules

- Execute exits listed in `plan.json.positions_to_exit` before entries.
- Exits are sells of existing long positions only.
- Call `review_equity_order` before each exit.

## Trail rules

- `plan.json.positions_to_trail` is a local stop-management instruction.
- Log trail updates in `decision_log.md` and keep state synced through
  `state.json` at the next session.

### Resting stops — unblocked at the broker, blocked by position type

`place_equity_order` now accepts `type=stop_market` with `time_in_force=gtc`, so
the server-side stop this file used to be waiting on exists. It is still not
usable here: stop orders require whole shares, and every position this system
opens is fractional (`dollar_amount` market orders). The two are mutually
exclusive at Robinhood.

The consequence is worth stating plainly: stops are only enforced when a scan
runs, four times a day. A gap down at 9:31 is not sold until the 9:45 scan.

Turn resting stops on when positions become whole-share — that is, when the
account is large enough that `dollar_amount` sizing produces one or more full
shares. At that point place a GTC `stop_market` at `stop_price` immediately after
each entry fills, and cancel it on exit or when a trail moves the level.

## Fill confirmation

An acknowledged order is not a filled order. After placing, read the orders and
positions back from the broker and build `state.json` from broker quantities,
never from `plan.json`. A partial fill that gets journalled as a full one makes
every later stop, trail, and loss-limit calculation wrong, and nothing surfaces
the error until the position is closed.

Pass the `ref_id` that `order_plan.py` stamps on every entry and exit. It is a
uuid5 of session date + ticker + side, so a retried session re-sends the same
key and the broker dedupes rather than doubling the position.

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
