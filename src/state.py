"""
Small persistent state shared by every thread and process of the app: result caches and the app-wide
rate limit. SQLite (WAL mode) instead of JSON files / process memory:
- writes are atomic transactions, so a crash or two concurrent writers can't leave a half-written cache;
- the rate limit is enforced across processes (Streamlit UI + API server), not per process;
- no extra server to run (see docs/adr/0005-sqlite-for-shared-state.md).
"""

import json
import os
import sqlite3
import threading
import time

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


def _conn() -> sqlite3.Connection:
    path = config.STATE_DB
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
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
                ):
                    _retry_locked(statement, conn)
                _initialised.add(path)
        _local.conn, _local.path = conn, path
    return conn


def cache_get_many(namespace: str, keys: list[str]) -> dict[str, object]:
    """Return {key: value} for the keys that are cached in this namespace."""
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
