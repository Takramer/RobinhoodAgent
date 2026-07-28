#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo "──────────────────────────────────────"
echo "RobinhoodAgent  $(date '+%Y-%m-%d %H:%M %Z')"
echo "──────────────────────────────────────"

# Stage 1a — Signal scoring (writes signals.json)
echo "[1/4] Scoring watchlist..."
python3 signals_pipeline.py --write-signals

# Stage 1b — Portfolio state (validates or generates state.json)
echo "[2/4] Portfolio state..."
if [ "${ROBINHOODAGENT_SAMPLE_STATE:-0}" = "1" ] || [ ! -f state.json ]; then
  python3 portfolio_state.py --write-sample --state state.json
else
  python3 portfolio_state.py --validate --state state.json
fi

# Stage 1c — Order plan (writes plan.json)
echo "[3/4] Building order plan..."
python3 order_plan.py --state state.json

# Stage 1d — Setup logger (appends 3/4 and 4/4 setups to setup_log.csv)
echo "[4/4] Logging setups..."
python3 setup_logger.py

# Checker half of the maker/checker split: order_plan.py wrote the plan, this
# refuses to bless one that is structurally broken or unsafe. set -e stops here
# on rejection. Stage 2 re-validates its own regenerated plan — that is the one
# that actually trades.
echo "[gate] Validating plan..."
python3 validate_plan.py

echo ""
echo "Stage 1 complete. plan.json ready for Stage 2 (Claude Code execution)."
echo "Review plan.json or run: claude --print \"\$(cat session_prompt.md)\""
