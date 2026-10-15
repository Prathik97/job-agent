# Job Agent

An open source job search agent for product managers. It discovers openings, scores each one against your profile, tailors your resume per job using only facts you provide, tracks everything in a Google Sheet, and sends a daily digest on Telegram. You review and click submit. It never auto-applies.

## How it works

```
Scout  ->  Normalize  ->  Screener  ->  Tailor  ->  Tracker  ->  Daily digest
(find)     (dedupe)       (score)       (resume)    (sheet)      (Telegram)
```

| Stage | What it does | Tool |
|-------|--------------|------|
| Scout | Reads Greenhouse, Lever and Ashby job boards, aggregator APIs, job alert emails, and Firecrawl for pages with no feed | Public JSON, Adzuna, JSearch, Gmail, Firecrawl |
| Screener | Stage 1 drops obvious mismatches cheaply, stage 2 scores survivors out of 100 with reasons | Claude Haiku, Claude Sonnet |
| Tailor | Picks the most relevant achievements from your master profile and drafts a resume and cover note | Claude Sonnet, Claude Opus for top picks |
| Tracker | One row per job with score, status, link and resume version | Google Sheets |
| Digest | Top matches delivered each morning | Telegram bot |

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

## Cost

Using Haiku for the bulk filter and Sonnet only for survivors keeps API spend low, roughly 500 to 1,500 rupees a month for daily runs. Without the two stage filter, cost can be ten times higher.

## Project status

Scaffold and configuration are done. Scout, Screener, Tailor, Tracker and scheduling are being built in the order listed in `CLAUDE.md`.

## Contributing

Pull requests are welcome. Never commit personal data or keys. Add tests for any new source.

## License

MIT. See `LICENSE`.
