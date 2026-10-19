#!/bin/zsh
# Daily pipeline run (scout then screen). Started by launchd; see scheduler/README.md.
# launchd sends stdout and stderr to data/clean/scheduler.log; the run summary is also appended to
# data/clean/run_stats.jsonl by run_pipeline (with started_at and duration_s). Nothing is sent
# anywhere. The cost cap is MAX_COST_PER_RUN in config.py, same as the dashboard.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/clean
echo "scheduled run started $(date '+%Y-%m-%d %H:%M:%S %Z')"
# caffeinate -i keeps the Mac from idle-sleeping until the run is done; a short DarkWake would
# otherwise suspend it after a few seconds.
exec /usr/bin/caffeinate -i .venv/bin/python run_pipeline.py --trigger scheduled
