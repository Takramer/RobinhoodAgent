#!/bin/bash
# Stage 1 under launchd. Runs from the TradingRuntime clone (outside ~/Downloads,
# which TCC blocks for background jobs). Pulls latest code, then runs the pipeline.
set -uo pipefail
cd "$(dirname "$0")"

# Import the vendored engine, not the pip-editable install (which points into
# the TCC-protected Downloads dev copy).
export PYTHONPATH="$PWD/engine"

# Best-effort code sync — never let a network failure block the trading session.
git pull --ff-only --quiet || echo "WARN: git pull failed, running with local code"
git submodule update --init --quiet || echo "WARN: submodule sync failed"

exec bash run_session.sh
