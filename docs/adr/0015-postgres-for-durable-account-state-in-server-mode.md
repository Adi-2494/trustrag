# 0015 - Durable account state (accounts, sessions, ownership, tombstones) in Postgres in server mode (amends 0005 and 0012)

**Status:** Accepted (P7; approved by the human, 2026-11-05). Drafted 2026-11-05 and revised the same day after the security review of the plan,
`docs/phases/p7-plan.md` section 8, findings 2, 5, 6, 9). SPEC-GAP-062 decided by the human on 2026-11-05: option (a), Postgres. SPEC P7 names Redis for "state"; this
ADR keeps durable records out of Redis and puts them in the same Postgres server as pgvector. It amends ADR 0012
("Workspace ownership in SQLite") and ADR 0005 for server mode only; SQLite stays the default.

## Context
- SPEC-GAP-043 (resolved by the human, 2026-11-04): accounts, sessions and ownership stay in SQLite via
  `src/state.py`; "do not put them in Redis without an ADR".
- P7 goal: remove the single-host limit. A SQLite file can be shared by processes on **one** host only. With two API
  containers on two hosts, each would have its own accounts, sessions and ownership rows: a session issued by host A
  would be unknown to host B (401), and a workspace created on A would be 404 on B (ADR 0012 fails closed). Safe, but
  multi-host accounts would not work.
- The P6 guarantees that must survive on any backend (ADR 0011, ADR 0012, SPEC-GAP-060):
  1. `create_session(…, expected_hash)` re-checks `disabled` **and** that the password hash is unchanged in the same
     transaction that inserts the session, and enforces the per-account cap (oldest revoked).
  2. `rehash_password` is a compare-and-set on the old hash; it does not bump `password_changed` or revoke sessions.
  3. `set_password_hash` and `set_disabled(True)` change the account and delete its sessions in one transaction.
  4. `add_owned_workspace` counts live rows and inserts under one lock (workspace cap).
  5. `tombstone_workspace` is a single conditional update; tombstone rows are never removed; ids are never reused.
  6. `get_session_principal` joins `accounts` (disabled, `created < password_changed` rejected).
- Redis is an in-memory store whose durability depends on persistence settings and whose eviction can drop keys.
  Guarantees 1 and 3 need an atomic read of `accounts` and a write of `sessions`; with accounts and sessions in
  different stores this is not possible.

## Decision
- **In server mode (`STATE_BACKEND=redis`) the durable tables live in Postgres** (`DATABASE_URL`, the same server as
  pgvector), in a new module `src/state_postgres.py`, dispatched from the unchanged public functions of
  `src/state.py`. **Sessions stay with accounts** (Postgres, not Redis), so guarantees 1, 3 and 6 remain
  single-database transactions. Nothing durable goes to Redis.
- **Schema:** the same tables and columns as `src/state.py` `_P6_SCHEMA` (`accounts`, `sessions`, `workspaces`),
  same constraints (`username UNIQUE`, primary keys), plus `sessions.seq BIGINT GENERATED ALWAYS AS IDENTITY` to
  replace SQLite's `rowid` tie-break in the session cap. Timestamps stay epoch seconds (`DOUBLE PRECISION`), but in
  server mode they come from the **database clock** (`extract(epoch from now())`), not from the API host
  (SPEC-GAP-070, review finding 2): session `created`, `last_seen`, `expires`, idle and absolute expiry checks,
  `password_changed`, workspace `created` and pruning. The `now` arguments of the `src/state.py` functions are
  ignored in server mode unless `config.STATE_CLOCK == "caller"` (tests only, not an environment setting).
  `create_session` keeps the caller's lifetime (`expires - created`) and re-bases it on the database clock;
  `SessionInfo.expires_at` returned to the client is computed on the API host and is informational only. `login_attempts` and `calls` are not created in Postgres (they are in Redis); the `cache` table is (ADR 0014).
  Created at startup with `CREATE TABLE IF NOT EXISTS` under an advisory lock (no migration tool in P7,
  SPEC-GAP-077). Tables live in schema `trustrag`, owned by the non-superuser role `trustrag_app`
  (SPEC-GAP-086); connection handling (autocommit with explicit transaction blocks, rollback and discard on error,
  lock / statement / idle-in-transaction timeouts, bounded pool) is the same as in ADR 0013 (SPEC-GAP-085), so a
  stuck account row lock ends in `StorageUnavailable` instead of a hung login.
- **How each guarantee is kept** (Postgres default isolation, READ COMMITTED, with explicit locks):
  | Guarantee | Postgres implementation |
  |---|---|
  | 1 create_session | one transaction: `SELECT disabled, password_hash FROM accounts WHERE account_id = %s FOR UPDATE`; refuse if missing, disabled or hash ≠ `expected_hash`; `INSERT` session; `DELETE` sessions of the account beyond the newest `max_per_account` (by `created DESC, seq DESC`). The row lock serialises it against guarantee 3 and against concurrent logins of the same account |
  | 2 rehash CAS | single `UPDATE accounts SET password_hash = %s WHERE account_id = %s AND password_hash = %s`; Postgres re-evaluates the `WHERE` on the newest row version after waiting for a concurrent writer, so a reset that committed first makes it a no-op |
  | 3 set password / disable | one transaction: `UPDATE accounts …` (takes the row lock) then `DELETE FROM sessions WHERE account_id = %s` |
  | 4 workspace cap | one transaction: `pg_advisory_xact_lock(<64-bit key derived from sha256("ws:" + account_id)>)`, count live rows, insert or refuse. An advisory lock is used because the account row may not exist in tests with a fake authenticator |
  | 5 tombstone | single `UPDATE workspaces SET deleted = 1 WHERE workspace = %s AND account_id = %s AND deleted = 0` |
  | 6 resolve | the same join query; expired rows deleted; `touch_session` is a single guarded `UPDATE` |
- **Account CLI** (`python -m src.accounts …`) uses the same dispatch, so in server mode it must run with the server
  settings (for example `docker compose exec api python -m src.accounts …`). Unchanged commands.
- **Errors:** driver errors become `StorageUnavailable` (fixed message, 503) except the unique violation on
  `accounts.username`, which stays `DuplicateAccount`. On the login path every storage error becomes the existing
  429 body (SPEC-GAP-082), and a refund failure after the session was created is logged and swallowed (ADR 0014).
  The admin CLI prints a fixed message and exits 1.

## Consequences
- Accounts, sessions and ownership work across hosts; every P6 guarantee is one Postgres transaction, tested against
  both backends with real concurrency (plan section 3.2).
- File-permission controls of SPEC-GAP-044 (0700 / 0600) do not apply in server mode; password hashes, session-token
  hashes and ownership rows are protected by the database credentials and by not publishing the database port. This
  changes a THREAT_MODEL control (rows 27, 29): human gate.
- Backups now mean the Postgres database (vectors and durable state), not `chroma_db/`. Losing it has the same effect
  as losing the SQLite state DB: account workspaces become unreachable.
- No data migration: switching to server mode starts with no accounts; existing SQLite accounts and ChromaDB data stay
  on disk untouched and hidden (SPEC-GAP-074), like the v1.0 rollback in ADR 0012.
- If the human rejects this ADR, the conservative alternative (SPEC-GAP-062 option b) is: durable tables stay in
  SQLite and server mode refuses `ACCOUNTS_ENABLED=true` at startup, so P7 would remove the single-host limit for
  anonymous use only.

## Evidence
Read while planning: `src/state.py` (every P6 function and its transaction), ADR 0011, ADR 0012, SPEC-GAP-043,
SPEC-GAP-060, `tests/fakes.py::FakeAuthenticator` (account ids without `accounts` rows), `src/service.py`
(`_authorize`, `new_workspace`, `clear`).

Not verified: the database-clock functions and timeout parameters. Postgres `FOR UPDATE` / READ COMMITTED re-check behaviour and `pg_advisory_xact_lock` were not run here
(no Postgres available); they are from memory of the PostgreSQL documentation and are the subject of the concurrency
tests in `docs/phases/p7-plan.md` section 3.2.
