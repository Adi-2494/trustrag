# P7 verification report: server-based storage (pgvector, Redis/Valkey) for multi-host

- Phase: P7
- Date: 2026-11-05 (re-verification run, supersedes the first NOT VERIFIED run; first-run evidence kept below where it still applies)
- HEAD: `4c2c68db107c9d157489b318c0f245473c57a95e` (branch `main`, "p7: architecture plan (#19)")
- Uncommitted changes: yes. The whole phase is uncommitted (58 entries in `git status --short`).
- Verifier: independent re-run with `.venv/bin/...` (no `python` on the system PATH). `.coverage` removed before each coverage run (cache files only, no data). `.env` and `.env.server` were never read.

## Verdict: VERIFIED

Every gate I ran this session is green, the earlier blockers are closed, and all four acceptance criteria are met. Two items are informational, not blocking: the local-only coverage number (SPEC-GAP-092) and one compose command I could not run as asked (see the gate table, last row).

## Closure of the earlier blockers

| Earlier finding | Re-check | Status |
|---|---|---|
| F1 ruff I001 `tests/test_storage_settings.py:162` | `ruff check .` -> `All checks passed!` | closed |
| F2 coverage 93.83% < P6 94.30% | with services: `Total coverage: 95.34%` (TOTAL 2491 stmts, 92 missed) = +1.04 over P6 | closed |
| F3 gate without services fails 85% | now 84.07% without services; logged as SPEC-GAP-092 (line 99 of `docs/SPEC-GAPS.md`, conservative default: the gate is the run with the services, per SPEC-GAP-071) | accepted by default, informational |
| F5 server profile does not enable accounts | README step 3 ("Turn accounts on for the server APIs", `echo 'ACCOUNTS_ENABLED=true' >> .env.server`), `.env.server.example` lines 9-11 (commented `ACCOUNTS_ENABLED=true`), ARCHITECTURE mentions it; matches `docker-compose.yml` (`env_file: [.env, .env.server]` on `api-server-1/2` only, line 98 and 124) and the `tests/integration/test_multi_host.py` header, which needs a signed-in account | closed (docs match) |
| F6 bare `--profile server up` also starts api/ui | `docker-compose.yml` header lines 10-13 and README step 4 both say to name the services (`postgres redis api-server-1 api-server-2`); in the compose file only `postgres`, `redis`, `api-server-1`, `api-server-2` have `profiles: [server]` (lines 53, 75, 94, 121), `api` and `ui` have none | closed (docs match) |

## Gate table

| Check | Command | Result | Key output |
|---|---|---|---|
| Tests + coverage, with services (the gate, SPEC-GAP-071) | `TRUSTRAG_TEST_DATABASE_URL='postgresql://trustrag_test:...@127.0.0.1:55432/trustrag_test' TRUSTRAG_TEST_REDIS_URL='redis://:...@127.0.0.1:56379/0' .venv/bin/python -m pytest -q --cov` | pass | `749 passed, 12 skipped in 69.18s`; `TOTAL 2491 92 554 38 95%`; `Required test coverage of 85.0% reached. Total coverage: 95.34%` (>= 94.30%) |
| Skips in that run (`-rs`) | same env, `-rs` | as designed | 3 multi-host integration tests (need `TRUSTRAG_IT_*`), 6 `local_only` SQLite-file tests, `test_server_state.py:443` ("not running in CI"), 2 app-role tests (`TRUSTRAG_TEST_APP_ROLE_URL` unset; they passed in the first run against the real init script) |
| Tests + coverage, no services (informational, SPEC-GAP-092) | `env -u TRUSTRAG_TEST_DATABASE_URL -u TRUSTRAG_TEST_REDIS_URL .venv/bin/python -m pytest -q --cov` | tests pass, threshold fails (by SPEC-GAP-092) | `494 passed, 267 skipped in 31.68s`; `FAIL Required test coverage of 85.0% not reached. Total coverage: 84.07%` |
| Lint | `.venv/bin/ruff check .` | pass | `All checks passed!` |
| Format | `.venv/bin/ruff format --check .` | pass | `124 files already formatted` |
| Types | `.venv/bin/mypy` | pass | `Success: no issues found in 28 source files` |
| Invisible characters | `.venv/bin/python scripts/check_invisible_chars.py; echo rc=$?` | pass | no output, `rc=0` |
| Compose config, default | `docker compose config -q` | pass | `rc=0` |
| Compose config, server profile | `docker compose --profile server config -q` | could not run as asked | `rc=1`: `env file .../.env.server not found`. That is expected: `api-server-*` require `.env.server` (README step 2), and I did not create one or read a real one. The profile's wiring was checked by reading `docker-compose.yml` (above) and was exercised live in the first run |
| Offline eval, pgvector eval, docker build | not re-run | carried from first run | see "Changes since the first run" |

### Changes since the first run (what stands, what does not)
Checked with `find ... -newer docs/phases/p7-report.md` (report mtime 19:47:22) over `src/`, `api/`, `eval/`: no file is newer. Files newer than the first report are only `docker-compose.yml` (header comment), `README.md`, `ARCHITECTURE.md`, `.env.server.example`, `tests/test_storage_settings.py` (import sort) and `tests/test_server_error_paths.py` (new, 32 tests). Therefore:
- The live AC2 run (`3 passed in 4.01s` against two real containers) still stands: nothing under `api/` or `src/` changed. The compose change is a comment only (I did not diff the comment against the old file, but no non-comment compose key is newer in my checks of the service blocks above).
- `make eval-free` and `make eval-free-pgvector` need no re-run: no `eval/` or `src/` retrieval file changed. Results from the first run stand: recall@4 100%, MRR 0.97, identical to the `RESULTS.md` baseline; pgvector identical to Chroma (0.0 points).
- `docker compose build` was not re-run (Dockerfile and the Python image inputs unchanged since the first run; compose change is a comment).

### Test deletion check (redone)
I extracted every `def test_` name per file at `HEAD` (285 names) and checked each exists as `def <name>` in the same working-tree file: `missing=0`. No test function from HEAD is missing.

## Acceptance criteria

| Criterion | Test / evidence | Status | Note |
|---|---|---|---|
| P7-AC1: the same service and security suites pass against both backends | `tests/conftest.py` fixtures `storage_backend` (params `local`, `server`), `store_for_backend`, `real_store`; module-level `pytestmark = usefixtures("storage_backend")` in the suites; `tests/test_server_state.py`, `tests/test_server_fixes.py`, `tests/test_vectorstore_contract.py`, `tests/test_storage_settings.py` | met | Run this session: 749 passed, 12 skipped (server); 494 passed, 267 skipped (local). First run, real `trustrag_app` role: 720 passed. Server-parametrised tests show up as passes with the server env and as skips without it. The 6 `local_only` tests (read the SQLite file) are skipped on the server by design. Existing tests were not deleted (see DoD). One caveat: the passes were produced against pgvector 0.8.7 / PG 17 and Valkey 9.1.3 only. |
| P7-AC2: rate limit shared across two API containers (integration test) | `tests/integration/test_multi_host.py`: `test_login_rate_limit_is_shared_across_two_api_containers`, `test_session_from_one_container_works_on_the_other`, `test_account_workspace_created_on_one_container_is_usable_on_the_other` | met | Run live against two containers of the compose server profile: `3 passed in 4.01s` (details below). Also in-process: the 4-process shared-limit test in `tests/test_architecture.py` passes on `[server]`. |
| P7-AC3: pgvector retrieval eval within 1 point of ChromaDB, or explained | `make eval-free-pgvector`; `eval/results/results-pgvector.json`; `tests/test_eval_baseline.py::test_pgvector_run_writes_separate_files_and_leaves_baseline_untouched`, `test_chroma_results_unchanged_after_store_refactor` | met | I compared `results.json` (Chroma, same session) with `results-pgvector.json` field by field after dropping `run_at`, `commit`, `commit_dirty`: the only difference is the extra key `vector_store: "pgvector"`. Retrieval 56 questions, recall@4 1.0, MRR 0.973 on both; all 12 ablation rows and the Qasper block are identical. Difference 0.0 points (SPEC-GAP-073 tolerance: 1 point). The pgvector run did not touch `history.jsonl` (stayed at 7 lines), as intended. |
| P7-AC4: new ADRs supersede or amend 0003 and 0005 | `tests/test_security.py::test_p7_adrs_amend_0003_and_0005_and_are_indexed` (passes); ADRs 0013, 0014, 0015 | met | The three ADRs carry `**Status:** Accepted (P7; approved by the human, 2026-11-05)`; 0013 amends 0003, 0014 amends 0005, 0015 amends 0005 and 0012. The test also checks they are in `docs/adr/README.md` and that 0003, 0005, 0012 still exist. They amend rather than supersede, which the SPEC wording ("supersede or amend") allows. |

Scope check from SPEC ("ChromaDB + SQLite remain the default; compose profile adds Postgres and Redis"): default `docker compose config --services` is `api`, `ui` only; `VECTOR_STORE` default `chroma`, `STATE_BACKEND` default `sqlite` per plan and `tests/test_storage_settings.py` (passes). Valkey is used as the Redis-protocol server (SPEC-GAP-065, human decision), with `redis==8.1.0` as the client.

### Live server-profile check (P7-AC2)

Human approval for this was given in the task (2026-11-05). Setup, all throwaway:
- Checked first that `.env.server` and `~/.trustrag-p7-verify` did not exist, and that ports 8001 and 8002 were free.
- Created `~/.trustrag-p7-verify` (0700) with `pg_superuser_password`, `pg_app_password`, `redis_auth.conf` (`requirepass <pw>`) at 0444, and a throwaway repo-root `.env.server` (`DATABASE_URL`, `REDIS_URL`).
- Started `docker compose -p trustrag-p7-verify --profile server up -d --build postgres redis api-server-1 api-server-2` with `TRUSTRAG_SECRETS_DIR=~/.trustrag-p7-verify`. I named the four server services explicitly, so the default `api` and `ui` were not started; their config was checked with `docker compose config --format json` (only variable names were printed, never values).
- Results:
  - Healthchecks: `api-server-1` and `api-server-2` `(healthy)`, `postgres` and `redis` `(healthy)`; `/health` returned `{"status":"ok"}` on 8001 and 8002.
  - Valkey: `CONFIG GET appendonly` -> `yes`; `maxmemory` -> `67108864`; `maxmemory-policy` -> `noeviction`; `INFO memory`: `used_memory_human:1005.53K`, `maxmemory_human:64.00M`, `maxmemory_policy:noeviction`. An unauthenticated `valkey-cli ping` gave `NOAUTH Authentication required.`
  - Ports and networks: `postgres` and `redis` have `ports=[]` and `PortBindings {}`; both are only on network `backend`, which is `internal: true` (`docker network inspect` -> `true`) and contains only the postgres, redis and two api-server containers. `ui` is on `default` only and has none of `DATABASE_URL`, `REDIS_URL`, `VECTOR_STORE`, `STATE_BACKEND` in its environment; the same holds for the default `api`.
  - The api-servers connect as `trustrag_app` (Postgres `pg_stat_activity` showed `trustrag_app|2`), and the schema `trustrag` had tables `accounts, cache, chunks, sessions, vector_workspaces, workspaces`.
  - Account: created with `printf ... | docker compose ... exec -T api-server-1 python -m src.accounts create --password-stdin verifyuser` (output `Account created.`); `api-server-2 ... list` showed it (shared Postgres).
  - First integration run: **3 failed** (`login` returned 404), because accounts are off by default and the compose server profile does not turn them on; `ACCOUNTS_ENABLED=true` has to come from `.env` or `.env.server` (see finding F5). I added `ACCOUNTS_ENABLED=true` to my throwaway `.env.server`, recreated the two api containers, and re-ran.
  - `TRUSTRAG_IT_API_URLS=http://127.0.0.1:8001,http://127.0.0.1:8002 TRUSTRAG_IT_USERNAME=verifyuser TRUSTRAG_IT_PASSWORD=... .venv/bin/python -m pytest -q -rs -v -m integration tests/integration` -> `collected 3 items ... 3 passed in 4.01s`. (The account survived the container recreation, i.e. durable in Postgres.)
- Cleanup: `docker compose -p trustrag-p7-verify --profile server down -v` removed the four containers, both networks and both volumes; I deleted `~/.trustrag-p7-verify` and the repo-root `.env.server` (confirmed with `ls`: `No such file or directory` for both). `docker ps -a` afterwards shows only `trustrag-p7-testvk` and `trustrag-p7-testpg` (both untouched, up 17 hours); `docker volume ls | grep trustrag-p7-verify` and `docker network ls | grep p7-verify` printed nothing. The default project's volumes and the test containers were not touched. The `trustrag-p7-verify-pg` container from the app-role step was removed (`docker rm -f` echoed its name; absent from `docker ps -a`). The tag `trustrag:latest` was rebuilt by `docker compose build` and `up --build` (same name as before).

## Definition of Done (CLAUDE.md section 7)

- [x] Every P7 acceptance criterion has a passing test (table above). AC2's three tests only run against the live stack (verified live in the first run, still valid, see above); in CI and a plain run they skip.
- [x] Full quality gate green, run by me: tests+coverage with services, ruff, format, mypy, invisible chars all pass (see gate table). Caveat: the no-services coverage run fails the threshold by design (SPEC-GAP-092).
- [x] Coverage at least 85% and not lower than before: 95.34% >= 94.30% (P6).
- [x] No open critical/high review findings: see "Review findings" (all fixed; two accepted residuals are low).
- [x] New security behaviour in `THREAT_MODEL.md` with regression tests: rows 43-63 present; `test_threat_model_lists_p7_rows_with_existing_tests` and `test_p7_threat_plan_names_resolve_to_real_tests` are in the passing full run.
- [x] ADRs 0013-0015 Accepted; `ARCHITECTURE.md`, `README.md`, `CHANGELOG.md`, `SECURITY.md`, `.env.example`, `.env.server.example` updated.
- [x] `make eval-free` shows no regression (first run; no eval/src change since, see above); pgvector eval identical to Chroma.
- [x] No secrets, no invisible characters (`rc=0`), no debug leftovers (first-run grep over diff and new files; `.env`/`.env.server` not in the tree; passwords in the repo are CI throwaways marked `# gitleaks:allow` and `change-me-` placeholders). `gitleaks` not run.

## Review findings

Statuses below are what I could verify from code and tests in this session. The manager's review records are not in the repository, so the finding wording follows the manager's list; for each I name the evidence I found. "Verified" means a test or file exists and passed in the full run above; it does not mean I re-reviewed the design.

### Initial code review (11)

| # | Finding | Status | Evidence |
|---|---|---|---|
| 1 | Critical: existing tests deleted | fixed | HEAD test-name check, `missing=0` (285 of 285 present) |
| 2 | Eval default path | fixed | `eval/results/RESULTS.md` diff is only the `Run:` line; `tests/test_eval_baseline.py::test_pgvector_run_writes_separate_files_and_leaves_baseline_untouched`, `test_chroma_results_unchanged_after_store_refactor` pass |
| 3 | Schema bypass | fixed | `tests/test_server_fixes.py::test_missing_schema_raises_storage_unavailable_and_creates_nothing_in_public`, `test_server_state.py::test_teardown_refuses_unexpected_schema_or_prefix` pass |
| 4 | Dead pooled connections | fixed | `test_server_fixes.py` P7-N1 tests (dead pooled conn + failing reconnect -> `StorageUnavailable`) pass |
| 5 | Test gaps | fixed | new `test_server_fixes.py`, `test_server_error_paths.py` (32 tests); coverage 95.34% |
| 6 | Per-request sweep | fixed | `test_cleanup_sweeps_run_at_most_once_per_interval`; `test_pgvector_sweep_yields_to_a_sweep_already_running` (advisory lock) |
| 7 | Deadlock risk | fixed | `test_overlapping_cache_writes_in_opposite_order_do_not_deadlock`, upserts in sorted key order |
| 8 | `getattr` defaults | fixed per manager; not individually traced by me | no failing test; I did not grep every call site |
| 9 | Stale pgvector results | fixed | `eval/results/RESULTS-pgvector.md` and `results-pgvector.json` regenerated by `make eval-free-pgvector` in the first run, identical to Chroma |
| 10 | Embed lock (CR10) | accepted residual | no code change expected; I found no test for it and no ADR/THREAT_MODEL wording naming "CR10" (grep). Accept in writing at the gate |
| 11 | SPEC-GAP-091 | open, default accepted | pgvector workspace row created at first upload; documented in ADR 0013; confirm at the gate |

### Security review (9)

| # | Finding | Status | Evidence |
|---|---|---|---|
| 1 | CI app role untested | fixed | `.github/workflows/tests.yml` sets `TRUSTRAG_TEST_APP_ROLE_URL` and runs `docker/postgres-init.sh`; `test_app_db_role_is_not_superuser` and `test_app_role_cannot_create_in_public_and_owns_schema_trustrag` passed in my first run against the real init script (skip locally here) |
| 2 | Compose hardening test | fixed | `tests/test_security.py::test_compose_server_profile_hardening`, `test_compose_redis_sets_maxmemory` pass |
| 3 | Cleanup DoS | fixed | throttle and advisory-lock tests (see code review 6); `test_new_workspace_cost_does_not_grow_with_purged_tombstones` |
| 4 | Row 60 wording | fixed | `THREAT_MODEL.md` row 60 and the "Not defended" note (line 125) describe the 429-vs-401 weak oracle |
| 5 | Eval mixed mode | fixed | `tests/test_storage_settings.py::test_mixed_backend_combinations_are_refused` passes |
| 6 | Schema bypass | fixed | as code review 3 |
| 7 | Lazy cache deletion | fixed per manager; related tests (`test_server_fixes.py` P7-N2 expired-row purge) pass | not traced further |
| 8 | Weak race test | fixed | `test_expired_row_purge_and_opposite_order_writers_do_not_deadlock` and the 4-process shared-limit test in `tests/test_architecture.py` pass |
| 9 | Init password | fixed | `docker/postgres-init.sh` lines 16-17 set `log_statement = 'none'` and `log_min_duration_statement = -1`; `test_server_fixes.py` P7-N5 test checks the order before `CREATE ROLE ... PASSWORD` |

### Re-review N1-N5

| # | Finding | Status | Evidence |
|---|---|---|---|
| N1 | Double slot release | fixed | N1 tests: slot count intact after dead conn + failed reconnect; fixed 503 on data routes, uniform 429 on login |
| N2 | Cache deadlock with expired rows | fixed | `test_expired_row_purge_and_opposite_order_writers_do_not_deadlock` |
| N3 | Tombstone purge growth | fixed | `test_new_workspace_cost_does_not_grow_with_purged_tombstones` (2000 tombstones, no full scan or big `ANY()` outside a sweep) |
| N4 | Failed sweep fails `new_workspace` | fixed | `test_failing_sweep_does_not_fail_new_workspace_and_is_not_retried_in_the_interval` |
| N4/N5 docs | THREAT_MODEL table blank line | fixed | N4 (docs) test in `test_server_fixes.py` (blank line inside the attacks table) passes |
| N5 | init `log_statement` | fixed | see security 9 |

### Leftovers

| Item | Status | Evidence |
|---|---|---|
| Row 55 wording | fixed per manager | THREAT_MODEL rows 55-57 exist; I checked existence of cited tests via `test_p7_threat_plan_names_resolve_to_real_tests` (passes), not the wording |
| Rows 55-57 citations | fixed | same test passes |
| `log_min_duration_statement` | fixed | `docker/postgres-init.sh:17` and its test |
| Post-KDF 429 vs 401 oracle | accepted residual | stated in `THREAT_MODEL.md` (line 125) as not defended; needs the human's written acceptance |

## SPEC-GAPs

- **SPEC-GAP-092 (new, open):** local-only coverage 84.07% fails the 85% threshold because server modules are covered only by `[server]` tests. Default: the gate is the run with the services (SPEC-GAP-071; CI sets them). Confirm at the gate. Contributors need the two test containers locally.
- **SPEC-GAP-091: open**, default accepted (see above). **SPEC-GAP-009: open**, default stays ChromaDB + SQLite; P7 does not change it.
- SPEC-GAP-062 to 090 are `resolved` in the file (first-run reading). Lock hashes were not independently regenerated.

## Limits of this verification

- Not run: `make eval` (paid), CI itself, `gitleaks`, a clean-image `docker compose up` of the default stack, a multi-arch build, `docker compose --profile server config` (needs a `.env.server`, which I did not create).
- Not re-run this session: `make eval-free`, `make eval-free-pgvector`, `docker compose build`, the live AC2 stack; their first-run results stand because no `src/`, `api/` or `eval/` file changed since (find -newer check above).
- The first-run live check also covered the compose server stack (healthchecks, Valkey settings, internal network, `trustrag_app` role); cleanup was confirmed then.
- Backends tested: pgvector 0.8.7 on PG 17 and Valkey 9.1.3 only.
- Side effects this session: `.coverage` re-created. No code, test or doc other than this report was edited. No git write command was run.
