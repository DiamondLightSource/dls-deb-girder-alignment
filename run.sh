#!/usr/bin/env bash
# Girder alignment tool — Linux/macOS launcher.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  echo "For LIVE EPICS also install cothread (or pyepics) and aapy:"
  echo "  ./.venv/bin/pip install cothread aapy"
fi
exec ./.venv/bin/python server.py "$@"
