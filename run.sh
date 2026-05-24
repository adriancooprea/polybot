#!/usr/bin/env bash
# One-command launcher for polybot (Linux/macOS).
#
#   ./run.sh                # bootstrap, then run the full bot
#   ./run.sh download       # step 1: fetch the dataset
#   ./run.sh rank           # step 2: rank wallets
#   ./run.sh monitor        # step 3: consensus signals
#   ./run.sh dashboard      # step 5: dashboard
#
# Creates a venv, installs deps, copies .env.example -> .env on first run.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="${PYTHON:-python3}"

if [ ! -d "$VENV" ]; then
  echo ">> creating virtualenv ($VENV)"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# install only when the package isn't importable yet (fast re-runs)
if ! python -c "import polybot" 2>/dev/null; then
  echo ">> installing dependencies"
  pip install --quiet --upgrade pip
  pip install --quiet -e .
fi

if [ ! -f .env ]; then
  echo ">> first run: creating .env from .env.example (edit it to add API keys)"
  cp .env.example .env
fi

exec polybot "${1:-run}" "${@:2}"
