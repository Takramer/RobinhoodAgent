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

claude -p "$(cat AGENT_TASK_PROMPT.md)" \
  --allowedTools \
    "mcp__robinhood" \
    "mcp__claude_ai_Robinhood" \
    "Read" "Glob" "Grep" \
    "Edit(./state.json)" "Edit(./decision_log.md)" \
    "Bash(python3 order_plan.py*)" "Bash(bash run_session.sh*)" \
    "Bash(python3 outcome_tracker.py*)" "Bash(date*)" "Bash(cat *)" \
  --max-turns 80 2>&1
STATUS=$?

if [ $STATUS -ne 0 ]; then
  echo "STAGE 2 FAILED (exit $STATUS) at $STAMP — no orders may have been placed." \
    | tee -a decision_log.md
fi
exit $STATUS
