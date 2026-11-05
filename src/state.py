"""
Small persistent state shared by every thread and process of the app: result caches and the app-wide
rate limit. SQLite (WAL mode) instead of JSON files / process memory:
- writes are atomic transactions, so a crash or two concurrent writers can't leave a half-written cache;
- the rate limit is enforced across processes (Streamlit UI + API server), not per process;
- no extra server to run (see docs/adr/0005-sqlite-for-shared-state.md).

P6 adds local accounts, server-side sessions, workspace ownership and the login rate limit to the same file
(ADR 0011 / 0012), additively: the v1.0 tables and functions are unchanged. Because the file now holds password
and session hashes, the state directory is 0700 and the DB and its -wal/-shm files 0600 (SPEC-GAP-044). Tokens are
stored only as sha256 hex digests (hashed by the caller); plain passwords and tokens never reach this module.
Every multi-step rule (session cap, workspace cap, login reservation) runs in ONE `BEGIN IMMEDIATE` transaction, so
it holds across threads and across processes.

P7 server mode (`config.STATE_BACKEND == "redis"`, ADR 0014 / 0015): every public function below keeps its name,
signature and return shape and dispatches to `state_redis` (call window, login attempts) or `state_postgres`
(accounts, sessions, ownership, caches). No SQLite file is opened then. A store failure raises `StorageUnavailable`.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from typing import Any

from src import config

_local = threading.local()  # sqlite3 connections must not be shared between threads
_init_lock = threading.Lock()
_initialised: set[str] = set()


def _retry_locked(statement: str, conn: sqlite3.Connection) -> None:
    # Switching to WAL / creating tables can fail with "database is locked" without waiting for the busy
    # timeout when another process initialises the same file at the same moment; retry briefly.
    for attempt in range(100):
        try:
            conn.execute(statement)
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or attempt == 99:
                raise
            time.sleep(0.05)


def _chmod(path: str, mode: int) -> None:
    # POSIX permission bits only; Windows ignores them, and a failure (file owned by someone else, read-only
    # mount) must not stop the app from starting.
    if os.name == "nt":
        return
    with suppress(OSError):
        os.chmod(path, mode)


def _tighten_permissions(path: str) -> None:
    """State directory 0700; DB and its -wal/-shm files 0600 (also tightens files made by v1.0 with 0644)."""
    _chmod(config.CHROMA_DIR, 0o700)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            _chmod(path + suffix, 0o600)


_P6_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS accounts (account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,"
    " password_hash TEXT NOT NULL, created REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,"
    " password_changed REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
    " created REAL NOT NULL, last_seen REAL NOT NULL, expires REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS sessions_account ON sessions (account_id)",
    "CREATE TABLE IF NOT EXISTS workspaces (workspace TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
    " created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS workspaces_account ON workspaces (account_id)",
    "CREATE TABLE IF NOT EXISTS login_attempts (key TEXT NOT NULL, ts REAL NOT NULL, attempt TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS login_attempts_key_ts ON login_attempts (key, ts)",
)

TOUCH_INTERVAL_SECONDS = 60  # last_seen is written at most this often per session, to avoid a write per request


def _server() -> bool:
    """True in server mode (`STATE_BACKEND=redis`): counters in Redis, durable state and caches in Postgres."""
    return config.STATE_BACKEND == "redis"


def _pg() -> Any:
    from src import state_postgres  # lazy: local mode must not need psycopg

    return state_postgres


def _rd() -> Any:
    from src import state_redis  # lazy: local mode must not need redis

    return state_redis


def _conn() -> sqlite3.Connection:
    path = config.STATE_DB
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path:
        os.makedirs(config.CHROMA_DIR, mode=0o700, exist_ok=True)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.name != "nt" and not os.path.exists(path):
            # Create the file 0600 from the start, so there is no window with the default umask.
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # explicit transactions below
        conn.execute("PRAGMA busy_timeout = 30000")
        with _init_lock:  # threads of this process initialise the schema one at a time, once per file
            if path not in _initialised:
                for statement in (
                    "PRAGMA journal_mode=WAL",
                    "CREATE TABLE IF NOT EXISTS cache (namespace TEXT, key TEXT, value TEXT,"
                    " created REAL, PRIMARY KEY (namespace, key))",
                    "CREATE TABLE IF NOT EXISTS calls (ts REAL)",
                    "CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)",
                    *_P6_SCHEMA,
                ):
                    _retry_locked(statement, conn)
                _initialised.add(path)
        _tighten_permissions(path)  # on every open: -wal/-shm appear once the connection is in WAL mode
        _local.conn, _local.path = conn, path
    return conn


def cache_get_many(namespace: str, keys: list[str]) -> dict[str, object]:
    """Return {key: value} for the keys that are cached in this namespace."""
    if _server():
        return _pg().cache_get_many(namespace, keys)  # type: ignore[no-any-return]
    if not keys:
        return {}
    found: dict[str, object] = {}
    conn = _conn()
    for start in range(0, len(keys), 500):  # stay under SQLite's parameter limit
        part = keys[start : start + 500]
        placeholders = ",".join("?" * len(part))  # only "?" markers are built into the SQL; values are bound
        query = f"SELECT key, value FROM cache WHERE namespace = ? AND key IN ({placeholders})"  # noqa: S608
        rows = conn.execute(query, [namespace, *part]).fetchall()
        found.update({key: json.loads(value) for key, value in rows})
    return found


def cache_set_many(namespace: str, items: dict[str, object]) -> None:
    """Store several values in one atomic transaction."""
    if _server():
        _pg().cache_set_many(namespace, items)
        return
    if not items:
        return
    conn = _conn()
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO cache (namespace, key, value, created) VALUES (?, ?, ?, ?)",
            [(namespace, key, json.dumps(value), now) for key, value in items.items()],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def try_acquire_call(limit: int, window_seconds: int = 3600) -> float:
    """
    Record one model call if fewer than `limit` happened in the last window (across all processes).
    Returns 0 if allowed, otherwise the seconds until a slot frees up.
    """
    if _server():
        return float(_rd().try_acquire_call(limit, window_seconds))
    conn = _conn()
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")  # takes the write lock, so check-then-insert is atomic across processes
    try:
        conn.execute("DELETE FROM calls WHERE ts < ?", (now - window_seconds,))
        count, oldest = conn.execute("SELECT COUNT(*), MIN(ts) FROM calls").fetchone()
        if count >= limit:
            conn.execute("COMMIT")
            return max(1.0, window_seconds - (now - float(oldest)))
        conn.execute("INSERT INTO calls (ts) VALUES (?)", (now,))
        conn.execute("COMMIT")
        return 0.0
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def _write() -> Iterator[sqlite3.Connection]:
    """One `BEGIN IMMEDIATE` transaction: takes the write lock, so check-then-write is atomic across processes."""
    conn = _conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class DuplicateAccount(ValueError):
    """The username is already taken."""


class StorageUnavailable(Exception):
    """
    A server-mode store (Postgres or Redis) failed or timed out. Deliberately NOT a ValueError (the API's 404
    handler) and NOT an LLMError (the injection scanner would degrade to rules-only). The driver's own message can
    contain hosts and credentials, so it is only ever reachable through `__cause__`; this class carries a fixed text.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*(args or ("Storage is temporarily unavailable. Please try again.",)))


# ------------------------------------------------------------------------------------------------ accounts
_ACCOUNT_COLUMNS = "account_id, username, password_hash, created, disabled, password_changed"


def create_account(account_id: str, username: str, password_hash: str, now: float | None = None) -> None:
    """Insert an account. `username` is already canonical; raises DuplicateAccount if it exists."""
    if _server():
        _pg().create_account(account_id, username, password_hash, now)
        return
    stamp = time.time() if now is None else now
    try:
        with _write() as conn:
            conn.execute(
                f"INSERT INTO accounts ({_ACCOUNT_COLUMNS}) VALUES (?, ?, ?, ?, 0, ?)",  # noqa: S608
                (account_id, username, password_hash, stamp, stamp),
            )
    except sqlite3.IntegrityError as error:
        raise DuplicateAccount(username) from error


def get_account_by_username(username: str) -> dict[str, Any] | None:
    """The full account row (including the password hash) or None."""
    if _server():
        return _pg().get_account_by_username(username)  # type: ignore[no-any-return]
    row = (
        _conn()
        .execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE username = ?", (username,))  # noqa: S608
        .fetchone()
    )
    if row is None:
        return None
    keys = [name.strip() for name in _ACCOUNT_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


def list_accounts() -> list[dict[str, Any]]:
    """Accounts for the admin CLI, oldest first. The password hash is deliberately not included."""
    if _server():
        return _pg().list_accounts()  # type: ignore[no-any-return]
    rows = _conn().execute("SELECT account_id, username, created, disabled FROM accounts ORDER BY created, username")
    return [{"account_id": a, "username": u, "created": c, "disabled": bool(d)} for a, u, c, d in rows.fetchall()]


def set_password_hash(account_id: str, password_hash: str, now: float | None = None) -> bool:
    """Change the password and revoke every session of the account (sessions older than the change are dead)."""
    if _server():
        return bool(_pg().set_password_hash(account_id, password_hash, now))
    stamp = time.time() if now is None else now
    with _write() as conn:
        changed = conn.execute(
            "UPDATE accounts SET password_hash = ?, password_changed = ? WHERE account_id = ?",
            (password_hash, stamp, account_id),
        ).rowcount
        conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    return bool(changed)


def rehash_password(account_id: str, old_hash: str, new_hash: str) -> bool:
    """
    Swap in a stronger hash of the SAME password, only if the stored hash is still `old_hash` (compare-and-set).
    Not a password change: `password_changed` stays and no session is revoked. False = the hash changed meanwhile.
    """
    if _server():
        return bool(_pg().rehash_password(account_id, old_hash, new_hash))
    with _write() as conn:
        changed = conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE account_id = ? AND password_hash = ?",
            (new_hash, account_id, old_hash),
        ).rowcount
    return bool(changed)


def set_disabled(account_id: str, disabled: bool) -> bool:
    """Disable / enable an account. Disabling revokes its sessions; its workspaces are kept."""
    if _server():
        return bool(_pg().set_disabled(account_id, disabled))
    with _write() as conn:
        changed = conn.execute(
            "UPDATE accounts SET disabled = ? WHERE account_id = ?", (int(disabled), account_id)
        ).rowcount
        if disabled:
            conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
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
    Store a session (by token hash) and revoke the oldest ones beyond `max_per_account`. `disabled` is re-checked
    inside the same transaction, so an account disabled mid-login cannot get a session. When `expected_hash` is
    given (a password login) the account's CURRENT password hash must still equal it, so a password reset that
    landed after the password was verified leaves no surviving session. False = not created.
    """
    if _server():
        return bool(_pg().create_session(token_hash, account_id, created, expires, max_per_account, expected_hash))
    with _write() as conn:
        row = conn.execute(
            "SELECT disabled, password_hash FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if row is None or row[0] or (expected_hash is not None and row[1] != expected_hash):
            return False
        conn.execute(
            "INSERT INTO sessions (token_hash, account_id, created, last_seen, expires) VALUES (?, ?, ?, ?, ?)",
            (token_hash, account_id, created, created, expires),
        )
        conn.execute(
            "DELETE FROM sessions WHERE account_id = ? AND token_hash NOT IN"
            " (SELECT token_hash FROM sessions WHERE account_id = ? ORDER BY created DESC, rowid DESC LIMIT ?)",
            (account_id, account_id, max(1, max_per_account)),
        )
    return True


def get_session_principal(token_hash: str, now: float, idle_seconds: float) -> dict[str, Any] | None:
    """
    {account_id, username, last_seen} for a live session, else None. Dead means: past the absolute expiry, idle longer than
    `idle_seconds`, account disabled, or created before the last password change. Expired rows are deleted.
    """
    if _server():
        return _pg().get_session_principal(token_hash, now, idle_seconds)  # type: ignore[no-any-return]
    conn = _conn()
    row = conn.execute(
        "SELECT s.account_id, a.username, s.created, s.last_seen, s.expires, a.disabled, a.password_changed"
        " FROM sessions s JOIN accounts a ON a.account_id = s.account_id WHERE s.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    account_id, username, created, last_seen, expires, disabled, changed = row
    if now >= expires or now - last_seen > idle_seconds:
        delete_session(token_hash)
        return None
    if disabled or created < changed:
        return None
    return {"account_id": account_id, "username": username, "last_seen": last_seen}


def touch_session(token_hash: str, now: float) -> None:
    """
    Record activity with ONE autocommit UPDATE (no `BEGIN IMMEDIATE`). Callers only call it when the session is
    at least TOUCH_INTERVAL_SECONDS stale (see `get_session_principal`'s `last_seen`), because even an UPDATE that
    matches nothing takes the write lock; the `last_seen` guard keeps two racing touches from both writing.
    """
    if _server():
        _pg().touch_session(token_hash, now)
        return
    _conn().execute(
        "UPDATE sessions SET last_seen = ? WHERE token_hash = ? AND last_seen <= ?",
        (now, token_hash, now - TOUCH_INTERVAL_SECONDS),
    )


def delete_session(token_hash: str) -> None:
    if _server():
        _pg().delete_session(token_hash)
        return
    with _write() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def prune_expired(now: float, idle_seconds: float, attempt_window_seconds: float) -> None:
    """Drop sessions past their absolute or idle limit and login attempts older than the window (SPEC-GAP-051)."""
    if _server():  # login attempts live in Redis and expire there by themselves
        _pg().prune_expired(now, idle_seconds)
        return
    with _write() as conn:
        conn.execute("DELETE FROM sessions WHERE expires <= ? OR last_seen < ?", (now, now - idle_seconds))
        conn.execute("DELETE FROM login_attempts WHERE ts < ?", (now - attempt_window_seconds,))


# ------------------------------------------------------------------------------------------------ workspaces
def add_owned_workspace(workspace: str, account_id: str, limit: int, now: float | None = None) -> bool:
    """Record ownership unless the account already has `limit` live (not tombstoned) workspaces. False = at cap."""
    if _server():
        return bool(_pg().add_owned_workspace(workspace, account_id, limit, now))
    stamp = time.time() if now is None else now
    with _write() as conn:
        (live,) = conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE account_id = ? AND deleted = 0", (account_id,)
        ).fetchone()
        if live >= limit:
            return False
        conn.execute(
            "INSERT INTO workspaces (workspace, account_id, created, deleted) VALUES (?, ?, ?, 0)",
            (workspace, account_id, stamp),
        )
    return True


def workspace_record(workspace: str) -> tuple[str, int] | None:
    """(owner account_id, deleted flag) or None when no ownership row exists."""
    if _server():
        return _pg().workspace_record(workspace)  # type: ignore[no-any-return]
    row = _conn().execute("SELECT account_id, deleted FROM workspaces WHERE workspace = ?", (workspace,)).fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


def list_owned_workspaces(account_id: str) -> list[str]:
    """Live workspace ids of one account, oldest first."""
    if _server():
        return _pg().list_owned_workspaces(account_id)  # type: ignore[no-any-return]
    rows = _conn().execute(
        "SELECT workspace FROM workspaces WHERE account_id = ? AND deleted = 0 ORDER BY created, workspace",
        (account_id,),
    )
    return [str(r[0]) for r in rows.fetchall()]


def list_owned_workspace_info(account_id: str) -> list[tuple[str, float]]:
    """(workspace id, created epoch seconds) of one account's live workspaces, oldest first, ties by id."""
    if _server():
        return _pg().list_owned_workspace_info(account_id)  # type: ignore[no-any-return]
    rows = _conn().execute(
        "SELECT workspace, created FROM workspaces WHERE account_id = ? AND deleted = 0 ORDER BY created, workspace",
        (account_id,),
    )
    return [(str(w), float(c)) for w, c in rows.fetchall()]


def live_owned_ids() -> set[str]:
    """Every live owned workspace id (cleanup must keep these whatever their age)."""
    if _server():
        return _pg().live_owned_ids()  # type: ignore[no-any-return]
    return {str(r[0]) for r in _conn().execute("SELECT workspace FROM workspaces WHERE deleted = 0")}


def tombstoned_ids() -> set[str]:
    """Every deleted workspace id (cleanup purges their vectors; the rows stay so ids are never reused)."""
    if _server():
        return _pg().tombstoned_ids()  # type: ignore[no-any-return]
    return {str(r[0]) for r in _conn().execute("SELECT workspace FROM workspaces WHERE deleted = 1")}


def tombstone_workspace(workspace: str, account_id: str) -> bool:
    """Mark the workspace deleted if `account_id` owns it. The row is never removed. True = newly tombstoned."""
    if _server():
        return bool(_pg().tombstone_workspace(workspace, account_id))
    with _write() as conn:
        changed = conn.execute(
            "UPDATE workspaces SET deleted = 1 WHERE workspace = ? AND account_id = ? AND deleted = 0",
            (workspace, account_id),
        ).rowcount
    return bool(changed)


# ------------------------------------------------------------------------------------------------ login rate limit
def reserve_login_attempt(
    keys: Sequence[str], limits: Mapping[str, tuple[int, float]], now: float
) -> tuple[str | None, float]:
    """
    Atomically reserve one login attempt (ADR 0011 step 2). `limits` maps each key to (max attempts, window
    seconds). In ONE transaction: prune each key's rows older than ITS OWN window (a 1 s global reservation must
    never delete the 15 minute user and client rows), count each key's rows inside its window, and either refuse (nothing inserted; returns (None, retry_after_seconds)) or insert one row per key
    sharing a new attempt id (returns (attempt_id, 0.0)). Concurrent wrong guesses therefore cannot all pass the
    check before any is recorded, in this process or another.
    """
    if _server():
        return _rd().reserve_login_attempt(keys, limits, now)  # type: ignore[no-any-return]
    with _write() as conn:
        retry_after = 0.0
        for key in keys:
            maximum, window = limits[key]
            conn.execute("DELETE FROM login_attempts WHERE key = ? AND ts < ?", (key, now - window))
            count, oldest = conn.execute(
                "SELECT COUNT(*), MIN(ts) FROM login_attempts WHERE key = ? AND ts > ?", (key, now - window)
            ).fetchone()
            if count >= maximum:
                retry_after = max(retry_after, 1.0, float(oldest) + window - now)
        if retry_after:
            return None, retry_after
        attempt_id = uuid.uuid4().hex
        conn.executemany(
            "INSERT INTO login_attempts (key, ts, attempt) VALUES (?, ?, ?)",
            [(key, now, attempt_id) for key in keys],
        )
    return attempt_id, 0.0


def refund_login_attempt(attempt_id: str, keys: Sequence[str]) -> None:
    """Remove only this attempt's rows for `keys` (success, or the hashing queue was full); others are untouched."""
    if _server():
        _rd().refund_login_attempt(attempt_id, keys)
        return
    if not keys:
        return
    placeholders = ",".join("?" * len(keys))  # only "?" markers are built into the SQL; values are bound
    with _write() as conn:
        conn.execute(
            f"DELETE FROM login_attempts WHERE attempt = ? AND key IN ({placeholders})",  # noqa: S608
            (attempt_id, *keys),
        )
