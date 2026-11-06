"""
P7 backend selection (docs/phases/p7-plan.md 2.1, 2.4 and 3.1 "selection"; SPEC-GAP-063, -070).

Local-mode tests: they need no Postgres or Redis. Anything from `src.storage`, `src.pgvector_store`,
`src.state_postgres` or `src.state_redis` is imported inside the test, so a missing module fails that test alone.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from src import config
from src.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
SECRET_PG = "postgresql://appuser:pg-s3cret-pw@db.internal.example:5432/trustrag"
SECRET_REDIS = "redis://:rd-s3cret-pw@cache.internal.example:6379/0"
LEAKS = ("pg-s3cret-pw", "rd-s3cret-pw", "db.internal.example", "cache.internal.example", "appuser")


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def _blob(error: ValidationError) -> str:
    return "\n".join([str(error), repr(error), json.dumps(error.errors(), default=str)])


def test_defaults_are_chroma_and_sqlite():
    """P7-AC1 (selection): with no environment the profile is local; no URL is required."""
    s = _settings()
    assert (s.vector_store, s.state_backend) == ("chroma", "sqlite")
    assert (s.database_url, s.redis_url) == ("", "")


def test_server_profile_is_accepted_with_both_urls():
    """P7-AC1 (selection): pgvector + redis with valid URLs is the one accepted server profile."""
    s = _settings(vector_store="pgvector", state_backend="redis", database_url=SECRET_PG, redis_url=SECRET_REDIS)
    assert (s.vector_store, s.state_backend) == ("pgvector", "redis")
    s = _settings(
        vector_store="pgvector",
        state_backend="redis",
        database_url=SECRET_PG.replace("postgresql://", "postgres://"),
        redis_url=SECRET_REDIS.replace("redis://", "rediss://"),
    )
    assert s.vector_store == "pgvector"


@pytest.mark.parametrize(
    ("vector_store", "state_backend"),
    [("pgvector", "sqlite"), ("chroma", "redis")],
)
def test_mixed_backend_combinations_are_refused(vector_store, state_backend):
    """P7-AC1 (selection) / threat 52: only chroma+sqlite or pgvector+redis start; a mixed fleet is refused."""
    with pytest.raises(ValidationError) as error:
        _settings(
            vector_store=vector_store, state_backend=state_backend, database_url=SECRET_PG, redis_url=SECRET_REDIS
        )
    assert "vector_store" in str(error.value).lower() or "state_backend" in str(error.value).lower()


def test_unknown_backend_names_are_refused():
    for field, value in (("vector_store", "faiss"), ("state_backend", "memcached")):
        with pytest.raises(ValidationError):
            _settings(**{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"vector_store": "pgvector", "state_backend": "redis"},  # no URLs at all
        {"vector_store": "pgvector", "state_backend": "redis", "database_url": SECRET_PG},  # no Redis URL
        {"vector_store": "pgvector", "state_backend": "redis", "redis_url": SECRET_REDIS},  # no database URL
    ],
)
def test_server_settings_require_urls(kwargs):
    """P7-AC1 (selection): a server setting without its URL fails at startup, naming the setting."""
    with pytest.raises(ValidationError) as error:
        _settings(**kwargs)
    text = str(error.value).lower()
    assert "database_url" in text or "redis_url" in text


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "vector_store": "pgvector",
            "state_backend": "redis",
            "database_url": "mysql://u:pg-s3cret-pw@db.internal.example/x",
            "redis_url": SECRET_REDIS,
        },
        {
            "vector_store": "pgvector",
            "state_backend": "redis",
            "database_url": SECRET_PG,
            "redis_url": "http://:rd-s3cret-pw@cache.internal.example:6379",
        },
        {"vector_store": "pgvector", "state_backend": "sqlite", "database_url": SECRET_PG, "redis_url": SECRET_REDIS},
        {
            "vector_store": "pgvector",
            "state_backend": "redis",
            "database_url": "not a url pg-s3cret-pw appuser",
            "redis_url": SECRET_REDIS,
        },
    ],
)
def test_settings_errors_never_echo_urls(kwargs):
    """P7-AC1 / threat 49: a rejected setting names the field but never repeats a URL, user, host or password."""
    with pytest.raises(ValidationError) as error:
        _settings(**kwargs)
    blob = _blob(error.value)
    for leak in LEAKS:
        assert leak not in blob, leak


def test_settings_errors_from_the_environment_never_echo_urls(monkeypatch):
    """Same, when the values come from the environment / .env instead of keyword arguments."""
    monkeypatch.setenv("VECTOR_STORE", "pgvector")
    monkeypatch.setenv("STATE_BACKEND", "redis")
    monkeypatch.setenv("DATABASE_URL", "ftp://appuser:pg-s3cret-pw@db.internal.example/x")
    monkeypatch.setenv("REDIS_URL", SECRET_REDIS)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    blob = _blob(error.value)
    for leak in LEAKS:
        assert leak not in blob, leak


def test_service_default_store_follows_vector_store_setting(monkeypatch):
    """P7-AC1 (selection): TrustRAGService(store=None) builds the store named by config.VECTOR_STORE."""
    from src import pgvector_store
    from src.service import TrustRAGService
    from src.vectorstore import ChromaStore

    monkeypatch.setattr(config, "VECTOR_STORE", "chroma", raising=False)
    assert isinstance(TrustRAGService().store, ChromaStore)

    class Sentinel:
        pass

    monkeypatch.setattr(pgvector_store, "PgVectorStore", Sentinel)
    monkeypatch.setattr(config, "VECTOR_STORE", "pgvector", raising=False)
    assert isinstance(TrustRAGService().store, Sentinel)


def test_make_vector_store_rejects_an_unknown_setting(monkeypatch):
    from src import storage

    monkeypatch.setattr(config, "VECTOR_STORE", "faiss", raising=False)
    with pytest.raises(ValueError):
        storage.make_vector_store()


def test_pgvector_store_has_the_vectorstore_protocol_methods():
    """P7-AC1 (contract): PgVectorStore offers exactly the methods of the unchanged `VectorStore` protocol."""
    import inspect

    from src.interfaces import VectorStore
    from src.pgvector_store import PgVectorStore

    for name in (
        "new_workspace",
        "add_chunks",
        "retrieve",
        "all_chunks",
        "delete_file",
        "list_files",
        "count",
        "clear",
        "cleanup",
    ):
        assert callable(getattr(PgVectorStore, name)), name
        assert str(inspect.signature(getattr(PgVectorStore, name))) == str(
            inspect.signature(getattr(VectorStore, name))
        ), name


def _run_child(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    full_env = {k: v for k, v in os.environ.items() if k not in ("VECTOR_STORE", "STATE_BACKEND", "STATE_CLOCK")}
    full_env.update(env)
    script = f"import sys; sys.path.insert(0, {str(ROOT)!r})\nsys.modules['psycopg'] = None\nsys.modules['redis'] = None\n{code}"
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, env=full_env, timeout=120, check=False
    )


def test_local_mode_never_imports_psycopg_or_redis(tmp_path):
    """P7-AC1 (selection) / threat 52: the default profile runs end to end with both drivers made un-importable."""
    code = (
        "import api.main\n"
        "from src import storage, state, accounts, service\n"
        "storage.make_vector_store()\n"
        "assert state.try_acquire_call(5) == 0\n"
        "state.cache_set_many('ns', {'k': 1})\n"
        "assert state.cache_get_many('ns', ['k']) == {'k': 1}\n"
        "print('ok')\n"
    )
    done = _run_child(code, CHROMA_DIR=str(tmp_path / "c"))
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr[-800:]


def test_server_modules_import_without_the_drivers(tmp_path):
    """P7-AC1: the driver packages are imported lazily, so the new modules import (and local mode runs) without them."""
    code = "import src.state_redis, src.state_postgres, src.pgvector_store\nprint('ok')\n"
    done = _run_child(code, CHROMA_DIR=str(tmp_path / "c"))
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr[-800:]


def test_state_clock_override_is_not_an_environment_setting(tmp_path):
    """P7-AC1 (selection) / SPEC-GAP-070: STATE_CLOCK is a module constant for tests; the environment cannot change it."""
    assert "state_clock" not in Settings.model_fields
    assert config.STATE_CLOCK == "store"
    done = _run_child(
        "from src import config\nprint(config.STATE_CLOCK)\n", STATE_CLOCK="caller", CHROMA_DIR=str(tmp_path / "c")
    )
    assert done.returncode == 0 and done.stdout.strip() == "store", done.stderr[-800:]


def test_p7_config_constants_have_the_planned_values():
    """Plan 2.4 / SPEC-GAP-085: the numbers every timeout and bound test relies on."""
    expected = {
        "VECTOR_STORE": "chroma",
        "STATE_BACKEND": "sqlite",
        "PG_SCHEMA": "trustrag",
        "REDIS_PREFIX": "trustrag:{t}:",
        "EMBEDDING_DIM": 384,
        "STATE_CLOCK": "store",
        "SERVER_CACHE_TTL_HOURS": 24,
        "PG_LOCK_TIMEOUT_MS": 5000,
        "PG_STATEMENT_TIMEOUT_MS": 15000,
        "PG_IDLE_IN_TRANSACTION_TIMEOUT_MS": 30000,
        "PG_MAX_CONNECTIONS_PER_PROCESS": 10,
        "PG_POOL_WAIT_SECONDS": 5,
        "REDIS_TIMEOUT_SECONDS": 5,
    }
    actual = {name: getattr(config, name, "<missing>") for name in expected}
    assert actual == expected
