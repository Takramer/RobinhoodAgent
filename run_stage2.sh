#!/bin/bash
# Stage 2 under launchd: headless Claude Code session that executes the plan
# through the Robinhood MCP per AGENT_TASK_PROMPT.md. Autonomous — places real
# orders when LIVE_TRADING_ENABLED=true in session_prompt.md.
#
# Permissions are a scoped allowlist (NOT --dangerously-skip-permissions):
# Robinhood MCP tools, project file reads/writes, and the specific pipeline
# commands Stage 2 is allowed to run. Anything else prompts — and in headless
# mode an un-allowlisted call fails closed rather than executing.
set -uo pipefail
cd "$(dirname "$0")"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$PWD/engine"

STAMP=$(date '+%Y-%m-%d %H:%M %Z')
echo "── Stage 2 session $STAMP ──"

# Market-hours guard. launchd does not wake the machine: a slot missed while
# asleep fires whenever the Mac comes back, which on 2026-07-27 meant a 15:15
# "closing run" at ~19:00 with the market shut. No order can fill then, so
# spend no tokens — log the miss and exit. session_prompt.md rule 1.
# ponytail: weekday 9:30–16:00 ET only, no holiday or half-day calendar. On
# those days the session runs and finds nothing tradeable, which costs one
# cheap run; add a calendar if that ever matters.
read -r DOW HHMM <<< "$(TZ=America/New_York date '+%u %H%M')"
MINS=$((10#$HHMM))  # 10# so 0930 is not read as octal
if [ "$DOW" -gt 5 ] || [ "$MINS" -lt 930 ] || [ "$MINS" -gt 1600 ]; then
  echo "SKIPPED: market closed at $(TZ=America/New_York date '+%a %H:%M ET') \
— scheduled slot missed (machine asleep?). No session run." | tee -a decision_log.md
  exit 0
fi

# Flat-book skip. With entries off and no positions, the morning session exists
# only to confirm zero. order_plan.py decides this (plan.nothing_to_manage);
# anything unreadable or stale falls through and runs the session. The closing
# run always goes live, so a fill placed outside this system is still caught
# the same day.
if [ "$MINS" -lt 1400 ] && python3 -c "
import json, sys, datetime
p = json.load(open('plan.json'))
sys.exit(0 if p.get('nothing_to_manage') and
              p.get('session_date') == datetime.date.today().isoformat() else 1)
" 2>/dev/null; then
  echo "SKIPPED: nothing to manage at $(TZ=America/New_York date '+%a %H:%M ET') \
— flat book, entries disabled. Closing run still checks live." | tee -a decision_log.md
  exit 0
fi

# Sonnet at medium effort: the session is mechanical — read plan.json, copy
# live numbers into state.json, run the planner, place the sells it lists. The
# judgement lives in order_plan.py, not here. Opus was ~5x the cost for the
# same clerical work. Turn budget 60: a manage-only run needs ~15.
claude -p "$(cat AGENT_TASK_PROMPT.md)" \
  --model sonnet \
  --effort medium \
  --allowedTools \
    "mcp__robinhood" \
    "mcp__claude_ai_Robinhood" \
    "Read" "Glob" "Grep" \
    "Edit(./state.json)" "Edit(./decision_log.md)" \
    "Bash(python3 order_plan.py*)" "Bash(bash run_session.sh*)" \
    "Bash(python3 validate_plan.py*)" \
    "Bash(python3 outcome_tracker.py*)" "Bash(date*)" "Bash(cat *)" \
  --max-turns 60 2>&1
STATUS=$?

if [ $STATUS -ne 0 ]; then
  echo "STAGE 2 FAILED (exit $STATUS) at $STAMP — no orders may have been placed." \
    | tee -a decision_log.md
fi
exit $STATUS
