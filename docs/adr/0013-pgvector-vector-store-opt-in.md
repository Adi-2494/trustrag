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
- **Server image and client pin** (SPEC-GAP-064, -065, -086, decided by the human on 2026-11-05):
  `pgvector/pgvector:0.8.7-pg17-bookworm@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8`
  (PostgreSQL 17, pgvector 0.8.7), pinned by digest in compose and CI; client `psycopg[binary]==3.3.6` (LGPL-3.0-only,
  used unmodified as a dynamically imported library; licence accepted by the human).
  - **Password authentication only, everywhere:** `POSTGRES_HOST_AUTH_METHOD=trust` stays forbidden, and the server
    is initialised with `POSTGRES_INITDB_ARGS="--auth-local=scram-sha-256 --auth-host=scram-sha-256"`. Without
    it the image writes `trust` for the unix socket, `127.0.0.1` and `::1`, so anyone with `docker exec` into the
    database container could connect without a password (`docs/phases/p7-dependency-proposal.md` section 3). The
    setting only applies when an empty data volume is first initialised; an existing volume keeps its `pg_hba.conf`.

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
- New dependency (`psycopg[binary]==3.3.6`) and new container image (`pgvector/pgvector:0.8.7-pg17-bookworm`, by
  digest): approved by the human on 2026-11-05; installing, lock regeneration and the compose change remain human
  gates. psycopg's LGPL-3.0 licence was accepted by the human.

## Evidence
Commands run while planning (read-only):
- `.venv/bin/python -I -c "import psycopg"` (and `psycopg2`, `psycopg_pool`, `pgvector`, `asyncpg`, `sqlalchemy`):
  `ModuleNotFoundError` for each. `grep -i -E "^(psycopg|pgvector|redis)" requirements*.lock`: no match.
- ChromaDB 1.5.9 `DefaultEmbeddingFunction()(['hello world'])`: length **384**, L2 norm **1.0**
  (model cached at `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2`).
- `chromadb/utils/embedding_functions/onnx_mini_lm_l6_v2.py`: `MODEL_NAME = "all-MiniLM-L6-v2"`, embeddings are
  normalised before return.

- `docs/phases/p7-dependency-proposal.md` (devops, 2026-11-05): image digest and platforms from
  `docker buildx imagetools inspect`; default `pg_hba.conf` has `trust` for socket and loopback, and with the
  `POSTGRES_INITDB_ARGS` above every line became `scram-sha-256` and passwordless socket and TCP logins failed;
  `vector.control` is not `trusted`, so the init script (superuser) creates the extension. psycopg 3.3.6 wheels for
  cp311 on x86_64 / aarch64 from PyPI JSON; that document's other psycopg checks were run on 3.3.8, not 3.3.6.

Not verified at planning time: psycopg 3 defaults (autocommit, transaction context manager, error classes) and the timeout
parameters were not run here. pgvector's `<=>` operator semantics and the `vector(n)` text input format were not run here (no
Postgres available); they are from the pgvector documentation as I remember it and must be confirmed by the first
contract test run. Retrieval numbers on pgvector: to be measured in P7 (P7-AC3); none are claimed here.

### Measured after the P7 build (added 2026-11-05, task T8)

**Retrieval eval (P7-AC3).** `make eval-free-pgvector` (`python -m eval.run_eval --suite free --vector-store
pgvector`), run on 2026-11-05 at 02:55:47, wrote `eval/results/results-pgvector.json` and
`eval/results/RESULTS-pgvector.md`; the ChromaDB run of the same day (02:53:10) is in `eval/results/results.json`.
Read from the two JSON files:

| Measure | ChromaDB (`results.json`) | pgvector (`results-pgvector.json`) |
|---|---|---|
| Self-authored suite, 56 questions, default settings (hybrid, top-4): recall@4 | 100% (`1.0`) | 100% (`1.0`) |
| same: MRR | 0.973 | 0.973 |
| Ablation (12 rows: chunk size 500/800/1200 × fixed/section × semantic/hybrid) | - | all 12 rows identical to ChromaDB (`ablation` lists compare equal) |
| Qasper v0.3 dev, 50 papers, 138 answerable questions: recall@4 | 0.529 | 0.529 |
| same: MRR | 0.362 | 0.362 |

The whole `public_benchmark` block (subset hash `fc15fa4f…`, exclusion counts, metrics) is identical. Difference: 0
points, inside the SPEC-GAP-073 tolerance (≤ 1 pp recall@4, ≤ 0.01 MRR). The pgvector run did not touch
`results.json`, `RESULTS.md`, `history.jsonl` or `TRENDS.md` (`eval/run_eval.py` writes `-pgvector` files and skips
`record_history`; `tests/test_eval_baseline.py::test_pgvector_run_writes_separate_files_and_leaves_baseline_untouched`).
The exact search with `ORDER BY distance, id` and identical in-process embeddings explain the identical ranking; the
contract test `test_semantic_ranking_matches_between_chroma_and_pgvector` checks it directly.

**Tests.** As reported to the architect by the engineering manager for the build tasks (not re-run by the architect;
the verifier's full-gate run in `docs/phases/p7-report.md` is authoritative): the full suite run against a throwaway
local PostgreSQL 17 / pgvector 0.8.7 and Valkey 9.1.3 gave **644 passed**, before the docs and eval tasks. The
behaviour of this ADR is covered by `tests/test_vectorstore_contract.py` (16 tests, every `VectorStore` method on both
stores, including `test_invalid_workspace_ids_raise_value_error_before_any_query`,
`test_cleanup_removes_chunks_without_workspace_row`, `test_add_chunks_racing_clear_leaves_no_reachable_or_kept_chunks`,
`test_text_with_nul_is_refused_not_altered`), `tests/test_storage_settings.py` (store selection, lazy imports),
`tests/test_security.py` (`test_workspaces_are_isolated` over both stores) and
`tests/test_server_state.py::test_state_modules_build_no_sql_from_values` /
`test_stuck_account_lock_times_out_with_503_not_hang` / `test_connection_recovers_after_failed_transaction` /
`test_pool_exhaustion_gives_503_not_hang` (connection rules). These runs settle the planning-time "not verified"
items above for pgvector's `<=>`, the `::vector` text cast and the psycopg transaction handling, as far as those tests
exercise them. Installed clients checked by the architect: `psycopg 3.3.6`, `redis 8.1.0` (`.venv`).

**Build choices that differ from or refine the Decision text** (read in `src/pgvector_store.py`,
`src/state_postgres.py`, `src/config.py`, `eval/run_eval.py`):
- **Workspace row created by `add_chunks` only.** The Decision says the `vector_workspaces` row is "created on first
  access to a workspace (as `get_or_create_collection` does)". As built, only `add_chunks` creates it; `retrieve`,
  `count`, `list_files`, `all_chunks` and `delete_file` never write. Observable effects: an anonymous workspace's 24 h
  age starts at its first upload on pgvector, but at its first access of any kind on ChromaDB; a workspace that never
  received chunks has no row (and nothing to delete). Reads by an unknown id cannot create rows, which is the more
  conservative behaviour. **Not yet accepted:** this is a behaviour difference against SPEC P7 "without changing
  behaviour" and is flagged for the human at the P7 gate (accept, or change the code to create the row on read).
- The upsert is `ON CONFLICT (workspace) DO UPDATE SET created = vector_workspaces.created` rather than `DO NOTHING`:
  a no-op update that keeps `created` and takes the row lock, so `add_chunks` and `clear` (which deletes the row
  first, then the chunks) serialise.
- Time for `created` and the 24 h cutoff is `EXTRACT(EPOCH FROM clock_timestamp())` (see ADR 0015 Evidence).
- Connections: a bounded standard-library pool per process (`state_postgres.transaction()`, shared), not "one
  connection per thread"; this matches the "Connections and SQL" bullet of the Decision.
- `retrieve` returns `[]` for a question containing U+0000 instead of querying (text is never altered).
- `config.PG_SCHEMA = "trustrag"` is a module constant, not a setting. Tests patch it to a per-test schema; the eval
  cannot, so `make eval-free-pgvector` uses schema `trustrag` of whichever database `DATABASE_URL` names, and clears
  every workspace it built (`clear_created_workspaces`). On 2026-11-05 that was the throwaway local test database,
  as reported by the manager (not verified by the architect), so the eval shared that database's `trustrag` schema
  with any test that uses the default schema.
- **Flagged for review (not verified by running):** `PgVectorStore._ensure_tables` runs its `CREATE TABLE IF NOT
  EXISTS` through `state_postgres.transaction()` directly, not through `state_postgres.ensure_schema`, so it skips the
  "schema must already exist" check. If schema `trustrag` is missing and the connecting role may create objects in
  `public` (for example a superuser test URL), the vector tables would be created in `public` via the `search_path`.
  With the compose init script (`REVOKE CREATE ON SCHEMA public FROM PUBLIC`, non-superuser app role) the statement
  fails with `StorageUnavailable` instead.
  **Resolved in the P7 fix round:** the vector tables are now created by `state_postgres.ensure_schema(extra=...)`, so
  the "schema must exist" check and the single advisory lock apply to them as well.
