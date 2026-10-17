"""Shared test setup: Adzuna pacing must never really sleep in tests."""
import pytest

from scout import aggregator


@pytest.fixture(autouse=True)
def no_real_pacing(monkeypatch):
    monkeypatch.setattr(aggregator, "sleep", lambda seconds: None)
    aggregator._last_call.clear()
