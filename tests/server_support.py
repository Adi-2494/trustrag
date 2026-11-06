"""
Helpers for the P7 server-backend tests (docs/phases/p7-plan.md section 3, SPEC-GAP-071, -089).

Server tests need a real Postgres (with pgvector) and Redis, named by TRUSTRAG_TEST_DATABASE_URL and
TRUSTRAG_TEST_REDIS_URL. Without them the `[server]` cases skip with a clear reason; in CI (CI=true) a missing URL fails
`test_server_backends_configured_when_ci` instead. `psycopg` and `redis` are imported INSIDE functions only, so local
runs (where neither package is installed) can still collect and run every other test.

Every server test gets a fresh Postgres schema `test_<32 hex>` and a fresh Redis prefix `trustrag-test:<32 hex>:`.
Teardown deletes only what the guards below accept (SPEC-GAP-089).
"""

import os
import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from src import config

DATABASE_ENV = "TRUSTRAG_TEST_DATABASE_URL"
REDIS_ENV = "TRUSTRAG_TEST_REDIS_URL"
APP_ROLE_ENV = "TRUSTRAG_TEST_APP_ROLE_URL"
SKIP_REASON = f"server backends not configured ({DATABASE_ENV} and {REDIS_ENV} are not both set)"

SCHEMA_PATTERN = re.compile(r"^test_[0-9a-f]{32}$")
PREFIX_PATTERN = re.compile(r"^trustrag-test:[0-9a-f]{32}:$")


class UnsafeTeardown(RuntimeError):
    """A teardown was refused because the schema, prefix or URL did not look like a test's own."""


def configured() -> bool:
    return bool(os.environ.get(DATABASE_ENV)) and bool(os.environ.get(REDIS_ENV))


def database_url() -> str:
    return os.environ[DATABASE_ENV]


def redis_url() -> str:
    return os.environ[REDIS_ENV]


def assert_safe_teardown(schema: str, prefix: str, test_database_url: str, real_database_url: str = "") -> None:
    """SPEC-GAP-089: refuse to drop anything that is not a per-test schema / prefix, or on the real database."""
    if not SCHEMA_PATTERN.fullmatch(schema or ""):
        raise UnsafeTeardown("refusing to drop a schema that is not test_<32 hex>")
    if not PREFIX_PATTERN.fullmatch(prefix or ""):
        raise UnsafeTeardown("refusing to delete Redis keys outside trustrag-test:<32 hex>:")
    if real_database_url and test_database_url == real_database_url:
        raise UnsafeTeardown("the test database URL equals DATABASE_URL")


def real_database_url() -> str:
    """The configured production DATABASE_URL (environment or settings), only to compare it with the test URL."""
    return os.environ.get("DATABASE_URL", "") or str(
        getattr(getattr(config, "SETTINGS", None), "database_url", "") or ""
    )


def new_names() -> tuple[str, str]:
    token = uuid.uuid4().hex
    return f"test_{token}", f"trustrag-test:{token}:"


# ------------------------------------------------------------------------------------------------ Postgres / Redis
def pg_connect(schema: str | None = None, url: str | None = None) -> Any:
    import psycopg
    from psycopg import sql

    conn = psycopg.connect(url or database_url(), autocommit=True)
    if schema is not None:
        conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    return conn


def pg_rows(statement: str, params: tuple = (), *, schema: str | None = None) -> list[tuple]:
    """Run one statement (written by the test, never built from values) in the test schema and return its rows."""
    with pg_connect(schema if schema is not None else config.PG_SCHEMA) as conn:  # type: ignore[attr-defined]
        cursor = conn.execute(statement, params)
        return list(cursor.fetchall()) if cursor.description else []


def redis_client() -> Any:
    import redis

    return redis.Redis.from_url(redis_url(), decode_responses=True)


def redis_keys(prefix: str | None = None) -> list[str]:
    prefix = prefix if prefix is not None else config.REDIS_PREFIX  # type: ignore[attr-defined]
    return sorted(redis_client().scan_iter(match=prefix + "*"))


def activate(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point `config` at the server backends with a fresh schema and prefix (the `server_backend` fixture)."""
    if not configured():
        pytest.skip(SKIP_REASON)
    from psycopg import sql

    schema, prefix = new_names()
    app_url = os.environ.get(APP_ROLE_ENV, "")
    for name, value in {
        "VECTOR_STORE": "pgvector",
        "STATE_BACKEND": "redis",
        # P7 fix round: when the least-privilege app role is configured the code under test connects AS that role
        # (as in production); the fixture itself keeps using the admin URL to create / drop the schema.
        "DATABASE_URL": app_url or database_url(),
        "REDIS_URL": redis_url(),
        "PG_SCHEMA": schema,
        "REDIS_PREFIX": prefix,
        "STATE_CLOCK": "store",
    }.items():
        monkeypatch.setattr(config, name, value, raising=False)
    with pg_connect() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        if app_url:
            role = app_role_name(app_url)
            conn.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role))
            )
    reset_connections()
    return SimpleNamespace(schema=schema, prefix=prefix)


def app_role_name(url: str) -> str:
    """The database user named in the app-role URL (for the GRANT on the per-test schema)."""
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(url)["user"])


def reset_connections() -> None:
    """Drop pooled connections / cached clients so the next call uses the current schema and prefix (if supported)."""
    import importlib

    for module_name in ("src.state_postgres", "src.state_redis", "src.pgvector_store"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        reset = getattr(module, "reset_for_tests", None)
        if callable(reset):
            reset()


def deactivate(handle: SimpleNamespace) -> None:
    """Drop the test schema and delete the test prefix's keys, after the guards (SPEC-GAP-089)."""
    from psycopg import sql

    assert_safe_teardown(handle.schema, handle.prefix, database_url(), real_database_url())
    reset_connections()
    with pg_connect() as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(handle.schema)))
    client = redis_client()
    for key in list(client.scan_iter(match=handle.prefix + "*")):
        client.delete(key)


def child_snapshot() -> dict[str, Any]:
    """The backend settings a spawned child process must apply to share this test's state (multi-process tests)."""
    names = ["STATE_DB", "CHROMA_DIR", "VECTOR_STORE", "STATE_BACKEND", "DATABASE_URL", "REDIS_URL", "PG_SCHEMA"]
    names += ["REDIS_PREFIX", "STATE_CLOCK"]
    return {name: getattr(config, name) for name in names if hasattr(config, name)}


def apply_snapshot(snapshot: dict[str, Any]) -> None:
    """Called inside a child process: apply `child_snapshot()` to the child's own `config` module."""
    for name, value in snapshot.items():
        setattr(config, name, value)
