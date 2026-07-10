# Robinhood MCP Contract

Source: https://robinhood.com/us/en/support/articles/trading-with-your-agent/

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

## Account and portfolio tools

- `get_accounts` - view Robinhood accounts. Always call this first and read `account_number`; never hardcode it.
- `get_portfolio` - get portfolio value, buying power, and asset-class breakdown.
- `get_equity_positions` - get open equity positions with quantity and cost basis.
- `get_equity_quotes` - get real-time equity quotes and prior close for up to 20 symbols.
- `get_equity_orders` - get equity order status history.
- `search` - resolve company names or partial names to tickers.

## Watchlist tools

- `add_to_watchlist`
- `follow_list`
- `get_popular_lists`
- `get_watchlists`
- `get_watchlist_items`
- `unfollow_list`
- `update_watchlist`

## Equity order tools

- `get_equity_tradability` - check whether a symbol is tradeable and fractionally tradeable.
- `review_equity_order` - simulate an equity order and receive pre-trade warnings.
- `place_equity_order` - place a real equity order.
- `cancel_equity_order` - cancel an open equity order.

## Options tools

Options are currently rolling out. Robinhood lists these tools, but this project should ignore them until a separate options strategy exists:

- `get_option_chains`
- `get_option_instruments`
- `get_option_quotes`
- `get_option_positions`
- `get_option_orders`
- `review_option_order`
- `cancel_option_order`
- `place_option_order`

## Current project safety rules

- No live trades during dry-run development.
- Entries must be long equity buys only.
- Bear-side exposure must use inverse ETFs from `config.json`; do not attempt short selling.
- Every planned order must call `review_equity_order` before `place_equity_order`.
- Fractional entries use market orders with `dollar_amount`.
- `place_equity_order` is allowed only when the session prompt explicitly says `LIVE_TRADING_ENABLED=true`.
