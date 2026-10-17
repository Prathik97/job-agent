"""Single home for model IDs and screener tunables. Change a model here and nowhere else."""

HAIKU = "claude-haiku-4-5-20251001"   # stage 1 filter, email parsing
SONNET = "claude-sonnet-5-5"          # stage 2 scoring, tailoring
OPUS = "claude-opus-5-5"              # polish for jobs scoring 85+

# USD per million tokens (input, output), base rates without caching or batch discounts.
# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-16).
PRICES = {
    HAIKU: (1.00, 5.00),
    SONNET: (2.00, 10.00),
    OPUS: (4.00, 20.00),
}

# Description truncation. Long text keeps the first 70% and last 30% of the budget,
# because requirements and location limits usually sit at the end.
STAGE1_MAX_CHARS = 3000
STAGE2_MAX_CHARS = 8000
HEAD_FRACTION = 0.7

# Verdict thresholds (shortlist_min_score, stretch_min_score) live in profile.yaml.

# Dry-run assumption only: share of stage 1 jobs expected to be kept.
ASSUMED_STAGE1_KEEP_RATE = 0.5

# ---------------------------------------------------------------
# Aggregator API budgets. PROVISIONAL: these are working guesses, NOT your real plan limits.
# TODO: replace them with the limits shown in the Adzuna developer dashboard and the RapidAPI
# dashboard for JSearch. A 429 or 402 from an API stops only that API for the rest of the run.
# ---------------------------------------------------------------
ADZUNA_DAILY_CALLS = 25          # PROVISIONAL - replace with your real Adzuna daily limit
ADZUNA_MONTHLY_CALLS = 300       # PROVISIONAL - replace with your real Adzuna monthly limit
JSEARCH_DAILY_CALLS = 4          # PROVISIONAL - replace with your real JSearch daily limit
JSEARCH_MONTHLY_CALLS = 120      # PROVISIONAL - replace with your real JSearch monthly limit

# Per-run caps so one run cannot burn the month. Queries rotate across runs (see scout/queries.py).
JSEARCH_MAX_CALLS_PER_RUN = 4
ADZUNA_MAX_CALLS_PER_RUN = 10

# Firecrawl (last-resort scraping): 1 credit per page. Hard cap per run, cache lifetime in hours.
FIRECRAWL_MAX_CREDITS_PER_RUN = 40
FIRECRAWL_CACHE_HOURS = 24

# Spend guard: a run aborts before any Stage 1 / Stage 2 call if the estimated cost is above
# this, unless --max-cost overrides it.
MAX_COST_PER_RUN = 1.00

# Role families whose verdict is capped at stretch, and which never appear in "Ready to apply".
CAPPED_ROLE_FAMILIES = ("physical_ops", "other")
# "Ready to apply" shows apply verdicts plus stretch jobs scoring at or above this.
READY_STRETCH_MIN_SCORE = 65
# Posting freshness defaults; profile.yaml `freshness:` overrides them.
STALE_NOTE_AFTER_DAYS = 60    # older: "possibly stale" note and cap at stretch
STALE_SKIP_AFTER_DAYS = 120   # older: capped at skip, reason "stale posting"
