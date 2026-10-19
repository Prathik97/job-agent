#!/bin/bash
# Double-click to do one full manual run (scout, then screen), print a short summary, then open the
# dashboard. Nothing runs on its own: this is the only way a run starts besides the dashboard's
# "Run pipeline now" button. Safeguards are the pipeline's own: $1.00 cost cap, API call budgets,
# robots.txt checks. The agent never submits an application.
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate || { echo "No .venv found. Run setup.sh first."; read -n 1 -s -r -p "Press any key to close"; exit 1; }

echo "Running the pipeline (this takes a few minutes)..."
python run_pipeline.py --trigger manual
CODE=$?
echo
python run_summary.py "$CODE"
echo

PORT=8765
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
  echo "Dashboard is already running."
  open "http://127.0.0.1:$PORT/"
  read -n 1 -s -r -p "Press any key to close"
  exit "$CODE"
fi
python -m dashboard.serve --port "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT INT TERM
sleep 1.5
open "http://127.0.0.1:$PORT/"
echo "Dashboard open at http://127.0.0.1:$PORT/  (close this window or press Ctrl+C to stop it)"
wait $SERVER
