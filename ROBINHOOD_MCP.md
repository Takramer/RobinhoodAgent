# Robinhood MCP Contract

Source: https://robinhood.com/us/en/support/articles/trading-with-your-agent/

Verified against the live server on 2026-08-20. Tool names here are exact —
earlier revisions of this file carried names that do not exist
(`follow_list`, `get_popular_lists`), and a Stage 2 run that guesses a name
burns a turn on an error.

Codex config:

```bash
codex mcp get robinhood
```

Expected server:

```text
name: robinhood
transport: streamable_http
url: https://agent.robinhood.com/mcp/trading
auth: OAuth
```

Claude Code project config:

```bash
claude mcp get robinhood
```

---

## Account and portfolio

- `get_accounts` — list accounts. Call first every session; read `account_number`
  and `agentic_allowed`. Never hardcode. Buying power here is unreliable — use
  `get_portfolio`.
- `get_portfolio` — portfolio value, buying power, asset-class breakdown.
- `get_equity_positions` — open equity positions with quantity and cost basis.
- `get_equity_orders` — order history; filter by `state`, `symbol`,
  `created_at_gte`, `placed_agent` (`agentic` = orders this system placed).
- `get_equity_tax_lots` — open lots per symbol, for specified-lot selling.
- `get_realized_pnl` — bucketed realized P&L over a window. Drives the daily and
  weekly loss limits.
- `get_pnl_trade_history` — per-trade realized P&L, chronological and paginated.
- `get_limited_margin_upgrade_info` — margin enrollment status.

## Live market data

- `get_equity_quotes` — real-time quotes plus prior close, up to 20 symbols.
- `get_equity_price_book` — **Level 2 order book**: full bid/ask ladder with
  resting size per level. Max 4 symbols per call. Used here as a pre-trade
  spread guard, not for alpha.
- `get_equity_fundamentals` — market cap, float, PE, 52-week range, trailing
  volume averages, today's OHLCV. Max 10 symbols.
- `get_financials` — statement-level company financials.
- `get_earnings_calendar` — market-wide earnings schedule over a window of up to
  31 days, forward or backward, with estimated/actual EPS and am/pm timing.
  `filter="high_market_cap"` limits to names above $1B. **This is the earnings
  blackout source.**
- `get_earnings_results` — earnings history for one known ticker.
- `get_indexes` — index ids by symbol. VIX, SPX, and NDX are available; RUT is
  restricted on this channel. **VIX id is the input to the live VIX gate.**
- `get_index_quotes` — live index values by id.
- `get_index_historicals` — index OHLC history.
- `get_equity_historicals` — OHLCV bars, up to 10 symbols, split-adjusted.
- `get_equity_technical_indicators` — server-side RSI, MACD, ATR, Bollinger,
  Keltner, supertrend, ADX, VWAP, OBV, and more over one symbol.
- `search` — resolve a name or partial name to an instrument, crypto pair
  (`asset_type="currency_pair"`), or index (`asset_type="market_index"`).

**Historical bars and indicators are not used by this project.** AgentTradingLogic
owns backtesting and model training. RobinhoodAgent reads only live state.

## Screening

- `get_scanner_filter_specs` — the full filter vocabulary. Call before building
  filters; do not guess `filter_type` names.
- `create_scan`, `get_scans`, `run_scan`, `update_scan_config`,
  `update_scan_filters` — saved market-wide screeners, evaluated live at request
  time. Roughly 50 filters across fundamentals, price/volume, options, technicals.

Not used by this project — the universe is fixed in `config.json`.

## Watchlists

`get_watchlists`, `create_watchlist`, `get_watchlist_items`, `add_to_watchlist`,
`remove_from_watchlist`, `update_watchlist`, `follow_watchlist`,
`unfollow_watchlist`, `get_popular_watchlists`.

Options have a separate watchlist: `get_option_watchlist`,
`add_option_to_watchlist`, `remove_option_from_watchlist`.

## Equity orders

- `get_equity_tradability` — tradeable and fractionally-tradeable flags.
- `review_equity_order` — simulate; returns the live quote plus pre-trade alerts
  (buying power, PDT, halt).
- `place_equity_order` — real order.
- `cancel_equity_order` — cancel an open order.

Order capability, current server:

| Feature | Note |
|---|---|
| `type` | `market`, `limit`, `stop_market`, `stop_limit` |
| `time_in_force` | `gfd`, `gtc` — **resting GTC stops are supported** |
| `market_hours` | `regular_hours`, `extended_hours`, `all_day_hours` |
| `dollar_amount` | requires `type=market` |
| Fractional shares | `type=market` + `regular_hours` only |
| `tax_lots` | specified-lot selling on sells, max 30 lots |
| `ref_id` | idempotency key — reuse on transport retries only |

Outside regular hours only limit orders execute. Market and stop orders placed
after hours queue for the next regular open.

**Stop orders and fractional positions are mutually exclusive.** Every position
this system opens is fractional (`dollar_amount` market orders), so resting
stops are unreachable at the current account size. See TRADING_RULES.md.

## Options

Full stack, gated on `option_level_2` or higher and `agentic_allowed`:
`get_option_chains`, `get_option_instruments`, `get_option_quotes`,
`get_option_historicals`, `get_option_positions`, `get_option_orders`,
`review_option_order`, `place_option_order`, `cancel_option_order`,
`exercise_option`, `cancel_option_exercise`, `get_option_level_upgrade_info`.

Out of scope. This project trades long equity only.

## Not available

- **Crypto trading.** No crypto quote or order tool exists. Crypto appears only
  as a `search` asset type, a watchlist id type, a `get_realized_pnl` asset
  class, and a scanner instrument type.
- **Futures.** App only.
- `preview_scan`, `get_scanner_datapoints`, `get_advanced_orders` — referenced in
  other tools' descriptions but not exposed by this server. Do not call them.

---

## Project safety rules

- Entries are long equity buys only. Bear exposure uses the inverse ETFs in
  `config.json`; never short.
- Every order calls `review_equity_order` before `place_equity_order`.
- Fractional entries use market orders with `dollar_amount`.
- `place_equity_order` runs only when the session prompt says
  `LIVE_TRADING_ENABLED=true`, and only against the account with
  `agentic_allowed=true`.
- Never log the full account number — suffix only.
