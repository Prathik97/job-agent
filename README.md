# Job Agent

An open source job search agent for product managers. It discovers openings, scores each one against your profile, tailors your resume per job using only facts you provide, and shows everything in a private local dashboard. You review and click submit. It never auto-applies.

## How it works

```
Scout  ->  Normalize  ->  Screener  ->  Tailor  ->  Dashboard
(find)     (dedupe)       (score)       (resume)    (review and apply)
```

| Stage | What it does | Tool |
|-------|--------------|------|
| Scout | Reads Greenhouse, Lever and Ashby job boards, aggregator APIs, job alert emails, and Firecrawl for pages with no feed | Public JSON, Adzuna, JSearch, Gmail, Firecrawl |
| Screener | Stage 1 drops obvious mismatches cheaply, stage 2 scores survivors out of 100 with reasons | Claude Haiku, Claude Sonnet |
| Tailor | Picks the most relevant achievements from your master profile and drafts a resume and cover note | Claude Sonnet, Claude Opus for top picks |
| Dashboard | One row per job with score, status, link and resume version, on your own machine | Local web page |

## Safety by design

- No auto submit. The agent stops before the submit button.
- The Tailor can only use facts from `master_profile.json`. A fact check fails any resume containing a number that is not in your master file.
- No direct scraping of LinkedIn or Naukri. Those come in through alert emails and aggregator APIs.
- Your personal files (`profile.yaml`, `master_profile.json`, `.env`) are gitignored.

## Quick start

1. Click the green Code button on this repo page, copy the URL, and run `git clone` with it.
2. Go into the folder and run `bash setup.sh`.
3. Activate the environment: `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`), then `pip install -r requirements.txt`.
4. Copy `profile.example.yaml` to `profile.yaml` and edit it for your target roles, location and deal breakers.
5. Copy `master_profile.example.json` to `master_profile.json` and replace it with your own experience, one achievement per entry.
6. Copy `.env.example` to `.env` and add your Anthropic API key. Other keys are added as you enable each source.
7. Run `python smoke_test.py`. All checks should show `[ok]`.

## Running it (manual)

Nothing runs on its own. Two ways to start a run:

- **Double-click `Run Job Agent.command`** in the project folder. It runs one full pipeline run, prints a short summary (new jobs, scored, apply, stretch, cost, calls left this month), then opens the dashboard.
- **"Run pipeline now"** in the dashboard (top right) shows the estimated cost first and runs in the background.

Both use the same safeguards: a $1.00 cost cap per run, monthly API call budgets, robots.txt checks, and no auto-submit. `Start Dashboard.command` opens the dashboard without running anything. An optional launchd schedule exists in `scheduler/` but is off and not installed.

## Cost

Using Haiku for the bulk filter and Sonnet only for survivors keeps API spend low, roughly 500 to 1,500 rupees a month at a few runs a week. Without the two stage filter, cost can be ten times higher.

## Project status

Scouts, Screener, Tailor and the dashboard work. Build order and remaining steps are in `CLAUDE.md`.

## Contributing

Pull requests are welcome. Never commit personal data or keys. Add tests for any new source.

## License

MIT. See `LICENSE`.
