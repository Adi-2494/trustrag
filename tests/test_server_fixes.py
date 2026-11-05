"""
P7 fix round: tests for the review findings (server backends, eval wiring). Each test names the finding it pins.

Hooks these tests ASSUME (the implementation must provide them; none is a new public API):
- `src.pgvector_store._transaction()` stays the single place PgVectorStore gets a transaction (it is wrapped here to
  record SQL), and `pgvector_store.reset_for_tests()` (called by the `server_backend` fixture) also resets the
  cleanup throttle. The throttle interval is assumed to be much longer than one test (>= 1 minute).
- `state_postgres.transaction()` stays the single place state functions get a transaction.
- `[server]` tests skip without TRUSTRAG_TEST_DATABASE_URL / TRUSTRAG_TEST_REDIS_URL.
"""

import contextlib
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src import config, state
from tests import server_support
from tests.fakes import FakeStore
from tests.p6_support import PASSWORD, add_account, comparable, install_api, login_body, make_service

ROOT = Path(__file__).resolve().parent.parent
STATE_TABLES = ("accounts", "sessions", "workspaces", "cache", "chunks", "vector_workspaces")
CONSTANT_VECTOR = "[" + ",".join(["0.1"] * 384) + "]"


@pytest.fixture
def const_embed(monkeypatch):
    """No embedding model needed: every text maps to the same vector."""
    from src import pgvector_store

    monkeypatch.setattr(pgvector_store, "_embed", lambda texts: [CONSTANT_VECTOR] * len(texts))


def _chunk(cid="c1", text="Annual leave is 25 days."):
    return {"id": cid, "text": text, "file_name": "a.pdf", "page": 1}


class _Recorder:
    """Wraps a `_transaction` factory; records every SQL string executed through `.execute` and `.cursor().executemany`."""

    def __init__(self, real, sink):
        self.real, self.sink = real, sink

    def __call__(self):
        manager, sink = self.real(), self.sink

        class Tx:
            def __enter__(self):
                self.conn = manager.__enter__()
                return self

            def __exit__(self, *exc):
                return manager.__exit__(*exc)

            def execute(self, sql, *args, **kwargs):
                sink.append(("execute", str(sql), args))
                return self.conn.execute(sql, *args, **kwargs)

            @contextlib.contextmanager
            def cursor(self, *a, **k):
                with self.conn.cursor(*a, **k) as cur:

                    class Cur:
                        def executemany(self_inner, sql, rows, *aa, **kk):
                            rows = list(rows)
                            sink.append(("executemany", str(sql), rows))
                            return cur.executemany(sql, rows, *aa, **kk)

                        def __getattr__(self_inner, name):
                            return getattr(cur, name)

                    yield Cur()

            def __getattr__(self, name):
                return getattr(self.conn, name)

        return Tx()


# ---------------------------------------------------------------------------- missing schema (review: public pollution)
def _public_tables():
    return {
        r[0]
        for r in server_support.pg_rows(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'", schema="public"
        )
    }


def test_missing_schema_raises_storage_unavailable_and_creates_nothing_in_public(
    server_backend, monkeypatch, const_embed
):
    """P7 review (schema) / threat 57, 63: PG_SCHEMA naming a missing schema fails closed for state AND vectors."""
    from src.pgvector_store import PgVectorStore
    from src.state import StorageUnavailable

    before = _public_tables()
    monkeypatch.setattr(config, "PG_SCHEMA", "no_such_schema_" + uuid.uuid4().hex[:8])
    server_support.reset_connections()
    store = PgVectorStore()
    try:
        with pytest.raises(StorageUnavailable):
            state.list_accounts()
        with pytest.raises(StorageUnavailable):
            store.add_chunks(store.new_workspace(), [_chunk()])
        with pytest.raises(StorageUnavailable):
            store.count(store.new_workspace())
        created = _public_tables() - before
        assert created == set(), f"tables were created in public: {sorted(created)}"
    finally:  # leave the shared throwaway database as it was, even when the implementation polluted it
        for table in sorted(_public_tables() - before):
            if table in STATE_TABLES:
                server_support.pg_rows(f'DROP TABLE IF EXISTS public."{table}" CASCADE', schema="public")


# ---------------------------------------------------------------------------- dead pooled connection
def test_dead_pooled_connection_is_retried_once_on_a_fresh_one(server_backend):
    """P7 review (connections) / threat 57: after the pooled backend is killed the next state call still succeeds."""
    from src import state_postgres

    state.create_account(uuid.uuid4().hex, "survivor", "hash")  # warms the pool and the schema
    with state_postgres.transaction() as conn:
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]  # the pooled connection goes back to the pool
    killed = server_support.pg_rows("SELECT pg_terminate_backend(%s)", (pid,))
    assert killed == [(True,)]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and server_support.pg_rows(
        "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
    ):
        threading.Event().wait(0.02)
    assert [a["username"] for a in state.list_accounts()] == ["survivor"]  # one retry on a fresh connection


# ---------------------------------------------------------------------------- cleanup throttling and cache expiry
def test_cleanup_sweeps_run_at_most_once_per_interval(server_backend, monkeypatch, const_embed):
    """P7 review (cleanup load) / threat 56: many cleanup() calls run the orphan sweep and age delete once per interval."""
    from src import pgvector_store
    from src.pgvector_store import PgVectorStore

    store = PgVectorStore()
    store.add_chunks(store.new_workspace(), [_chunk()])  # tables exist before the spy is installed
    seen: list = []
    monkeypatch.setattr(pgvector_store, "_transaction", _Recorder(pgvector_store._transaction, seen))
    for _ in range(10):
        store.cleanup()
    with ThreadPoolExecutor(8) as pool:
        for future in [pool.submit(store.cleanup) for _ in range(16)]:
            future.result(timeout=60)
    sql = [s for kind, s, _ in seen if kind == "execute"]
    sweeps = [s for s in sql if "DELETE FROM chunks" in s and "NOT EXISTS" in s]
    ageing = [s for s in sql if "DELETE FROM vector_workspaces" in s]
    assert len(sweeps) <= 1, f"orphan sweep ran {len(sweeps)} times for 26 cleanup() calls"
    assert len(ageing) <= 1, f"age cleanup ran {len(ageing)} times for 26 cleanup() calls"


def test_cleanup_also_deletes_expired_cache_rows(server_backend, const_embed):
    """P7 review (caches) / threat 55, 56: cleanup() removes expired cache rows, not only expired reads / writes."""
    from src.pgvector_store import PgVectorStore

    state.cache_set_many("ocr", {"old": "PAGE TEXT"})
    server_support.pg_rows("UPDATE cache SET created = created - %s", ((config.SERVER_CACHE_TTL_HOURS + 1) * 3600,))
    state.cache_set_many("scan", {"fresh": {"verdict": "SAFE"}})  # this write purges expired rows too: re-age "old"
    server_support.pg_rows(
        "INSERT INTO cache (namespace, key, value, created) VALUES ('ocr', 'stale', '1', extract(epoch from now()) - %s)",
        ((config.SERVER_CACHE_TTL_HOURS + 2) * 3600,),
    )
    assert ("stale",) in server_support.pg_rows("SELECT key FROM cache")
    PgVectorStore().cleanup()
    assert server_support.pg_rows("SELECT key FROM cache ORDER BY key") == [("fresh",)]


# ---------------------------------------------------------------------------- cache upsert ordering
def test_cache_set_many_writes_keys_in_sorted_order(server_backend):
    """P7 review (deadlock) / threat 57: upserts go in sorted key order, so overlapping writers cannot deadlock."""
    from src import state_postgres

    state.cache_set_many("ns", {"b": 1})  # schema exists
    seen: list = []
    real = state_postgres.transaction
    state_postgres.transaction = _Recorder(real, seen)
    try:
        state.cache_set_many("ns", {"k9": 1, "k1": 2, "k5": 3, "k3": 4})
    finally:
        state_postgres.transaction = real
    batches = [rows for kind, s, rows in seen if kind == "executemany" and "INSERT INTO cache" in s]
    assert batches, "no cache upsert was recorded"
    keys = [row[1] for row in batches[0]]
    assert keys == sorted(keys), f"upserts are not in sorted key order: {keys}"


def test_overlapping_cache_writes_in_opposite_order_do_not_deadlock(server_backend):
    """P7 review (deadlock) / threat 57: two writers with the same keys in opposite order both succeed."""
    keys = [f"k{i:02d}" for i in range(40)]
    state.cache_set_many("ns", {k: 0 for k in keys})
    barrier = threading.Barrier(2)

    def writer(order):
        barrier.wait()
        for round_ in range(5):
            state.cache_set_many("ns", {k: round_ for k in order})

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(writer, keys), pool.submit(writer, list(reversed(keys)))]
        for future in futures:
            future.result(timeout=60)  # a deadlock surfaces as StorageUnavailable from the loser


# ---------------------------------------------------------------------------- store clock (STATE_CLOCK="store")
def _session(account_id, token_hash, lifetime=3600.0):
    now = time.time()
    assert state.create_session(token_hash, account_id, now, now + lifetime, 10)


def test_store_clock_idle_expiry_follows_the_database_clock(server_backend):
    """P7 review (clock) / threat 54: a session idle past the limit on the DATABASE clock dies, whatever the caller says."""
    assert config.STATE_CLOCK == "store"
    account_id = uuid.uuid4().hex
    state.create_account(account_id, "idler", "hash")
    live, idle = "a" * 64, "b" * 64
    _session(account_id, live)
    _session(account_id, idle)
    assert state.get_session_principal(idle, time.time(), 1800) is not None
    server_support.pg_rows("UPDATE sessions SET last_seen = last_seen - 3600 WHERE token_hash = %s", (idle,))
    assert state.get_session_principal(idle, time.time(), 1800) is None
    assert server_support.pg_rows("SELECT 1 FROM sessions WHERE token_hash = %s", (idle,)) == []  # and is deleted
    assert state.get_session_principal(live, time.time() + 10 * 3600, 1800) is not None  # fast caller: still live


def test_store_clock_touch_writes_only_when_stale_and_uses_database_time(server_backend, fast_kdf):
    """P7 review (touch) / threat 54: fresh sessions cost no write per request; a stale one is touched with store time."""
    from src.accounts import SessionAuthenticator

    service = make_service()
    add_account("alice")
    token = str(service.login_raw(login_body("alice", PASSWORD), "c1")["token"])
    authenticator = SessionAuthenticator()

    def version():
        return server_support.pg_rows("SELECT xmin::text, last_seen FROM sessions")[0]

    first = version()
    for _ in range(5):
        assert authenticator.resolve(token) is not None
    assert version() == first, "a fresh session was written to on resolve"
    server_support.pg_rows("UPDATE sessions SET last_seen = last_seen - 600")
    stale = version()
    assert authenticator.resolve(token) is not None
    after = version()
    assert after[0] != stale[0] and after[1] > stale[1], "a stale session was not touched"
    database_now = server_support.pg_rows("SELECT extract(epoch from clock_timestamp())::float8")[0][0]
    assert abs(after[1] - database_now) < 5


# ---------------------------------------------------------------------------- eval wiring (review: pgvector run)
def _fresh_run_eval(monkeypatch, vector_store):
    """Import eval.run_eval anew with config.VECTOR_STORE set (as if VECTOR_STORE=<x> were in the environment)."""
    import importlib
    import sys

    import eval as eval_pkg
    from eval import run_eval as old

    monkeypatch.setattr(config, "VECTOR_STORE", vector_store, raising=False)
    monkeypatch.setenv("CHROMA_DIR", config.CHROMA_DIR)  # the module rewrites it; monkeypatch restores it
    monkeypatch.setitem(sys.modules, "eval.run_eval", old)
    monkeypatch.delitem(sys.modules, "eval.run_eval")
    monkeypatch.setattr(eval_pkg, "run_eval", old, raising=False)  # restored at teardown
    return importlib.import_module("eval.run_eval")


def test_default_eval_run_uses_chroma_even_when_environment_says_pgvector(tmp_path, monkeypatch):
    """P7 review (eval) / P7-AC3: VECTOR_STORE=pgvector at import must not turn a default run into a pgvector run."""
    from tests.test_public_benchmark import _patch_run_eval

    _fresh_run_eval(monkeypatch, "pgvector")
    run_eval, _q, _paid, results_dir = _patch_run_eval(monkeypatch, tmp_path, ["--suite", "free"])
    seen = []
    monkeypatch.setattr(run_eval, "build_index", lambda *a, **k: seen.append(type(run_eval.STORE).__name__) or "ws")
    monkeypatch.setattr(run_eval, "eval_retrieval", lambda *a, **k: seen.append(type(run_eval.STORE).__name__) or {})
    with contextlib.suppress(SystemExit):
        run_eval.main()
    assert set(seen) <= {"ChromaStore"} and seen, f"the default run used {seen}"
    written = sorted(p.name for p in results_dir.iterdir())
    assert "results.json" in written and "RESULTS.md" in written, written
    assert not [n for n in written if "pgvector" in n.lower()], written


def test_pgvector_eval_run_uses_a_coherent_backend_pair(tmp_path, monkeypatch):
    """P7 review (eval): `--vector-store pgvector` runs with STATE_BACKEND matched to redis, or is refused."""
    from tests.test_public_benchmark import _patch_run_eval

    run_eval, _q, _paid, results_dir = _patch_run_eval(
        monkeypatch, tmp_path, ["--suite", "free", "--vector-store", "pgvector"]
    )
    monkeypatch.setattr(config, "VECTOR_STORE", "chroma", raising=False)
    monkeypatch.setattr(config, "STATE_BACKEND", "sqlite", raising=False)
    monkeypatch.setattr(run_eval.storage, "make_vector_store", FakeStore)
    pairs = []
    monkeypatch.setattr(
        run_eval, "build_index", lambda *a, **k: pairs.append((config.VECTOR_STORE, config.STATE_BACKEND)) or "ws"
    )
    refused = False
    try:
        run_eval.main()
    except SystemExit as exc:
        refused = exc.code not in (None, 0)
    if refused:
        assert pairs == [], "a refused run must not build any index"
        assert not (results_dir / "results-pgvector.json").exists()
    else:
        assert pairs and set(pairs) == {("pgvector", "redis")}, f"incoherent backend pair during the run: {pairs}"


def test_pgvector_eval_run_always_clears_created_workspaces(tmp_path, monkeypatch):
    """P7 review (eval): workspaces built by a pgvector run are cleared even when the run fails half way."""
    from tests.test_public_benchmark import _patch_run_eval

    run_eval, _q, _paid, _dir = _patch_run_eval(
        monkeypatch, tmp_path, ["--suite", "free", "--vector-store", "pgvector"]
    )
    store = FakeStore()
    cleared = []
    monkeypatch.setattr(store, "clear", cleared.append)
    monkeypatch.setattr(run_eval.storage, "make_vector_store", lambda: store)
    monkeypatch.setattr(config, "STATE_BACKEND", "redis", raising=False)
    monkeypatch.setattr(config, "VECTOR_STORE", "chroma", raising=False)  # main() assigns it; restored afterwards
    created = []

    def build(*a, **k):
        ws = store.new_workspace()
        run_eval.CREATED_WORKSPACES.append(ws)
        created.append(ws)
        return ws

    def boom(*a, **k):
        raise RuntimeError("retrieval failed half way")

    monkeypatch.setattr(run_eval, "build_index", build)
    monkeypatch.setattr(run_eval, "eval_retrieval", lambda *a, **k: (build(), boom())[1])
    with contextlib.suppress(BaseException):
        run_eval.main()
    assert created and sorted(cleared) == sorted(created)
    assert run_eval.CREATED_WORKSPACES == []


def test_results_markdown_names_the_vector_store_for_pgvector_runs():
    """P7 review (eval): RESULTS-pgvector.md says which vector store produced it; the baseline report is unchanged."""
    from eval.run_eval import write_markdown

    base = json.loads((ROOT / "tests" / "data" / "results_full_v1.json").read_text(encoding="utf-8"))
    plain = write_markdown(copy.deepcopy(base))
    marked = write_markdown(copy.deepcopy(base) | {"vector_store": "pgvector"})
    assert "pgvector" in marked.lower()
    assert "pgvector" not in plain.lower()


# ================================================================ P7 fix round 2
# Assumptions for this block (the backend fix must provide them):
# - `state_postgres._pool()._slots` is the `threading.BoundedSemaphore` of free connection slots; its internal counter
#   `_value` is read to prove no slot is released twice (idle: exactly config.PG_MAX_CONNECTIONS_PER_PROCESS free).
# - `state_postgres._connect` is the single place a NEW connection is made (monkeypatched to fail).
# - the throttled sweep step is `PgVectorStore._sweep` (static); a failing sweep is logged with a fixed event whose
#   name contains "cleanup" or "sweep", never raised, and counts as a claimed interval (no retry within it).
def _kill_pooled_backend():
    """Terminate the Postgres backend behind the idle pooled connection (as a restart / failover would)."""
    from src import state_postgres

    with state_postgres.transaction() as conn:
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    assert server_support.pg_rows("SELECT pg_terminate_backend(%s)", (pid,)) == [(True,)]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and server_support.pg_rows(
        "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
    ):
        threading.Event().wait(0.02)


def _fail_new_connections(monkeypatch):
    from src import state_postgres
    from src.state import StorageUnavailable

    def refuse(key):
        raise StorageUnavailable

    monkeypatch.setattr(state_postgres, "_connect", refuse)


def _free_slots():
    from src import state_postgres

    return state_postgres._pool()._slots._value


def test_failed_reconnect_after_dead_connection_releases_the_slot_once(server_backend, monkeypatch):
    """P7-N1 / threat 57: dead pooled conn + failing reconnect -> StorageUnavailable (not ValueError), slot count intact."""
    from src import state_postgres
    from src.state import StorageUnavailable

    state.create_account(uuid.uuid4().hex, "survivor", "hash")
    _kill_pooled_backend()
    with monkeypatch.context() as patch:
        _fail_new_connections(patch)
        with pytest.raises(StorageUnavailable):
            state.list_accounts()
    assert _free_slots() == config.PG_MAX_CONNECTIONS_PER_PROCESS
    with state_postgres.transaction() as conn:  # the pool still works
        assert conn.execute("SELECT 1").fetchone() == (1,)
    assert [a["username"] for a in state.list_accounts()] == ["survivor"]
    assert _free_slots() == config.PG_MAX_CONNECTIONS_PER_PROCESS


def test_failed_reconnect_gives_fixed_503_and_uniform_login_429(server_backend, monkeypatch, accounts_on, fast_kdf):
    """P7-N1 / threat 45, 57, 60: same situation through the API: fixed 503 on data routes, uniform 429 on login."""
    from fastapi.testclient import TestClient

    from tests.test_server_state import FIXED_503, RATE_LIMITED

    add_account("alice")
    client = TestClient(install_api(monkeypatch, make_service()).app, raise_server_exceptions=False)
    workspace = "0123456789abcdef" * 2
    _kill_pooled_backend()
    with monkeypatch.context() as patch:
        _fail_new_connections(patch)
        response = client.get(f"/v1/workspaces/{workspace}/documents")
    assert (response.status_code, response.json()) == (503, FIXED_503), response.text
    assert _free_slots() == config.PG_MAX_CONNECTIONS_PER_PROCESS
    state.list_accounts()  # a fresh pooled connection again
    seen = set()
    for user, password in (("alice", PASSWORD), ("alice", "a wrong password 12"), ("nobody", PASSWORD)):
        _kill_pooled_backend()
        with monkeypatch.context() as patch:
            _fail_new_connections(patch)
            response = client.post("/v1/auth/login", json={"username": user, "password": password})
        assert response.status_code == 429 and response.json() == RATE_LIMITED, response.text
        seen.add(repr(comparable(response)))
        state.list_accounts()
    assert len(seen) == 1
    assert _free_slots() == config.PG_MAX_CONNECTIONS_PER_PROCESS


# ---------------------------------------------------------------------------- expired cache rows vs concurrent writers
def test_expired_row_purge_and_opposite_order_writers_do_not_deadlock(server_backend):
    """P7-N2 / threat 57: an aged key among the written ones must not make two opposite-order writers deadlock."""
    keys = [f"k{i}" for i in range(1, 10)]
    state.cache_set_many("ns", {k: 0 for k in keys})
    for round_ in range(50):
        server_support.pg_rows(
            "UPDATE cache SET created = created - %s WHERE namespace = 'ns' AND key = 'k5'",
            ((config.SERVER_CACHE_TTL_HOURS + 1) * 3600,),
        )
        barrier = threading.Barrier(2)

        def writer(order, round_=round_, barrier=barrier):
            barrier.wait()
            state.cache_set_many("ns", {k: round_ for k in order})

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(writer, keys), pool.submit(writer, keys[::-1])]
            for future in futures:
                future.result(timeout=60)  # a deadlock surfaces as StorageUnavailable from the loser


# ---------------------------------------------------------------------------- purge cost does not grow with tombstones
def _server_service(const_embed):
    from src.pgvector_store import PgVectorStore

    return make_service(store=PgVectorStore())


def test_new_workspace_cost_does_not_grow_with_purged_tombstones(server_backend, const_embed):
    """P7-N3 / threat 56: with 2000 purged tombstones, a new_workspace() outside a sweep runs no full scan or big ANY()."""
    import re

    from src import state_postgres

    state.create_account("acct-owner", "owner", "hash")
    server_support.pg_rows(
        "INSERT INTO workspaces (workspace, account_id, created, deleted) "
        "SELECT 'acct-' || md5(i::text), 'acct-owner', extract(epoch from now()), 1 FROM generate_series(1, 2000) i"
    )
    service = _server_service(const_embed)
    service.new_workspace()  # claims this interval's sweep and purges the tombstones
    seen: list = []
    real = state_postgres.transaction
    state_postgres.transaction = _Recorder(real, seen)
    try:
        assert service.new_workspace()  # within the interval: no sweep claimed
    finally:
        state_postgres.transaction = real
    for kind, sql, args in seen:
        flat = " ".join(sql.split())
        unrestricted = re.search(r"\bFROM workspaces\b", flat) and not re.search(
            r"\bworkspace\s*=|\baccount_id\s*=|\bLIMIT\b", flat
        )
        assert not unrestricted, f"full workspaces scan outside a sweep: {flat}"
        for value in args if kind == "execute" else ():
            for item in value if isinstance(value, tuple | list) else (value,):
                if isinstance(item, list | tuple | set | frozenset):
                    assert len(item) <= 50, f"ANY(array) of {len(item)} ids outside a sweep: {flat}"


def test_deleted_workspace_vectors_are_gone_even_after_later_cleanups(
    server_backend, const_embed, accounts_on, fast_kdf
):
    """P7-N3 / threat 56: deleting an owned workspace removes its vectors (store.clear), whatever cleanup later does."""
    service = _server_service(const_embed)
    add_account("alice")
    from tests.p6_support import sign_in

    token = sign_in(service, "alice")
    workspace = service.new_workspace(session=token)
    service.store.add_chunks(workspace, [_chunk()])
    assert service.store.count(workspace) == 1
    service.clear(workspace, session=token)
    assert service.store.count(workspace) == 0
    service.new_workspace(session=token)
    assert service.store.count(workspace) == 0
    assert server_support.pg_rows("SELECT count(*) FROM chunks WHERE workspace = %s", (workspace,)) == [(0,)]


# ---------------------------------------------------------------------------- best-effort sweep
def test_failing_sweep_does_not_fail_new_workspace_and_is_not_retried_in_the_interval(
    server_backend, monkeypatch, caplog, const_embed
):
    """P7-N4 / threat 56: a failing throttled sweep is logged (fixed event), new_workspace() still works, no retry."""
    import logging

    from src.pgvector_store import PgVectorStore
    from src.state import StorageUnavailable

    calls: list = []

    def boom(keep_ids):
        calls.append(keep_ids)
        raise StorageUnavailable

    monkeypatch.setattr(PgVectorStore, "_sweep", staticmethod(boom))
    service = _server_service(const_embed)
    with caplog.at_level(logging.DEBUG):
        first = service.new_workspace()
    assert first and len(first) == 32
    assert len(calls) == 1
    assert any(("cleanup" in r.getMessage().lower() or "sweep" in r.getMessage().lower()) for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    second = service.new_workspace()
    assert second and second != first
    assert len(calls) == 1, "the failed sweep was retried within the interval"


# ---------------------------------------------------------------------------- static checks (no server needed)
def test_threat_model_attack_table_has_no_blank_line_between_rows():
    """P7-N4 (docs) / threat model: a blank line inside the attacks table splits it and drops the later rows."""
    import re

    lines = (ROOT / "THREAT_MODEL.md").read_text(encoding="utf-8").splitlines()
    numbered = [i for i, line in enumerate(lines) if re.match(r"^\| \d+ \|", line)]
    assert numbered, "attack rows not found"
    blanks = [i + 1 for i in range(numbered[0], numbered[-1]) if not lines[i].startswith("|")]
    assert blanks == [], f"non-table lines inside the attacks table at lines {blanks}"


def test_postgres_init_script_silences_statement_logging_before_the_password_is_used():
    """P7-N5 / threat 61: the init session sets log_statement = 'none' before CREATE ROLE ... PASSWORD."""
    import re

    script = (ROOT / "docker" / "postgres-init.sh").read_text(encoding="utf-8")
    setting = re.search(r"SET\s+(?:SESSION\s+)?log_statement\s*(?:=|TO)\s*'?none'?", script, re.IGNORECASE)
    assert setting, "docker/postgres-init.sh does not set log_statement to 'none'"
    assert setting.start() < script.index("CREATE ROLE trustrag_app"), (
        "log_statement is set after the password statement"
    )
    assert setting.start() < script.index(":'apppw'")
    duration = re.search(r"SET\s+(?:SESSION\s+)?log_min_duration_statement\s*(?:=|TO)\s*-1\b", script, re.IGNORECASE)
    assert duration, "docker/postgres-init.sh does not set log_min_duration_statement = -1"
    assert duration.start() < script.index("CREATE ROLE trustrag_app"), (
        "log_min_duration_statement is set after the password statement"
    )
    assert duration.start() < script.index(":'apppw'")
