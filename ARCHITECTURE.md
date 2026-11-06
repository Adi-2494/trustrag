# TrustRAG architecture

TrustRAG answers questions about a user's PDFs using only those documents, fact-checks every answer, and
scans every document for prompt-injection attacks before it is indexed. This document explains how the
pieces fit together and why they are shaped this way. Individual decisions are recorded in [`docs/adr/`](docs/adr/).

## Components

```mermaid
flowchart LR
    subgraph Clients
        UI[Streamlit UI<br/>app.py]
        HTTP[Any HTTP client]
        CLI["Admin CLI<br/>python -m src.accounts"]
    end
    subgraph Backend
        API[FastAPI<br/>api/main.py]
        SVC[TrustRAGService<br/>src/service.py<br/>_authorize first]
    end
    subgraph Accounts["Accounts (src/, ADR 0011)"]
        ACC[accounts<br/>SessionAuthenticator + PasswordLogin]
        PW[passwords<br/>scrypt, DUMMY_HASH, KDF slots]
    end
    subgraph Pipeline["Pipeline modules (src/)"]
        LOAD[loader + ocr]
        CHUNK[chunker]
        SCAN[injection_scanner]
        RET[retrieval<br/>semantic + BM25 + RRF]
        GEN[generator]
        VER[verifier]
    end
    subgraph Ports["Injected interfaces (src/interfaces.py)"]
        LLM[[LLMClient]]
        VS[[VectorStore]]
        AUTH[[Authenticator]]
    end
    UI -- in-process --> SVC
    UI -- "TRUSTRAG_API_URL<br/>(Authorization: Bearer)" --> API
    HTTP --> API --> SVC
    CLI --> ACC
    SVC --> LOAD & CHUNK & SCAN & RET & GEN & VER
    LOAD & SCAN & GEN & VER & RET -.-> LLM
    SVC & RET -.-> VS
    SVC -.-> AUTH
    AUTH -. implemented by .-> ACC
    ACC --> PW
    LLM --> CLAUDE[(Claude: subscription via<br/>Claude Code, Anthropic API<br/>or AWS Bedrock)]
    VS -->|local, default| CHROMA[(ChromaDB, one collection per workspace<br/>ws_&lt;hex&gt; anonymous, acct_&lt;hex&gt; accounts)]
    VS -->|server| PGV[(Postgres + pgvector<br/>vector_workspaces, chunks)]
    SVC & ACC & LLM -.-> STATE[[src/state.py<br/>same public functions]]
    STATE -->|local, default| SQLITE[(SQLite state<br/>caches + rate limits,<br/>accounts, sessions, ownership)]
    STATE -->|server| SRV[(Valkey: rate-limit counters<br/>Postgres: accounts, sessions,<br/>ownership, caches)]
```

Every store sits behind an existing seam: vectors behind `VectorStore`, everything else behind the public functions of
`src/state.py`. The two storage modes are described in [Storage modes](#storage-modes-p7).

| Layer | Responsibility | Key files |
|---|---|---|
| **Clients** | Render results; no pipeline logic. The UI talks to the service in-process, or to the API when `TRUSTRAG_API_URL` is set. With `ACCOUNTS_ENABLED=true` the UI offers "Sign in" or "Continue without an account"; the session token lives only in `st.session_state` and the API client sends it as `Authorization: Bearer`. The admin CLI is the only way to create and manage accounts. | `app.py`, `src/api_client.py`, `src/accounts.py` (CLI) |
| **API** | HTTP, outer gate (`X-API-Key`), bearer-token extraction, sign-in routes, request IDs, error-to-status mapping, `/metrics`. `ACCOUNTS_ENABLED` gates only the sign-in routes, session use and `GET /v1/workspaces`. | `api/main.py` |
| **Service** | The pipeline as two use cases, `upload` and `ask`, plus workspace management (`new_workspace`, `list_workspaces`, `list_files`, `count`, `clear_documents`, `clear`) and `login` / `logout`, with dependencies injected. Every workspace method takes an optional keyword-only `session` and calls `_authorize` first. | `src/service.py` |
| **Accounts** | Local password accounts (ADR 0011): scrypt hashing, username canonicalisation, password policy, rate-limited login, server-side sessions, admin CLI. | `src/accounts.py`, `src/passwords.py` |
| **Pipeline** | One responsibility per module (load, OCR, chunk, scan, retrieve, generate, verify). | `src/*.py` |
| **Interfaces** | `LLMClient`, `VectorStore` and `Authenticator` protocols, so providers, stores and (in P8) sign-in methods are swappable and tests need no monkeypatching. | `src/interfaces.py`, `src/llm.py` |
| **Storage** | Store selection by settings (local or server mode), the server `VectorStore`, and the server back ends of `src/state.py`. Local mode never imports the server drivers. | `src/storage.py`, `src/pgvector_store.py`, `src/state_postgres.py`, `src/state_redis.py` |
| **Cross-cutting** | Validated settings, shared state, structured logs and metrics, input/output safety helpers. | `src/settings.py`, `src/config.py`, `src/state.py`, `src/observability.py`, `src/safety.py` |

## Data flow

### Upload (once per document)

```mermaid
sequenceDiagram
    participant C as Client
    participant S as TrustRAGService
    participant L as loader / ocr
    participant K as chunker
    participant X as injection_scanner
    participant V as VectorStore
    C->>S: upload(workspace, file_name, bytes, session=None)
    S->>S: _authorize(workspace, session) first: 401 / 404 before any work
    S->>S: sanitise file name, enforce size limit, start request ID
    S->>L: extract text per page (pypdf)
    L-->>L: pages without text -> render -> Claude Haiku OCR (cached)
    S->>K: 800-char chunks, 150 overlap, stable ids
    S->>X: rules (normalised text + chunk seams) + Claude Haiku (10 per call, 4 in parallel, cached)
    X-->>S: ScanReport (risk per flagged chunk)
    S->>S: drop chunks at the admin's risk levels (default HIGH) + user's stricter extras
    S->>V: delete older version of the file, add safe chunks (private workspace)
    S->>S: _authorize again (owner may have deleted the workspace meanwhile -> clear + 404)
    S-->>C: UploadResult
```

### Ask (every question)

```mermaid
sequenceDiagram
    participant C as Client
    participant S as TrustRAGService
    participant R as retrieval
    participant G as generator
    participant F as verifier
    C->>S: ask(workspace, question, session=None)
    S->>S: _authorize(workspace, session) first: 401 / 404 before retrieval
    S->>R: semantic top-12 + BM25 top-12 -> reciprocal rank fusion -> top-4 (optional Haiku rerank)
    S->>G: Claude Sonnet, escaped context, strict grounding rules, cite [file, p.N]
    S->>F: one Claude Haiku call: split into claims, label, quote; quotes verified word for word
    F-->>S: Verification (claims + trust score)
    S-->>C: AskResult (answer, sources, verification, request ID, timing)
```

Anonymous callers (`session=None`) follow exactly the v1.0 path; the extra authorisation step only refuses ids that
have an ownership row or the `acct-` prefix (see [Accounts and workspaces](#accounts-and-workspaces)).

### Sign in (`ACCOUNTS_ENABLED=true` only)

```mermaid
sequenceDiagram
    participant C as Client
    participant L as PasswordLogin
    participant S as state.py
    participant P as passwords.py
    C->>L: login(raw body, client_key or None)
    L->>L: 1. shape check (body <= 4096 bytes, JSON object, string fields, password <= 1024 chars), canonical username
    L->>S: 2. reserve_login_attempt: one BEGIN IMMEDIATE - prune, count user / client / global keys, refuse or insert
    S-->>L: refused -> 429 + Retry-After (no hashing)
    L->>S: 3. get_account_by_username (skipped for a bad shape)
    L->>P: 4. kdf_slot (5 s wait; busy -> refund + 429)
    L->>P: 5. exactly one scrypt vs stored hash or DUMMY_HASH, compare_digest
    alt right password, account enabled
        L->>S: 6. separate atomic transactions: rehash if parameters changed (compare-and-set); create_session (re-checks disabled AND unchanged hash, caps sessions); refund user/client rows
        L-->>C: SessionInfo (token, username, expires_at)
    else unknown user / wrong password / disabled / bad shape
        L-->>C: LoginFailed "Wrong username or password." (reservation kept as a failure)
    end
```

Step 6 is not one transaction as ADR 0011 describes; the split is recorded in SPEC-GAP-060. The whole login runs in
the threadpool (`run_in_threadpool`), so scrypt does not block the event loop.

Bad-shape bodies still cost a reservation (client and global keys only) and one hash against `DUMMY_HASH`, so every
failure does the same hashing work.

## Accounts and workspaces

Accounts are opt-in (`ACCOUNTS_ENABLED`, default `false`). Decisions: [ADR 0011](docs/adr/0011-local-password-accounts-and-server-side-sessions.md)
(accounts, hashing, login order, sessions) and [ADR 0012](docs/adr/0012-workspace-ownership-and-retention.md)
(ownership, retention).

| Part | What it does | Key files |
|---|---|---|
| **Accounts** | Created by the administrator only (`python -m src.accounts create\|set-password\|disable\|enable\|list`); no sign-up route. Passwords are typed twice with `getpass`, or piped with `--password-stdin`; a non-terminal stdin without that flag is refused; never argv or environment. Usernames: `isascii()` first, then `lower()`, then `fullmatch([a-z0-9][a-z0-9._-]{2,31})`; that one canonical value is used for lookup, rate-limit key and CLI. Passwords: 12 to 1024 characters, not equal to the username. `account_id` is a random 32-hex id, never the username. | `src/accounts.py`, `src/passwords.py` |
| **Hashing** | Standard-library `hashlib.scrypt` (N = 2^17, r = 8, p = 1, 16-byte salt, 32-byte key, `maxmem` 256 MiB), stored as `scrypt$n=…,r=…,p=…$<salt hex>$<key hex>`. Rehashed on a successful login when the parameters differ: a compare-and-set inside the KDF slot that never overwrites a concurrent password reset and does not revoke sessions. `DUMMY_HASH` is built at import with the configured parameters. A bounded semaphore (`KDF_MAX_CONCURRENT = 2`, `KDF_WAIT_SECONDS = 5`) caps simultaneous hashes. | `src/passwords.py`, `src/config.py` |
| **Login rate limit** | Keys `sha256("user:" + canonical)`, `sha256("client:" + address)` and `"global"`, counted in the `login_attempts` table inside one `BEGIN IMMEDIATE` transaction. Defaults: 5 per user and 20 per client per rolling 15 minutes, 5 per second across all clients. A success refunds the user and client rows; a failure keeps them. No client key when the address is unknown (`client_key=None`). | `src/accounts.py`, `src/state.py` |
| **Sessions** | `secrets.token_urlsafe(32)`, new at every login; only `sha256(token)` is stored. Idle 2 h, absolute 24 h, at most 10 per account (oldest revoked). `resolve` joins `accounts` and rejects a session of a disabled account or one created before the last password change. Logout, `set-password` and `disable` delete sessions. `last_seen` is written at most once per 60 s. | `src/accounts.py` (`SessionAuthenticator`), `src/state.py` |
| **Auth interface** | `Authenticator` protocol (`resolve`, `start_session`, `logout`) and `Principal` (`account_id`, `username`). The service depends only on the protocol; P8 SSO can add another login class that calls the same `start_session`. | `src/interfaces.py`, `src/models.py` |
| **Workspace ids** | Anonymous: `<32 hex>` → collection `ws_<32 hex>` (v1.0). Account: `acct-<32 hex>` → collection `acct_<32 hex>`. `_collection_name` accepts these two forms and nothing else. Clients must treat ids as opaque. | `src/vectorstore.py` |
| **Ownership** | One `workspaces` row per account workspace (`account_id`, `created`, `deleted`). `new_workspace(session=…)` creates an `acct-` id and inserts the row only while the account has fewer than `MAX_WORKSPACES_PER_ACCOUNT` (default 10) live rows; otherwise `WorkspaceLimitReached`. `list_workspaces` returns the caller's live rows as `WorkspaceList`. Owner delete (`clear`) tombstones the row first (`deleted = 1`, the id is never reused), then clears the vectors. | `src/service.py`, `src/state.py` |
| **Retention** | Every `new_workspace` calls `store.cleanup(keep=live owned ids, purge=tombstoned ids)`, whatever the flag. `ws_*` older than 24 h are deleted as in v1.0; `acct_*` are never deleted by age, only when their id is in `purge`. | `src/service.py`, `src/vectorstore.py` |

### Authorisation rule

One function, `TrustRAGService._authorize(workspace, session)`, is the first statement of `list_files`, `count`,
`clear`, `upload` (before the size check, loader, OCR and scanner) and `ask` (before retrieval). It is always on,
whatever `ACCOUNTS_ENABLED` says. The token is checked first, so the answer to an invalid token does not depend on the
workspace id.

| Workspace id | Ownership row | Caller | Result |
|---|---|---|---|
| any | any | token that does not resolve (unknown, malformed, expired, revoked) | `NotAuthenticated` (401), never a fallback to anonymous |
| `acct-<hex>` | live, owner A | session of A | allowed |
| `acct-<hex>` | live, owner A | session of B, or anonymous | `WorkspaceNotFound` (404) |
| `acct-<hex>` | tombstoned or missing | anyone | 404 (fail closed) |
| `<hex>` | none | anonymous | allowed (v1.0 path) |
| `<hex>` | none | any session | 404 |
| `<hex>` | a row exists (not expected) | | handled like an `acct-` id |

`WorkspaceNotFound` is the same error as an unknown id, so a caller cannot tell "exists but not yours" from "does not
exist".

### HTTP API additions

Every route keeps the `X-API-Key` gate when `API_KEY` is set. The session token is accepted only in the
`Authorization: Bearer` header (never a query parameter or cookie); it is read from the request headers without a
declared parameter, so existing OpenAPI operations do not change. A malformed header or a token longer than 512
characters gives 401.

| Method, path | Request | Success | Errors |
|---|---|---|---|
| `POST /v1/auth/login` | raw body of at most 4096 bytes, JSON `{username, password}`, parsed by hand (no pydantic model, so no 422 echo) | `200 SessionInfo` | `401 {"detail":"Wrong username or password."}` for unknown user, wrong password, disabled account and every bad shape; `429 {"detail":"Too many sign-in attempts. Please try again later."}` + `Retry-After` |
| `POST /v1/auth/logout` | bearer | `204` (also for unknown or expired tokens) | |
| `GET /v1/auth/me` | bearer | `200 {"username": …}` | `401` + `WWW-Authenticate: Bearer` |
| `GET /v1/workspaces` | bearer required | `200 WorkspaceList` (own live workspaces) | `401` |
| `POST /v1/workspaces` | no bearer → anonymous workspace (v1.0); bearer → `acct-` workspace | `201 {"workspace": …}` | `401` invalid token; `409` at the per-account cap |
| `DELETE /v1/workspaces/{id}/documents` | optional bearer | `204`; removes the documents, keeps the workspace and its owner (no tombstone) | same `401` / `404` as below |
| existing workspace routes (documents, questions, `DELETE`) | optional bearer | unchanged | `401` for an invalid token, checked before the id; `404 "Workspace not found."` when not authorised, before the empty-workspace 409 and before any upload processing |

With `ACCOUNTS_ENABLED=false`, `/v1/auth/*` answer `404`, `GET /v1/workspaces` answers `405` with `Allow: POST`, and
the `Authorization` header is ignored, as in v1.0. `/metrics` records `/v1/auth/*` requests with `status="-"`, so
sign-in outcomes are not counted. uvicorn runs with `--no-proxy-headers`, and the login client key is the TCP peer
address (`X-Forwarded-For` is never read).

### Flag off and rollback

With `ACCOUNTS_ENABLED=false` no session can be presented, so account workspaces are unreachable but kept: cleanup
still passes `keep` / `purge`. Rolling back to v1.0 neither deletes them (v1.0 cleanup only touches `ws_*`) nor
exposes them (v1.0 rejects `acct-` ids). An `acct_` collection whose state row is lost is refused and not deleted
automatically; recovery is a human action.

## Trust boundaries

Everything that comes from a document or from a model is treated as **untrusted**:

| Boundary | Untrusted input | Controls |
|---|---|---|
| Upload | file bytes, file name | size and page limits, pdfium render cap, file-name sanitisation |
| Indexing | document text | two-layer injection scanner, fail-closed fallback, seam checks |
| Prompts | chunk text, file names, the question | HTML-escaping inside fixed tags (random tag names in the scanner), "data, not instructions" system rules |
| Verification | model output | quotes must exist verbatim; refusal only if the answer *is* the refusal sentence |
| Rendering | model output | images and link targets stripped before Markdown rendering |
| Access | network clients | localhost binding by default, optional `APP_PASSWORD` / `API_KEY` (unchanged outer gates), per-session workspaces, rate limits |
| Sign-in | username, password, raw login body, client address | body read by hand (size cap, fixed 401, no echo); canonical ASCII usernames; atomic per-user / per-client / global rate limit before any hashing; one scrypt per attempt (`DUMMY_HASH` for unknown users); bounded hashing slots; uvicorn `--no-proxy-headers` |
| Sessions | bearer token | random 256-bit tokens, new at each login; SHA-256 only at rest; header only; idle and absolute expiry; revoked by logout, password change and disable; per-account cap |
| Workspace ownership | workspace id, session | `_authorize` first in every workspace method, always on; 404 identical to an unknown id; `acct-` ids fail closed without a live owner row; tombstones; re-authorise after upload writes |
| State file (local mode) | anyone with file-system access | state directory 0700, DB and `-wal` / `-shm` 0600 (tightened on open); salted scrypt hashes; token hashes only |
| Server stores (server mode) | anyone who can reach Postgres or Valkey, driver error text, a host with a wrong clock | no published ports, `internal` backend network without the UI, password authentication (scram-sha-256 also inside the container), non-superuser role `trustrag_app` owning only schema `trustrag`, bound parameters only, fixed error text, store clocks; see [Storage modes](#storage-modes-p7) and THREAT_MODEL rows 43-63 |

The full analysis, including what is *not* defended against, is in [`THREAT_MODEL.md`](THREAT_MODEL.md).

## State

This section describes **local mode** (the default). Where each piece of state lives in server mode is in
[Storage modes](#storage-modes-p7).

| Data | Where | Lifetime |
|---|---|---|
| Chunks and embeddings, anonymous | ChromaDB, one collection per workspace (`ws_<random id>`) | deleted after 24 h of age, or on "Clear" |
| Chunks and embeddings, accounts | ChromaDB, one collection per workspace (`acct_<random id>`) | never deleted by age; deleted when the owner deletes the workspace (immediately, and again by the next cleanup via `purge`) |
| OCR and scan results | SQLite `cache` table, keyed by prompt version + file hash | until the prompt version changes |
| Rate-limit window | SQLite `calls` table | rolling hour, shared by all processes |
| Accounts | SQLite `accounts` table (canonical username, scrypt hash, `disabled`, `password_changed`) | until an administrator changes it; accounts are disabled, not deleted, in P6 |
| Sessions | SQLite `sessions` table (SHA-256 of the token, `created`, `last_seen`, `expires`) | `last_seen` is written only when at least 60 s stale (`touch_session`); until logout, expiry (pruned at login), password change, disable or the per-account cap |
| Workspace ownership | SQLite `workspaces` table (owner, `created`, `deleted` tombstone) | forever; tombstone rows are kept so ids are never reused |
| Login attempts | SQLite `login_attempts` table (hashed keys, timestamp, attempt id) | pruned once older than the longest window; each key is pruned against its own window |
| Metrics | process memory | process lifetime (scraped via `/metrics`) |

The state DB (`<CHROMA_DIR>/trustrag_state.sqlite`) was a cache in v1.0. Since P6 it also holds durable data
(accounts and ownership, ADR 0012), so it must be backed up with `chroma_db/` and must not be wiped as a cache: losing
it makes account workspaces unreachable (not deleted). On open, `CHROMA_DIR` is created or tightened to 0700 and the
DB, `-wal` and `-shm` files to 0600. All new tables are additive; the v1.0 tables are unchanged.

## Storage modes (P7)

TrustRAG has two storage modes. **Local mode is the default** and is the v1.0/P6 behaviour, unchanged: one host,
no server. **Server mode** is opt-in and lets several API containers, on one or more hosts, share documents, accounts
and rate limits. Decisions: [ADR 0013](docs/adr/0013-pgvector-vector-store-opt-in.md) (pgvector store),
[ADR 0014](docs/adr/0014-redis-for-shared-counters-and-caches.md) (Valkey counters, caches in Postgres),
[ADR 0015](docs/adr/0015-postgres-for-durable-account-state-in-server-mode.md) (durable account state in Postgres).
They amend ADRs 0003, 0005 and 0012 for server mode only.

| Mode | `VECTOR_STORE` | `STATE_BACKEND` | Needs | Hosts |
|---|---|---|---|---|
| **local** (default) | `chroma` | `sqlite` | nothing | one |
| **server** | `pgvector` | `redis` | `DATABASE_URL` (`postgresql://` or `postgres://`), `REDIS_URL` (`redis://` or `rediss://`) | many |

`Settings` accepts only these two combinations. Any other combination, or a server mode without both URLs, fails at
startup with a fixed sentence that names the setting and never repeats a URL (`hide_input_in_errors`, URL fields with
`repr=False`, and a hand-built validation error with no `input`). "Redis" in setting and module names means the Redis
protocol; the server in compose and CI is Valkey 9.1.3 (ADR 0014).

### Components

```mermaid
flowchart LR
    SVC[TrustRAGService] -.-> VS[[VectorStore]]
    EVAL[eval/run_eval.py] --> FAC
    SVC -->|store=None| FAC[src/storage.py<br/>make_vector_store]
    FAC -->|chroma| CH[ChromaStore<br/>src/vectorstore.py]
    FAC -->|pgvector| PGS[PgVectorStore<br/>src/pgvector_store.py]
    SVC & ACC[accounts] & LLM[llm] & SCAN[scanner / ocr] --> ST[src/state.py<br/>dispatch on STATE_BACKEND]
    ST -->|sqlite| SQ[(SQLite WAL file)]
    ST -->|redis: call window,<br/>login attempts| SR[src/state_redis.py] --> RD[(Valkey<br/>Lua scripts, TIME)]
    ST -->|redis: accounts, sessions,<br/>ownership, caches| SP[src/state_postgres.py]
    PGS -->|"transaction()"| SP
    SP --> PG[(Postgres 17 + pgvector 0.8.7<br/>schema trustrag)]
    CH --> CHD[(ChromaDB embedded)]
    PGS -. same MiniLM embeddings in-process .-> EMB[ChromaDB DefaultEmbeddingFunction]
```

| Module | Role |
|---|---|
| `src/storage.py` | `make_vector_store()` reads `config.VECTOR_STORE` and returns `ChromaStore` or `PgVectorStore`. Used by `TrustRAGService(store=None)` and by `eval/run_eval.py`. Imports lazily. |
| `src/state.py` | Unchanged public functions (names, signatures, return shapes). Each one dispatches on `config.STATE_BACKEND`, read at call time: SQLite code as before, or `state_redis` / `state_postgres`. In server mode no SQLite file is opened. Defines `StorageUnavailable`. |
| `src/state_redis.py` | Model-call window and login reservation as Lua scripts, login refund as one `MULTI`/`EXEC`. One client per process (rebuilt after a fork or a URL change), connect and socket timeouts of `REDIS_TIMEOUT_SECONDS` (5 s). |
| `src/state_postgres.py` | Accounts, sessions, ownership, tombstones and caches. Owns the shared connection handling: `transaction()` yields a pooled autocommit connection inside one transaction block. |
| `src/pgvector_store.py` | `PgVectorStore`, the server `VectorStore`. Opens no connections of its own; it uses `state_postgres.transaction()`. |

`psycopg` and `redis` are imported inside functions only, so local mode runs without them being importable
(`tests/test_storage_settings.py::test_local_mode_never_imports_psycopg_or_redis`).

### Where each piece of state lives

| Data | Local mode | Server mode | Clock in server mode | Lifetime in server mode |
|---|---|---|---|---|
| Chunks and embeddings | ChromaDB, one collection per workspace | Postgres `chunks` (shared table, `workspace` column, `embedding vector(384)`) + `vector_workspaces` (one row per workspace, `created`) | DB clock (`created`, 24 h cutoff) | as local: anonymous workspaces deleted 24 h after `created` unless in `keep`; account workspaces only when in `purge` |
| Model-call window | SQLite `calls` | Valkey ZSET `<prefix>calls` | Valkey `TIME` | rolling window; key expires with the window (`PEXPIRE`) |
| Login attempts | SQLite `login_attempts` | Valkey ZSET `<prefix>login:<key>` per rate-limit key (key is `sha256(...)` or `global`) | Valkey `TIME` | each key pruned against its own window; key expires with its window |
| OCR and scan caches | SQLite `cache`, no expiry | Postgres `cache` | DB clock | 24 h (`SERVER_CACHE_TTL_HOURS`): older rows ignored on read, deleted on each write |
| Accounts | SQLite `accounts` | Postgres `accounts` | DB clock (`created`, `password_changed`) | until an administrator changes it |
| Sessions | SQLite `sessions` | Postgres `sessions` (+ `seq` identity column for the cap tie-break) | DB clock | idle 2 h, absolute 24 h, cap 10, as P6 |
| Workspace ownership + tombstones | SQLite `workspaces` | Postgres `workspaces` | DB clock (`created`) | forever; tombstones kept |
| Metrics | process memory | process memory (per container) | - | process lifetime |

`<prefix>` is `config.REDIS_PREFIX` (`trustrag:{t}:`; `{t}` is a hash tag, so every key of one deployment maps to one
slot). Valkey holds only these two counter families, so its memory is bounded by the rate limits, not by documents.
Postgres tables live in schema `config.PG_SCHEMA` (`trustrag`, a module constant, not a setting), owned by the role
`trustrag_app`. `docker/postgres-init.sh` (run once, as the superuser, on an empty volume) creates the `vector`
extension in `public`, the role (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, password read from a
secret file), the schema, and revokes `CREATE` on `public` from `PUBLIC`. The application creates its tables with
`CREATE TABLE IF NOT EXISTS` under a Postgres advisory lock, so two containers starting at once do not race
(`test_concurrent_schema_initialisation_succeeds`). It never creates the schema: `state_postgres.ensure_schema` refuses
(`StorageUnavailable`) when the schema is missing, so a wrong schema cannot put the state tables into `public`.
`PgVectorStore` creates its two tables under its own advisory lock **without** that schema check (flagged for review in
ADR 0013 Evidence); with the compose init script the app role cannot create tables in `public` anyway.

Backups in server mode mean the Postgres database (documents and durable state). Valkey holds only counters; losing it
resets the rate limits. Switching modes migrates nothing: local data stays on disk, untouched and hidden.

### Connections and SQL

- Autocommit connections; every operation is one explicit transaction block (`conn.transaction()`), committed on
  success and rolled back on any exception.
- A standard-library bounded pool per process: at most `PG_MAX_CONNECTIONS_PER_PROCESS` (10) connections, a caller
  waits at most `PG_POOL_WAIT_SECONDS` (5 s), then `StorageUnavailable`. A connection that failed, or is not idle after
  its transaction, is closed instead of returned. After a fork the child never reuses the parent's sockets.
- Each new connection sets `search_path = trustrag, public` (identifier bound with `psycopg.sql.Identifier`) and
  `lock_timeout` 5 s, `statement_timeout` 15 s, `idle_in_transaction_session_timeout` 30 s, so a stuck row lock ends in
  a 503, not a hung worker.
- SQL text is constant: values are always bound parameters (static scan:
  `test_state_modules_build_no_sql_from_values`). Every `chunks` statement has a `workspace = %s` predicate, and the
  workspace id is validated with the same rules as ChromaDB (`validate_workspace_id`, shared with `src/vectorstore.py`)
  before any SQL runs.

### How the P6 guarantees are kept in server mode

| Guarantee | Server implementation |
|---|---|
| Call window check-and-record | one Lua script on Valkey `TIME`: prune, count, refuse or `ZADD`, `PEXPIRE` |
| Login reservation over user / client / global keys | one Lua script over all keys; each key pruned against its own window; if any key is full nothing is inserted; otherwise the same attempt id is added to every key |
| Refund only this attempt | `ZREM <attempt id>` per key in one `MULTI`/`EXEC` |
| Script replies | always `{status, seconds-as-string}`; Python parses the string and floors every refusal at 1 s, so a refusal can never read as `0` = allowed |
| `create_session(expected_hash)` re-check + cap | one transaction: `SELECT … FOR UPDATE` on the account row, refuse if missing, disabled or hash changed, insert, delete beyond the newest `max_per_account` by `created DESC, seq DESC` |
| Rehash compare-and-set | single conditional `UPDATE` |
| Set password / disable + revoke sessions | one transaction (`UPDATE` takes the row lock, then `DELETE` sessions) |
| Workspace cap | one transaction under `pg_advisory_xact_lock(sha256("ws:" + account_id))` |
| Tombstone, never reuse | single conditional `UPDATE`; rows never deleted |
| Chunk never without its workspace row | `add_chunks` upserts the `vector_workspaces` row first (taking its row lock) and the chunks in the same transaction; `clear` deletes the row, then the chunks; `cleanup` also deletes chunks whose workspace row is gone |

### Clocks

In server mode the stores' clocks are authoritative and the `now` arguments passed to `src/state.py` are ignored, so a
host whose clock is fast or slow can neither prune other hosts' rate-limit rows nor shorten, extend or age out
sessions, caches or anonymous workspaces:

- Valkey scripts read `TIME`.
- Postgres statements use `clock_timestamp()` (the current time when the statement runs, not the transaction start).
- A new session keeps the **remaining** lifetime the caller asked for: `expires = DB time + (caller's expires − caller's
  time.time())`. `SessionInfo.expires_at` returned to the client is computed on the API host and is informational.
- `get_session_principal` judges expiry on the DB clock and returns `last_seen` translated into the caller's clock, so
  the caller's "touch at most every 60 s" rule still works.

`config.STATE_CLOCK` (`"store"`) is a module constant, not a `Settings` field, so it cannot be changed from the
environment (`test_state_clock_override_is_not_an_environment_setting`); only tests that freeze time set it to
`"caller"`. Clock tests: `test_skewed_host_cannot_prune_other_hosts_attempts`,
`test_skewed_host_cannot_extend_or_expire_sessions`, `test_skewed_host_cannot_age_out_anonymous_workspaces`.

### Failure handling (fail closed)

A Postgres or Valkey error, timeout, full pool or unexpected script reply becomes `StorageUnavailable`. It is not a
`ValueError` (which the API maps to 404) and not an `LLMError` (which would let the injection scanner fall back to rules
only). Its message is fixed; the driver error, whose text can contain hosts, users or passwords, is kept only as
`__cause__` and never shown.

| Where | What the caller sees |
|---|---|
| Any API route except login | `503 {"detail":"Storage is temporarily unavailable. Please try again."}` (`api/main.py` handler) |
| `POST /v1/auth/login` | the existing `429 {"detail":"Too many sign-in attempts. Please try again later."}` with `Retry-After: 1`, the same for unknown user, wrong and right password; logged as `auth.storage_unavailable` with no username |
| Refund after the session was created | logged as `auth.refund_failed` and swallowed; the user keeps the session that was issued |
| Model-call limit during upload | `StorageUnavailable` propagates out of the scanner, so the upload is refused with 503 instead of being scanned by rules only |
| Cache read | an error is an error, never treated as a cache miss |
| Streamlit UI (in-process) | the same fixed message (`st.error`), then the run stops; no traceback |
| Admin CLI | `Error: Storage is temporarily unavailable. Please try again.`, exit 1 |

Local mode never raises `StorageUnavailable`. Tests: `tests/test_server_state.py` (`test_storage_errors_give_fixed_503_without_driver_text`,
`test_redis_down_login_gives_same_429_and_runs_no_kdf`, `test_postgres_down_login_response_is_same_for_unknown_wrong_and_right`,
`test_refund_failure_after_session_does_not_503`, `test_call_limit_store_error_aborts_upload_not_rules_only`,
`test_cache_error_is_not_treated_as_a_miss`, `test_ui_storage_unavailable_shows_fixed_message`,
`test_admin_cli_storage_error_prints_fixed_message`, `test_stuck_account_lock_times_out_with_503_not_hang`,
`test_pool_exhaustion_gives_503_not_hang`).

### Differences from the P7 plan

What was built differs from `docs/phases/p7-plan.md` and the ADR texts in these points:

| Plan / ADR text | Built | Status |
|---|---|---|
| DB clock as `extract(epoch from now())` (ADR 0015) | `clock_timestamp()` | recorded in ADR 0015 Evidence |
| `create_session` keeps the caller's lifetime `expires - created`, re-based on the DB clock (ADR 0015) | re-bases the caller's **remaining** lifetime `expires - time.time()` (differs by the milliseconds between computing `expires` and the insert) | recorded in ADR 0015 Evidence |
| `vector_workspaces` row created "on first access to a workspace", like `get_or_create_collection` (ADR 0013) | created only by `add_chunks`; reads never create it. An anonymous workspace's 24 h age therefore starts at its first upload on pgvector, and at its first access of any kind on ChromaDB | recorded in ADR 0013 Evidence; **flagged for the human gate** |
| `ON CONFLICT DO NOTHING` for the workspace row (ADR 0013) | `ON CONFLICT … DO UPDATE SET created = vector_workspaces.created` (a no-op update that takes the row lock, serialising `add_chunks` with `clear`) | recorded in ADR 0013 Evidence |
| "One connection per thread, no pool package" (ADR 0013, first bullet) and "bounded per-process pool" (same ADR, later bullet) | bounded stdlib pool per process | consistent with the later bullet |
| Per-test schema for tests (`config.PG_SCHEMA` patched) | as planned for tests; `PG_SCHEMA` is a module constant, so `make eval-free-pgvector` always uses schema `trustrag` of whatever database `DATABASE_URL` names (it clears every workspace it creates) | recorded in ADR 0013 Evidence |
| Login storage error → existing 429 + `Retry-After` | `Retry-After: 1` (`LoginRateLimited(1.0)`) | as planned |

## Observability

Every log line is JSON with a request ID that follows one upload or question through every stage, including
worker threads. Each Claude call logs its purpose, prompt version, model, tokens, cost and latency - never
document text or answers. Sign-in events (`auth.login_failed`, `auth.rate_limited`, `auth.busy`, `auth.logout`) carry
no submitted username; `auth.login_succeeded`, `auth.logout`, `workspace.created` and `workspace.deleted` carry the
`account_id`, never a token or username. Passwords, tokens and token hashes are never
logged. `/metrics` exposes counters and stage timings in Prometheus format. Prompt
versions (`config.PROMPT_VERSIONS`) are also stored with every evaluation run, so a behaviour change can be
traced to the prompt change that caused it.

## Evaluation

The evaluation suite (`eval/`) is a client of the pipeline modules. It is not part of the product and is not in the
Docker image. It runs two benchmarks. The first is the self-authored suite, whose output format is unchanged since
v1.0. The second is a public benchmark on 50 papers from Qasper (dev split, CC BY 4.0). Every run is also appended to
a history file, which feeds a trend report.

```mermaid
flowchart LR
    DL["eval/qasper_download.py<br/>(network, human gate)"] -->|verified subset| SUB[(subset.json<br/>outside the repo)]
    MAN[eval/data/qasper_manifest.json<br/>pinned URL + hashes] -.-> DL & QL
    RUN[eval/run_eval.py] --> SELF[self-authored suite]
    RUN --> QL[eval/qasper.py] --> SUB
    RUN --> RJ[results.json + RESULTS.md]
    RUN --> H[history.jsonl<br/>append one line] --> TR[eval/trend.py] --> TM[TRENDS.md]
```

| Part | What it does | Key files |
|---|---|---|
| **Download** (once per machine) | Uses only the standard library. Accepts HTTPS only, with a verified TLS context, and also refuses a non-HTTPS redirect. Applies a 200 MiB byte cap while reading. The archive is held in memory and its SHA-256 must match the manifest. A gzip archive is decompressed with a 512 MiB byte cap (`MAX_DECOMPRESSED`) before any tar parsing. Exactly one allow-listed tar member is read with `extractfile`. That member must be a regular file within the size cap, in an archive of at most 50 members with no unsafe names. Nothing is written to disk from a member name. The script selects the first 50 sorted paper ids with an answerable question and writes only that subset, as canonical JSON, atomically, with mode 0600. The downloader creates the data directory with mode 0700. It refuses a data directory that is a symlink, and an existing directory that is not owned by the current user or that holds anything other than `subset.json`. Immediately before writing the subset it re-checks the data directory (outside the repository, not a symlink, owner, contents), in case it was swapped during the download. Any of these refusals prints a friendly error and exits 1. The first run needs `--record-hash`, which pins the archive hash, subset hash and paper ids in the manifest (trust on first use). A human reviews the manifest and commits it. The script refuses `--record-hash` when a hash is already pinned. | `eval/qasper_download.py`, `make eval-download-qasper` |
| **Data directory** | `~/.cache/trustrag/qasper/` by default, overridden by `TRUSTRAG_EVAL_DATA_DIR`. The read path (`resolve_data_dir`) only resolves and validates the path; it never creates the directory or changes its mode (only the downloader does). A path inside the repository is refused by an identity check: `os.path.samefile` compares the path and each existing ancestor with the repository root, which also covers case-insensitive filesystems, symlinks and hardlinks. A path that cannot be resolved (for example a symlink loop) is refused with a friendly `QasperDataError`, not a traceback. This keeps the data out of git, the Docker context and the invisible-character scan. | `eval/qasper.py` |
| **Loader** | On every load it re-hashes `subset.json` against the manifest and stops on a mismatch. It parses the file as plain JSON, rejecting NaN and Infinity. Fields are validated one by one: a malformed paper is skipped and counted in `rejected_records`, and its text is never echoed. One malformed question rejects the whole paper record; this is documented behaviour. The manifest is validated too: `dataset`, `version`, `split` and `licence` must match `[A-Za-z0-9 ._()-]{1,64}`, and `source_url` must be HTTPS, printable ASCII, with no spaces and none of `|<>[]()`. `citation` is never rendered and is unconstrained. Paper and question ids must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. The module does no network I/O. | `eval/qasper.py` |
| **Mapping to TrustRAG** | Each paper becomes `Page`s with no PDF rendering: page 1 is the title and abstract, then one page per section. The file name is `sanitize_filename("qasper-<id>.pdf")`, so citations still read `[file, p.N]`. Each paper gets its own workspace, built with `chunk_pages` and indexed through `VectorStore`, then cleared after its questions. Retrieval uses the default settings with rerank off. | `eval/qasper.py` |
| **Question rules** | A question is *unanswerable* if every annotator marked it so. It is *answerable* if every annotator answered it and at least one text evidence paragraph remains after dropping `FLOAT SELECTED` (figure or table) evidence. Every other question is excluded and counted by reason: `mixed_answerability`, `figure_or_table_only` or `no_evidence`. Gold evidence is the union of all annotators' text paragraphs. | `eval/qasper.py` |
| **Scoring** | `evidence_hit(chunk, paragraph, min_chars=200)` is a pure function. After normalisation, a chunk hits if it contains the whole paragraph, or if their longest common substring is at least `min(len(paragraph), 200)` characters. 200 is above the 150-character chunk overlap. From these hits the suite computes recall@k and MRR over answerable questions. With `--suite all`, answers are generated without fact-checking to compute correct-refusal and false-refusal rates. | `eval/metrics.py`, `eval/qasper.py` |
| **Run integration** | `run_eval.py` sets `ANONYMIZED_TELEMETRY=False` before ChromaDB is imported. This applies to the eval process only; the product setting is unchanged. Results gain a `public_benchmark` block (dataset, version, split, licence, source URL, subset hash, metrics, exclusion counts). RESULTS.md gains a matching section after all existing sections, so earlier output stays byte-identical. `preflight_qasper()` loads the manifest, resolves the data directory and verifies an existing subset before any paid suite runs. A `QasperDataError` or `OSError` there ends the run with a non-zero exit before any paid suite runs and before any results are written. If the Qasper step itself fails later, after the paid suites (any exception), the paid results are kept: the block becomes `{status: "error", reason: "The Qasper benchmark step failed."}` (a fixed string, with no exception text or paths), and the run still writes `results.json`, RESULTS.md, a history line (without Qasper metrics or the `qasper` dataset hash) and TRENDS.md, then exits non-zero. If the subset is missing or not yet pinned, the block has `status: "skipped"` with a reason and the run continues with exit 0. | `eval/run_eval.py` |
| **History** | After every run, one strict-ASCII JSON line is appended to the history file. The line holds the run time, commit, a dirty-tree flag, suite, models, prompt versions, settings, dataset hashes and a fixed list of metrics. Earlier lines are never rewritten. Each entry is validated before it is written and again on load (patterned fields use full matches, so a trailing newline is rejected), and any bad line fails the load with its line number. The first entry was backfilled from the committed `results.json`. | `eval/history.py`, `eval/results/history.jsonl` |
| **Trend report** | A static, deterministic Markdown report with the last 10 runs per metric, newest first, and the change from the previous run. The change is marked "(dataset changed)" only when the relevant dataset hash changed: the `qasper` hash for `qasper.*` metrics, the `self_authored` hash otherwise. It contains only validated numbers, hashes, enum values and timestamps. It is regenerated after each run and by `make eval-trend` (no network). | `eval/trend.py`, `eval/results/TRENDS.md` |

Decisions: [ADR 0009](docs/adr/0009-public-benchmark-data-handling.md) (public benchmark data handling) and
[ADR 0010](docs/adr/0010-eval-history-jsonl-and-static-trend-report.md) (history file and static trend report). The
related threats are in [`THREAT_MODEL.md`](THREAT_MODEL.md).

## Deployment shapes

| Shape | Command | When |
|---|---|---|
| Single process | `streamlit run app.py` | local use, demos |
| One container | `docker run ... trustrag` | local use without a Python setup |
| API + UI | `docker compose up` | separate backend; other clients can use the API |
| Server mode, two API containers | `docker compose --profile server up -d --build postgres redis api-server-1 api-server-2` (plus `ACCOUNTS_ENABLED=true` in `.env.server`; the default `api`/`ui` have no profile and are not started this way) | several API containers sharing documents, accounts and rate limits (P7) |

In every shape, accounts are off unless `ACCOUNTS_ENABLED=true`, and accounts are created with
`python -m src.accounts create <username>`, run as the service user so the 0600 state file keeps its owner (in server
mode the CLI must run with the server settings, for example `docker compose exec api-server-1 python -m src.accounts
…`, and writes to Postgres). The API
container runs uvicorn with `--no-proxy-headers`. In compose, every UI sign-in reaches the API from the UI container's
address, so the per-username limit is the main brute-force control there (see [`THREAT_MODEL.md`](THREAT_MODEL.md)).

In local mode ChromaDB runs embedded, so one API process owns a data directory. Scaling out uses server mode
(see [Storage modes](#storage-modes-p7)).

### Default compose and the `server` profile

```mermaid
flowchart LR
    subgraph default["default network"]
        UI[ui :7860] --> API[api :8000<br/>local mode]
        A1[api-server-1 :8001]
        A2[api-server-2 :8002]
    end
    subgraph backend["backend network (internal: true)"]
        PG[(postgres<br/>pgvector 0.8.7-pg17)]
        VK[(redis service<br/>Valkey 9.1.3)]
    end
    API --> VOL[(trustrag-data volume<br/>ChromaDB + SQLite)]
    A1 & A2 --> PG & VK
```

| Service | Profile | Networks | Published port | Settings and secrets |
|---|---|---|---|---|
| `api` | none (default) | default | `127.0.0.1:8000` | `.env`; local mode; unchanged by P7 |
| `ui` | none (default) | default | `127.0.0.1:7860` | `.env`; client of `api`; never sees backend URLs and is not on `backend` |
| `postgres` | `server` | `backend` only | none | superuser password via `POSTGRES_PASSWORD_FILE` (compose file secret); `POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256`; `docker/postgres-init.sh` creates the extension, role and schema; volume `trustrag-pgdata` |
| `redis` (Valkey) | `server` | `backend` only | none | `docker/redis.conf`: `appendonly yes`, `appendfsync everysec`, `maxmemory 64mb`, `maxmemory-policy noeviction`, `include` of the secret file holding `requirepass`; healthcheck reads the password into `REDISCLI_AUTH`; volume `trustrag-redisdata` |
| `api-server-1`, `api-server-2` | `server` | `backend` + default | `127.0.0.1:8001`, `127.0.0.1:8002` | `.env` + `.env.server` (backend URLs, read by these two services only); `VECTOR_STORE=pgvector` and `STATE_BACKEND=redis` set explicitly in compose |

`docker compose up` without a profile starts only `api` and `ui`, exactly as before P7. Images other than the locally
built `trustrag:latest` are pinned by digest, in compose and in the CI `services:` (`test_docker_and_ci_use_pins`).
Secrets for the server profile are three files in `$TRUSTRAG_SECRETS_DIR` (default `~/.trustrag/server-secrets`):
`pg_superuser_password`, `pg_app_password`, `redis_auth.conf`. No password appears on a command line in compose.
`.env.server` is git- and docker-ignored. The two server API containers exist so the verifier can run
`tests/integration/test_multi_host.py` against two real containers (P7-AC2); there is no server-mode UI service.

CI (`.github/workflows/tests.yml`) runs the same Postgres and Valkey images as job service containers with throwaway
passwords and sets `TRUSTRAG_TEST_DATABASE_URL` / `TRUSTRAG_TEST_REDIS_URL`, so the `[server]` test cases run in CI
instead of skipping. Each server test gets its own schema `test_<32 hex>` and key prefix `trustrag-test:<32 hex>:`,
dropped afterwards only if they match those patterns and the test URL differs from `DATABASE_URL`. The two-container
integration test is not run in CI.
