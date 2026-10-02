#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# Vendored engine (git submodule). Prefer ./engine over any editable install
# that might point at a different checkout.
if [ ! -d engine/rhengine ]; then
  echo "ERROR: engine/rhengine missing. Run: git submodule update --init --recursive" >&2
  exit 1
fi
export PYTHONPATH="${PWD}/engine${PYTHONPATH:+:$PYTHONPATH}"

echo "──────────────────────────────────────"
echo "RobinhoodAgent  $(date '+%Y-%m-%d %H:%M %Z')"
echo "──────────────────────────────────────"

# Stage 1a — Signal scoring (writes signals.json)
echo "[1/5] Scoring watchlist..."
python3 signals_pipeline.py --write-signals

# Stage 1b — Portfolio state (validates or generates state.json)
echo "[2/5] Portfolio state..."
if [ "${ROBINHOODAGENT_SAMPLE_STATE:-0}" = "1" ] || [ ! -f state.json ]; then
  python3 portfolio_state.py --write-sample --state state.json
else
  python3 portfolio_state.py --validate --state state.json
fi

# Stage 1c — Order plan (writes plan.json)
echo "[3/5] Building order plan..."
python3 order_plan.py --state state.json

# Stage 1d — Setup logger (appends 3/4 and 4/4 setups to setup_log.csv)
echo "[4/5] Logging setups..."
python3 setup_logger.py

# Stage 1e — Score yesterday's and older signals. Offline: the Massive cache is
# one file per date for the whole market, already warmed by the scan above, so
# this costs no API calls. Non-fatal — bookkeeping never blocks a trading day.
echo "[5/5] Scoring past setups..."
python3 outcome_tracker.py || echo "WARN: outcome_tracker failed, setup_log not scored"

# Checker half of the maker/checker split: order_plan.py wrote the plan, this
# refuses to bless one that is structurally broken or unsafe. set -e stops here
# on rejection. Stage 2 re-validates its own regenerated plan — that is the one
# that actually trades.
echo "[gate] Validating plan..."
python3 validate_plan.py

echo ""
echo "Stage 1 complete. plan.json ready for Stage 2 (Claude Code execution)."
echo "Review plan.json or run: claude --print \"\$(cat session_prompt.md)\""
