"""Shared test setup: every test gets its own state database, so tests never touch real caches or limits."""

import pytest

from src import config


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DB", str(tmp_path / "state.sqlite"))
    yield
