"""
P7 server-backend error paths and rebuild rules (docs/phases/p7-plan.md 2.3, 3.1, 3.2; ADRs 0013-0015; threats 45, 49).

Two kinds of test: monkeypatched drivers (always run when `redis` / `psycopg` are importable) and real throwaway
servers (the `server_backend` fixture; skipped without TRUSTRAG_TEST_* URLs). Every error must become the fixed
`StorageUnavailable` with the driver error only as `__cause__`, so no driver text can reach a client.
"""

import os
import time
import uuid

import pytest

from src import config, state
from src.state import StorageUnavailable
from tests import server_support

SECRET = "hunter2-pw@10.9.8.7"


# ------------------------------------------------------------------------------------------------ Redis, no server
@pytest.fixture
def redis_mod(monkeypatch):
    pytest.importorskip("redis")
    from src import state_redis

    state_redis.reset_for_tests()
    monkeypatch.setattr(config, "STATE_CLOCK", "store", raising=False)
    monkeypatch.setattr(config, "REDIS_PREFIX", "trustrag-test:unit:", raising=False)
    yield state_redis
    state_redis.reset_for_tests()


def _script_returning(redis_mod, monkeypatch, reply):
    monkeypatch.setattr(redis_mod, "_script", lambda name, source: lambda keys, args: reply)


@pytest.mark.parametrize(
    "reply",
    [[], ["allowed"], ["maybe", "0"], ["refused", "not-a-number"], ["allowed", "0", "extra"], "refused"],
)
def test_redis_unexpected_call_reply_is_never_allowed(redis_mod, monkeypatch, reply):
    """P7-AC1 (failure): a Lua reply the code does not recognise fails closed instead of returning 'allowed' (0.0)."""
    _script_returning(redis_mod, monkeypatch, reply)
    with pytest.raises(StorageUnavailable):
        redis_mod.try_acquire_call(5)


def test_redis_refusal_wait_is_at_least_one_second(redis_mod, monkeypatch):
    """P7-AC1: a sub-second or zero wait in a refusal is raised to 1.0 (a falsy wait would read as 'allowed')."""
    _script_returning(redis_mod, monkeypatch, [b"refused", b"0.000000"])
    assert redis_mod.try_acquire_call(5) == 1.0
    _script_returning(redis_mod, monkeypatch, ["allowed", "0"])
    assert redis_mod.try_acquire_call(5) == 0.0


@pytest.mark.parametrize(
    "reply",
    [[], ["reserved"], ["reserved", "someone-elses-attempt"], ["refused", "x"], ["other", "1"]],
)
def test_redis_unexpected_login_reply_is_never_a_reservation(redis_mod, monkeypatch, reply):
    """P7-AC1 (failure): only a 'reserved' reply that echoes OUR attempt id counts as a reservation."""
    _script_returning(redis_mod, monkeypatch, reply)
    with pytest.raises(StorageUnavailable):
        redis_mod.reserve_login_attempt(["u"], {"u": (3, 60.0)}, 1000.0)


def test_redis_login_refusal_is_at_least_one_second(redis_mod, monkeypatch):
    _script_returning(redis_mod, monkeypatch, ["refused", "0.25"])
    assert redis_mod.reserve_login_attempt(["u"], {"u": (3, 60.0)}, 1000.0) == (None, 1.0)


def test_redis_login_with_no_keys_reserves_without_a_round_trip(redis_mod, monkeypatch):
    """No key means no limit: a fresh attempt id, and the script is never called."""

    def boom(name, source):
        raise AssertionError("no Redis call expected")

    monkeypatch.setattr(redis_mod, "_script", boom)
    attempt, wait = redis_mod.reserve_login_attempt([], {}, 0.0)
    assert attempt and wait == 0.0
    redis_mod.refund_login_attempt("anything", [])  # nothing to refund: also no call


def test_redis_driver_errors_become_storage_unavailable_with_cause_only(redis_mod, monkeypatch):
    """Threat 45/49: a driver error carries credentials in its text; it may only be chained, never re-raised."""

    def failing(name, source):
        def run(keys, args):
            raise ConnectionError(f"redis://:{SECRET}/0 refused")

        return run

    monkeypatch.setattr(redis_mod, "_script", failing)
    for call in (
        lambda: redis_mod.try_acquire_call(5),
        lambda: redis_mod.reserve_login_attempt(["u"], {"u": (3, 60.0)}, 1.0),
    ):
        with pytest.raises(StorageUnavailable) as caught:
            call()
        assert SECRET not in str(caught.value) and SECRET not in repr(caught.value.args)
        assert isinstance(caught.value.__cause__, ConnectionError)


def test_redis_refund_failure_is_storage_unavailable(redis_mod, monkeypatch):
    """The refund path (pipeline) fails closed too; the caller decides whether a failed refund matters."""

    class Pipe:
        def zrem(self, key, member):
            pass

        def execute(self):
            raise ConnectionError(SECRET)

    class Client:
        def pipeline(self, transaction):
            assert transaction is True
            return Pipe()

    monkeypatch.setattr(redis_mod, "_client", Client)
    with pytest.raises(StorageUnavailable) as caught:
        redis_mod.refund_login_attempt("attempt", ["a", "b"])
    assert SECRET not in str(caught.value) and isinstance(caught.value.__cause__, ConnectionError)


def test_redis_without_url_is_unavailable_and_malformed_url_is_chained(redis_mod, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "", raising=False)
    with pytest.raises(StorageUnavailable):
        redis_mod.try_acquire_call(5)
    monkeypatch.setattr(config, "REDIS_URL", f"bogus-scheme://:{SECRET}@host/0", raising=False)
    with pytest.raises(StorageUnavailable) as caught:
        redis_mod.try_acquire_call(5)
    assert SECRET not in str(caught.value) and caught.value.__cause__ is not None


def test_redis_client_is_reused_and_rebuilt_on_url_or_pid_change(redis_mod, monkeypatch):
    """One client per process; a new URL or a fork (new pid) builds a new client (and drops cached scripts)."""
    monkeypatch.setattr(config, "REDIS_URL", "redis://:pw@127.0.0.1:1/0", raising=False)  # lazily connected
    first = redis_mod._client()
    assert redis_mod._client() is first
    redis_mod._holder["scripts"]["calls"] = object()
    monkeypatch.setattr(config, "REDIS_URL", "redis://:pw@127.0.0.1:2/0", raising=False)
    second = redis_mod._client()
    assert second is not first and redis_mod._holder["scripts"] == {}
    real_pid = os.getpid()
    monkeypatch.setattr(redis_mod.os, "getpid", lambda: real_pid + 1)
    assert redis_mod._client() is not second


def test_redis_reset_for_tests_closes_the_client_and_survives_a_failing_close(redis_mod, monkeypatch):
    closed = []

    class Dead:
        def close(self):
            closed.append(True)
            raise RuntimeError("already dead")

    redis_mod._holder.update(client=Dead(), key=("u", 1.0), pid=os.getpid(), scripts={"calls": object()})
    redis_mod.reset_for_tests()
    assert closed == [True]
    assert redis_mod._holder["client"] is None and redis_mod._holder["scripts"] == {}
    redis_mod.reset_for_tests()  # idempotent


def test_redis_script_is_registered_once_per_client(redis_mod, monkeypatch):
    registered = []

    class Client:
        def register_script(self, source):
            registered.append(source)
            return lambda keys, args: ["allowed", "0"]

    monkeypatch.setattr(redis_mod, "_client", Client)
    redis_mod._holder["scripts"] = {}
    for _ in range(3):
        assert redis_mod.try_acquire_call(5) == 0.0
    assert len(registered) == 1


# ------------------------------------------------------------------------------------------------ Redis, real server
def test_redis_real_server_call_window_refuses_with_positive_wait_then_recovers(server_backend):
    """P7-AC1: against real Redis the third call in a window of 2 is refused with a wait >= 1.0; scripts reply as strings."""
    assert state.try_acquire_call(2, 60) == 0.0
    assert state.try_acquire_call(2, 60) == 0.0
    wait = state.try_acquire_call(2, 60)
    assert 1.0 <= wait <= 60.0
    keys = server_support.redis_keys()
    assert keys == [server_backend.prefix + "calls"]
    assert server_support.redis_client().pttl(keys[0]) > 0  # the counter key expires on its own


def test_redis_real_server_refund_removes_only_named_attempts(server_backend):
    limits = {"u": (1, 600.0), "c": (1, 600.0)}
    attempt, _ = state.reserve_login_attempt(["u", "c"], limits, time.time())
    assert attempt
    assert state.reserve_login_attempt(["u", "c"], limits, time.time())[0] is None
    state.refund_login_attempt("not-the-attempt", ["u", "c"])  # removing a stranger's id frees nothing
    assert state.reserve_login_attempt(["u", "c"], limits, time.time())[0] is None
    state.refund_login_attempt(attempt, ["u", "c"])
    assert state.reserve_login_attempt(["u", "c"], limits, time.time())[0] is not None


def test_redis_real_server_unreachable_fails_closed(server_backend, monkeypatch):
    """P7-AC1 (failure): a Redis that cannot be reached is StorageUnavailable, not an allowed call."""
    from src import state_redis

    monkeypatch.setattr(config, "REDIS_URL", "redis://:wrong@127.0.0.1:1/0", raising=False)
    monkeypatch.setattr(config, "REDIS_TIMEOUT_SECONDS", 0.5, raising=False)
    state_redis.reset_for_tests()
    with pytest.raises(StorageUnavailable):
        state.try_acquire_call(5, 60)
    with pytest.raises(StorageUnavailable):
        state.refund_login_attempt("x", ["u"])


# ------------------------------------------------------------------------------------------------ Postgres
@pytest.fixture
def pg_mod(monkeypatch):
    pytest.importorskip("psycopg")
    from src import state_postgres

    state_postgres.reset_for_tests()
    yield state_postgres
    state_postgres.reset_for_tests()


def test_postgres_without_url_or_with_bad_url_is_unavailable_and_chained(pg_mod, monkeypatch):
    """Threat 45/49: a missing or malformed DATABASE_URL (it holds the password) fails closed; text only chained."""
    monkeypatch.setattr(config, "DATABASE_URL", "", raising=False)
    with pytest.raises(StorageUnavailable):
        pg_mod.cache_get_many("ns", ["k"])
    monkeypatch.setattr(config, "DATABASE_URL", f"postgresql://u:{SECRET}@127.0.0.1:1/db", raising=False)
    monkeypatch.setattr(config, "PG_POOL_WAIT_SECONDS", 1, raising=False)
    with pytest.raises(StorageUnavailable) as caught:
        pg_mod.cache_get_many("ns", ["k"])
    assert SECRET not in str(caught.value) and caught.value.__cause__ is not None


def test_postgres_empty_cache_lookup_needs_no_connection(pg_mod, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "", raising=False)  # would be unavailable if it connected
    assert pg_mod.cache_get_many("ns", []) == {}


def test_postgres_pool_is_rebuilt_on_key_change_and_not_closed_after_fork(pg_mod, monkeypatch):
    """A new URL/schema retires the old pool (closes its idle connections); a new pid (fork) must NOT close the
    parent's sockets, only stop using them."""
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@127.0.0.1:1/a", raising=False)
    monkeypatch.setattr(config, "PG_SCHEMA", "test_pool_a", raising=False)
    first = pg_mod._pool()
    assert pg_mod._pool() is first
    monkeypatch.setattr(config, "PG_SCHEMA", "test_pool_b", raising=False)
    second = pg_mod._pool()
    assert second is not first and first.closed and not second.closed
    real_pid = os.getpid()
    monkeypatch.setattr(pg_mod.os, "getpid", lambda: real_pid + 1)
    third = pg_mod._pool()
    assert third is not second and not second.closed


def test_postgres_killed_pooled_connection_is_retried_once_on_a_fresh_one(server_backend):
    """P7-AC1: a pooled connection killed server-side (restart, failover) is replaced and the call still succeeds."""
    state.cache_set_many("ns", {"k": {"v": 1}})
    assert state.cache_get_many("ns", ["k"]) == {"k": {"v": 1}}
    from src import state_postgres

    pids = [c.info.backend_pid for c in state_postgres._pool()._idle]
    assert pids, "a pooled connection was expected after the first calls"
    with server_support.pg_connect() as admin:
        for pid in pids:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert state.cache_get_many("ns", ["k"]) == {"k": {"v": 1}}


def test_postgres_dead_idle_connections_are_discarded_not_reused(server_backend):
    from src import state_postgres

    state.cache_set_many("ns", {"k": 1})
    pool = state_postgres._pool()
    dead = pool._idle[0]
    dead.close()  # closed locally: `closed` is True, so acquire() must skip and close it
    conn = pool.acquire()
    try:
        assert conn is not dead and not conn.closed
    finally:
        pool.release(conn, True)
    fresh = pool.acquire(fresh=True)  # drops the remaining idle connections and opens a new one
    try:
        assert not fresh.closed
    finally:
        pool.release(fresh, True)


def test_postgres_session_of_disabled_account_or_before_password_change_is_dead(server_backend):
    """P7-AC1 / threat 37: sessions die when the account is disabled or created before the last password change."""
    account_id = uuid.uuid4().hex
    state.create_account(account_id, "dora", "hash-0")
    now = time.time()
    assert state.create_session("tok-1", account_id, now, now + 3600, 5)
    live = state.get_session_principal("tok-1", now, 600)
    assert live and live["username"] == "dora"
    assert state.set_disabled(account_id, True)
    assert state.get_session_principal("tok-1", now, 600) is None
    assert state.set_disabled(account_id, False)
    state.set_password_hash(account_id, "hash-1", now + 10)
    assert state.get_session_principal("tok-1", now, 600) is None


# ------------------------------------------------------------------------------------------------ pgvector
def test_pgvector_noop_and_guard_paths_touch_no_database(monkeypatch):
    """Empty input and unusable queries return at once (and need neither a connection nor the embedding model)."""
    pytest.importorskip("psycopg")
    from src.pgvector_store import PgVectorStore

    monkeypatch.setattr(config, "DATABASE_URL", "", raising=False)  # any connection attempt would raise
    store = PgVectorStore()
    workspace = "0123456789abcdef" * 2
    store.add_chunks(workspace, [])
    assert store.retrieve(workspace, "anything", 0) == []
    assert store.retrieve(workspace, "nul\x00byte", 3) == []


def test_pgvector_valid_accepts_only_workspace_ids():
    pytest.importorskip("psycopg")
    from src import pgvector_store

    assert pgvector_store._valid("0123456789abcdef" * 2)
    assert not pgvector_store._valid("../etc/passwd")
    assert not pgvector_store._valid("")


def test_pgvector_sweep_yields_to_a_sweep_already_running(server_backend):
    """Two processes never sweep at once: while another session holds the sweep lock this one removes nothing."""
    from src import pgvector_store

    with server_support.pg_connect() as other:
        other.execute("SELECT pg_advisory_lock(%s::bigint)", (pgvector_store._SWEEP_LOCK_KEY,))
        try:
            assert pgvector_store.PgVectorStore._sweep([]) == 0
        finally:
            other.execute("SELECT pg_advisory_unlock(%s::bigint)", (pgvector_store._SWEEP_LOCK_KEY,))
    assert pgvector_store.PgVectorStore._sweep([]) == 0  # lock free again: runs (nothing to remove)
