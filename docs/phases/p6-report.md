# P6 verification report: user accounts and persistent workspaces

- Phase: P6
- Date: 2026-11-04
- HEAD: `eae245e2163582eda3cb4454c8e03e6fbd4c59b3` (branch `p6-architecture`)
- Uncommitted changes: yes. The whole phase is uncommitted (list in "git status" below).
- Verifier: independent re-run with `.venv/bin/...`. `.pytest_cache`, `.mypy_cache` and `.ruff_cache` were deleted first.

## Verdict: VERIFIED

Every gate passed and every acceptance criterion is met. Five P6 SPEC-GAPs (057-061) are still open and need a human decision or confirmation at the gate. Limits of this verification are listed at the end.

## Gate table

| Check | Command | Real result |
|---|---|---|
| Tests and coverage | `.venv/bin/python -m pytest -q --cov` | 382 passed in 23.81s. TOTAL 94% (94.30%). "Required test coverage of 85.0% reached." |
| Lint | `.venv/bin/ruff check .` | All checks passed! |
| Format | `.venv/bin/ruff format --check .` | 104 files already formatted |
| Types | `.venv/bin/python -m mypy` | Success: no issues found in 24 source files |
| Invisible characters | `.venv/bin/python scripts/check_invisible_chars.py; echo exit=$?` | no output, exit=0 |
| Offline eval | not re-run (see below) | Verified from files: all metrics unchanged |
| Docker build | `docker compose build` | `Image trustrag:latest Built`, exit 0 |
| scrypt in image | `docker compose run --rm --no-deps api python -c "import hashlib; hashlib.scrypt(b'x', salt=b'0'*16, n=2**17, r=8, p=1, maxmem=2**28, dklen=32); print('ok')"` | `ok` (service `api`, `--no-deps`; it only created the compose network `talos_2_default` and a one-off container) |

### Coverage before and after
- Before (P5 report, `docs/phases/p5-report.md`): TOTAL 91.84% (92%), 253 tests.
- After: TOTAL 94.30% (94%), 382 tests. Not lower. New modules: `src/accounts.py` 96%, `src/passwords.py` 95%.
- Caveat: the P5 figure comes from the P5 report; I did not run the old tree.

### Eval-free comparison
I did not run `make eval-free`, as instructed (it appends a history row). The run on record was made by the earlier, interrupted verifier run at 2026-11-04T11:29:37, commit `eae245e`, dirty. I checked it from the files with `git diff eval/results/`:
- `history.jsonl`: exactly one new line appended (`commit eae245e`, `commit_dirty: true`, suite `free`). Metrics in that line: retrieval recall@k 1.0, MRR 0.973; injection rules_only precision 0.909, recall 0.667, false-alarm 0.067; Qasper recall@k 0.529, MRR 0.362. These equal the previous two free-suite rows and the backfilled B8 baseline.
- `TRENDS.md`: every metric row for the new run shows `+0.000`.
- `RESULTS.md`: the only changed line is the `Run:` header (timestamp and commit). No metric changed.
- Result: no regression. I could not confirm "Claude calls made: 0" for that run, because I did not see its console output.

## Acceptance criteria

I extracted all 76 test names from section 3 of `docs/phases/p6-plan.md` and checked with grep that each is defined under `tests/`. None was missing. I then ran the five P6 test files (`test_ownership.py`, `test_accounts.py`, `test_anonymous_mode.py`, `test_ui_accounts.py`, `test_security.py`): 162 passed, none skipped (no skip lines under `-rs` in the full run). The full suite also passed (382).

| Criterion | Tests | Status | Note |
|---|---|---|---|
| P6-AC1: a user can only list, query or delete their own workspaces (service and API) | Service: `test_service_user_lists_only_own_workspaces`, `..._cannot_query_other_users_workspace`, `..._cannot_delete_other_users_workspace`, `..._cannot_list_files_count_or_upload_to_other_users_workspace`, `test_service_anonymous_caller_cannot_reach_account_workspace`, `test_acct_id_without_live_row_is_refused`, `test_upload_or_ask_to_foreign_workspace_makes_zero_llm_calls`. API: `test_api_user_lists_only_own_workspaces`, `test_api_other_users_workspace_is_404_on_every_route` (parametrised), `test_api_anonymous_request_to_account_workspace_is_404`, `test_api_question_to_foreign_empty_workspace_is_404_not_409`, `test_invalid_bearer_gives_identical_401_...` | met | All exist and pass (`tests/test_ownership.py`). They cover list, query, delete, files, count, upload at service level and every route at API level, plus fail-closed and zero-LLM-call checks. Judged sufficient. |
| P6-AC2: credentials never stored or logged in plain text; login rate-limited and timing-safe | `test_password_hash_is_salted_scrypt_with_configured_params`, `test_state_db_never_contains_plain_password_or_token`, `test_session_token_stored_only_as_sha256`, `test_logs_never_contain_password_token_or_submitted_username`, `test_login_is_rate_limited_per_username`, `..._per_client_address`, `test_login_rate_limit_is_shared_across_processes`, `test_concurrent_wrong_logins_run_kdf_at_most_limit_times`, `test_dummy_hash_uses_current_kdf_params`, `test_login_runs_kdf_exactly_once_for_unknown_wrong_and_disabled`, `test_verify_password_uses_compare_digest`, `test_login_timing_does_not_depend_on_username_existence`, `test_api_login_validation_error_does_not_echo_password`, `test_admin_cli_reads_password_with_getpass_not_argv`, `test_state_db_dir_and_files_are_owner_only`, `test_scrypt_is_available_with_required_params` (all in `tests/test_accounts.py`) | met | All exist and pass. Timing safety is proved structurally (one KDF run with identical params for unknown, wrong and disabled users) with a statistical test as a secondary check, as the plan says. The statistical test is machine-dependent and could be flaky, but it passed on every run I made (3 full or partial runs). The container check confirms scrypt with n=2**17 works in the built image. |
| P6-AC3: anonymous mode keeps B5 (v1.0) behaviour | `test_anonymous_api_responses_match_v1_golden` (flag false and true; golden `tests/data/p6_anonymous_golden.json`), `test_new_paths_answer_as_v1_when_accounts_disabled`, `test_authorization_header_ignored_when_accounts_disabled`, `test_existing_operations_openapi_schema_unchanged`, `test_anonymous_workspaces_still_expire_after_24h`, `test_service_default_signatures_unchanged_for_anonymous_callers` (`tests/test_anonymous_mode.py`); `test_ui_first_load_unchanged_when_accounts_disabled` (`tests/test_ui_accounts.py`); existing tests `test_workspaces_are_isolated`, `test_workspace_ids_are_validated`, `test_api_end_to_end`, `test_api_errors_are_user_safe`, `test_api_key_is_enforced_when_configured` | met | All exist and pass. Golden file is new and was written by the QA step, not captured from a v1.0 checkout by me. It deliberately omits `DELETE` of an unowned `acct-` id (v1.0 returns 204, P6 returns 404 by ADR 0012), which is a stated, accepted difference (SPEC-GAP-057 note). |
| P6-AC4: THREAT_MODEL rows for account takeover, enumeration, session theft, each with a test | `test_threat_model_lists_p6_rows_with_existing_tests` (`tests/test_security.py`) plus the section-4 tests (`test_login_failures_are_indistinguishable`, `test_rate_limit_response_identical_for_unknown_user`, `test_no_signup_route_exists`, `test_token_never_accepted_from_query_or_cookie`, `test_session_expires_idle_and_absolute`, `test_logout_and_password_reset_revoke_sessions`, `test_session_cap_per_account_revokes_oldest`, `test_global_login_rate_cap`, `test_kdf_saturation_fails_fast_and_other_routes_stay_responsive`, `test_api_ignores_cookies_for_authentication`, `test_metrics_do_not_expose_login_outcomes`, and the four UI tests) | met | The row test is not trivial: it parses `THREAT_MODEL.md` rows 26-42, asserts that each row cites the expected tests, that every cited test is defined under `tests/`, that the "Account holder" actor exists and that four "Not defended against" limits are present. Passed. Rows exist for takeover (26, 27), enumeration (28) and session theft (29). I verified that test existence and presence of text, not that every cited test is the strongest possible proof. |
| P6-AC5: retention for account workspaces follows SPEC-GAP-004 | `test_account_workspaces_survive_cleanup_past_24h` (real `ChromaStore`), `test_service_passes_owned_and_tombstoned_ids_to_cleanup`, `test_disabling_accounts_neither_deletes_nor_exposes_owned_workspaces`, `test_v1_cleanup_does_not_touch_owned_collections`, `test_deleted_owned_workspace_id_is_404_for_anonymous_and_owner`, `test_upload_racing_owner_delete_never_leaves_reachable_data`, `test_disabled_account_keeps_workspaces` (`tests/test_ownership.py`) | met | All exist and pass. Account workspaces survive the 24h cleanup, deletion tombstones the id and purges the data, and flag-off or disable never deletes. SPEC-GAP-004 is the retention decision; I did not re-read its wording against ADR 0012 line by line. |

## Definition of Done (CLAUDE.md section 7)

- [x] Every P6 acceptance criterion has a passing test (table above).
- [x] Full quality gate green, run by me in this session, output recorded above (eval-free verified from files, see limits).
- [x] Coverage 94.30%, at least 85% and not lower than the P5 figure (91.84%).
- [x] No open critical or high review findings: not independently re-checked. I did not read the review records; confirm with the manager.
- [x] New security behaviour in `THREAT_MODEL.md` (rows 26-42, new actor and asset) with a passing regression test (`test_threat_model_lists_p6_rows_with_existing_tests`).
- [x] ADRs 0011 and 0012 exist with `Status: Accepted (P6; approved by the human, 2026-11-04)`. `ARCHITECTURE.md` (+160/-), `README.md` (+68/-) and `CHANGELOG.md` (+20) are modified in the working tree. `SECURITY.md` and `.env.example` are also updated.
- [x] `make eval-free` shows no regression (every metric unchanged, TRENDS `+0.000`, see above).
- [x] No secrets, no invisible characters, no debug leftovers:
  - Invisible-character script exit 0.
  - grep for `sk-ant`, `AKIA`, private-key headers and hard-coded passwords in `src/accounts.py`, `src/passwords.py`, `api/`, `app.py`: no hits. No `.env` or `.sqlite` file among untracked files. `.env.example` diff contains only commented placeholders.
  - grep of the new files for `breakpoint`, `pdb`, `TODO`, `FIXME`: no hits. `print(` appears only in `src/accounts.py` lines 326-341, the admin CLI's user-facing output (an intended CLI, not debugging). The added lines of the tracked-file diff contain no `print(`, `breakpoint(`, `pdb`, `TODO` or `FIXME`.
  - I did not run `gitleaks` (see limits).

## Open findings and SPEC-GAPs

Status read from `docs/SPEC-GAPS.md` (resolved gaps 030-056 are not repeated). Still open:

| SPEC-GAP | Topic | Status in file |
|---|---|---|
| 057 | Python interfaces the P6 tests call (names, signatures, stored hash form, UI widget keys) | open (naming only; implemented as written, tests pass) |
| 058 | UI "Clear all documents" for a signed-in user keeps the workspace (new `service.clear_documents`) | open, default applied, confirm at gate |
| 059 | In API mode all UI sign-ins share one per-client bucket (20 failures per 15 min lock all UI sign-ins) | open, needs human decision (documenting only; no control change) |
| 060 | ADR 0011 login step 6 as one transaction versus the split atomic steps implemented | open, confirm at gate |
| 061 | Accounts with old scrypt parameters are timing-distinguishable until their next login | open, confirm at gate |

Findings from my own checks: none failing. Observations:
- The anonymous golden file differs from v1.0 in one deliberate place (unowned `acct-` DELETE gives 404 instead of 204). It is recorded in SPEC-GAP-057.
- The timing test (`test_login_timing_does_not_depend_on_username_existence`) is a machine-load-sensitive statistical test. It passed, but it may be flaky on a loaded CI runner.

## git status --short (at the time of the run)

```
 M .env.example
 M ARCHITECTURE.md
 M CHANGELOG.md
 M README.md
 M SECURITY.md
 M THREAT_MODEL.md
 M api/main.py
 M app.py
 M docker-compose.yml
 M docs/SPEC-GAPS.md
M  docs/phases/p6-plan.md
 M eval/results/RESULTS.md
 M eval/results/TRENDS.md
 M eval/results/history.jsonl
 M eval/results/results.json
 M src/api_client.py
 M src/config.py
 M src/interfaces.py
 M src/models.py
 M src/service.py
 M src/settings.py
 M src/state.py
 M src/vectorstore.py
 M tests/conftest.py
 M tests/fakes.py
 M tests/test_security.py
?? src/accounts.py
?? src/passwords.py
?? tests/data/p6_anonymous_golden.json
?? tests/p6_golden.py
?? tests/p6_support.py
?? tests/test_accounts.py
?? tests/test_anonymous_mode.py
?? tests/test_ownership.py
?? tests/test_ui_accounts.py
```
(`docs/phases/p6-report.md` is added by this report.)

## Limits of this verification

- `make eval-free` was not run by me, by instruction. The result rests on the files written by the interrupted earlier run. I did not see that run's console output and did not independently reproduce the numbers.
- Coverage "before" is the P5 report figure, not a re-run of the old tree.
- Tests use fakes and a real embedded Chroma in temp dirs. No live Claude calls, no real multi-user deployment, no browser test beyond Streamlit `AppTest`.
- I judged tests by name, assertions in the threat-model test, and passing status. I did not read every test body, and I did not mutate the code to check that tests fail when a control is removed.
- The scrypt check ran in the built image for one parameter set on this machine's Docker; it shows scrypt works and fits in memory with `maxmem=2**28`, not that it fits under any production memory limit.
- No `gitleaks` scan was run. The secret check was grep-based only.
- I did not read code reviewer or security reviewer outputs, so "no open critical/high findings" is not independently confirmed.
- Docker build ran with the host's Docker cache and network. The run created the network `talos_2_default`; nothing was deleted.
