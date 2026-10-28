from datetime import date

import pytest

from sync_engine.adapters.base import FailureConfig
from sync_engine.pipeline import Pipeline


@pytest.fixture
def pipe(tmp_path):
    """Fresh data dir, pinned today, no real sleeping, no API key."""
    return Pipeline(tmp_path, today=date(2026, 11, 2), failures=FailureConfig(), sleep=lambda s: None, base_delay=0)


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
