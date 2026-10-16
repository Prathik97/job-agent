#!/bin/bash
# Double-click to start the private job dashboard and open it in the browser.
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate || { echo "No .venv found. Run setup.sh first."; exit 1; }
PORT=8765
python -m dashboard.serve --port "$PORT" "$@" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT INT TERM
sleep 1.5
open "http://127.0.0.1:$PORT/"
wait $SERVER
