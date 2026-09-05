#!/bin/bash
# Double-click this file (in Finder) to start Investment Tracker.
# Runs on port 8001 (deliberately NOT 8000 -- that's budget-app's port,
# on this same machine, and the two must never collide).

set -e
cd "$(dirname "$0")"
PORT=8001
URL="http://127.0.0.1:$PORT"

if [ ! -d ".venv" ] || ! .venv/bin/python3 --version >/dev/null 2>&1; then
  # Missing OR broken (e.g. this folder was copied from another machine --
  # a venv's python binary is a symlink tied to the machine it was built
  # on, by username/architecture/uv install path, so a copied .venv
  # almost never survives the move intact).
  echo "No usable .venv found -- (re)creating one for this machine..."
  rm -rf .venv
  uv venv
  source .venv/bin/activate
  uv pip install --index-url https://pypi.ci.artifacts.walmart.com/artifactory/api/pypi/external-pypi/simple \
    --allow-insecure-host pypi.ci.artifacts.walmart.com -r requirements.txt
else
  source .venv/bin/activate
fi

# If something is already answering on this port, just open it instead of
# trying (and failing) to start a second copy.
if curl -s -o /dev/null "$URL"; then
  echo "Investment Tracker is already running at $URL -- opening it."
  open "$URL"
  exit 0
fi

echo ""
echo "Starting Investment Tracker on $URL ..."
echo "Close this window (or press Ctrl+C) to stop the server."
echo ""

uvicorn app.main:app --reload --port "$PORT" &
SERVER_PID=$!

# Poll for real readiness instead of guessing with a fixed sleep --
# only open the browser once the server actually answers.
for i in $(seq 1 30); do
  if curl -s -o /dev/null "$URL"; then
    open "$URL"
    break
  fi
  sleep 0.5
done

wait $SERVER_PID
echo ""
read -p "Server stopped. Press Enter to close this window..."
