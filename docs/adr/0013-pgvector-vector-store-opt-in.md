# 0013 - Opt-in pgvector `VectorStore` for multi-host deployments (amends 0003)

**Status:** Accepted (P7; approved by the human, 2026-11-05). Drafted 2026-11-05 and revised the same day after the security review of the plan,
`docs/phases/p7-plan.md` section 8, findings 4, 5, 6). Amends ADR 0003 if accepted; ADR 0003 stays the default and is not
superseded. Changing the status of ADR 0003 is a human action.

## Context
- SPEC P7: remove the single-host limit of ADRs 0003 and 0005 "without changing behaviour". Scope: a `VectorStore`
  implementation on pgvector, selected by settings; ChromaDB stays the default (SPEC-GAP-009).
- ADR 0003 consequence: "An embedded store is owned by one process. Running several API instances would require a
  server-based store (pgvector is the natural next step) - implemented as another `VectorStore`, with no pipeline
  changes."
- P7-AC3: retrieval eval on pgvector must be within 1 point of the ChromaDB baseline (recall@4 100%, MRR 0.97,
  `eval/results/RESULTS.md`), or the difference must be explained.
- `src/retrieval.py` hybrid mode uses `VectorStore.retrieve` (semantic) and `VectorStore.all_chunks` (BM25 input),
  then RRF. So the store only has to reproduce the semantic ranking; BM25 and RRF are store-independent.
- ADR 0012 rules every store must honour: ids `<32 hex>` (anonymous) and `acct-<32 hex>` (account); `ws_` data
  deleted after 24 h unless in `keep`; `acct_` data never deleted by age, only when in `purge`.
- Embeddings today: ChromaDB's `DefaultEmbeddingFunction` (ONNX all-MiniLM-L6-v2), cosine space
  (`configuration={"hnsw": {"space": "cosine"}}` in `src/vectorstore.py`). ChromaDB's HNSW index is approximate.
- Nothing for Postgres is installed: `psycopg`, `psycopg2`, `psycopg_pool`, `pgvector`, `asyncpg`, `sqlalchemy` are
  not importable in `.venv`, and none is in `requirements*.lock`.

## Decision
- **New implementation, existing interface.** `PgVectorStore` in a new module `src/pgvector_store.py` implements the
  unchanged `VectorStore` protocol (`src/interfaces.py`). No pipeline, service or retrieval change.
  `TrustRAGService(store=None)` builds the store from settings through one factory (plan section 2.4).
- **Selection:** setting `VECTOR_STORE` = `chroma` (default) or `pgvector`; `pgvector` requires `DATABASE_URL`.
  Mixed combinations with the state backend are refused at startup in P7 (ADR 0014, SPEC-GAP-063).
- **Same embeddings, computed in the app.** Chunks and questions are embedded in-process with the same ChromaDB
  `DefaultEmbeddingFunction` (384 dimensions, unit-normalised; measured, see Evidence). `chromadb` therefore stays a
  dependency in pgvector mode. No embedding API, no new model; embeddings never leave the deployment.
- **Schema** (created by the app with `CREATE TABLE IF NOT EXISTS` in the app schema `trustrag` under a Postgres
  advisory lock, so two containers starting at once cannot race; the `vector` extension is created in `public` by
  the compose init script, not by the app role; SPEC-GAP-077, -086):
  - `vector_workspaces(workspace TEXT PRIMARY KEY, created DOUBLE PRECISION NOT NULL)` - mirrors a ChromaDB
    collection and its `created` metadata: a row is created on first access to a workspace (as
    `get_or_create_collection` does) and deleted by `clear`.
  - `chunks(workspace TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL, file_name TEXT NOT NULL,
    page INTEGER NOT NULL, embedding vector(384) NOT NULL, PRIMARY KEY (workspace, id))` plus a btree index on
    `(workspace, file_name)`.
- **Exact search, no ANN index** (SPEC-GAP-066): `ORDER BY embedding <=> $q, id LIMIT top_k` with
  `WHERE workspace = $ws`. `<=>` is pgvector's cosine distance, the same space ChromaDB uses. Workspaces are small
  (one user's files), so an exact scan is cheap and deterministic; an HNSW/IVFFlat index combined with a per-workspace
  filter can return fewer than `top_k` rows, which would change behaviour. Adding an ANN index later needs a new ADR
  with eval evidence.
- **Vectors are passed as text and cast** (`$1::vector`), so the `pgvector` Python package is not needed. The only new
  client library is `psycopg` 3 (shared with ADR 0015; exact pin is SPEC-GAP-064). One connection per thread, like
  `src/state.py`; no pool package.
- **Behaviour parity rules:**
  - `add_chunks` upserts the `vector_workspaces` row (`ON CONFLICT DO NOTHING`) **and** the chunks in one
    transaction, so a chunk never exists without its workspace row; `clear` deletes both in one transaction; `cleanup`
    also deletes chunks whose workspace has no `vector_workspaces` row (orphans from a race with `clear`;
    review finding 4a).
  - `retrieve` returns `[]` for an empty workspace, at most `min(top_k, count)` rows, `distance` rounded to 3 places.
  - `list_files` sorted unique names; `count`; `delete_file` by `file_name`; `clear` deletes the workspace's rows and
    its `vector_workspaces` row (no error when empty); `all_chunks` returns every chunk.
  - Every statement carries `WHERE workspace = $ws` with a bound parameter; the workspace id is validated with the same
    two regexes as `src/vectorstore.py` before any SQL runs (invalid → `ValueError`, i.e. the existing 404).
  - `cleanup(keep, purge)`: deletes anonymous workspaces whose `created` is older than 24 h and not in `keep`, and
    account workspaces only when in `purge`. Never deletes an account workspace by age.
  - Chunk text containing U+0000 (which Postgres `TEXT` cannot store) is refused with the existing friendly upload
    error, never silently altered (SPEC-GAP-076).
  - Database errors surface as `StorageUnavailable` (fixed message, 503 at the API), never a raw driver message that
    could contain host or user names (SPEC-GAP-069).
  - Time for `vector_workspaces.created` and the 24 h cutoff comes from the database clock in server mode
    (SPEC-GAP-070), so a host with a fast clock cannot age out other hosts' anonymous workspaces early.
- **Connections and SQL** (review findings 5, 6; SPEC-GAP-085, -086), shared with ADR 0015:
  - `autocommit=True` connections; every multi-statement operation is an explicit transaction block; on any error
    the transaction is rolled back and the connection discarded, the next call opens a new one.
  - Server-side `lock_timeout`, `statement_timeout` and `idle_in_transaction_session_timeout` set per connection;
    a timeout becomes `StorageUnavailable`, never a hung worker.
  - A bounded per-process pool (stdlib only, waiting at most a few seconds, then `StorageUnavailable`).
  - The app connects as `trustrag_app` (NOSUPERUSER, NOCREATEDB, NOCREATEROLE) that owns only schema `trustrag`;
    `search_path` is `trustrag, public` (the `vector` type lives in `public`), set with `psycopg.sql.Identifier`.
  - SQL text is never built from values with f-strings or `%`; values are always bound parameters, identifiers only
    via `psycopg.sql.Identifier`.

## Consequences
- Several API containers on several hosts can share one vector store. ChromaDB + SQLite remain the default; a laptop
  still needs no server.
- A Postgres server becomes an operational dependency in pgvector mode (backups, upgrades, credentials). Database
  credentials replace file permissions as the access control for stored document text (ADR 0015, THREAT_MODEL
  delta in `docs/phases/p7-plan.md`).
- Document text is also held in the server-mode OCR cache for at most 24 h (ADR 0014), and in Postgres heap/WAL
  pages after deletion until `VACUUM` reclaims them (accepted residual, THREAT_MODEL).
- Documents are stored in one shared table; isolation relies on the `WHERE workspace = …` predicate in every
  statement instead of separate collections. A contract test checks every method against two workspaces.
- Exact search scales linearly with a workspace's chunk count; acceptable at P7 sizes, revisit with an ADR if not.
- Switching `VECTOR_STORE` does not migrate data: ChromaDB data stays on disk untouched, the new store starts empty
  (SPEC-GAP-074).
- New dependency (`psycopg`) and new container image (Postgres with pgvector): human gates (lock regeneration,
  compose change).

## Evidence
Commands run while planning (read-only):
- `.venv/bin/python -I -c "import psycopg"` (and `psycopg2`, `psycopg_pool`, `pgvector`, `asyncpg`, `sqlalchemy`):
  `ModuleNotFoundError` for each. `grep -i -E "^(psycopg|pgvector|redis)" requirements*.lock`: no match.
- ChromaDB 1.5.9 `DefaultEmbeddingFunction()(['hello world'])`: length **384**, L2 norm **1.0**
  (model cached at `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2`).
- `chromadb/utils/embedding_functions/onnx_mini_lm_l6_v2.py`: `MODEL_NAME = "all-MiniLM-L6-v2"`, embeddings are
  normalised before return.

Not verified: psycopg 3 defaults (autocommit, transaction context manager, error classes) and the timeout
parameters were not run here. pgvector's `<=>` operator semantics and the `vector(n)` text input format were not run here (no
Postgres available); they are from the pgvector documentation as I remember it and must be confirmed by the first
contract test run. Retrieval numbers on pgvector: to be measured in P7 (P7-AC3); none are claimed here.
