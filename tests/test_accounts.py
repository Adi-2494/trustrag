"""
P6 accounts and sign-in (ADR 0011, plan sections 2.4 and 3, criterion P6-AC2).

Interfaces fixed by these tests are listed in tests/p6_support.py (SPEC-GAP-057). In short:
    passwords.hash_password / verify_password / needs_rehash / canonical_username / check_password_policy / DUMMY_HASH
    accounts.create_account / set_password / set_disabled / list_accounts / PasswordLogin / SessionAuthenticator / main
    service.login_raw(raw_body, client_key) -> SessionInfo, raising accounts.LoginFailed / accounts.LoginRateLimited
Hashes are `scrypt$n=<N>,r=<R>,p=<P>$<salt>$<key>` (ADR 0011); salt and key are hex or base64 text.
Most tests lower the scrypt cost with the `fast_kdf` fixture; the three tests named in plan section 3
(`..._salted_scrypt_with_configured_params`, `..._dummy_hash_uses_current_kdf_params`, the timing test) use the real one.
"""

import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import multiprocessing
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from src import config
from tests.fakes import FakeLLM, FakeStore
from tests.p6_support import (
    BAD_LOGIN,
    OTHER_PASSWORD,
    PASSWORD,
    RATE_LIMITED,
    add_account,
    bearer,
    install_api,
    login_body,
    make_service,
    sign_in,
)

# P7-AC1: every test in this module runs on the local backend and, when configured, the server backends.
pytestmark = pytest.mark.usefixtures("storage_backend")

ROOT = Path(__file__).resolve().parent.parent
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")


def _decode(text: str) -> bytes:
    """Salt / key text is hex or base64 (standard or URL-safe, padded or not); the tests do not pin which."""
    if len(text) % 2 == 0 and re.fullmatch(r"[0-9a-f]+", text):
        return bytes.fromhex(text)
    return base64.b64decode(text.replace("-", "+").replace("_", "/") + "=" * (-len(text) % 4))


def _params(encoded: str) -> dict[str, int]:
    kind, params, _salt, _key = encoded.split("$")
    assert kind == "scrypt"
    return {k: int(v) for k, v in (item.split("=") for item in params.split(","))}


# ---------------------------------------------------------------------------- hashing
def test_password_hash_is_salted_scrypt_with_configured_params():
    """P6-AC2 / threat 27: salted scrypt with the configured parameters; the stored form never holds the password."""
    from src import passwords

    first, second = passwords.hash_password(PASSWORD), passwords.hash_password(PASSWORD)
    assert first != second  # random salt
    kind, _params_text, salt_text, key_text = first.split("$")
    assert kind == "scrypt" and _params(first) == {"n": config.SCRYPT_N, "r": config.SCRYPT_R, "p": config.SCRYPT_P}
    assert (config.SCRYPT_N, config.SCRYPT_R, config.SCRYPT_P) == (2**17, 8, 1)  # ADR 0011
    salt, key = _decode(salt_text), _decode(key_text)
    assert len(salt) == 16 and len(key) == 32
    assert _decode(second.split("$")[2]) != salt  # a fresh salt for every hash
    expected = hashlib.scrypt(
        PASSWORD.encode(),
        salt=salt,
        n=config.SCRYPT_N,
        r=config.SCRYPT_R,
        p=config.SCRYPT_P,
        maxmem=config.SCRYPT_MAXMEM,
        dklen=32,
    )
    assert key == expected
    assert PASSWORD not in first and passwords.verify_password(PASSWORD, first)
    assert not passwords.verify_password(OTHER_PASSWORD, first)
    assert passwords.needs_rehash(first) is False


def test_dummy_hash_uses_current_kdf_params():
    """P6-AC2 / threat 28 (review #12): DUMMY_HASH has the configured parameters -> unknown users cost the same KDF."""
    from src import passwords

    assert _params(passwords.DUMMY_HASH) == {"n": config.SCRYPT_N, "r": config.SCRYPT_R, "p": config.SCRYPT_P}
    assert passwords.needs_rehash(passwords.DUMMY_HASH) is False
    assert not passwords.verify_password(PASSWORD, passwords.DUMMY_HASH)


def test_login_rehashes_when_params_change(fast_kdf, monkeypatch):
    """P6-AC2 / threat 27 (review #12): after a correct login, a hash with old parameters is replaced."""
    from src import passwords, state

    add_account("alice")
    old_hash = state.get_account_by_username("alice")["password_hash"]
    assert _params(old_hash)["n"] == 2**4 and passwords.needs_rehash(old_hash) is False

    monkeypatch.setattr(config, "SCRYPT_N", 2**5)
    assert passwords.needs_rehash(old_hash) is True
    service = make_service()
    sign_in(service, "alice")
    new_hash = state.get_account_by_username("alice")["password_hash"]
    assert new_hash != old_hash and _params(new_hash)["n"] == 2**5
    assert sign_in(service, "alice")  # the new hash still verifies the same password
    assert passwords.needs_rehash(new_hash) is False


def test_verify_password_uses_compare_digest(monkeypatch):
    """P6-AC2: key comparison is constant-time (hmac.compare_digest); malformed stored hashes just fail."""
    from src import passwords

    monkeypatch.setattr(config, "SCRYPT_N", 2**4, raising=False)
    encoded = passwords.hash_password(PASSWORD)
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    monkeypatch.setattr(passwords, "compare_digest", spy, raising=False)  # in case it was imported by name
    assert passwords.verify_password(PASSWORD, encoded) is True
    assert passwords.verify_password("wrong password!!", encoded) is False
    assert len(calls) >= 2
    for garbage in ("", "garbage", "scrypt$n=x,r=8,p=1$aa$bb", "scrypt$n=16,r=8,p=1$zz", "md5$1$2$3"):
        assert passwords.verify_password(PASSWORD, garbage) is False


# ---------------------------------------------------------------------------- policy and usernames
def test_password_policy_bounds_and_username_rules():
    """P6-AC2 / SPEC-GAP-032, -033: 12-1024 chars, not the username; usernames are ASCII, lowercased, fullmatched."""
    from src import passwords

    ok = passwords.check_password_policy
    ok("a" * 12, "alice")
    ok("a" * 1024, "alice")
    for bad in ("a" * 11, "", "a" * 1025):
        with pytest.raises(passwords.PasswordPolicyError):
            ok(bad, "alice")
    with pytest.raises(passwords.PasswordPolicyError):
        ok("alicealicealice", "alicealicealice")  # equal to the username
    assert issubclass(passwords.PasswordPolicyError, ValueError)

    canon = passwords.canonical_username
    assert canon("alice") == canon("Alice") == canon("ALICE") == "alice"
    assert canon("a.b_c-d9") == "a.b_c-d9" and canon("a" * 32) == "a" * 32 and canon("abc") == "abc"
    for bad in (
        "ab",
        "a" * 33,
        "",
        "-abc",
        ".abc",
        "_abc",
        "ali ce",
        " alice",
        "alice ",
        "alice\n",
        "ali\nce",
        "al@ce",
        "alice@example.com",
        "élise",
        "ａlice",
        "Kelly",
        "kelly\u200b",
        "al\x00ice",
        "ali/ce",
        "../etc",
    ):
        assert canon(bad) is None, repr(bad)
    assert "K".lower() == "k"  # why isascii() must run BEFORE lower()
    assert canon(123) is None and canon(None) is None  # type: ignore[arg-type]


@settings(max_examples=300, deadline=None)
@given(st.text())
def test_canonical_username_is_ascii_idempotent_and_matches_the_rule(raw):
    """P6-AC2 (property): whatever is typed, the result is None or one canonical value for key, lookup and CLI."""
    from src import passwords

    result = passwords.canonical_username(raw)
    if result is not None:
        assert result.isascii() and USERNAME_RE.fullmatch(result) and result == result.lower()
        assert passwords.canonical_username(result) == result
    else:
        assert not (raw.isascii() and USERNAME_RE.fullmatch(raw.lower()))


@settings(max_examples=100, deadline=None)
@given(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{2,31}", fullmatch=True))
def test_every_ascii_username_has_one_canonical_form(raw):
    from src import passwords

    assert passwords.canonical_username(raw) == raw.lower()


def test_username_variants_share_one_rate_limit_bucket(fast_kdf, clock):
    """P6-AC2 / threat 41 (review #7): Alice/ALICE/alice share one bucket; look-alike Unicode never reaches an account."""
    from src.accounts import LoginFailed, LoginRateLimited

    service = make_service()
    add_account("alice")
    add_account("kelly")
    for i, variant in enumerate(("Alice", "ALICE", "alice", "aLiCe", "ALiCe")):
        clock.tick(1)
        with pytest.raises(LoginFailed):
            service.login_raw(login_body(variant, "wrong password 1234"), f"client-{i}")  # distinct clients
    clock.tick(1)
    with pytest.raises(LoginRateLimited):  # the sixth try, in any spelling, with the RIGHT password
        service.login_raw(login_body("alice", PASSWORD), "client-9")
    with pytest.raises(LoginRateLimited):
        service.login_raw(login_body("ALICE", PASSWORD), "client-10")

    lookalikes = ("Kelly", "ＫＥＬＬＹ", "kelly\u200b", "kelly\n", " kelly", "KELLY ")
    for i, variant in enumerate(lookalikes):
        clock.tick(1)
        with pytest.raises(LoginFailed):  # rejected as bad input even with the correct password
            service.login_raw(login_body(variant, PASSWORD), f"other-{i}")
    clock.tick(1)
    assert sign_in(service, "kelly", client_key="other-final")  # their failures never touched kelly's own bucket


# ---------------------------------------------------------------------------- nothing sensitive at rest or in logs
def _all_state_bytes() -> bytes:
    return b"".join(p.read_bytes() for p in sorted(Path(config.STATE_DB).parent.glob(Path(config.STATE_DB).name + "*")))


@pytest.mark.local_only
def test_state_db_never_contains_plain_password_or_token(fast_kdf):
    """P6-AC2 / threat 27: neither passwords (right or wrong) nor session tokens appear in the DB or its WAL."""
    from src.accounts import LoginFailed

    service = make_service()
    add_account("alice")
    token = sign_in(service, "alice")
    with pytest.raises(LoginFailed):
        service.login_raw(login_body("alice", "a wrong password 98765"), "c1")
    blob = _all_state_bytes()
    assert blob and b"alice" in blob  # the files were really read
    for secret in (PASSWORD, "a wrong password 98765", token):
        assert secret.encode() not in blob


@pytest.mark.local_only
def test_session_token_stored_only_as_sha256(fast_kdf):
    """P6-AC2 / threat 29: `sessions.token_hash` is sha256(token); the token itself is stored nowhere."""
    import sqlite3

    service = make_service()
    principal = add_account("alice")
    token = sign_in(service, "alice")
    with sqlite3.connect(config.STATE_DB) as db:
        rows = db.execute("SELECT token_hash, account_id FROM sessions").fetchall()
        dump = "\n".join(db.iterdump())
    assert rows == [(hashlib.sha256(token.encode()).hexdigest(), principal["account_id"])]
    assert token not in dump and len(token) >= 43  # secrets.token_urlsafe(32)


def test_logs_never_contain_password_token_or_submitted_username(fast_kdf, clock):
    """P6-AC2 / threat 29, 34: log events carry no password, token (or its hash) or submitted username."""
    from src.accounts import LoginFailed
    from src.observability import JsonFormatter, logger

    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(JsonFormatter().format(record) + " | " + record.getMessage())

    handler, old_level = Capture(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        service = make_service()
        add_account("alice")
        token = sign_in(service, "alice")
        for submitted in ("zz-secret-user", "alice", "ZßZ-odd-user"):
            clock.tick(1)
            with pytest.raises(LoginFailed):
                service.login_raw(login_body(submitted, "wrong-pass-0987654"), "1.2.3.4")
        with pytest.raises(LoginFailed):
            service.login_raw(
                b'{"username": "zz-secret-user", "password": "wrong-pass-0987654',  # bad JSON; gitleaks:allow
                "1.2.3.4",
            )
        service.new_workspace(session=token)
        service.logout(token)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    assert lines, "the auth events should be logged"
    text = "\n".join(lines)
    assert any("auth.login_failed" in line for line in lines)
    for secret in (
        PASSWORD,
        "wrong-pass-0987654",
        token,
        hashlib.sha256(token.encode()).hexdigest(),
        "zz-secret-user",
        "ZßZ-odd-user",
        "Z\\u00dfZ-odd-user",
    ):
        assert secret not in text, secret


# ---------------------------------------------------------------------------- rate limits
def test_login_is_rate_limited_per_username(fast_kdf, clock, scrypt_spy):
    """P6-AC2 / threat 26: 5 failures per username per window, then 429 + Retry-After (even for the right password)."""
    from src.accounts import LoginFailed, LoginRateLimited

    service = make_service()
    add_account("alice")
    for i in range(5):
        clock.tick(1)
        with pytest.raises(LoginFailed):
            service.login_raw(login_body("alice", "wrong password 1234"), f"client-{i}")
    clock.tick(1)
    scrypt_spy.reset()
    with pytest.raises(LoginRateLimited) as limited:
        service.login_raw(login_body("alice", PASSWORD), "client-new")
    assert limited.value.retry_after > 0 and scrypt_spy.calls == 0  # refused before any KDF work
    clock.tick(config.LOGIN_WINDOW_MINUTES * 60 + 5)  # the rolling window moves on
    assert sign_in(service, "alice", client_key="client-new")


def test_login_is_rate_limited_per_client_address(fast_kdf, clock):
    """P6-AC2 / threat 26: 20 failures per client address per window, whichever usernames are tried."""
    from src.accounts import LoginFailed, LoginRateLimited

    service = make_service()
    for i in range(20):
        clock.tick(0.1)
        with pytest.raises(LoginFailed):
            service.login_raw(login_body(f"user{i:02d}", "wrong password 1234"), "198.51.100.7")
    clock.tick(0.1)
    with pytest.raises(LoginRateLimited):
        service.login_raw(login_body("a-fresh-user", "wrong password 1234"), "198.51.100.7")
    with pytest.raises(LoginFailed):  # another address is unaffected
        service.login_raw(login_body("a-fresh-user", "wrong password 1234"), "198.51.100.8")
    for i in range(25):  # no known address (client_key None, e.g. Streamlit on localhost): no shared bucket
        clock.tick(0.1)
        with pytest.raises(LoginFailed):
            service.login_raw(login_body(f"anon{i:02d}", "wrong password 1234"), None)


def _child_logins(args):
    """Runs in a spawned process: wrong logins for one user against the shared DB; returns (failed, limited, kdf)."""
    snapshot, username, attempts, tag = args
    import hashlib as child_hashlib

    from src import config as child_config
    from tests.server_support import apply_snapshot

    apply_snapshot(snapshot)  # P7: the parent's backend (SQLite file, or the shared Postgres + Redis)
    child_config.SCRYPT_N = 2**4
    child_config.LOGIN_MAX_PER_SECOND = 1000
    kdf_calls, real = [0], child_hashlib.scrypt

    def counting(*a, **k):
        kdf_calls[0] += 1
        return real(*a, **k)

    from src import accounts  # imported first: its import-time DUMMY_HASH scrypt run must not be counted

    child_hashlib.scrypt = counting
    login = accounts.PasswordLogin()
    failed = limited = 0
    for i in range(attempts):
        try:
            login.login(login_body(username, "wrong password 1234"), f"{tag}-{i}")
        except accounts.LoginRateLimited:
            limited += 1
        except accounts.LoginFailed:
            failed += 1
    return failed, limited, kdf_calls[0]


def _run_processes(username: str) -> list[tuple[int, int, int]]:
    from tests.server_support import child_snapshot

    jobs = [(child_snapshot(), username, 4, f"p{n}") for n in range(4)]
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        return pool.map(_child_logins, jobs)


def test_login_rate_limit_is_shared_across_processes(fast_kdf):
    """P6-AC2 / threat 26: four processes x 4 wrong tries for one user -> exactly 5 get through, 11 are refused."""
    add_account("alice")
    results = _run_processes("alice")
    assert sum(r[0] for r in results) == 5 and sum(r[1] for r in results) == 11


def test_concurrent_wrong_logins_run_kdf_at_most_limit_times(fast_kdf, scrypt_spy):
    """P6-AC2 / threat 36 (review #1): check + reservation are atomic, so a burst cannot exceed 5 KDF runs."""
    from src.accounts import LoginFailed, LoginRateLimited

    service = make_service()
    add_account("alice")
    add_account("bobby")
    scrypt_spy.reset()

    def attempt(i):
        try:
            service.login_raw(login_body("alice", "wrong password 1234"), f"thread-client-{i}")
        except LoginRateLimited:
            return "limited"
        except LoginFailed:
            return "failed"
        return "ok"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(16)))
    assert outcomes.count("failed") == 5 and outcomes.count("limited") == 11
    assert scrypt_spy.calls == 5  # the KDF ran only for the attempts that were let through

    results = _run_processes("bobby")  # the same across processes
    assert sum(r[0] for r in results) == 5 and sum(r[2] for r in results) == 5


def test_successful_login_refunds_its_reservation(fast_kdf, clock):
    """P6-AC2 / threat 36: a correct login does not use up the failure budget, earlier failures still count."""
    from src.accounts import LoginFailed, LoginRateLimited

    service = make_service()
    add_account("alice")
    for i in range(8):  # more successes than the budget of 5: each refunds its own reservation
        clock.tick(1)
        assert sign_in(service, "alice", client_key=f"c{i}")
    for i in range(4):
        clock.tick(1)
        with pytest.raises(LoginFailed):
            service.login_raw(login_body("alice", "wrong password 1234"), f"w{i}")
    clock.tick(1)
    assert sign_in(service, "alice", client_key="ok")  # success after 4 failures; refunds only itself
    clock.tick(1)
    with pytest.raises(LoginFailed):  # the 5th failure is still allowed ...
        service.login_raw(login_body("alice", "wrong password 1234"), "w4")
    clock.tick(1)
    with pytest.raises(LoginRateLimited):  # ... the 6th attempt is refused: the failures were kept
        service.login_raw(login_body("alice", PASSWORD), "w5")


def test_login_runs_kdf_exactly_once_for_unknown_wrong_and_disabled(fast_kdf, scrypt_spy):
    """P6-AC2 / threat 28: equal hashing work whatever the reason for failure (DUMMY_HASH for unknown users)."""
    from src import accounts
    from src.accounts import LoginFailed

    service = make_service()
    add_account("alice")
    add_account("dora")
    accounts.set_disabled("dora", True)
    cases = {
        "unknown user": ("nobody", PASSWORD),
        "wrong password": ("alice", "wrong password 1234"),
        "disabled, right password": ("dora", PASSWORD),
        "disabled, wrong password": ("dora", "wrong password 1234"),
    }
    for label, (username, password) in cases.items():
        scrypt_spy.reset()
        with pytest.raises(LoginFailed):
            service.login_raw(login_body(username, password), "c")
        assert scrypt_spy.calls == 1, label
    scrypt_spy.reset()
    assert sign_in(service, "alice", client_key="c")
    assert scrypt_spy.calls == 1  # success: one check, no rehash when the parameters are current


def test_login_timing_does_not_depend_on_username_existence(monkeypatch):
    """P6-AC2 / threat 28 (secondary; the structural proof is the DUMMY_HASH test): median times agree within 0.67-1.5."""
    from src.accounts import LoginFailed

    for name, value in (
        ("LOGIN_MAX_FAILURES_PER_USER", 10_000),
        ("LOGIN_MAX_FAILURES_PER_CLIENT", 10_000),
        ("LOGIN_MAX_PER_SECOND", 1_000),
    ):
        monkeypatch.setattr(config, name, value, raising=False)
    service = make_service()
    add_account("alice")  # real scrypt parameters, as in production

    def timed(username):
        started = time.perf_counter()
        with pytest.raises(LoginFailed):
            service.login_raw(login_body(username, "wrong password 1234"), None)
        return time.perf_counter() - started

    timed("alice"), timed("ghost-warmup")  # warm-up: imports, SQLite connection, memory allocation
    known, unknown = [], []
    for i in range(7):  # interleaved, so machine load affects both the same way
        known.append(timed("alice"))
        unknown.append(timed(f"ghost{i}"))
    ratio = statistics.median(unknown) / statistics.median(known)
    assert 0.67 <= ratio <= 1.5, (ratio, known, unknown)


# ---------------------------------------------------------------------------- the HTTP login route
@pytest.fixture
def auth_api(monkeypatch, accounts_on, fast_kdf):
    main = install_api(monkeypatch, make_service(store=FakeStore(), llm=FakeLLM()))
    return TestClient(main.app)


SECRET = "SUPER-SECRET-PW-123456"
_LOGIN_CASES = {
    "invalid json": b'{"username": "alice", "password": "' + SECRET.encode(),
    "not an object": json.dumps(["alice", SECRET]).encode(),
    "missing password": json.dumps({"username": "alice", "passw0rd": SECRET}).encode(),
    "missing username": json.dumps({"user": "alice", "password": SECRET}).encode(),
    "non-string password": json.dumps({"username": "alice", "password": 12345678901234, "note": SECRET}).encode(),
    "non-string username": json.dumps({"username": ["alice"], "password": SECRET}).encode(),
    "1025-char password": json.dumps({"username": "alice", "password": "S" * 1025}).encode(),
    "body over 4096 bytes": json.dumps({"username": "alice", "password": SECRET, "pad": "p" * 5000}).encode(),
    "non-ASCII username": json.dumps({"username": "élise", "password": SECRET}).encode(),
    "empty body": b"",
    "null": b"null",
}


@pytest.mark.parametrize("raw", list(_LOGIN_CASES.values()), ids=list(_LOGIN_CASES))
def test_api_login_validation_error_does_not_echo_password(auth_api, raw):
    """P6-AC2 / threat 34 (review #6): every bad shape is the same fixed 401; never a 422 echoing the input."""
    add_account("alice")
    response = auth_api.post("/v1/auth/login", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 401 and response.json() == BAD_LOGIN
    assert SECRET not in response.text and "S" * 20 not in response.text
    assert "input" not in response.text and "loc" not in response.text


def test_api_login_success_returns_session_info(auth_api):
    """P6-AC2: a good login is 200 with token, username and an absolute expiry (SessionInfo, plan 2.2)."""
    add_account("alice")
    response = auth_api.post("/v1/auth/login", json={"username": "ALICE", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "username", "expires_at"} and body["username"] == "alice"
    me = auth_api.get("/v1/auth/me", headers=bearer(body["token"]))
    assert me.status_code == 200 and me.json() == {"username": "alice"}
    assert auth_api.post("/v1/auth/logout", headers=bearer(body["token"])).status_code == 204
    assert auth_api.get("/v1/auth/me", headers=bearer(body["token"])).status_code == 401


# ---------------------------------------------------------------------------- admin CLI
class _Stdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _getpass(*answers: str):
    queue, prompts = list(answers), []

    def fake(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        assert queue, "getpass was asked for more input than the test expected"
        return queue.pop(0)

    fake.prompts = prompts  # type: ignore[attr-defined]
    return fake


def _never_getpass(prompt: str = "") -> str:
    raise AssertionError("getpass must not be called here")


def _output(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_admin_cli_reads_password_with_getpass_not_argv(fast_kdf, capsys):
    """P6-AC2 / threat 34 (review #14, SPEC-GAP-055): typed twice, non-TTY refused, fixed errors, never from argv."""
    from src import accounts

    gp = _getpass(PASSWORD, PASSWORD)
    assert accounts.main(["create", "Alice"], getpass=gp, stdin=_Stdin(tty=True)) == 0
    assert len(gp.prompts) == 2  # typed twice
    assert [a["username"] for a in accounts.list_accounts()] == ["alice"]  # canonical form
    assert sign_in(make_service(), "alice")
    assert PASSWORD not in _output(capsys)

    # mismatch -> fails, nothing created
    assert accounts.main(["create", "carol"], getpass=_getpass(PASSWORD, OTHER_PASSWORD), stdin=_Stdin(tty=True)) == 1
    mismatch = _output(capsys)
    assert PASSWORD not in mismatch and OTHER_PASSWORD not in mismatch and mismatch.strip()

    # non-interactive stdin is refused unless --password-stdin is given
    assert accounts.main(["create", "carol"], getpass=_never_getpass, stdin=_Stdin(PASSWORD + "\n", tty=False)) == 1
    assert "--password-stdin" in _output(capsys)
    assert [a["username"] for a in accounts.list_accounts()] == ["alice"]

    # --password-stdin reads exactly one line and never calls getpass
    assert (
        accounts.main(
            ["create", "carol", "--password-stdin"],
            getpass=_never_getpass,
            stdin=_Stdin(OTHER_PASSWORD + "\n", tty=False),
        )
        == 0
    )
    assert PASSWORD not in _output(capsys) and OTHER_PASSWORD not in _output(capsys)
    assert sign_in(make_service(), "carol", OTHER_PASSWORD)

    # policy and username failures use fixed strings that do not echo the input
    capsys.readouterr()
    assert accounts.main(["create", "dave"], getpass=_getpass("short", "short"), stdin=_Stdin(tty=True)) == 1
    message_a = _output(capsys)
    assert accounts.main(["create", "dave"], getpass=_getpass("short-two", "short-two"), stdin=_Stdin(tty=True)) == 1
    message_b = _output(capsys)
    assert message_a == message_b and "short-two" not in message_b and message_a.strip()
    assert accounts.main(["create", "x"], getpass=_getpass(PASSWORD, PASSWORD), stdin=_Stdin(tty=True)) == 1
    assert accounts.main(["create", "Kelly"], getpass=_getpass(PASSWORD, PASSWORD), stdin=_Stdin(tty=True)) == 1
    assert (
        accounts.main(["create", "alice"], getpass=_getpass(OTHER_PASSWORD, OTHER_PASSWORD), stdin=_Stdin(tty=True))
        == 1
    )
    assert sign_in(make_service(), "alice")  # the duplicate did not change alice's password
    assert sorted(a["username"] for a in accounts.list_accounts()) == ["alice", "carol"]
    capsys.readouterr()

    # a password can never be passed on the command line
    try:
        code = accounts.main(
            ["create", "erin", "--password", "a-long-password-123"], getpass=_never_getpass, stdin=_Stdin(tty=True)
        )
    except SystemExit as exit_:
        code = exit_.code
    assert code not in (0, None)
    assert "erin" not in [a["username"] for a in accounts.list_accounts()]


def test_admin_cli_manages_accounts(fast_kdf, capsys):
    """P6-AC2 / SPEC-GAP-037: set-password, disable, enable and list; list never prints hashes."""
    from src import accounts
    from src.accounts import LoginFailed

    service = make_service()
    add_account("alice")
    token = sign_in(service, "alice")
    tty = _Stdin(tty=True)

    assert accounts.main(["set-password", "alice"], getpass=_getpass(OTHER_PASSWORD, OTHER_PASSWORD), stdin=tty) == 0
    with pytest.raises(LoginFailed):
        sign_in(service, "alice", PASSWORD)
    assert sign_in(service, "alice", OTHER_PASSWORD)
    from src.service import NotAuthenticated

    with pytest.raises(NotAuthenticated):  # set-password revoked the old session
        service.list_workspaces(session=token)

    assert accounts.main(["disable", "alice"], getpass=_never_getpass, stdin=tty) == 0
    with pytest.raises(LoginFailed):
        sign_in(service, "alice", OTHER_PASSWORD)
    capsys.readouterr()
    assert accounts.main(["list"], getpass=_never_getpass, stdin=tty) == 0
    listing = _output(capsys)
    assert "alice" in listing and "scrypt$" not in listing and "disabled" in listing.lower()
    assert accounts.main(["enable", "alice"], getpass=_never_getpass, stdin=tty) == 0
    assert sign_in(service, "alice", OTHER_PASSWORD)
    capsys.readouterr()
    assert accounts.main(["disable", "ghost"], getpass=_never_getpass, stdin=tty) == 1  # unknown account
    assert _output(capsys).strip()


# ---------------------------------------------------------------------------- files, parameters, settings
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.local_only
def test_state_db_dir_and_files_are_owner_only(monkeypatch, tmp_path):
    """P6-AC2 / threat 27 (review #11, SPEC-GAP-044): dir 0700, DB and -wal/-shm 0600; old 0644 files are tightened."""
    from src import state

    def mode(path):
        return os.stat(path).st_mode & 0o777

    old_umask = os.umask(0o022)
    try:
        existing = tmp_path / "old"  # an upgrade: files created by v1.0 with default permissions
        existing.mkdir()
        existing.chmod(0o755)
        for suffix in ("", "-wal", "-shm"):
            file = Path(str(existing / "state.sqlite") + suffix)
            file.write_bytes(b"")
            file.chmod(0o644)
        monkeypatch.setattr(config, "CHROMA_DIR", str(existing))
        monkeypatch.setattr(config, "STATE_DB", str(existing / "state.sqlite"))
        state.list_accounts()
        assert mode(existing) == 0o700
        for suffix in ("", "-wal", "-shm"):
            assert mode(str(existing / "state.sqlite") + suffix) == 0o600, suffix

        fresh = tmp_path / "new" / "chroma"  # a first start: created with the right permissions
        monkeypatch.setattr(config, "CHROMA_DIR", str(fresh))
        monkeypatch.setattr(config, "STATE_DB", str(fresh / "state.sqlite"))
        state.list_accounts()
        add_account_ok = state.try_acquire_call(limit=5)  # a write, so the WAL file exists
        assert add_account_ok == 0.0
        assert mode(fresh) == 0o700
        for candidate in fresh.glob("state.sqlite*"):
            assert mode(candidate) == 0o600, candidate.name
        assert (fresh / "state.sqlite").exists()
    finally:
        os.umask(old_umask)


def test_scrypt_is_available_with_required_params():
    """P6-AC2: this Python / OpenSSL can run scrypt at the ADR 0011 parameters (the Docker image is checked by T11)."""
    assert (config.SCRYPT_N, config.SCRYPT_R, config.SCRYPT_P) == (2**17, 8, 1)
    assert config.SCRYPT_MAXMEM >= 128 * config.SCRYPT_N * config.SCRYPT_R
    key = hashlib.scrypt(
        b"password",
        salt=b"s" * 16,
        n=config.SCRYPT_N,
        r=config.SCRYPT_R,
        p=config.SCRYPT_P,
        maxmem=config.SCRYPT_MAXMEM,
        dklen=32,
    )
    assert len(key) == 32


def test_auth_constants_match_adr_0011():
    """P6-AC2: the fixed (non-settings) constants of ADR 0011 / plan 2.4."""
    assert config.KDF_MAX_CONCURRENT == 2 and config.KDF_WAIT_SECONDS == 5
    assert config.LOGIN_BODY_MAX_BYTES == 4096
    assert (config.PASSWORD_MIN_CHARS, config.PASSWORD_MAX_CHARS) == (12, 1024)
    assert config.ACCOUNTS_ENABLED is False  # opt-in (SPEC-GAP-039)
    assert config.MAX_LOGIN_ATTEMPTS == 5 and config.LOGIN_LOCKOUT_SECONDS == 60  # APP_PASSWORD gate unchanged


_SETTING_BOUNDS = {  # name: (default, upper bound)
    "session_idle_minutes": (120, 1440),
    "session_max_hours": (24, 720),
    "login_max_failures_per_user": (5, 100),
    "login_max_failures_per_client": (20, 1000),
    "login_window_minutes": (15, 1440),
    "login_max_per_second": (5, 100),
    "max_workspaces_per_account": (10, 1000),
    "max_sessions_per_account": (10, 100),
}


@pytest.mark.parametrize("name", list(_SETTING_BOUNDS))
def test_new_settings_reject_zero_negative_and_huge_values(name):
    """P6-AC2 (review #16): every new integer setting is > 0 with an upper bound; defaults per SPEC-GAP-034/036/041/049/051."""
    from pydantic import ValidationError

    from src.settings import Settings

    default, upper = _SETTING_BOUNDS[name]
    assert getattr(Settings(_env_file=None), name) == default
    assert Settings(_env_file=None).accounts_enabled is False
    assert getattr(Settings(_env_file=None, **{name: upper}), name) == upper
    for bad in (0, -1, upper + 1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{name: bad})


def test_x_forwarded_for_does_not_change_login_client_key(auth_api):
    """P6-AC2 / threat 42 (review #3): spoofed forwarding headers do not buy fresh buckets; uvicorn runs with --no-proxy-headers."""
    for i in range(20):
        response = auth_api.post(
            "/v1/auth/login",
            json={"username": f"user{i:02d}", "password": "wrong password 1234"},
            headers={
                "X-Forwarded-For": f"203.0.113.{i}",
                "X-Real-IP": f"203.0.114.{i}",
                "Forwarded": f"for=198.51.100.{i}",
            },
        )
        assert response.status_code == 401
    spoofed = auth_api.post(
        "/v1/auth/login",
        json={"username": "user-new", "password": "wrong password 1234"},
        headers={"X-Forwarded-For": "192.0.2.99"},
    )
    assert spoofed.status_code == 429 and spoofed.json() == RATE_LIMITED  # still the same client as before

    for path in (*ROOT.glob("src/*.py"), *ROOT.glob("api/*.py"), ROOT / "app.py"):
        assert "x-forwarded-for" not in path.read_text(encoding="utf-8").lower(), path  # the app never reads XFF

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    command = next(line for line in compose.splitlines() if "api.main:app" in line)
    assert "--no-proxy-headers" in command
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    run_lines = [line for line in readme.splitlines() if "uvicorn api.main:app" in line]
    assert run_lines and all("--no-proxy-headers" in line for line in run_lines)


# ---------------------------------------------------------------------------- review fixes (fix step, P6)
def test_bad_shape_login_without_client_key_does_not_reset_user_limit(fast_kdf, clock):
    """P6-AC2 / threat 26 (security H1): a bad-shape login that reserves only the global key must not prune user rows."""
    from src.accounts import LoginFailed, LoginRateLimited

    service = make_service()
    add_account("alice")
    for i in range(config.LOGIN_MAX_FAILURES_PER_USER):
        with pytest.raises(LoginFailed):
            service.login_raw(login_body("alice", "wrong password 1234"), f"h1-client-{i}")
    with pytest.raises(LoginRateLimited):
        service.login_raw(login_body("alice", PASSWORD), "h1-client-x")  # sanity: the user key is at its limit

    clock.tick(2)  # longer than the 1 s global window, far shorter than the failure window
    with pytest.raises(LoginFailed):  # bad shape, address unknown: only the 1 s global key is reserved
        service.login_raw(b"this is not json", None)
    with pytest.raises(LoginRateLimited):  # the user's failures must still count
        service.login_raw(login_body("alice", PASSWORD), "h1-client-y")


def test_login_racing_password_reset_gets_no_surviving_session(fast_kdf, monkeypatch):
    """P6-AC2 / threat 37 (security H3): a login verified with the OLD password while an admin resets it is not a live session."""
    from src import accounts, passwords
    from src.accounts import LoginFailed, PasswordLogin, SessionAuthenticator

    add_account("alice", PASSWORD)
    real_verify = passwords.verify_password

    def verify_then_admin_resets(password, encoded):
        matched = real_verify(password, encoded)
        accounts.set_password("alice", OTHER_PASSWORD)  # lands after the check, before the session is created
        return matched

    monkeypatch.setattr(passwords, "verify_password", verify_then_admin_resets)
    try:
        info = PasswordLogin().login(login_body("alice", PASSWORD), "h3-client")
    except LoginFailed:
        return  # refused: fine
    monkeypatch.setattr(passwords, "verify_password", real_verify)
    assert SessionAuthenticator().resolve(info["token"]) is None, "a session obtained with the old password survived"


def _count_slots_during(monkeypatch):
    """Spy: how many kdf_slot contexts are open at any moment (and the number open when `hashing` ran)."""
    from src import passwords

    state_ = {"open": 0, "max": 0, "open_at_hash": []}
    real_slot, real_hash = passwords.kdf_slot, passwords.hash_password

    from contextlib import contextmanager

    @contextmanager
    def counting_slot(timeout):
        with real_slot(timeout):
            state_["open"] += 1
            state_["max"] = max(state_["max"], state_["open"])
            try:
                yield
            finally:
                state_["open"] -= 1

    def spying_hash(password):
        state_["open_at_hash"].append(state_["open"])
        return real_hash(password)

    monkeypatch.setattr(passwords, "kdf_slot", counting_slot)
    monkeypatch.setattr(passwords, "hash_password", spying_hash)
    return state_


def test_rehash_does_not_undo_concurrent_password_reset(fast_kdf, monkeypatch):
    """P6-AC2 / threat 27 (review M1): an admin password change landing during the rehash must win."""
    from src import accounts, passwords, state

    add_account("alice", PASSWORD)  # stored with N=16
    monkeypatch.setattr(config, "SCRYPT_N", 2**5)  # the operator raised the cost: the next login rehashes
    real_hash, fired = passwords.hash_password, []

    def hash_with_admin_reset_in_between(password):
        if password == PASSWORD and not fired:  # the rehash of the old password
            fired.append(True)
            accounts.set_password("alice", OTHER_PASSWORD)
        return real_hash(password)

    monkeypatch.setattr(passwords, "hash_password", hash_with_admin_reset_in_between)
    with contextlib.suppress(Exception):  # refusing the login is acceptable; only the stored password matters here
        sign_in(make_service(), "alice", PASSWORD, client_key="m1")
    monkeypatch.setattr(passwords, "hash_password", real_hash)
    assert fired, "the rehash did not run"
    stored = state.get_account_by_username("alice")["password_hash"]
    assert passwords.verify_password(OTHER_PASSWORD, stored), "the admin's new password was overwritten"
    assert not passwords.verify_password(PASSWORD, stored), "the old password works again after the reset"


def test_concurrent_login_after_param_bump_is_not_refused(fast_kdf, monkeypatch):
    """P6-AC2 (review N2): login A's rehash landing before login B's must not make B fail; B is refunded.

    Distinct from a real password reset (test_rehash_does_not_undo_concurrent_password_reset), which must refuse.
    """
    from src import passwords, state

    add_account("alice", PASSWORD)
    monkeypatch.setattr(config, "SCRYPT_N", 2**5)  # needs_rehash is now true for the stored hash
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_USER", 1, raising=False)  # an unrefunded attempt would lock out
    account = state.get_account_by_username("alice")
    old_hash = str(account["password_hash"])
    real_verify, fired = passwords.verify_password, []

    def verify_then_login_a_rehashes(password, encoded):
        result = real_verify(password, encoded)
        if result and not fired and encoded == old_hash:  # B verified against the old hash ...
            fired.append(True)  # ... and login A's compare-and-set rehash lands before B's
            assert state.rehash_password(str(account["account_id"]), old_hash, passwords.hash_password(PASSWORD))
        return result

    monkeypatch.setattr(passwords, "verify_password", verify_then_login_a_rehashes)
    token = sign_in(make_service(), "alice", PASSWORD, client_key="n2")  # must not raise LoginFailed
    assert token and fired, "the race was not injected"
    monkeypatch.setattr(passwords, "verify_password", real_verify)
    assert sign_in(make_service(), "alice", PASSWORD, client_key="n2")  # B was refunded: not counted as a failure


def test_rehash_keeps_other_sessions(fast_kdf, monkeypatch):
    """P6-AC2 / threat 27 (review M1): rehashing the same password on login is not a password change."""
    from src import passwords, state
    from src.accounts import SessionAuthenticator

    principal = add_account("alice", PASSWORD)
    auth = SessionAuthenticator()
    other = auth.start_session(principal["account_id"])["token"]
    old_hash = state.get_account_by_username("alice")["password_hash"]
    monkeypatch.setattr(config, "SCRYPT_N", 2**5)
    assert passwords.needs_rehash(old_hash)

    new_token = sign_in(make_service(), "alice", PASSWORD, client_key="m1")
    assert state.get_account_by_username("alice")["password_hash"] != old_hash  # the rehash did happen
    assert auth.resolve(other) is not None, "rehash on login signed the account's other sessions out"
    assert auth.resolve(new_token) is not None


def test_rehash_runs_inside_a_kdf_slot(fast_kdf, monkeypatch):
    """P6-AC2 / threat 42 (review M1): the rehash scrypt is bounded by the same KDF slots as the check."""
    from src import state

    add_account("alice", PASSWORD)
    monkeypatch.setattr(config, "SCRYPT_N", 2**5)
    spy = _count_slots_during(monkeypatch)
    sign_in(make_service(), "alice", PASSWORD, client_key="m1")
    assert state.get_account_by_username("alice")["password_hash"]  # login worked
    assert spy["open_at_hash"], "the rehash did not run"
    assert all(count >= 1 for count in spy["open_at_hash"]), spy


def _capture_events():
    """Context manager: every `event(...)` log record as (name, fields, rendered JSON line)."""
    from src.observability import JsonFormatter, logger

    records: list[tuple[str, dict, str]] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append((record.getMessage(), dict(getattr(record, "fields", {})), JsonFormatter().format(record)))

    @contextlib.contextmanager
    def capture():
        handler, old_level = Capture(), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            yield records
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    return capture()


def test_logout_and_workspace_events_carry_account_id(fast_kdf, clock):
    """P6-AC2 / threat 29, 34 (review CR5): logout and owner workspace create / delete are logged with the account id only."""
    service = make_service()
    principal = add_account("alice")
    token = sign_in(service, "alice")
    with _capture_events() as records:
        workspace = service.new_workspace(session=token)
        service.clear(workspace, session=token)
        service.logout(token)
    by_name = {name: fields for name, fields, _ in records}
    for name in ("workspace.created", "workspace.deleted", "auth.logout"):
        assert name in by_name, (name, sorted(by_name))
        assert by_name[name].get("account_id") == principal["account_id"], name
    dump = "\n".join(line for _, _, line in records)
    assert token not in dump and "alice" not in dump
