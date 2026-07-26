# CLOSING_SCAN.md — Exit Detection & Execution Logic

**This is the spec. `order_plan.py` (`_build_exit_and_trail_actions`) is the
implementation.** The scheduled agent does NOT re-derive these checks by hand:
it writes live prices into `state.json`, runs `order_plan.py`, and executes
`plan.json.positions_to_exit` / `positions_to_trail`. Two engines evaluating
the same rules is how they drift apart — and it cost a few thousand tokens a
run. Read this file when changing the rules; do not read it during a run.

Checks run against live Robinhood prices (`get_equity_positions` +
`get_equity_quotes`) written into `state.json` at the start of each session.

Every exit detected here is executed immediately: `review_equity_order`
(side=sell, type=market) → if no blocking alerts → `place_equity_order`. There
is no queue and no confirmation step. Exits are never blocked by the kill
switch or VIX gate — only new entries are.
---

## Data Required Per Open Position

Pull from `decision_log.md` (the historical record of what was entered and at
what stop/target) cross-referenced with live data:

| Field | Source |
|---|---|
| Entry price, stop, target, ATR | `decision_log.md` — the entry that opened this position |
| Entry date | `decision_log.md` |
| Current price | `get_equity_quotes` (live) |
| Current quantity / shares held | `get_equity_positions` (live) |
| Sessions held | Count trading days between entry date and today |

If a position exists in `get_equity_positions` but has no matching entry in
`decision_log.md` (e.g. a manual trade placed outside the agent), flag it in
`decision_log.md` as `UNTRACKED — needs manual review` and do not attempt to
size or place an exit for it automatically.

---

## Exit Checks (in order — first match wins)

### 1. Hard Stop Loss
```
if direction == "bull" and current_price <= stop_price:
    → EXIT NOW, reason: "SL", full position, market order
if direction == "bear" and current_price >= stop_price:
    → EXIT NOW, reason: "SL", full position, market order
```
No exceptions. No "let's see if it recovers." If price has touched the stop,
it is sold at this scan (review → place) regardless of any other condition below.

### 2. Take Profit Target
```
if direction == "bull" and current_price >= target_price:
    → EXIT NOW, reason: "TP", full position, market order
if direction == "bear" and current_price <= target_price:
    → same logic, inverse direction
```
Full position, not a 75% partial: at current sizing a 25% runner is roughly
$1 of stock, at or below Robinhood's fractional order minimum, so the stub
could not be sold later. The Chandelier trail (check 3) is what lets winners
run instead.

### 3. Breakeven Trail Trigger
```
profit_in_atr = (current_price - entry_price) / atr   [bull]
              = (entry_price - current_price) / atr   [bear]

if profit_in_atr >= 1.5 and stop_price != entry_price:
    → STOP UPDATE (not a full exit): raise stop to the higher of breakeven
      (entry_price) and the Chandelier level (current_price − chandelier_atr_mult × ATR).
      Ratchet up only — never lower the stop.
    → Log this as a trail update in decision_log.md, not a trade. Persistent
      stop orders via MCP are still log-only (see TRADING_RULES.md) — record
      the new stop and let the next scan enforce it, do not place a stop order.
```

### 4. Time Stop
```
if sessions_held >= 10:
    → EXIT NOW, reason: "Time", full position, market order
```
Leveraged/inverse ETFs (from `leveraged_tickers` in config.json) use
`sessions_held >= 3` instead of 10.

### 5. Session-End Proximity Check (informational only)
```
if abs(current_price - stop_price) <= (0.3 * atr):
    → FLAG in decision_log.md: "Near stop — overnight gap risk"
```
This does not auto-execute an exit — price has not hit the stop yet, so
selling here would override the strategy's own risk parameters. It is logged
as a heads-up only; the hard stop in check 1 handles the actual exit if price
reaches it.

---

## Kill Switch Checks (run before any entries are placed, not just exits)

```
daily_realized_pnl_pct = (todays_realized_pnl / portfolio_value) * 100
if daily_realized_pnl_pct <= -3.0:
    → BLOCK all new entries this session
    → Existing exits (stop/target/time) still execute normally — the kill
      switch stops new risk, it doesn't freeze positions that need to exit

weekly_realized_pnl_pct = (weekly_realized_pnl / monday_open_value) * 100
if weekly_realized_pnl_pct <= -5.0:
    → BLOCK all new entries until next Monday
    → Same exception: hard stops and time stops still execute normally
```

---

## Output Format

Every closing scan executes qualifying exits immediately (review → place) and
appends a permanent record to `decision_log.md` regardless of whether anything
qualified — a "nothing to exit today" entry is still logged so the audit trail
has no gaps. Use the Session Log format in `session_prompt.md`.

---

## What This File Does NOT Do

- Does not place a new *entry*. This file only ever reduces or closes exposure.
- Does not place a persistent stop order. Trail/breakeven updates are logged to
  `decision_log.md`; the next scan enforces them via check 1.
- Does not modify `state.json` — that remains the dry-run Python-side model.
  Live position tracking is `decision_log.md` + live MCP calls, kept separate
  on purpose so a bug in the dry-run pipeline can never touch real positions.
- Does not average down, add to positions, or pyramid.
