# 0014 - Opt-in Redis-protocol (Valkey) backend for shared rate limits; caches in Postgres (amends 0005)

**Status:** Accepted (P7; approved by the human, 2026-11-05). Drafted 2026-11-05 and revised the same day after the security review of the plan,
`docs/phases/p7-plan.md` section 8, findings 2, 3, 8, 9, 12, 14). Amends ADR 0005 if accepted; SQLite stays the
default and ADR 0005 is not superseded. Changing the status of ADR 0005 is a human action. (The file name keeps the
first draft's wording; after the review the caches no longer live in Redis. The server chosen on 2026-11-05 is
Valkey, a Redis-protocol compatible server; see Decision. "Redis" below means the Redis protocol / data model, and
the setting names `STATE_BACKEND=redis`, `REDIS_URL` and `REDIS_PREFIX` are kept.)

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
- Licence (SPEC-GAP-065): Redis server releases after the 7.2 line are no longer distributed under BSD-3-Clause
  (Redis 7.4: RSALv2 / SSPLv1; Redis 8: AGPLv3 / RSALv2 / SSPLv1 - from memory, `docs/phases/p7-dependency-proposal.md`
  section 2). Valkey is the BSD-3-Clause fork of Redis 7.2.4 and speaks the same protocol.

## Decision
- **Server: Valkey, client: redis-py** (SPEC-GAP-064, -065, decided by the human on 2026-11-05). The counter server
  is `valkey/valkey:9.1.3@sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7` (BSD-3-Clause),
  pinned by digest in compose and CI. Reason: licence - it keeps the deployment on a permissive licence with no
  AGPL/SSPL/RSAL review, while staying Redis-protocol compatible, so the Python client is redis-py (`redis==8.1.0`,
  MIT) and the design below (Lua scripts, `TIME`, ZSETs, `MULTI`/`EXEC`, AOF, `maxmemory`) is unchanged. Valkey's
  image ships `redis-cli` as a link to `valkey-cli`, so the healthcheck wording below holds. Any other
  Redis-protocol server is a change to this ADR.
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
- New dependency (`redis==8.1.0`) and image (Valkey 9.1.3, by digest): approved by the human on 2026-11-05
  (SPEC-GAP-064, -065); installing it and regenerating the locks remain human gates. A future switch back to Redis
  itself needs a licence review and a new ADR.

## Evidence
Commands run while planning (read-only):
- `.venv/bin/python -I -c "import redis"` and `import fakeredis`: `ModuleNotFoundError`. No `redis` line in
  `requirements*.lock`.
- `grep -rn "state\." src api eval`: every state access is a module-level function of `src/state.py`.
- `src/llm.py::call_claude`: `wait = state.try_acquire_call(...)`; `if wait:` refuses.
- `src/injection_scanner.py:234`: `except LLMError:` only.
- `src/accounts.py::PasswordLogin.login`: `refund_login_attempt` runs after `start_session`.

- `docs/phases/p7-dependency-proposal.md` (devops, 2026-11-05): Valkey 9.1.3 digest and amd64/arm64 platforms from
  `docker buildx imagetools inspect`; Valkey licence BSD-3-Clause from the GitHub API; config `include` +
  `requirepass`, `noeviction`, AOF and `REDISCLI_AUTH` healthcheck checked in a short-lived container. The Redis 7.4 / 8
  licence terms are from memory in that document (not verified).

Not verified at planning time (nothing installed, no server): redis-py API names and typing; Lua `TIME` use inside scripts; the
integer conversion of Lua number replies; the Redis configuration directives. Each is exercised by a named test in
`docs/phases/p7-plan.md` section 3 on the first run against a real Redis.

### Measured after the P7 build (added 2026-11-05, task T8)

**Tests.** As reported to the architect by the engineering manager for the build tasks (not re-run by the architect;
the verifier's full-gate run in `docs/phases/p7-report.md` is authoritative): the full suite run against a throwaway
local Valkey 9.1.3 and PostgreSQL 17 / pgvector 0.8.7 gave **644 passed**, before the docs and eval tasks. The
decisions of this ADR are covered by:
- `tests/test_server_state.py`: `test_refused_call_never_returns_zero_wait`,
  `test_login_refusal_with_subsecond_retry_is_still_refused` (string replies, 1 s floor),
  `test_global_window_prune_does_not_drop_user_or_client_rows`, `test_refused_reservation_inserts_no_rows_for_any_key`,
  `test_refund_removes_only_this_attempts_rows` (script atomicity), `test_skewed_host_cannot_prune_other_hosts_attempts`
  (`TIME`), `test_redis_holds_only_counter_keys`, `test_cache_entries_have_ttl`, `test_ocr_cache_entries_expire`
  (caches in Postgres, 24 h), `test_redis_down_model_call_is_refused`, `test_call_limit_store_error_aborts_upload_not_rules_only`,
  `test_redis_down_login_gives_same_429_and_runs_no_kdf`, `test_refund_failure_after_session_does_not_503`,
  `test_admin_cli_storage_error_prints_fixed_message` (fail closed).
- `tests/test_architecture.py::test_rate_limit_is_shared_across_processes` and
  `tests/test_accounts.py::test_login_rate_limit_is_shared_across_processes`, parametrised `[server]` (one Valkey shared
  by several processes; P7-AC2 in CI).
- `tests/integration/test_multi_host.py` (two real API containers, P7-AC2 over HTTP): run by the verifier, not in CI;
  no result is claimed here.
- `tests/test_security.py` compose checks (`maxmemory`, `noeviction`, `appendonly`, no password on a command line).
These runs settle the planning-time "not verified" items above (redis-py API, Lua `TIME`, reply handling) as far as
those tests exercise them; whether Lua number replies are truncated was not tested directly, because the scripts never
return bare numbers. Installed client checked by the architect: `redis 8.1.0` (`.venv`).

**Build notes** (read in `src/state_redis.py`, `src/accounts.py`, `docker/redis.conf`, `docker-compose.yml`):
- Both scripts accept an optional caller time in `ARGV` that is used only when `config.STATE_CLOCK == "caller"`;
  otherwise the argument is empty and the script reads `TIME`.
- An unexpected script reply (not `allowed` / `refused` / `reserved` with this attempt id) raises
  `StorageUnavailable`; it is never read as "allowed".
- On the login path `StorageUnavailable` becomes `LoginRateLimited(1.0)`, so the 429 carries `Retry-After: 1`, and the
  event `auth.storage_unavailable` (no username) is logged.
- The CI job (`.github/workflows/tests.yml`) starts Valkey with `VALKEY_EXTRA_FLAGS` including `--requirepass` and a
  throwaway password, and its health command sets `REDISCLI_AUTH` inline. That is CI-only and throwaway; the compose
  `server` profile keeps the password in the mounted secret file as decided.
