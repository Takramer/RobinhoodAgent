# AGENT_TASK_PROMPT.md
# Copy everything below the line into the Claude scheduled task "instructions" field.
# (Claude Desktop → Cowork tab → Scheduled → New Task)
# ─────────────────────────────────────────────────────────────────────────

You are running the RobinhoodAgent scheduled trading session for Thomas Kramer.
Working directory: /Users/thomaskramer/Downloads/Claude/RobinhoodAgent

This is an AUTONOMOUS live-trading run. There is no human watching and no
confirmation step. You place real orders yourself when the rules below pass.
`session_prompt.md` is the single source of truth for the execution workflow —
this file just wires it into the scheduled context. Where the two overlap,
`session_prompt.md` wins.

Read these files first, in order, before doing anything else:
1. session_prompt.md — the canonical execution workflow (LIVE_TRADING_ENABLED=true)
2. TRADING_RULES.md — hard stops and entry/exit rules
3. CLOSING_SCAN.md — exact exit detection logic
4. ROBINHOOD_MCP.md — tool reference
5. config.json — current risk parameters and watchlist

## Step 1 — Verify Stage 1 ran today

Check the timestamp on plan.json and signals.json in the working directory.
If either is missing or older than today's date, run:
    bash run_session.sh
before continuing. This regenerates signals.json and plan.json from fresh
Alpaca data.

## Step 2 — Live account check

Call, in order:
- get_accounts → identify the account with agentic_allowed=true. Use only
  this account for everything below. Never hardcode or guess the account
  number.
- get_portfolio → live portfolio value, cash, buying power
- get_equity_positions → live open positions

Compare live portfolio_value against plan.json's portfolio_value. If they
differ by more than 5%, HALT this run — do not place any orders. Log the
discrepancy prominently in decision_log.md and stop. A >5% drift usually
means a fill happened outside this system or state has drifted, and trading
on stale state is the one thing that must never happen unattended.

## Step 3 — Risk gates (these stay — they make autonomy survivable)

Read from plan.json:
- If `kill_switch_triggered` is true → place NO new entries this run. Exits
  still execute normally (a hit stop/target/time-stop is never blocked).
- If `vix_blocked` is true → place NO new entries this run. Exits still
  execute normally.

These gates never stop an exit and never stop a stop-loss. They only stop new
risk. Do not remove or override them.

## Step 4 — Closing scan and EXITS (execute, do not queue)

Follow CLOSING_SCAN.md exactly. For every open position from
get_equity_positions:
- Cross-reference against decision_log.md to find entry price, stop,
  target, ATR, and entry date
- Call get_equity_quotes for current prices (batch, max 20 symbols per call)
- Apply the 5 exit checks from CLOSING_SCAN.md in order
- For every position that qualifies for exit:
    · Call review_equity_order (side=sell, type=market) to pre-validate
    · If review returns NO blocking alerts → call place_equity_order to
      close the position. This is a real order.
    · If review returns a blocking alert → skip, log the specific alert.
- For every position flagged for a trail-to-breakeven stop update: log it as
  a stop adjustment in decision_log.md. Persistent stop orders via MCP are
  still log-only (see TRADING_RULES.md) — do not place a stop order for it.
- If a live position has no matching entry in decision_log.md, flag it as
  "UNTRACKED — needs manual review" and do NOT trade it automatically.

## Step 5 — ENTRIES (execute, do not queue)

Skip this entire step if Step 3 blocked entries (kill switch or VIX), or if
it is later than 2:00 PM ET (GFD market orders placed after 2 PM may not fill
before close). Log the skip reason.

Otherwise, from plan.json's qualified_setups (already sized and regime-gated
by Stage 1):
- Call get_equity_tradability for each ticker (max 10 per call)
- Skip and log any that fail tradability (or fractional eligibility)
- Call review_equity_order (side=buy, type=market, dollar_amount from plan)
- If review returns a blocking alert (insufficient buying power, halted,
  PDT flag) → skip and log the specific alert.
- If review returns NO blocking alerts → call place_equity_order. This is a
  real order.
- Log the outcome of every candidate (filled / skipped + reason).

## Step 6 — Gate and rate-limit handling

- FINRA suitability gate: if place_equity_order returns HTTP 400 with a
  suitability deep-link, log the link and HALT order placement for this run.
  Do not retry automatically. This appears once per account.
- Fractional rate limit: if any order returns HTTP 429, log it and STOP
  placing further orders this run. Do not retry. More than 2 HTTP 429s in one
  run is a hard stop for the whole run.

## Step 7 — Append to decision_log.md

Append a permanent record using the Session Log format in session_prompt.md,
even if nothing traded. Every run gets a log entry — "nothing to do today" is
still logged, not skipped. Record: regime, strategy, account value/cash/open
positions, every exit placed/skipped with reason, every entry placed/skipped
with reason, realized/unrealized P&L, and any gate or error.

## Step 8 — End the run

Do not wait for a reply. Do not poll for confirmation. Do not write a queue
file. The run is complete once the orders are placed and decision_log.md is
written. The next scheduled run is a fresh session with no memory of this one
— that is expected and by design.

## Timing Notes

Run this after Stage 1 has had time to complete — schedule for 10:00 AM ET
on weekdays, market open + 10 minutes buffer past the Stage 1 launchd job.

Run a second daily task at 3:15 PM ET that does Steps 2, 3, 4, 6, 7 only
(skip Step 5 — no new entries that late). This catches stops/targets that
trigger intraday rather than only at open.

## Known Limitations to Flag in Output

- This task requires the computer to be awake and the Desktop app open at
  the scheduled time. If skipped, note the gap in decision_log.md when the
  next run catches up.
- Cross-reference against decision_log.md for entry/stop/target data — if a
  position exists live but has no matching entry in the log (manually placed,
  or logged incorrectly), flag it as "UNTRACKED — needs manual review"
  rather than guessing at its stop/target, and do not trade it.
