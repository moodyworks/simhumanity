#!/usr/bin/env bash
# Launch the simhumanity server. Creates the venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "No .env found — copying .env.example. Edit it to set your DeepSeek key/paths."
  cp .env.example .env
fi

# Stop any previous instance first, so restarts are clean and we never pile up
# orphaned servers holding the port (which makes a "restart" silently no-op).
pkill -9 -f 'server\.main' 2>/dev/null || true
sleep 0.5

exec ./.venv/bin/python -m server.main
