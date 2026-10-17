# Daily run (macOS launchd)

Runs `run_pipeline.py` (scout, then screen) every day at 8:30 AM Mac-local time (IST here) with the
cost cap from `MAX_COST_PER_RUN` in `config.py`. Results go to `data/clean/run_stats.jsonl` only.
Nothing is sent to Telegram, email or anywhere else.

Install (copy, then load):

    cp scheduler/com.prathikkumar.jobagent.daily.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.prathikkumar.jobagent.daily.plist

Turn off (stops the schedule; keeps the file so you can turn it back on):

    launchctl bootout gui/$(id -u)/com.prathikkumar.jobagent.daily

Remove completely: run the turn-off command, then `rm ~/Library/LaunchAgents/com.prathikkumar.jobagent.daily.plist`.

Test now without waiting: `launchctl kickstart gui/$(id -u)/com.prathikkumar.jobagent.daily`.

Notes: the Mac must be on (a sleeping Mac runs the job when it wakes). macOS may ask whether
`zsh` can access the Desktop folder the first time; if you decline, the job fails silently.
