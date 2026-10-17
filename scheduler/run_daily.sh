#!/bin/zsh
# Daily pipeline run (scout then screen). Started by launchd; see scheduler/README.md.
# The result goes to data/clean/run_stats.jsonl only (run_pipeline writes it); nothing else is logged
# and nothing is sent anywhere. The cost cap is MAX_COST_PER_RUN in config.py, same as the dashboard.
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python run_pipeline.py --trigger scheduled >/dev/null 2>&1
