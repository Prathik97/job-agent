"""Text helpers: description truncation, hashing, token estimates."""
from __future__ import annotations

import hashlib

from config import HEAD_FRACTION


def truncate(text: str, limit: int) -> str:
    """Keep the first 70% and last 30% of `limit` chars when text is too long."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * HEAD_FRACTION)
    tail = limit - head
    return f"{text[:head]}\n[...truncated...]\n{text[-tail:]}"


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def est_tokens(text: str) -> int:
    """Rough estimate: ~4 characters per token."""
    return max(1, len(text) // 4)
