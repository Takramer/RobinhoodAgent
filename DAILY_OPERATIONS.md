# RobinhoodAgent — Daily Operational Timeline

---

## Autonomous Workflow (Current)

Two automated layers now run without you touching anything:

1. **launchd (Stage 1)** — 9:50 AM ET weekdays. Pure Python, fully background,
   no app needs to be open. Scans, sizes, writes plan.json.
2. **Scheduled agent task (Stage 2 — autonomous execution)** — 10:00 AM ET
   weekdays. Reads Stage 1 output, checks live Robinhood account, runs the
   closing scan on open positions, and **places real orders itself**
   (`review_equity_order` → `place_equity_order`) for every qualifying exit
   and entry. No queue file, no confirmation reply — it trades and logs to
   `decision_log.md`. Risk gates still apply (kill switch, daily/weekly loss
   limits, VIX blocks, SL/TP/time-stop exits). See AGENT_TASK_PROMPT.md for
   the exact instructions this task runs.

Stage 2 also runs a closing session at 3:15 PM ET (exits only — no entries
after 2 PM ET).

**Manage-only mode is ON.** `config.json` → `"entries_enabled": false`. The
planner emits zero `qualified_setups`, so the scheduled runs only manage open
positions: exits, trails, logging. To resume buying, set it back to `true` —
and consider restoring the midday runs (12:20/13:35 Stage 1, 12:30/13:45
Stage 2) that were removed from the two plists alongside it.

**Which agent runs Stage 2 is a switch: `executor.conf`.** One word —
`claude`, `codex`, or `off`. Both CLIs run the same AGENT_TASK_PROMPT.md
against the same Robinhood MCP; `off` logs the skip and places no orders. Env
wins for a single run: `EXECUTOR=claude bash run_stage2.sh`. Currently set to
**codex** to stop spending Claude usage — see CODEX_HANDOFF.md for the
one-time commissioning steps.

Under `claude` the session runs on Sonnet at medium effort with a 60-turn
budget; under `codex` on gpt-5.6-terra at medium effort, no turn cap. The
permission fence differs: Claude gets a scoped tool allowlist, Codex gets
`approval_policy=never` plus a workspace-write sandbox. Either way the session
is clerical — the judgement is in order_plan.py.

Stage 1 also scores `setup_log.csv` daily (`outcome_tracker.py`, step 5/5) —
offline off the Massive cache, no API calls. `python3 outcome_tracker.py
--summary` prints win rate and avg R by score and strategy without refetching.
Stage 2 skips its morning session when `plan.nothing_to_manage` is true (flat
book, entries off); the closing run always goes live.

`validate_plan.py` gates both stages: Stage 1 checks the plan it writes, Stage
2 re-checks the plan it regenerates from live account state. A rejected plan
halts the run with no orders placed and the FAIL lines in `decision_log.md`;
run `python3 validate_plan.py` yourself to see why. Stage 2 also exits before
spending anything if it fires outside 9:30–16:00 ET — a slot missed while the
Mac slept used to run a full session against a closed market.

Everything below reflects the fully manual version of this workflow — still
accurate if you're running Stage 2 by hand instead of via the scheduled task.

---

## Every Weekday

### 9:50 AM ET — Stage 1 fires automatically (no action required)
macOS launchd runs `run_session.sh`. You don't open anything.

What happens in ~90 seconds:
1. Fetches bars for the full watchlist (~900 tickers) + SPY
2. Classifies SPY regime → selects strategy
3. Estimates VIX regime from SPY ATR
4. Scores every ticker — 3/4 and 4/4 setups go to `qualified_setups`; the
   meta-model P(win) filter in order_plan is the quality gate
5. Checks kill switches (daily/weekly loss limits)
6. Sizes positions with ATR math, applies VIX multiplier, enforces sector cap
7. Writes `signals.json` → `plan.json`
8. Appends all 3/4 and 4/4 setups to `setup_log.csv`

Output lands in `logs/session.log`. Check it if you want to see what fired.

---

### 10:00–10:15 AM ET — Stage 2 executes autonomously (no action required)

**Option A — scheduled task (default, fully autonomous)**
The scheduled task runs AGENT_TASK_PROMPT.md automatically through whichever
CLI `executor.conf` names. It checks the live
account, runs the closing scan, and places every qualifying exit and entry
itself (`review_equity_order` → `place_equity_order`), then logs to
`decision_log.md`. You do not confirm anything. Review `decision_log.md`
afterward if you want to see what it did.

**Option B — Manual chat session (fallback)**
Come to this Claude chat. Say "run Stage 2."
Claude reads `plan.json`, connects to Robinhood MCP, and runs the same
autonomous execution workflow from `session_prompt.md`.

**What happens during execution (either option):**
- Exits first: any position that hit its stop, target, or 10-session time limit
- Then entries: each 4/4 setup in plan.json → `review_equity_order` → `place_equity_order`
- Fractional rate limit: no more than ~10 orders per session before a 429

**No new entries after 2:00 PM ET.** GFD market orders placed after 2 PM may not fill
before close and create confusion. Runs after 2 PM should place no new entries (exits still run).

---

### 3:15 PM ET — Outcome check (optional, 2 minutes)

```bash
cd ~/Downloads/Claude/RobinhoodAgent
python3 outcome_tracker.py
```

What it does:
- Fetches recent bars for all open setups in `setup_log.csv`
- Walks bar-by-bar to detect if stop or target was hit since signal date
- Updates `outcome_reason` (TP / SL / Time) and `outcome_pnl_r` (R-multiples)
- Prints a quick summary: win rate by score, average R, open positions

Also a good time to check if any open position is approaching:
- Its stop price (within 0.3× ATR) — consider closing before overnight
- Its 10-session time limit (at session 8 or 9) — prepare to exit next day
- A trailing stop update (if position is up 1.5× ATR) — move stop to breakeven

For now these trail updates are manual: look at `decision_log.md` and the Robinhood app.
Automated stop management comes in a later phase once MCP stop orders are tested.

---

### 4:15 PM ET — End-of-day review (optional, 2 minutes)

```bash
tail -50 ~/Downloads/Claude/RobinhoodAgent/decision_log.md
```

Skim for: any errors, unexpected skips, rate-limit flags, order fills confirmed.
Takes 2 minutes. If everything looks normal, you're done for the day.

---

## Weekly (Friday After Close)

```bash
cd ~/Downloads/Claude/RobinhoodAgent
python3 outcome_tracker.py --summary
```

This prints the full breakdown:
- Total signals logged (3/4 and 4/4 separately)
- Win rate and average R for each score tier
- Win rate by strategy
- All still-open setups with entry prices

**What you're looking for over time:**
- Is 4/4 win rate materially higher than 3/4? If yes, keep the hard filter.
- Is 3/4 win rate above 50%? If yes, consider whether lowering the threshold makes sense.
- Which strategy is producing the best R? Does the regime selection match that?
- Are any sectors consistently producing losers? Consider removing them from the watchlist.

---

## What to Do If Something Looks Wrong

| Situation | What to do |
|---|---|
| `logs/session.log` shows an error | Read the error, check if it's Alpaca credentials or a bad ticker |
| `plan.json` has 0 qualified setups | Normal — market conditions didn't qualify anything today |
| `plan.json` has `kill_switch_triggered: true` | Check the reason — daily or weekly loss limit hit |
| `plan.json` has `vix_blocked: true` | VIX regime too high — no entries today, correct behavior |
| `vix_regime` and `vix_regime_proxy` disagree in the log | Expected. The proxy is SPY realized vol, `vix_level` is the real index. Only the proxy sizes trades until `config.json` sets `"vix_source": "robinhood"` |
| `vix_level: null` in the plan | The VIX index call failed. Sizing fell back to the proxy — no action needed |
| Entries skipped with "reports earnings inside the blackout window" | Correct — a report lands within 3 sessions. PEAD entries are never blacked out this way |
| Entries skipped with "wide/absent book" | Spread was over 50 bps. Usually a halt or a pre-open quote — check the ticker before overriding |
| Stage 2 returns HTTP 429 | Rate limit hit — stop entries for today, try again tomorrow |
| Order filled at unexpected price | Market order slippage — acceptable on liquid large-caps |
| Position not in Robinhood app | Check `decision_log.md` for the exact fill confirmation |

---

## Daily Time Cost

| Time | Task | Duration |
|---|---|---|
| 9:50 AM | Stage 1 fires (automatic) | 0 min |
| 10:00 AM | Stage 2 execution | 2–3 min |
| 3:15 PM | Outcome tracker (optional) | 2 min |
| 4:15 PM | Log review (optional) | 2 min |
| **Total** | | **~5–7 min/day** |

Friday weekly review adds ~5 minutes. The system is designed to run mostly without you.

---

## When to Intervene Manually

Almost never. The only reasons to open Robinhood app or run a mid-day session:

- A position is down >3% intraday on news (earnings surprise, halt, fraud)
- SPY drops >2% gap at open after Stage 1 already ran — the signal is stale
- You want to close a position early for personal reasons

Outside of these, let the system run its full cycle. Intervening on normal volatility
is the fastest way to break a strategy that's working.

---

## File Outputs After Each Session

```
signals.json     → What the scanner found (overwrites each day)
plan.json        → What the agent plans to do (overwrites each day)
setup_log.csv    → Permanent record of all 3/4 and 4/4 setups (append-only)
decision_log.md  → Permanent record of all execution decisions (append-only)
logs/session.log → Terminal output from run_session.sh (overwrites each day)
```

`setup_log.csv` and `decision_log.md` are the only permanent records.
Open `setup_log.csv` in Numbers or Excel any time to analyze the data.
