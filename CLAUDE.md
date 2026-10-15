# CLAUDE.md

Claude Code reads this file at the start of every session. It is the build brief for this project.

## What this project is

A job search agent for senior product managers. It finds openings, scores them against a profile, tailors a resume per job, logs everything to a tracker, and sends a daily digest. The human reviews and clicks submit. The agent never auto-submits an application.

## Architecture (a small recruiting team)

| Module | Folder | Job |
|--------|--------|-----|
| Scout | `scout/` | Pull raw jobs from official feeds, aggregators, alert emails, and Firecrawl for pages with no feed |
| Normalizer | `scout/normalize.py` | Convert every source to one schema and dedupe |
| Screener | `screener/` | Stage 1 cheap filter (Haiku), stage 2 deep scoring (Sonnet) |
| Tailor | `tailor/` | Build a per-job resume from `master_profile.json` only |
| Tracker | `tracker/` | Log every job to a Google Sheet, send the daily Telegram digest |
| Runner | `run.py` | Runs the whole pipeline in order |

## Data schema (every job, every source)

```
id, source, title, company, location, remote_type, url, description, posted_date, fetched_at
```

`id` is a hash of company + title + location. Dedupe on it.

## Input files

- `profile.yaml`: target titles, experience bounds, location rules, scoring weights. Your private config, gitignored.
- `master_profile.json`: the only source of truth for resume facts. Gitignored.
- `profile.example.yaml` and `master_profile.example.json`: sanitized public templates.

## Models

| Task | Model ID | Why |
|------|----------|-----|
| Stage 1 filter, email parsing | `claude-haiku-4-5-20251001` | Cheapest, bulk work |
| Stage 2 scoring, tailoring | `claude-sonnet-5-5` | Quality and cost balance |
| Polish for jobs scoring 85 and above | `claude-opus-5-5` | Use sparingly |

Keep model IDs in one file, `config.py`, so a change is one edit.

## Hard rules

1. Never auto-submit an application. Assisted apply stops before the submit button.
2. The Tailor may only use facts in `master_profile.json`. Never invent a metric, tool, title, date, or team size. After tailoring, run a check that every number in the output exists in the master file, and fail the job if not.
3. Never scrape LinkedIn or Naukri directly. Use alert emails and aggregator APIs for those.
4. Respect robots.txt and rate limits on every source. Cache results, and only re-read a page when its content hash changes.
5. Never commit secrets. All keys come from `.env`. Never print keys in logs.
6. Never commit `profile.yaml`, `master_profile.json`, `data/`, or `output/`. They hold personal data.
7. Screener output must be valid JSON validated with pydantic. Retry once on invalid JSON, then log and skip.
8. Every pipeline step must be idempotent. Running it twice must not create duplicate rows.

## Screener rubric (100 points)

Role and title fit 25, domain fit 20, seniority fit 20, location fit 15, company and growth 10, comp signal 10. Weights live in `profile.yaml`. Output per job: `score`, `reasons` (list), `red_flags` (list), `verdict` (apply, stretch, skip).

Location rule: Bengaluru onsite or hybrid, or fully remote that is open to India residents. Reject text such as "US only" or "work authorization required".

## Build order

Work through these one at a time. Each step ends with a runnable command and a test.

1. Scaffold done. `python smoke_test.py` passes.
2. `scout/greenhouse.py`, `scout/lever.py`, `scout/ashby.py` using public job board JSON endpoints and a `companies.yaml` list.
3. `scout/adzuna.py` and `scout/jsearch.py` aggregator clients.
4. `scout/gmail_alerts.py` parsing LinkedIn, Naukri, Instahyre and Wellfound alert emails.
5. `scout/normalize.py` with dedupe, plus unit tests.
6. `screener/stage1.py` (Haiku) and `screener/stage2.py` (Sonnet) with pydantic models.
7. `tracker/sheet.py` writing to Google Sheets with gspread, and `tracker/telegram.py` for the digest.
8. `tailor/tailor.py` with the fact check from rule 2.
9. `run.py` tying it together, then a GitHub Actions cron workflow at `.github/workflows/daily.yml` running at 03:30 UTC (9:00 AM IST).
10. `scout/firecrawl_scout.py` for dream companies without a feed.
11. Evaluation: `tests/eval_set.csv` with 30 hand labelled jobs and `tests/eval.py` comparing agent scores to labels.
12. Assisted apply using Claude in Chrome or Playwright, stopping before submit.

## Conventions

- Python 3.10 or higher, type hints, small functions, one responsibility per file.
- Use `requests` with timeouts and retries. No bare `except`.
- Log with the `logging` module, not print.
- Tests with pytest. Mock API calls in tests, never hit paid APIs in CI.
- Commit after each build step with a clear message.
