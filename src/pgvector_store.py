"""
The opt-in server `VectorStore` (ADR 0013): chunks and their embeddings live in Postgres with the pgvector extension,
so several API hosts share one store. It behaves like `ChromaStore` (src/vectorstore.py) as seen from the pipeline.

Why it looks the way it does:
- Embeddings are computed in the app with the same ChromaDB model as the default store (384 dimensions), so
  rankings match. Vectors travel as text and are cast with `::vector`; the `pgvector` Python package is not needed.
- Search is exact (`<=>` cosine distance, no ANN index): a per-workspace filter on an ANN index can return fewer than
  `top_k` rows, and one user's files are small (SPEC-GAP-066).
- Every statement is a constant string with bound parameters and a `workspace = %s` predicate; ids are validated with
  the same rules as ChromaDB before any SQL runs. Connections, timeouts, `search_path` and error mapping come from
  `src/state_postgres.transaction()`; this module opens none of its own.
- `add_chunks` locks/creates the workspace row and inserts the chunks in one transaction; `clear` removes both, row
  first. A chunk therefore never outlives its workspace row, and `cleanup` also removes any orphan left by a crash.
- Text is never altered: U+0000 (which Postgres TEXT cannot hold) is refused (SPEC-GAP-076).

Heavy imports (psycopg, the embedding model) are lazy so the default local profile never needs them.
"""

import threading
import time
from typing import Any

from src import config
from src.models import Chunk, RetrievedChunk
from src.observability import event
from src.state import StorageUnavailable
from src.vectorstore import new_workspace_id, validate_workspace_id

_embedder: Any = None
_embedder_lock = threading.Lock()
_SWEEP_LOCK_KEY = 7_013_002  # advisory-lock key of the throttled sweeps
_NUL_MESSAGE = "This document contains a character (NUL) that cannot be stored. Please re-export the PDF and try again."

# Interval between two orphan sweeps / age cleanups. `service.new_workspace` calls `cleanup` on every new workspace;
# the sweeps scan whole tables, so they run at most this often per process, and (try-lock) one at a time per database.
CLEANUP_INTERVAL_SECONDS = 300.0
_cleanup_lock = threading.Lock()
_last_cleanup: list[float | None] = [None]  # monotonic time of the last claimed sweep

# The vector tables are created together with the state tables by `state_postgres.ensure_schema`, so the "schema must
# already exist" check and the single advisory lock apply to them too (a wrong PG_SCHEMA can not fill `public`).
if config.EMBEDDING_DIM != 384:  # SQL text is never built from values (threat 48), so the DDL below holds the literal
    raise RuntimeError("EMBEDDING_DIM changed: update vector(384) in VECTOR_DDL and migrate the chunks table.")
VECTOR_DDL = (
    "CREATE TABLE IF NOT EXISTS vector_workspaces (workspace TEXT PRIMARY KEY, created DOUBLE PRECISION NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chunks (workspace TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL,"
    " file_name TEXT NOT NULL, page INTEGER NOT NULL, embedding vector(384) NOT NULL,"
    " PRIMARY KEY (workspace, id))",
    "CREATE INDEX IF NOT EXISTS chunks_file ON chunks (workspace, file_name)",
)


def reset_for_tests() -> None:
    """Forget the cleanup throttle (the test fixture switches schemas between tests)."""
    with _cleanup_lock:
        _last_cleanup[0] = None


def _embed(texts: list[str]) -> list[str]:
    """Embed texts with the ChromaDB default model; return pgvector text literals like `[0.1,0.2,...]`."""
    global _embedder  # noqa: PLW0603 - the model is loaded once per process
    with _embedder_lock:
        if _embedder is None:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            _embedder = DefaultEmbeddingFunction()
        vectors = _embedder(texts)
    return ["[" + ",".join(repr(float(x)) for x in vector) + "]" for vector in vectors]


def _transaction() -> Any:
    from src import state_postgres

    return state_postgres.transaction()


def _connection() -> Any:
    """A transaction context with the tables guaranteed to exist (and the schema checked)."""
    from src import state_postgres

    state_postgres.ensure_schema(extra=VECTOR_DDL)
    return _transaction()


class PgVectorStore:
    """`VectorStore` on Postgres + pgvector. Constructing it does not connect."""

    def new_workspace(self, *, persistent: bool = False) -> str:
        return new_workspace_id(persistent)

    def add_chunks(self, workspace: str, chunks: list[Chunk]) -> None:
        validate_workspace_id(workspace)
        if not chunks:
            return
        for c in chunks:
            if "\x00" in c["text"] or "\x00" in c["file_name"] or "\x00" in c["id"]:
                from src.loader import LoaderError

                raise LoaderError(_NUL_MESSAGE)
        vectors = _embed([c["text"] for c in chunks])
        rows = [
            (workspace, c["id"], c["text"], c["file_name"], int(c["page"]), vector)
            for c, vector in zip(chunks, vectors, strict=True)
        ]
        with _connection() as conn:
            # Taking the row lock first serialises this with `clear`, which deletes the row before the chunks.
            conn.execute(
                "INSERT INTO vector_workspaces (workspace, created) "
                "VALUES (%s, EXTRACT(EPOCH FROM clock_timestamp())) "
                "ON CONFLICT (workspace) DO UPDATE SET created = vector_workspaces.created",
                (workspace,),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (workspace, id, text, file_name, page, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (workspace, id) DO UPDATE SET text = EXCLUDED.text, "
                    "file_name = EXCLUDED.file_name, page = EXCLUDED.page, embedding = EXCLUDED.embedding",
                    rows,
                )

    def retrieve(self, workspace: str, question: str, top_k: int) -> list[RetrievedChunk]:
        validate_workspace_id(workspace)
        if top_k <= 0 or "\x00" in question:
            return []
        (vector,) = _embed([question])
        with _connection() as conn:
            rows = conn.execute(
                "SELECT id, text, file_name, page, embedding <=> %s::vector AS distance FROM chunks "
                "WHERE workspace = %s ORDER BY distance, id LIMIT %s",
                (vector, workspace, top_k),
            ).fetchall()
        return [
            {"id": r[0], "text": r[1], "file_name": r[2], "page": int(r[3]), "distance": round(float(r[4]), 3)}
            for r in rows
        ]

    def all_chunks(self, workspace: str) -> list[Chunk]:
        validate_workspace_id(workspace)
        with _connection() as conn:
            rows = conn.execute(
                "SELECT id, text, file_name, page FROM chunks WHERE workspace = %s ORDER BY file_name, page, id",
                (workspace,),
            ).fetchall()
        return [{"id": r[0], "text": r[1], "file_name": r[2], "page": int(r[3])} for r in rows]

    def delete_file(self, workspace: str, file_name: str) -> None:
        validate_workspace_id(workspace)
        with _connection() as conn:
            conn.execute("DELETE FROM chunks WHERE workspace = %s AND file_name = %s", (workspace, file_name))

    def list_files(self, workspace: str) -> list[str]:
        validate_workspace_id(workspace)
        with _connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT file_name FROM chunks WHERE workspace = %s ORDER BY file_name", (workspace,)
            ).fetchall()
        return sorted(str(r[0]) for r in rows)

    def count(self, workspace: str) -> int:
        validate_workspace_id(workspace)
        with _connection() as conn:
            row = conn.execute("SELECT count(*) FROM chunks WHERE workspace = %s", (workspace,)).fetchone()
        return int(row[0]) if row else 0

    def clear(self, workspace: str) -> None:
        try:
            validate_workspace_id(workspace)
        except ValueError:
            return  # like ChromaStore: a bad id is ignored, never acted on
        with _connection() as conn:
            conn.execute("DELETE FROM vector_workspaces WHERE workspace = %s", (workspace,))
            conn.execute("DELETE FROM chunks WHERE workspace = %s", (workspace,))

    # Tells the service not to compute keep / purge itself: it would scan the ownership table on every request.
    resolves_cleanup_ids = True

    def cleanup(self, *, keep: frozenset[str] = frozenset(), purge: frozenset[str] = frozenset()) -> int:
        """
        Delete purged account workspaces and aged anonymous ones (not in `keep`); sweep orphan chunks and expired
        cache rows.

        Explicit `purge` ids are cheap (primary-key lookups) and never throttled. Everything else scans tables, so
        it runs at most once per `CLEANUP_INTERVAL_SECONDS` per process, and only the holder of a non-blocking
        advisory lock runs it: live owned ids are protected, and tombstoned workspaces that still hold vectors are
        purged, by joining against the ownership table in SQL (no id list is ever shipped). The sweep is best
        effort: a storage failure is logged with a fixed event and never fails the caller or resets the throttle,
        so aged anonymous workspaces may outlive the maximum age by up to the throttle interval.
        """
        purge_ids = sorted(w for w in purge if w.startswith("acct-") and _valid(w))
        removed = 0
        if purge_ids:
            with _connection() as conn:
                gone = conn.execute(
                    "DELETE FROM vector_workspaces WHERE workspace = ANY(%s::text[]) RETURNING workspace", (purge_ids,)
                ).fetchall()
                conn.execute("DELETE FROM chunks WHERE workspace = ANY(%s::text[])", (purge_ids,))
            removed += len(gone)
        if not _claim_sweep():
            return removed
        try:
            removed += self._sweep(sorted(keep))
        except StorageUnavailable:
            event("storage.cleanup_failed")  # fixed text: no driver message, no ids
        return removed

    @staticmethod
    def _sweep(keep_ids: list[str]) -> int:
        from src import state_postgres

        with _connection() as conn:
            locked = conn.execute("SELECT pg_try_advisory_xact_lock(%s::bigint)", (_SWEEP_LOCK_KEY,)).fetchone()
            if not locked or not locked[0]:
                return 0  # another process is sweeping right now
            # One statement: tombstoned workspaces that still have vector rows (delete normally clears them
            # synchronously) and aged anonymous ones that are not live owned. Their chunks go with the orphan sweep below.
            gone = conn.execute(
                "DELETE FROM vector_workspaces v WHERE EXISTS (SELECT 1 FROM workspaces w WHERE w.workspace = v.workspace"
                " AND w.deleted = 1) OR (left(v.workspace, 5) <> 'acct-'"
                " AND v.created < EXTRACT(EPOCH FROM clock_timestamp()) - %s AND v.workspace <> ALL(%s::text[])"
                " AND NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.workspace = v.workspace AND w.deleted = 0))"
                " RETURNING v.workspace",
                (config.WORKSPACE_MAX_AGE_HOURS * 3600, keep_ids),
            ).fetchall()
            # Orphans: chunks whose workspace row is gone (this cleanup's victims, or a crashed/racing writer).
            conn.execute(
                "DELETE FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM vector_workspaces w WHERE w.workspace = c.workspace)"
            )
            row = conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::float8").fetchone()
            state_postgres.delete_expired_cache(conn, float(row[0]) if row else time.time())
        return len(gone)


def _claim_sweep() -> bool:
    """True for the one caller that may sweep now (per-process interval); the others skip."""
    with _cleanup_lock:
        last, now = _last_cleanup[0], time.monotonic()
        if last is not None and now - last < CLEANUP_INTERVAL_SECONDS:
            return False
        _last_cleanup[0] = now
        return True


def _valid(workspace: str) -> bool:
    try:
        validate_workspace_id(workspace)
    except ValueError:
        return False
    return True
