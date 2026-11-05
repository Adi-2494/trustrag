"""
Durable state of server mode on Postgres: accounts, sessions, workspace ownership + tombstones and the 24 h caches
(ADR 0015, ADR 0014; amends ADR 0005 / 0012). `src/state.py` dispatches here when `config.STATE_BACKEND == "redis"`;
the function names, signatures and return shapes are the same as the SQLite versions.

Why Postgres and not Redis for this: every P6 guarantee (session creation re-checks the account and the password
hash, password change + session revocation, workspace cap) is one transaction over several rows, which needs a
database with row locks. Why the database clock: with several API hosts a host with a fast or slow clock must not
extend or expire sessions, so unless `config.STATE_CLOCK == "caller"` (tests that freeze time only) every timestamp
comes from `clock_timestamp()` and the callers' `now` arguments are ignored.

This module also owns the connection handling that `src/pgvector_store.py` shares (SPEC-GAP-085, -086):
- `transaction()` yields a pooled autocommit connection inside `conn.transaction()`. A stdlib bounded pool (at most
  `PG_MAX_CONNECTIONS_PER_PROCESS`, waits `PG_POOL_WAIT_SECONDS`) replaces a pool package. Every connection gets
  `search_path = <PG_SCHEMA>, public` (set with `psycopg.sql.Identifier`) and lock / statement / idle-in-transaction
  timeouts, so a stuck row lock ends in `StorageUnavailable` instead of a hung login.
- Any driver error rolls back, closes the connection and becomes `StorageUnavailable` with the driver error only as
  `__cause__` (its text can hold hosts and credentials and is never shown). A unique violation keeps the connection
  and raises `UniqueViolationError` (a `StorageUnavailable` subclass) so callers can turn it into `DuplicateAccount`.
- SQL text is never built from values: bound parameters only, identifiers only through `psycopg.sql.Identifier`.
`psycopg` is imported inside functions only, so local mode works without it being importable.
"""

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from typing import TYPE_CHECKING, Any

from src import config
from src.state import TOUCH_INTERVAL_SECONDS, DuplicateAccount, StorageUnavailable

if TYPE_CHECKING:
    import psycopg

    Conn = psycopg.Connection[Any]


class UniqueViolationError(StorageUnavailable):
    """A unique / primary key constraint rejected a write. The transaction was rolled back; the connection is fine."""


# ------------------------------------------------------------------------------------------------ pool
class _Pool:
    """Bounded set of connections for one (URL, schema, timeouts) combination in one process."""

    def __init__(self, key: tuple[Any, ...], size: int) -> None:
        self.key = key
        self._slots = threading.BoundedSemaphore(max(1, size))
        self._idle: list[Conn] = []
        self._lock = threading.Lock()
        self.closed = False

    def acquire(self, fresh: bool = False) -> "Conn":
        """A pooled connection, or (`fresh`) a new one after dropping the idle ones, which may share the dead state."""
        if fresh:
            with self._lock:
                idle, self._idle = self._idle, []
            for old in idle:
                _close(old)
        if not self._slots.acquire(timeout=float(config.PG_POOL_WAIT_SECONDS)):
            raise StorageUnavailable
        try:
            while True:
                with self._lock:
                    conn = self._idle.pop() if self._idle else None
                if conn is None:
                    return _connect(self.key)
                if not conn.closed and not conn.broken:
                    return conn
                _close(conn)
        except BaseException:
            self._slots.release()
            raise

    def release(self, conn: "Conn", healthy: bool) -> None:
        try:
            if healthy and not self.closed and not conn.closed:
                with self._lock:
                    self._idle.append(conn)
            else:
                _close(conn)
        finally:
            self._slots.release()

    def retire(self) -> None:
        with self._lock:
            self.closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            _close(conn)


def _close(conn: "Conn") -> None:
    with suppress(Exception):  # closing a dead connection may itself fail; nothing useful to do with that
        conn.close()


_pools_lock = threading.Lock()
_pool_state: dict[str, Any] = {"pool": None, "pid": None}
_init_lock = threading.Lock()
_initialised: set[tuple[Any, ...]] = set()


def _pool_key() -> tuple[Any, ...]:
    return (
        str(config.DATABASE_URL),
        str(config.PG_SCHEMA),
        int(config.PG_LOCK_TIMEOUT_MS),
        int(config.PG_STATEMENT_TIMEOUT_MS),
        int(config.PG_IDLE_IN_TRANSACTION_TIMEOUT_MS),
        int(config.PG_MAX_CONNECTIONS_PER_PROCESS),
    )


def _pool() -> _Pool:
    key, pid = _pool_key(), os.getpid()
    with _pools_lock:
        pool: _Pool | None = _pool_state["pool"]
        if pool is not None and (pool.key != key or _pool_state["pid"] != pid):
            if _pool_state["pid"] == pid:
                pool.retire()
            pool = None  # after a fork the parent's sockets are not ours to use or close
        if pool is None:
            pool = _Pool(key, key[5])
            _pool_state["pool"], _pool_state["pid"] = pool, pid
        return pool


def _connect(key: tuple[Any, ...]) -> "Conn":
    import psycopg
    from psycopg import sql

    url, schema, lock_ms, statement_ms, idle_ms, _ = key
    if not url:
        raise StorageUnavailable
    conn: Conn | None = None
    try:
        conn = psycopg.connect(url, autocommit=True, connect_timeout=int(config.PG_POOL_WAIT_SECONDS))
        conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
        conn.execute(
            "SELECT set_config('lock_timeout', %s, false), set_config('statement_timeout', %s, false),"
            " set_config('idle_in_transaction_session_timeout', %s, false)",
            (str(lock_ms), str(statement_ms), str(idle_ms)),
        )
        return conn
    except Exception as error:  # any driver or URL problem: the text is not shown, only chained
        if conn is not None:
            _close(conn)
        raise StorageUnavailable from error


@contextmanager
def transaction() -> Iterator["Conn"]:
    """
    A pooled autocommit connection inside one transaction (commit on success, rollback on any exception).

    A pooled connection can have been killed since it was last used (server restart, failover, idle timeout). That
    shows up as an OperationalError when the transaction starts (`BEGIN` is the first statement). Nothing of this
    transaction has run then, so it is retried once on a fresh connection. An error after any statement ran is never
    retried: the work may or may not have been applied.
    """
    import psycopg
    from psycopg import errors
    from psycopg.pq import TransactionStatus

    pool = _pool()
    conn: Conn = pool.acquire()
    held: Conn | None = conn  # the connection this call still owes a release for (exactly one release each)
    stack = ExitStack()
    try:
        try:
            stack.enter_context(conn.transaction())
        except psycopg.OperationalError:
            held = None
            pool.release(conn, False)
            conn = held = pool.acquire(fresh=True)
            stack.enter_context(conn.transaction())
    except psycopg.Error as error:
        if held is not None:
            pool.release(held, False)
        raise StorageUnavailable from error
    except BaseException:
        if held is not None:
            pool.release(held, False)
        raise
    healthy = True
    try:
        with stack:
            yield conn
    except errors.UniqueViolation as error:
        raise UniqueViolationError from error
    except psycopg.Error as error:
        healthy = False
        raise StorageUnavailable from error
    finally:
        pool.release(conn, healthy and conn.info.transaction_status == TransactionStatus.IDLE)


def reset_for_tests() -> None:
    """Drop pooled connections and the schema-initialised cache so a new schema / URL takes effect."""
    with _pools_lock:
        pool: _Pool | None = _pool_state["pool"]
        if pool is not None and _pool_state["pid"] == os.getpid():
            pool.retire()
        _pool_state["pool"], _pool_state["pid"] = None, None
    with _init_lock:
        _initialised.clear()


# ------------------------------------------------------------------------------------------------ schema
_STATE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS accounts (account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,"
    " password_hash TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,"
    " password_changed DOUBLE PRECISION NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
    " created DOUBLE PRECISION NOT NULL, last_seen DOUBLE PRECISION NOT NULL, expires DOUBLE PRECISION NOT NULL,"
    " seq BIGINT GENERATED ALWAYS AS IDENTITY)",
    "CREATE INDEX IF NOT EXISTS sessions_account ON sessions (account_id)",
    "CREATE TABLE IF NOT EXISTS workspaces (workspace TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
    " created DOUBLE PRECISION NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS workspaces_account ON workspaces (account_id)",
    "CREATE TABLE IF NOT EXISTS cache (namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
    " created DOUBLE PRECISION NOT NULL, PRIMARY KEY (namespace, key))",
    "CREATE INDEX IF NOT EXISTS cache_created ON cache (created)",
)


def _lock_key(text: str) -> int:
    """A signed 64-bit advisory-lock key derived from text (sha256), so the SQL never contains the text."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big", signed=True)


def ensure_schema(extra: Sequence[str] = ()) -> None:
    """
    Create the state tables (plus `extra` DDL statements of another module, e.g. the vector tables) if missing.
    Idempotent and safe when several processes start at once: all DDL runs in one transaction under an advisory lock.
    The schema itself must already exist (created by the database init script); it is never created here, so a
    wrong `PG_SCHEMA` can not silently put tables into `public`.
    """
    ident = (*_pool_key()[:2], tuple(extra))
    if ident in _initialised:
        return
    with _init_lock:
        if ident in _initialised:
            return
        schema = ident[1]
        with transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (_lock_key("schema:" + schema),))
            found = conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
            if found.fetchone() is None:
                raise StorageUnavailable
            for statement in (*_STATE_SCHEMA, *extra):
                conn.execute(statement)
        _initialised.add(ident)


@contextmanager
def _tx() -> Iterator["Conn"]:
    ensure_schema()
    with transaction() as conn:
        yield conn


def _stamp(conn: "Conn", caller_now: float | None = None) -> float:
    """The time to record: the database clock, or the caller's clock when tests freeze time (STATE_CLOCK)."""
    if config.STATE_CLOCK == "caller":
        return time.time() if caller_now is None else float(caller_now)
    row = conn.execute("SELECT extract(epoch FROM clock_timestamp())::float8").fetchone()
    return float(row[0]) if row else time.time()


# ------------------------------------------------------------------------------------------------ caches
def cache_get_many(namespace: str, keys: list[str]) -> dict[str, object]:
    """{key: value} for keys cached in this namespace and younger than SERVER_CACHE_TTL_HOURS."""
    if not keys:
        return {}
    with _tx() as conn:
        cutoff = _stamp(conn) - float(config.SERVER_CACHE_TTL_HOURS) * 3600
        rows = conn.execute(
            "SELECT key, value FROM cache WHERE namespace = %s AND key = ANY(%s) AND created > %s",
            (namespace, list(keys), cutoff),
        ).fetchall()
    return {str(key): json.loads(value) for key, value in rows}


def delete_expired_cache(conn: "Conn", now: float, limit: int = 1000) -> int:
    """
    Delete up to `limit` expired cache rows on `conn`. Rows are taken in key order with SKIP LOCKED, so this never
    waits for (or deadlocks with) a concurrent upsert of the same keys; what it skips is removed by the next call.
    """
    cutoff = now - float(config.SERVER_CACHE_TTL_HOURS) * 3600
    return conn.execute(
        "DELETE FROM cache WHERE (namespace, key) IN (SELECT namespace, key FROM cache WHERE created <= %s"
        " ORDER BY namespace, key LIMIT %s FOR UPDATE SKIP LOCKED)",
        (cutoff, limit),
    ).rowcount


def cache_set_many(namespace: str, items: dict[str, object]) -> None:
    """Upsert several values in one transaction (keys in sorted order: overlapping writers cannot deadlock)."""
    if not items:
        return
    # Expired rows are removed in their own short transaction after the upserts commit (see below), never inside this
    # one: deleting first, before the sorted upserts, could deadlock with a concurrent writer of an expired key.
    with _tx() as conn:
        now = _stamp(conn)
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO cache (namespace, key, value, created) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value, created = EXCLUDED.created",
                [(namespace, key, json.dumps(items[key]), now) for key in sorted(items)],
            )
    with suppress(StorageUnavailable), _tx() as conn:  # best effort: the throttled cleanup sweep also expires rows
        delete_expired_cache(conn, now)


# ------------------------------------------------------------------------------------------------ accounts
_ACCOUNT_KEYS = ("account_id", "username", "password_hash", "created", "disabled", "password_changed")


def create_account(account_id: str, username: str, password_hash: str, now: float | None = None) -> None:
    """Insert an account. `username` is already canonical; raises DuplicateAccount if it exists."""
    try:
        with _tx() as conn:
            stamp = _stamp(conn, now)
            conn.execute(
                "INSERT INTO accounts (account_id, username, password_hash, created, disabled, password_changed)"
                " VALUES (%s, %s, %s, %s, 0, %s)",
                (account_id, username, password_hash, stamp, stamp),
            )
    except UniqueViolationError as error:
        raise DuplicateAccount(username) from error


def get_account_by_username(username: str) -> dict[str, Any] | None:
    with _tx() as conn:
        row = conn.execute(
            "SELECT account_id, username, password_hash, created, disabled, password_changed"
            " FROM accounts WHERE username = %s",
            (username,),
        ).fetchone()
    return None if row is None else dict(zip(_ACCOUNT_KEYS, row, strict=True))


def list_accounts() -> list[dict[str, Any]]:
    with _tx() as conn:
        rows = conn.execute(
            "SELECT account_id, username, created, disabled FROM accounts ORDER BY created, username"
        ).fetchall()
    return [{"account_id": a, "username": u, "created": c, "disabled": bool(d)} for a, u, c, d in rows]


def set_password_hash(account_id: str, password_hash: str, now: float | None = None) -> bool:
    """Change the password and revoke every session of the account, in one transaction (row lock first)."""
    with _tx() as conn:
        stamp = _stamp(conn, now)
        changed = conn.execute(
            "UPDATE accounts SET password_hash = %s, password_changed = %s WHERE account_id = %s",
            (password_hash, stamp, account_id),
        ).rowcount
        conn.execute("DELETE FROM sessions WHERE account_id = %s", (account_id,))
    return bool(changed)


def rehash_password(account_id: str, old_hash: str, new_hash: str) -> bool:
    """Compare-and-set of a stronger hash of the same password. Postgres re-evaluates the WHERE after a lock wait."""
    with _tx() as conn:
        changed = conn.execute(
            "UPDATE accounts SET password_hash = %s WHERE account_id = %s AND password_hash = %s",
            (new_hash, account_id, old_hash),
        ).rowcount
    return bool(changed)


def set_disabled(account_id: str, disabled: bool) -> bool:
    with _tx() as conn:
        changed = conn.execute(
            "UPDATE accounts SET disabled = %s WHERE account_id = %s", (int(disabled), account_id)
        ).rowcount
        if disabled:
            conn.execute("DELETE FROM sessions WHERE account_id = %s", (account_id,))
    return bool(changed)


# ------------------------------------------------------------------------------------------------ sessions
def create_session(
    token_hash: str,
    account_id: str,
    created: float,
    expires: float,
    max_per_account: int,
    expected_hash: str | None = None,
) -> bool:
    """
    Store a session and revoke the oldest beyond the cap, all under `FOR UPDATE` on the account row. That lock
    serialises it against a password reset / disable and against other logins of the same account; `disabled` and
    the password hash are checked after the lock is held. In store-clock mode the caller's lifetime is kept but
    re-based on the database clock (`expires` is compared with the caller's own `time.time()`).
    """
    with _tx() as conn:
        row = conn.execute(
            "SELECT disabled, password_hash FROM accounts WHERE account_id = %s FOR UPDATE", (account_id,)
        ).fetchone()
        if row is None or row[0] or (expected_hash is not None and row[1] != expected_hash):
            return False
        if config.STATE_CLOCK == "caller":
            start, end = created, expires
        else:
            start = _stamp(conn)
            end = start + (expires - time.time())
        conn.execute(
            "INSERT INTO sessions (token_hash, account_id, created, last_seen, expires) VALUES (%s, %s, %s, %s, %s)",
            (token_hash, account_id, start, start, end),
        )
        conn.execute(
            "DELETE FROM sessions WHERE account_id = %s AND token_hash NOT IN"
            " (SELECT token_hash FROM sessions WHERE account_id = %s ORDER BY created DESC, seq DESC LIMIT %s)",
            (account_id, account_id, max(1, max_per_account)),
        )
    return True


def get_session_principal(token_hash: str, now: float, idle_seconds: float) -> dict[str, Any] | None:
    """{account_id, username, last_seen} for a live session, judged on the store clock; expired rows are deleted."""
    with _tx() as conn:
        row = conn.execute(
            "SELECT s.account_id, a.username, s.created, s.last_seen, s.expires, a.disabled, a.password_changed"
            " FROM sessions s JOIN accounts a ON a.account_id = s.account_id WHERE s.token_hash = %s",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        account_id, username, created, last_seen, expires, disabled, changed = row
        stamp = _stamp(conn, now)
        if stamp >= expires or stamp - last_seen > idle_seconds:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
            return None
    if disabled or created < changed:
        return None
    if config.STATE_CLOCK != "caller":
        last_seen = now - (stamp - last_seen)  # in the caller's frame, so its "stale enough to touch" test still works
    return {"account_id": account_id, "username": username, "last_seen": last_seen}


def touch_session(token_hash: str, now: float) -> None:
    """Record activity with one guarded UPDATE: two racing touches do not both write."""
    with _tx() as conn:
        stamp = _stamp(conn, now)
        conn.execute(
            "UPDATE sessions SET last_seen = %s WHERE token_hash = %s AND last_seen <= %s",
            (stamp, token_hash, stamp - TOUCH_INTERVAL_SECONDS),
        )


def delete_session(token_hash: str) -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


def prune_expired(now: float, idle_seconds: float) -> None:
    """Drop sessions past their absolute or idle limit. Login attempts expire by themselves in Redis."""
    with _tx() as conn:
        stamp = _stamp(conn, now)
        # A bounded batch that skips rows locked by someone else: every login calls this, and it must neither wait for
        # nor deadlock with `create_session` (which holds the account row and deletes that account's sessions).
        conn.execute(
            "DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions"
            " WHERE expires <= %s OR last_seen < %s ORDER BY token_hash LIMIT 200 FOR UPDATE SKIP LOCKED)",
            (stamp, stamp - idle_seconds),
        )


# ------------------------------------------------------------------------------------------------ workspaces
def add_owned_workspace(workspace: str, account_id: str, limit: int, now: float | None = None) -> bool:
    """Record ownership unless the account has `limit` live workspaces; an advisory lock per account makes it atomic."""
    with _tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (_lock_key("ws:" + account_id),))
        row = conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE account_id = %s AND deleted = 0", (account_id,)
        ).fetchone()
        if row is None or row[0] >= limit:
            return False
        conn.execute(
            "INSERT INTO workspaces (workspace, account_id, created, deleted) VALUES (%s, %s, %s, 0)",
            (workspace, account_id, _stamp(conn, now)),
        )
    return True


def workspace_record(workspace: str) -> tuple[str, int] | None:
    with _tx() as conn:
        row = conn.execute("SELECT account_id, deleted FROM workspaces WHERE workspace = %s", (workspace,)).fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


def list_owned_workspaces(account_id: str) -> list[str]:
    return [workspace for workspace, _ in list_owned_workspace_info(account_id)]


def list_owned_workspace_info(account_id: str) -> list[tuple[str, float]]:
    with _tx() as conn:
        rows = conn.execute(
            "SELECT workspace, created FROM workspaces WHERE account_id = %s AND deleted = 0"
            " ORDER BY created, workspace",
            (account_id,),
        ).fetchall()
    return [(str(w), float(c)) for w, c in rows]


def live_owned_ids() -> set[str]:
    with _tx() as conn:
        return {str(r[0]) for r in conn.execute("SELECT workspace FROM workspaces WHERE deleted = 0").fetchall()}


def tombstoned_ids() -> set[str]:
    with _tx() as conn:
        return {str(r[0]) for r in conn.execute("SELECT workspace FROM workspaces WHERE deleted = 1").fetchall()}


def tombstone_workspace(workspace: str, account_id: str) -> bool:
    """Mark the workspace deleted if `account_id` owns it. The row is never removed, so ids are never reused."""
    with _tx() as conn:
        changed = conn.execute(
            "UPDATE workspaces SET deleted = 1 WHERE workspace = %s AND account_id = %s AND deleted = 0",
            (workspace, account_id),
        ).rowcount
    return bool(changed)
