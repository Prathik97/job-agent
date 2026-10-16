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
