# Optional daily schedule (macOS launchd): OFF by default

**Nothing in this folder runs unless you install it, and it is not installed.** The job agent is
manual: double-click `Run Job Agent.command` in the project root, or press "Run pipeline now" in the
dashboard. Keep this folder only if you ever want an automatic run again; do not install it
otherwise.

If you do install it, it runs `run_pipeline.py` (scout, then screen) once a day at 8:30 AM Mac-local
time with the cost cap from `MAX_COST_PER_RUN` in `config.py`, uses Adzuna on Mondays only, and
writes `data/clean/run_stats.jsonl` and `data/clean/scheduler.log` (gitignored).

Install (generates the plist from the template with your home path, copies it to
`~/Library/LaunchAgents` and loads it):

    scheduler/install.sh

Turn off and remove (stops the schedule and deletes the installed and generated plists):

    scheduler/install.sh uninstall

Notes if you do use it: the Mac must be awake to run it (a sleeping Mac runs the missed job on
wake). `run_daily.sh` wraps the run in `caffeinate -i` so it finishes once started, and
`run_stats.jsonl` records `started_at` and `duration_s`. A wake before 8:30 needs
`sudo pmset repeat wakeorpoweron MTWRFSU 08:29:00`; cancel it with `sudo pmset repeat cancel`.
