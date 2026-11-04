"""Shared test setup: every test gets its own state database, so tests never touch real caches or limits."""

import hashlib
import threading
import time

import pytest

# P6: imported here, with the REAL scrypt parameters, so the import-time DUMMY_HASH is never built with fast_kdf's N=16.
from src import accounts as _accounts  # noqa: F401
from src import config
from src import passwords as _passwords  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DB", str(tmp_path / "state.sqlite"))
    # P6: opening the state DB tightens the permissions of the directory that holds it (CHROMA_DIR in production),
    # so tests keep both inside their own tmp dir and never create or chmod a real `chroma_db/`.
    monkeypatch.setattr(config, "CHROMA_DIR", str(tmp_path))
    yield


# ---------------------------------------------------------------------------- P6 helpers (docs/phases/p6-plan.md)
# These fixtures only patch `config` and the standard library, so they work before the P6 modules exist.
# Tests import `src.accounts` / `src.passwords` themselves, so a missing module fails that test, not the fixture.
@pytest.fixture
def fast_kdf(monkeypatch):
    """Cheap scrypt (N=16) for tests that are not about the real parameters, and no global login cap."""
    monkeypatch.setattr(config, "SCRYPT_N", 2**4, raising=False)
    monkeypatch.setattr(config, "SCRYPT_R", 8, raising=False)
    monkeypatch.setattr(config, "SCRYPT_P", 1, raising=False)
    monkeypatch.setattr(config, "LOGIN_MAX_PER_SECOND", 1000, raising=False)
    try:
        from src import passwords
    except ImportError:  # P6 not built yet: the test that needs it fails on its own import
        return
    dummy = passwords.hash_password("a-dummy-password-for-tests")
    monkeypatch.setattr(passwords, "DUMMY_HASH", dummy, raising=False)
    try:
        from src import accounts

        monkeypatch.setattr(accounts, "DUMMY_HASH", dummy, raising=False)
    except ImportError:
        pass


@pytest.fixture
def accounts_on(monkeypatch):
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", True, raising=False)


class Clock:
    """A controllable `time.time()`: expiry and rate-limit windows are tested without sleeping."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(1_800_000_000.0)
    monkeypatch.setattr(time, "time", fake)
    return fake


class ScryptSpy:
    """Counts every `hashlib.scrypt` call (all threads of this process): the KDF work an operation did."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def reset(self) -> int:
        with self._lock:
            before, self.calls = self.calls, 0
        return before


@pytest.fixture
def scrypt_spy(monkeypatch):
    spy, real = ScryptSpy(), hashlib.scrypt

    def counting(*args, **kwargs):
        with spy._lock:
            spy.calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", counting)
    return spy
