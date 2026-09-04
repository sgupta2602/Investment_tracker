#!/bin/bash
# Double-click this file (in Finder) to start Investment Tracker.
# It opens a Terminal window, starts the server, and opens your browser.
# Closing this window (or pressing Ctrl+C) stops the server.

set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "No .venv found -- setting one up first (only happens once)..."
  uv venv
  source .venv/bin/activate
  uv pip install --index-url https://pypi.ci.artifacts.walmart.com/artifactory/api/pypi/external-pypi/simple \
    --allow-insecure-host pypi.ci.artifacts.walmart.com -r requirements.txt
else
  source .venv/bin/activate
fi

echo ""
echo "Starting Investment Tracker..."
echo "Your browser will open automatically at http://127.0.0.1:8000"
echo "Close this window (or press Ctrl+C) to stop the server."
echo ""

# Open the browser a moment after the server has had time to boot.
( sleep 1.5 && open "http://127.0.0.1:8000" ) &

uvicorn app.main:app --reload

echo ""
read -p "Server stopped. Press Enter to close this window..."
