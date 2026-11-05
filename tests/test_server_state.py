"""
P7 server-state tests (docs/phases/p7-plan.md 3.1, 3.2, 4 and 8; ADRs 0013-0015; SPEC-GAP-062, -069, -070, -082, -085).

Three kinds of test live here:
- failure handling (fail closed, fixed messages): run on the local backend by making `src.state` functions raise
  `StorageUnavailable`; they fail until the P7 error contract exists;
- contract tests parametrised over `storage_backend` (`[local]` always runs, `[server]` needs TRUSTRAG_TEST_* URLs);
- server-only tests (`server_backend` fixture) that look into real Postgres / Redis.
`psycopg` / `redis` are only imported through `tests.server_support`, inside functions.
"""

import ast
import contextlib
import hashlib
import io
import multiprocessing
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import config, state
from tests import server_support
from tests.fakes import FakeLLM, FakeStore
from tests.p6_support import (
    HANDBOOK,
    PASSWORD,
    RATE_LIMITED,
    add_account,
    bearer,
    comparable,
    install_api,
    login_body,
    make_service,
)

ROOT = Path(__file__).resolve().parent.parent
FIXED_503 = {"detail": "Storage is temporarily unavailable. Please try again."}
DRIVER_TEXT = (
    'connection to server at "10.9.8.7", port 5432 failed: FATAL password authentication failed for user '
    '"trustrag_app" (postgresql://trustrag_app:hunter2-pw@db.internal.example/trustrag)'
)
LEAKS = ("10.9.8.7", "hunter2-pw", "trustrag_app", "db.internal.example", "password authentication")


def _unavailable():
    """A StorageUnavailable raised the way a driver failure would be: the driver text is only the __cause__."""
    from src.state import StorageUnavailable

    try:
        raise RuntimeError(DRIVER_TEXT)
    except RuntimeError as error:
        return StorageUnavailable(), error


def _raise_unavailable(*args, **kwargs):
    from src.state import StorageUnavailable

    try:
        raise RuntimeError(DRIVER_TEXT)
    except RuntimeError as error:
        raise StorageUnavailable from error


def _fail(monkeypatch, *names):
    """Make the named `src.state` functions raise StorageUnavailable (fails at once if that class does not exist yet)."""
    from src.state import StorageUnavailable  # noqa: F401

    for name in names:
        monkeypatch.setattr(state, name, _raise_unavailable)


def _api_login(client, username, password):
    return client.post("/v1/auth/login", json={"username": username, "password": password})


@pytest.fixture
def sec_api(monkeypatch, accounts_on, fast_kdf):
    return TestClient(install_api(monkeypatch, make_service()).app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------- error contract (plan 2.3, 2.5, SPEC-GAP-069)
def test_storage_unavailable_is_not_a_value_error_or_llm_error():
    """P7-AC1 (failure): never the 404 handler (ValueError) and never swallowed as `LLMError` (rules-only scan)."""
    from src.llm import LLMError
    from src.state import StorageUnavailable

    assert issubclass(StorageUnavailable, Exception)
    assert not issubclass(StorageUnavailable, ValueError | LLMError)


def test_storage_errors_give_fixed_503_without_driver_text(monkeypatch, caplog):
    """P7-AC1 (failure) / threat 45, 49: a storage failure is a fixed 503 (not 404, not 500); no driver text anywhere."""
    client = TestClient(install_api(monkeypatch, make_service()).app, raise_server_exceptions=False)
    _fail(monkeypatch, "workspace_record", "live_owned_ids")
    workspace = "0123456789abcdef" * 2
    responses = [
        client.get(f"/v1/workspaces/{workspace}/documents"),
        client.post(f"/v1/workspaces/{workspace}/questions", json={"question": "annual leave?"}),
        client.post("/v1/workspaces"),
    ]
    for response in responses:
        assert (response.status_code, response.json()) == (503, FIXED_503), response.text
        for leak in LEAKS:
            assert leak not in response.text and leak not in str(response.headers)
    for leak in LEAKS:
        assert leak not in caplog.text, leak


def test_redis_down_login_gives_same_429_and_runs_no_kdf(sec_api, monkeypatch, scrypt_spy):
    """P7-AC1 (failure) / threat 45: the rate-limit store is down -> the existing 429, and no password is hashed."""
    add_account("alice")
    scrypt_spy.reset()
    _fail(monkeypatch, "reserve_login_attempt")
    for username, password in (("alice", PASSWORD), ("alice", "a wrong password 12"), ("nobody", PASSWORD)):
        response = _api_login(sec_api, username, password)
        assert response.status_code == 429 and response.json() == RATE_LIMITED, response.text
        assert response.headers["Retry-After"].isdigit() and int(response.headers["Retry-After"]) >= 1
    assert scrypt_spy.calls == 0


@pytest.mark.parametrize("failing", ["reserve_login_attempt", "get_account_by_username", "prune_expired"])
def test_postgres_down_login_response_is_same_for_unknown_wrong_and_right(sec_api, monkeypatch, failing):
    """P7-AC1 (failure) / threat 60 (SPEC-GAP-082): a storage error on the login path is no oracle: one 429 for all."""
    add_account("alice")
    _fail(monkeypatch, failing)
    seen = set()
    for user, password in (("alice", PASSWORD), ("alice", "a wrong password 12"), ("nobody", PASSWORD)):
        status, body, headers = comparable(_api_login(sec_api, user, password))
        seen.add(repr((status, body, sorted(headers.items()))))
    assert len(seen) == 1, seen
    status, body, headers = comparable(_api_login(sec_api, "nobody", PASSWORD))
    assert status == 429 and body == RATE_LIMITED and "retry-after" in headers


def test_refund_failure_after_session_does_not_503(sec_api, monkeypatch):
    """P7-AC1 (failure) / threat 60: the refund after a created session fails -> still a login (200), failure logged."""
    from src import accounts

    add_account("alice")
    events = []
    monkeypatch.setattr(accounts, "event", lambda name, **fields: events.append((name, fields)))
    _fail(monkeypatch, "refund_login_attempt")
    response = _api_login(sec_api, "alice", PASSWORD)
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    assert sec_api.get("/v1/auth/me", headers=bearer(token)).status_code == 200  # the session is real and usable
    assert any(name == "auth.refund_failed" for name, _ in events), events
    assert "alice" not in repr(events) and token not in repr(events)  # no username or token in the log event


def test_redis_down_model_call_is_refused(monkeypatch):
    """P7-AC1 (failure) / threat 45: the call-limit store is down -> StorageUnavailable, no model call, not LLMError."""
    from src import claude_code, llm
    from src.state import StorageUnavailable

    def must_not_run(*args, **kwargs):
        raise AssertionError("a model call was made although the limit could not be checked")

    _fail(monkeypatch, "try_acquire_call")
    monkeypatch.setattr(claude_code, "ask_detailed", must_not_run)
    with pytest.raises(StorageUnavailable) as caught:
        llm.call_claude("some-model", "system", "prompt", 10)
    assert not isinstance(caught.value, llm.LLMError)


def test_call_limit_store_error_aborts_upload_not_rules_only(monkeypatch):
    """P7-AC1 (failure) / threat 62: an outage during the AI scan aborts the upload; it must not index rules-only."""
    from src import llm
    from src.service import TrustRAGService
    from src.state import StorageUnavailable

    _fail(monkeypatch, "try_acquire_call")
    store = FakeStore()
    service = TrustRAGService(store=store, llm=llm.call_claude)
    workspace = service.new_workspace()
    with pytest.raises(StorageUnavailable):
        service.upload(workspace, "handbook.pdf", HANDBOOK)
    assert store.count(workspace) == 0


def test_cache_error_is_not_treated_as_a_miss(monkeypatch):
    """P7-AC1 (failure): a cache read error propagates (no silent extra model calls); OCR and scan caches alike."""
    from src import ocr
    from src.state import StorageUnavailable

    _fail(monkeypatch, "cache_get_many")
    llm = FakeLLM()
    service = make_service(llm=llm)
    workspace = service.new_workspace()
    with pytest.raises(StorageUnavailable):
        service.upload(workspace, "handbook.pdf", HANDBOOK)
    assert not [call for call in llm.calls if call["purpose"] == "scan"]
    with pytest.raises(StorageUnavailable):
        ocr.ocr_pages(b"%PDF-1.4", [1], "f" * 64, client=FakeLLM())
    assert FakeLLM().calls == []


@pytest.mark.parametrize(
    "argv",
    [["create", "alice", "--password-stdin"], ["list"], ["disable", "alice"]],
)
def test_admin_cli_storage_error_prints_fixed_message(monkeypatch, capsys, argv):
    """P7-AC1 (failure) / threat 49: the admin CLI prints one fixed line and exits 1: no traceback, no driver text."""
    from src import accounts

    _fail(monkeypatch, "create_account", "get_account_by_username", "list_accounts")
    code = accounts.main(argv, stdin=io.StringIO(PASSWORD + "\n"))
    out, err = capsys.readouterr()
    assert code == 1
    assert "Storage is temporarily unavailable" in err
    assert "Traceback" not in out + err
    for leak in LEAKS:
        assert leak not in out + err


def test_ui_storage_unavailable_shows_fixed_message(monkeypatch):
    """P7-AC1 (failure): the in-process UI shows the same fixed message, never a traceback or driver text."""
    from src.state import StorageUnavailable  # noqa: F401
    from tests.test_ui_accounts import _launch

    service = make_service()
    service.new_workspace = _raise_unavailable  # type: ignore[method-assign]
    at = _launch(monkeypatch, service, accounts=False).run()
    assert not at.exception, [e.value for e in at.exception]
    shown = "\n".join(
        str(getattr(e, "value", "")) for e in [*at.error, *at.warning, *at.info, *at.markdown, *at.caption]
    )
    assert FIXED_503["detail"] in shown
    for leak in LEAKS:
        assert leak not in shown


# ---------------------------------------------------------------------------- script replies and per-key windows
def test_refused_call_never_returns_zero_wait(storage_backend, clock):
    """P7-AC1 (script replies) / threat 59: a refusal is never a falsy 0 (`if wait:` would treat it as allowed)."""
    assert state.try_acquire_call(1, 1) == 0
    clock.tick(0.2)
    wait = state.try_acquire_call(1, 1)
    assert wait >= 1.0


def test_login_refusal_with_subsecond_retry_is_still_refused(storage_backend, clock):
    """P7-AC1 (script replies) / threat 59: a refusal that frees up in under a second is still a refusal, floored at 1 s."""
    limits = {"global": (1, 1.0)}
    attempt, _ = state.reserve_login_attempt(["global"], limits, time.time())
    assert attempt is not None
    clock.tick(0.4)
    attempt, retry_after = state.reserve_login_attempt(["global"], limits, time.time())
    assert attempt is None and retry_after >= 1.0


def test_global_window_prune_does_not_drop_user_or_client_rows(storage_backend, clock):
    """P7-AC1 / 3.2 per-key windows: the 1 s global window never deletes the 15 minute user rows."""
    limits = {"user": (2, 900.0), "global": (5, 1.0)}
    for _ in range(2):
        assert state.reserve_login_attempt(["user", "global"], limits, time.time())[0] is not None
        clock.tick(2.0)
    assert state.reserve_login_attempt(["user", "global"], limits, time.time())[0] is None


def test_refused_reservation_inserts_no_rows_for_any_key(storage_backend, clock):
    """P7-AC1 / threat 44: a refused reservation leaves no row under ANY key (no partial counting)."""
    limits = {"a": (1, 900.0), "b": (3, 900.0)}
    assert state.reserve_login_attempt(["a", "b"], limits, time.time())[0] is not None
    for _ in range(3):
        assert state.reserve_login_attempt(["a", "b"], limits, time.time())[0] is None
    # `b` holds exactly the one successful row: two more b-only reservations fit under its limit of 3, the third not
    assert state.reserve_login_attempt(["b"], limits, time.time())[0] is not None
    assert state.reserve_login_attempt(["b"], limits, time.time())[0] is not None
    assert state.reserve_login_attempt(["b"], limits, time.time())[0] is None


def test_refund_removes_only_this_attempts_rows(storage_backend, clock):
    """P7-AC1 / 3.2: refunding attempt 1 frees exactly its own rows; attempt 2 still counts."""
    limits = {"u": (2, 900.0), "c": (2, 900.0)}
    first, _ = state.reserve_login_attempt(["u", "c"], limits, time.time())
    second, _ = state.reserve_login_attempt(["u", "c"], limits, time.time())
    assert first and second and first != second
    assert state.reserve_login_attempt(["u", "c"], limits, time.time())[0] is None
    state.refund_login_attempt(first, ["u", "c"])
    assert state.reserve_login_attempt(["u", "c"], limits, time.time())[0] is not None
    assert state.reserve_login_attempt(["u", "c"], limits, time.time())[0] is None


# ---------------------------------------------------------------------------- real concurrency (both backends)
def _new_account(name="racer", password_hash="hash-0"):  # noqa: S107 - a placeholder, not a credential
    account_id = uuid.uuid4().hex
    state.create_account(account_id, name, password_hash)
    return account_id


def test_create_session_and_set_password_serialise_under_real_concurrency(storage_backend):
    """P7-AC1 / 3.2 / threat 37: a login racing a password reset never leaves a live session."""
    account_id = _new_account()
    for i in range(25):
        current, token_hash = f"hash-{i}", hashlib.sha256(f"token-{i}".encode()).hexdigest()
        barrier = threading.Barrier(2)
        now = time.time()

        def create(current=current, token_hash=token_hash, now=now, barrier=barrier):
            barrier.wait()
            return state.create_session(token_hash, account_id, now, now + 3600, 10, current)

        def reset(i=i, barrier=barrier):
            barrier.wait()
            return state.set_password_hash(account_id, f"hash-{i + 1}")

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(create), pool.submit(reset)]
            for future in futures:
                future.result(timeout=60)
        assert state.get_session_principal(token_hash, time.time(), 3600) is None, i


def test_concurrent_logins_never_exceed_session_cap(storage_backend):
    """P7-AC1 / 3.2 / threat 29: 12 simultaneous session inserts with a cap of 3 leave exactly 3 live sessions."""
    account_id = _new_account()
    hashes = [hashlib.sha256(f"cap-{i}".encode()).hexdigest() for i in range(12)]
    now = time.time()
    barrier = threading.Barrier(len(hashes))

    def create(token_hash):
        barrier.wait()
        return state.create_session(token_hash, account_id, now, now + 3600, 3)

    with ThreadPoolExecutor(len(hashes)) as pool:
        assert all(pool.map(create, hashes))
    live = [h for h in hashes if state.get_session_principal(h, time.time(), 3600)]
    assert len(live) == 3


def test_concurrent_workspace_creation_respects_cap(storage_backend):
    """P7-AC1 / 3.2 / threat 32: ten simultaneous workspace creations with a cap of 3 record exactly 3."""
    account_id = _new_account()
    barrier = threading.Barrier(10)

    def add(_):
        barrier.wait()
        return state.add_owned_workspace("acct-" + uuid.uuid4().hex, account_id, 3)

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(add, range(10)))
    assert sum(results) == 3
    assert len(state.list_owned_workspaces(account_id)) == 3


def _schema_child(snapshot):
    from tests import server_support as support

    support.apply_snapshot(snapshot)
    from src import state as child_state

    return len(child_state.list_accounts())


def test_concurrent_schema_initialisation_succeeds(storage_backend):
    """P7-AC1 / 3.2: four processes initialising a fresh database at the same moment all succeed (advisory lock)."""
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        counts = pool.map(_schema_child, [server_support.child_snapshot()] * 4)
    assert counts == [0, 0, 0, 0]


# ---------------------------------------------------------------------------- static scan
def _sql_violations(path: Path) -> list[str]:
    keywords = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|SET)\b")
    safe_parts = {"Identifier", "Literal", "Placeholder", "SQL"}
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str))
            if keywords.search(text):
                found.append(f"{path.name}:{node.lineno} f-string SQL")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add):
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str) and keywords.search(left.value):
                found.append(f"{path.name}:{node.lineno} concatenated SQL")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            receiver = node.func.value
            if (
                isinstance(receiver, ast.Constant)
                and isinstance(receiver.value, str)
                and keywords.search(receiver.value)
            ):
                found.append(f"{path.name}:{node.lineno} str.format SQL")
            elif (
                isinstance(receiver, ast.Call)
                and getattr(receiver.func, "attr", getattr(receiver.func, "id", "")) == "SQL"
            ):
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    name = getattr(getattr(arg, "func", None), "attr", getattr(getattr(arg, "func", None), "id", ""))
                    if name not in safe_parts:
                        found.append(f"{path.name}:{node.lineno} sql.SQL.format with a non-Identifier argument")
    return found


def test_state_modules_build_no_sql_from_values():
    """P7-AC1 (DB role) / threat 48: no f-string / % / + / .format SQL in the Postgres modules (only sql.Identifier)."""
    paths = [ROOT / "src" / "state_postgres.py", ROOT / "src" / "pgvector_store.py"]
    for path in paths:
        assert path.exists(), f"{path.name} does not exist yet"
    assert [v for path in paths for v in _sql_violations(path)] == []


def test_sql_scan_detects_the_patterns_it_forbids(tmp_path):
    """The scanner itself: a file that builds SQL from values is reported, one using sql.Identifier is not."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        'def f(c, x):\n    c.execute(f"SELECT * FROM t WHERE a = {x}")\n    c.execute("DELETE FROM t WHERE a=%s" % x)\n'
    )
    good = tmp_path / "good.py"
    good.write_text('def f(c, x):\n    c.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(x)))\n')
    assert len(_sql_violations(bad)) == 2 and _sql_violations(good) == []


# ---------------------------------------------------------------------------- test safety (SPEC-GAP-089)
def test_teardown_refuses_unexpected_schema_or_prefix():
    """P7-AC1 (test safety) / threat 63: DROP SCHEMA / key deletion is refused for anything not test-shaped."""
    good_schema, good_prefix = server_support.new_names()
    server_support.assert_safe_teardown(good_schema, good_prefix, "postgresql://t/test", "postgresql://real/prod")
    for schema in ("trustrag", "public", "test_", "TEST_" + "a" * 32, "test_" + "g" * 32, "test_" + "a" * 31, ""):
        with pytest.raises(server_support.UnsafeTeardown):
            server_support.assert_safe_teardown(schema, good_prefix, "postgresql://t/test")
    for prefix in ("trustrag:{t}:", "trustrag-test:", "", "trustrag:", "trustrag-test:" + "a" * 32):
        with pytest.raises(server_support.UnsafeTeardown):
            server_support.assert_safe_teardown(good_schema, prefix, "postgresql://t/test")
    with pytest.raises(server_support.UnsafeTeardown):
        server_support.assert_safe_teardown(
            good_schema, good_prefix, "postgresql://real/prod", "postgresql://real/prod"
        )


def test_server_backends_configured_when_ci():
    """P7-AC1 (test safety) / SPEC-GAP-071: in CI (CI=true) the server backends must be configured, never skipped."""
    if os.environ.get("CI", "").lower() != "true":
        pytest.skip("not running in CI")
    assert server_support.configured(), (
        f"CI=true but {server_support.DATABASE_ENV} / {server_support.REDIS_ENV} are not both set: "
        "the [server] tests would be silently skipped"
    )


# ---------------------------------------------------------------------------- server-only: nothing sensitive at rest
def _dump_everything() -> str:
    """Every row of every table in the test schema and every Redis key/value under the test prefix, as text."""
    parts = []
    for (table,) in server_support.pg_rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ):
        if table == "chunks":
            continue  # 384-float embeddings; not relevant to secrets
        parts += [str(row) for row in server_support.pg_rows(f'SELECT * FROM "{table}"')]  # noqa: S608 - names from the catalog
    client = server_support.redis_client()
    for key in server_support.redis_keys():
        parts.append(key)
        parts.append(str(client.zrange(key, 0, -1, withscores=True)))
    return "\n".join(parts)


def test_server_state_never_contains_plain_password_or_token(server_backend, fast_kdf, clock):
    """P7-AC1 (server twins) / threat 50: neither passwords (right or wrong) nor tokens appear in Postgres or Redis."""
    from src.accounts import LoginFailed

    service = make_service()
    add_account("alice")
    token = str(service.login_raw(login_body("alice", PASSWORD), "c1")["token"])
    clock.tick(1)
    with pytest.raises(LoginFailed):
        service.login_raw(login_body("alice", "a wrong password 98765"), "c2")
    dump = _dump_everything()
    assert "alice" in dump  # the dump really contains the account row
    for secret in (PASSWORD, "a wrong password 98765", token):
        assert secret not in dump


def test_server_session_token_stored_only_as_sha256(server_backend, fast_kdf):
    """P7-AC1 (server twins) / threat 29, 50: `sessions.token_hash` is sha256(token) and the token is stored nowhere."""
    service = make_service()
    principal = add_account("alice")
    token = str(service.login_raw(login_body("alice", PASSWORD), "c1")["token"])
    rows = server_support.pg_rows("SELECT token_hash, account_id FROM sessions")
    assert rows == [(hashlib.sha256(token.encode()).hexdigest(), principal["account_id"])]
    assert token not in _dump_everything()


def test_server_mode_creates_no_sqlite_file(server_backend, fast_kdf, tmp_path):
    """P7-AC1 (server twins) / threat 52: server mode opens no SQLite file (state or chroma dir)."""
    service = make_service()
    add_account("alice")
    token = str(service.login_raw(login_body("alice", PASSWORD), "c1")["token"])
    state.try_acquire_call(10)
    state.cache_set_many("ns", {"k": 1})
    service.new_workspace(session=token)
    assert not Path(config.STATE_DB).exists()
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_rate_limit_keys_in_redis_are_hashed_pseudonyms(server_backend, fast_kdf):
    """P7-AC1 (server twins) / threat 28, 50: Redis keys hold sha256 pseudonyms or `global`, never names or addresses."""
    from src.accounts import LoginFailed, PasswordLogin

    add_account("alice")
    with pytest.raises(LoginFailed):
        PasswordLogin().login(login_body("alice", "a wrong password 12"), "203.0.113.9")
    keys = [k.removeprefix(config.REDIS_PREFIX) for k in server_support.redis_keys()]
    assert keys, "no Redis keys were written"
    for key in keys:
        assert key == "calls" or re.fullmatch(r"login:([0-9a-f]{64}|global)", key), key
        assert "alice" not in key and "203.0.113.9" not in key


# ---------------------------------------------------------------------------- server-only: clocks (threat 54)
def test_skewed_host_cannot_prune_other_hosts_attempts(server_backend, monkeypatch):
    """P7-AC1 (clock) / threat 54: a host whose clock is hours ahead cannot expire the other hosts' attempts."""
    limits = {"user": (1, 900.0)}
    assert state.reserve_login_attempt(["user"], limits, time.time())[0] is not None
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 10 * 3600)  # this "host" is 10 hours fast
    attempt, _ = state.reserve_login_attempt(["user"], limits, time.time())
    assert attempt is None  # the first host's row still counts: the store clock decided


def test_skewed_host_cannot_extend_or_expire_sessions(server_backend, monkeypatch):
    """P7-AC1 (clock) / threat 54: session liveness follows the database clock, whatever the caller's clock says."""
    account_id = _new_account()
    real_time = time.time
    live, dead = "a" * 64, "b" * 64
    now = real_time()
    assert state.create_session(live, account_id, now, now + 3600, 10)
    assert state.create_session(dead, account_id, now - 7200, now - 3600, 10)  # already expired by the real clock
    monkeypatch.setattr(time, "time", lambda: real_time() + 10 * 3600)  # a fast host would see `live` as expired
    assert state.get_session_principal(live, time.time(), 3600) is not None
    monkeypatch.setattr(time, "time", lambda: real_time() - 10 * 3600)  # a slow host would see `dead` as alive
    assert state.get_session_principal(dead, time.time(), 3600) is None


def test_skewed_host_cannot_age_out_anonymous_workspaces(server_backend, monkeypatch):
    """P7-AC1 (clock) / threat 54: the 24 h cutoff uses the database clock; a fast host deletes nothing."""
    from src.pgvector_store import PgVectorStore

    store = PgVectorStore()
    workspace = store.new_workspace()
    store.add_chunks(workspace, [{"id": "c1", "text": "Annual leave is 25 days.", "file_name": "a.pdf", "page": 1}])
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 72 * 3600)
    assert store.cleanup() == 0
    assert store.count(workspace) == 1


# ---------------------------------------------------------------------------- server-only: caches (threat 55, 56)
def _backdate_cache(hours: float) -> None:
    server_support.pg_rows("UPDATE cache SET created = created - %s", (hours * 3600,))


def test_cache_entries_have_ttl(server_backend):
    """P7-AC1 (caches) / threat 55: entries older than SERVER_CACHE_TTL_HOURS are ignored, and deleted on the next write."""
    state.cache_set_many("ns", {"old": {"x": 1}})
    assert state.cache_get_many("ns", ["old"]) == {"old": {"x": 1}}
    _backdate_cache(config.SERVER_CACHE_TTL_HOURS + 1)
    assert state.cache_get_many("ns", ["old"]) == {}
    state.cache_set_many("ns", {"new": 2})
    assert server_support.pg_rows("SELECT key FROM cache ORDER BY key") == [("new",)]


def test_ocr_cache_entries_expire(server_backend):
    """P7-AC1 (caches) / threat 56: OCR page text does not outlive the TTL in server mode."""
    from src import ocr

    key = ocr._cache_key("c" * 64, 1)
    state.cache_set_many("ocr", {key: "PAGE TEXT THAT MUST NOT LINGER"})
    assert state.cache_get_many("ocr", [key]) == {key: "PAGE TEXT THAT MUST NOT LINGER"}
    _backdate_cache(config.SERVER_CACHE_TTL_HOURS + 1)
    assert state.cache_get_many("ocr", [key]) == {}
    state.cache_set_many("ocr", {"other": "x"})  # any write purges expired rows
    assert "LINGER" not in _dump_everything()


def test_redis_holds_only_counter_keys(server_backend, fast_kdf):
    """P7-AC1 (caches) / threat 55: Redis memory is independent of document volume: only `calls` and `login:*`."""
    from src.accounts import LoginFailed, PasswordLogin

    add_account("alice")
    state.try_acquire_call(10)
    state.cache_set_many("ocr", {"k": "page text"})
    state.cache_set_many("scan", {"k": {"verdict": "SAFE"}})
    with pytest.raises(LoginFailed):
        PasswordLogin().login(login_body("alice", "a wrong password 12"), "c1")
    keys = [k.removeprefix(config.REDIS_PREFIX) for k in server_support.redis_keys()]
    assert "calls" in keys and any(k.startswith("login:") for k in keys)
    assert all(k == "calls" or k.startswith("login:") for k in keys), keys


# ---------------------------------------------------------------------------- server-only: connections (threat 57)
def test_stuck_account_lock_times_out_with_503_not_hang(server_backend, monkeypatch):
    """P7-AC1 (connections) / threat 57: a row lock held elsewhere ends in StorageUnavailable within bounds."""
    from src.state import StorageUnavailable

    monkeypatch.setattr(config, "PG_LOCK_TIMEOUT_MS", 500, raising=False)
    server_support.reset_connections()
    account_id = _new_account()
    blocker = server_support.pg_connect(config.PG_SCHEMA)
    try:
        blocker.execute("BEGIN")
        blocker.execute("SELECT 1 FROM accounts WHERE account_id = %s FOR UPDATE", (account_id,))
        started = time.monotonic()
        with pytest.raises(StorageUnavailable):
            state.set_disabled(account_id, True)
        assert time.monotonic() - started < 10
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_connection_recovers_after_failed_transaction(server_backend):
    """P7-AC1 (connections) / threat 57: errors roll back and discard; more failures than pool slots still leave it usable."""
    from src.state import DuplicateAccount

    state.create_account(uuid.uuid4().hex, "dupe", "hash")
    for _ in range(config.PG_MAX_CONNECTIONS_PER_PROCESS + 5):
        with pytest.raises(DuplicateAccount):
            state.create_account(uuid.uuid4().hex, "dupe", "hash")
    assert [a["username"] for a in state.list_accounts()] == ["dupe"]
    assert state.try_acquire_call(5) == 0


def test_pool_exhaustion_gives_503_not_hang(server_backend, monkeypatch):
    """P7-AC1 (connections) / threat 57: with every connection busy a further call gives StorageUnavailable, not a hang."""
    from src.state import StorageUnavailable

    monkeypatch.setattr(config, "PG_MAX_CONNECTIONS_PER_PROCESS", 1, raising=False)
    monkeypatch.setattr(config, "PG_POOL_WAIT_SECONDS", 1, raising=False)
    monkeypatch.setattr(config, "PG_LOCK_TIMEOUT_MS", 4000, raising=False)
    server_support.reset_connections()
    account_id = _new_account()
    blocker = server_support.pg_connect(config.PG_SCHEMA)
    try:
        blocker.execute("BEGIN")
        blocker.execute("SELECT 1 FROM accounts WHERE account_id = %s FOR UPDATE", (account_id,))
        holder = threading.Thread(target=lambda: _swallow(lambda: state.set_disabled(account_id, True)))
        holder.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # wait (bounded, by condition) until the holder is blocked on the row lock
            if blocker.execute("SELECT count(*) FROM pg_locks WHERE NOT granted").fetchone()[0]:
                break
            threading.Event().wait(0.05)
        started = time.monotonic()
        with pytest.raises(StorageUnavailable):
            state.list_accounts()
        assert time.monotonic() - started < 4
        holder.join(timeout=30)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def _swallow(call):
    with contextlib.suppress(Exception):  # the holder is expected to time out
        call()


def _app_role_url() -> str:
    """The app-role URL; in CI (CI=true) a missing URL is a failure, never a silent skip (review: DB role)."""
    url = os.environ.get(server_support.APP_ROLE_ENV, "")
    if not url:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(f"CI=true but {server_support.APP_ROLE_ENV} is not set: the least-privilege role is untested")
        pytest.skip(f"{server_support.APP_ROLE_ENV} is not set (the compose/CI app role is not available)")
    return url


def test_app_db_role_is_not_superuser():
    """P7-AC1 (DB role) / threat 58: the application's database role cannot create roles or databases."""
    url = _app_role_url()
    with server_support.pg_connect(url=url) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False, False)


def test_app_role_cannot_create_in_public_and_owns_schema_trustrag():
    """P7 review (DB role) / threat 58: the app role has no CREATE on `public`; schema `trustrag` is owned by it."""
    url = _app_role_url()
    with server_support.pg_connect(url=url) as conn:
        can_create = conn.execute("SELECT has_schema_privilege('public', 'CREATE')").fetchone()
        owner = conn.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'trustrag'").fetchone()
        user = conn.execute("SELECT current_user").fetchone()
    assert can_create == (False,)
    assert owner == ("trustrag_app",) and user == ("trustrag_app",)
