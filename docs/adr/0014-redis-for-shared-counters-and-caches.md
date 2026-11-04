# 0014 - Opt-in Redis backend for shared rate limits; caches in Postgres (amends 0005)

**Status:** Proposed (P7, 2026-11-05; revised the same day after the security review of the plan,
`docs/phases/p7-plan.md` section 8, findings 2, 3, 8, 9, 12, 14). Amends ADR 0005 if accepted; SQLite stays the
default and ADR 0005 is not superseded. Changing the status of ADR 0005 is a human action. (The file name keeps the
first draft's wording; after the review the caches no longer live in Redis.)

## Context
- SPEC P7: "state backend on Redis; selected by settings"; SQLite stays the default (SPEC-GAP-009).
- P7-AC2: the rate limit is shared across two API containers (integration test).
- ADR 0005 consequence: SQLite is "not shared across *hosts*; a multi-host deployment would swap this module for
  Redis behind the same functions."
- `src/state.py` today holds, in one SQLite file:
  - **Counters:** the app-wide model-call window (`calls`, `try_acquire_call`) and the login rate-limit reservations
    (`login_attempts`, `reserve_login_attempt` / `refund_login_attempt` / pruning).
  - **Caches:** OCR page text and scan verdicts (`cache`, `cache_get_many` / `cache_set_many`), no expiry.
  - **Durable records:** accounts, sessions, ownership, tombstones (ADR 0011 / 0012; ADR 0015 for server mode).
- Every caller uses the module-level functions of `src/state.py`. Tests switch state per test (`config.STATE_DB`)
  and freeze time by patching `time.time` (`tests/conftest.py`).
- `src/llm.py::call_claude` treats any truthy return of `try_acquire_call` as "refused" (`if wait:`), so a refusal
  that returns `0` would be an allow.
- Security review of the plan: with caller-supplied time, a host whose clock runs ahead prunes other hosts' rows
  (`ZREMRANGEBYSCORE … now - window`), and the 1-second global window is sensitive to sub-second skew (finding 2);
  `noeviction` without `maxmemory` plus never-expiring cache hashes lets caches grow until counters can no longer be
  written, which blocks sign-in (finding 3); Redis converts Lua numbers to integer replies, so a fractional wait can
  become `0` (finding 8, unconfirmed); a Redis error mapped to `LLMError` would silently degrade the injection scanner
  to rules-only (finding 14).
- `redis` (redis-py) is not installed in `.venv` and not in the locks.

## Decision
- **The public function set of `src/state.py` is the interface.** No new Protocol. `state.py` keeps its SQLite code
  and dispatches each public function by `config.STATE_BACKEND`, read at call time: `sqlite` (default) or `redis`.
  In `redis` mode no SQLite file is created.
- **Redis holds only the counters** (`REDIS_URL`; keys under `config.REDIS_PREFIX`, default `trustrag:{t}:`, one hash
  tag so every script stays in one slot; single node only, SPEC-GAP-080):
  | Data | Redis shape | Atomicity |
  |---|---|---|
  | Model-call window (`try_acquire_call`) | ZSET `calls`, member = random id, score = time | one Lua script: prune older than window, count, oldest, refuse or `ZADD`; `PEXPIRE` = window |
  | Login reservation (`reserve_login_attempt`) | ZSET `login:<key>` per key, member = attempt id | one Lua script over all keys: each key pruned against **its own** window only, counted; if any key is at its limit nothing is inserted; otherwise the same attempt id is added to every key and each key gets `PEXPIRE` = its window |
  | Login refund (`refund_login_attempt`) | `ZREM` of the attempt id per key | one `MULTI`/`EXEC` |
- **Caches live in Postgres in server mode** (same server as ADR 0013/0015): table
  `cache(namespace, key, value TEXT, created DOUBLE PRECISION, PRIMARY KEY (namespace, key))`, written with one
  multi-row upsert per call, read with one query. Entries expire after `config.SERVER_CACHE_TTL_HOURS` (24 h,
  SPEC-GAP-084): reads ignore older rows and each write deletes expired rows. This keeps Redis memory bounded and
  independent of document volume, and limits how long OCR page text outlives a deleted workspace. Local mode keeps
  the v1.0 cache without expiry (documented residual).
- **Authoritative clock in server mode** (SPEC-GAP-070, revised): the scripts read Redis `TIME`; the `now` argument
  passed by the caller is ignored. Every host therefore prunes and counts against one clock, and host skew cannot
  wipe or stretch another host's window. A module constant `config.STATE_CLOCK` (`"store"` by default, **not** a
  `Settings` field, so it cannot be set from the environment) lets tests switch to the caller's clock (`"caller"`)
  to keep the frozen-clock tests; dedicated tests run with `"store"`.
- **Script replies are strings, never bare numbers** (finding 8): each script returns
  `{status, retry_after_as_string}` (e.g. `{"refused", "0.4"}`); the Python side parses the float and applies
  `max(1.0, …)` to every refusal, so a refusal can never be read as `0` / allowed.
- **Failure is closed** (SPEC-GAP-069, -082):
  - `try_acquire_call` failing raises `StorageUnavailable` (not `LLMError`), so an upload aborts with the fixed 503
    instead of continuing with a rules-only scan (finding 14). `src/injection_scanner.py` catches only `LLMError`, so
    the exception propagates.
  - On the login path every storage error (Redis or Postgres) becomes the existing `LoginRateLimited` 429 body with
    `Retry-After`, identical for unknown, wrong and right passwords; a refund that fails **after** a session was
    created is logged (`auth.refund_failed`, no username) and swallowed, so the user gets the session it was issued
    (finding 9). This changes `src/accounts.py` (plan section 2.4).
  - The admin CLI prints a fixed message for `StorageUnavailable` and exits 1 (`raise … from None`, no driver text).
- **Redis configuration** (compose, SPEC-GAP-068, -083, -087): password from a mounted configuration file (never on a
  command line; the healthcheck uses `REDISCLI_AUTH`), port not published, only on an `internal: true` backend network
  joined by the server API containers, AOF on (`appendonly yes`, `appendfsync everysec`) on a named volume, explicit
  `maxmemory` with `maxmemory-policy noeviction`. A Redis ACL user limited to the key prefix is not in P7
  (SPEC-GAP-088).
- **Client timeouts:** connect and socket timeouts (SPEC-GAP-085) map to `StorageUnavailable`.
- **Selection rules** (SPEC-GAP-063): `STATE_BACKEND=redis` requires `REDIS_URL` and `DATABASE_URL`; only
  `chroma`+`sqlite` or `pgvector`+`redis` are accepted.

## Consequences
- The call limit and the login limits hold across containers and hosts (P7-AC2), against one clock; P6 atomicity of
  reservation and refund is kept by single scripts / transactions (plan section 3.2).
- Redis holds only small, self-expiring keys; its size is bounded by the global login cap and the call limit, not by
  documents.
- An outage of Redis or Postgres blocks sign-in (429), uploads and questions (503) instead of removing limits.
- Anyone with the Redis password can reset limits; anyone with the Postgres password can plant scan-cache verdicts.
  The trust boundary now includes those credentials (THREAT_MODEL delta).
- A Redis crash loses at most about one second of writes (AOF `everysec`). Accepted residual.
- Cache entries in server mode expire after 24 h, so re-uploading the same file a day later costs new OCR/scan calls.
- New dependency (`redis`) and image: human gates. Licence/fork choice: SPEC-GAP-065.

## Evidence
Commands run while planning (read-only):
- `.venv/bin/python -I -c "import redis"` and `import fakeredis`: `ModuleNotFoundError`. No `redis` line in
  `requirements*.lock`.
- `grep -rn "state\." src api eval`: every state access is a module-level function of `src/state.py`.
- `src/llm.py::call_claude`: `wait = state.try_acquire_call(...)`; `if wait:` refuses.
- `src/injection_scanner.py:234`: `except LLMError:` only.
- `src/accounts.py::PasswordLogin.login`: `refund_login_attempt` runs after `start_session`.

Not verified (nothing installed, no server): redis-py API names and typing; Lua `TIME` use inside scripts; the
integer conversion of Lua number replies; the Redis configuration directives. Each is exercised by a named test in
`docs/phases/p7-plan.md` section 3 on the first run against a real Redis.
