"""Shared fixtures: a clock the tests drive by hand."""

import pytest

from app import game as game_module


class FakeClock:
    """Stands in for `time.monotonic` so clock tests are exact, not flaky."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(game_module, "now", fake)
    return fake
