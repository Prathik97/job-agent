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
