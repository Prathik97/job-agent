# Daily run (macOS launchd)

Runs `run_pipeline.py` (scout, then screen) every day at 8:30 AM Mac-local time (IST here) with the
cost cap from `MAX_COST_PER_RUN` in `config.py`. The run summary goes to `data/clean/run_stats.jsonl`; stdout and stderr go to `data/clean/scheduler.log` (gitignored).
Nothing is sent to Telegram, email or anywhere else.

Install (generates the plist from the template with your home path, copies it to
`~/Library/LaunchAgents` and loads it):

    scheduler/install.sh

Turn off and remove (stops the schedule and deletes the installed and generated plists):

    scheduler/install.sh uninstall

Pause without removing: `launchctl bootout gui/$(id -u)/com.prathikkumar.jobagent.daily`
(turn back on by running `scheduler/install.sh` again).

Test now without waiting: `launchctl kickstart gui/$(id -u)/com.prathikkumar.jobagent.daily`.

Notes: the Mac must be on (a sleeping Mac runs the job when it wakes). macOS may ask whether
`zsh` can access the Desktop folder the first time; if you decline, the job fails silently.

## When the Mac is asleep at 8:30

launchd runs a missed 8:30 job when the Mac next wakes, so a sleeping Mac gives a late run, not a
skipped one. `run_daily.sh` wraps the run in `caffeinate -i` so it finishes once started, and
`run_stats.jsonl` records `started_at` and `duration_s` (the `timestamp` is the finish time).
To make the Mac wake just before 8:30 every day (needs your password, so run it yourself):

    sudo pmset repeat wakeorpoweron MTWRFSU 08:29:00

Cancel it with `sudo pmset repeat cancel`. On battery with the lid closed the wake is a short
background wake; the `caffeinate` wrapper keeps the Mac up long enough to finish.
