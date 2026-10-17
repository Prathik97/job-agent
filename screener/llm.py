"""Thin Anthropic wrapper: JSON extraction, one retry on invalid JSON, usage/cost tracking."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, TypeVar

from config import PRICES

log = logging.getLogger(__name__)
T = TypeVar("T")


class ScreenError(Exception):
    """Model output was still invalid after the single retry."""


@dataclass
class Usage:
    by_model: Dict[str, list] = field(default_factory=dict)
    calls: Dict[str, int] = field(default_factory=dict)

    def add(self, model: str, response) -> None:
        self.calls[model] = self.calls.get(model, 0) + 1
        u = getattr(response, "usage", None)
        entry = self.by_model.setdefault(model, [0, 0])
        entry[0] += getattr(u, "input_tokens", 0) or 0
        entry[1] += getattr(u, "output_tokens", 0) or 0

    def cost(self) -> float:
        return sum(cost_usd(m, i, o) for m, (i, o) in self.by_model.items())


def cost_usd(model: str, in_tokens: float, out_tokens: float) -> float:
    pin, pout = PRICES[model]
    return (in_tokens * pin + out_tokens * pout) / 1_000_000


def make_client():
    """Build the Anthropic client; the key comes from .env and is never logged."""
    from anthropic import Anthropic
    from dotenv import load_dotenv

    load_dotenv()
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing. Add it to .env.")
    return Anthropic(api_key=key)


def extract_json(text: str) -> dict:
    """Parse the first JSON object in the text, tolerating ``` fences and chatter."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def response_text(resp) -> str:
    """Text of the first text block. Newer models may lead with a thinking block."""
    for block in resp.content:
        if getattr(block, "type", "text") == "text" and getattr(block, "text", None) is not None:
            return block.text
    raise ValueError("response has no text block")


def ask_json(client, model: str, system: str, user: str, validate: Callable[[dict], T],
             usage: Usage, max_tokens: int) -> T:
    """Call the model; validate with pydantic. Retry once on invalid output, then raise."""
    last: Exception = ValueError("no attempt")
    for attempt in (1, 2):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage.add(model, resp)
        try:
            return validate(extract_json(response_text(resp)))
        except ValueError as exc:  # JSONDecodeError and pydantic ValidationError both qualify
            last = exc
            log.warning("invalid JSON from %s (attempt %d): %s", model, attempt, str(exc)[:120])
    raise ScreenError(str(last)[:200])
