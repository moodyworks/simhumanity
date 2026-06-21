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

exec ./.venv/bin/python -m server.main
