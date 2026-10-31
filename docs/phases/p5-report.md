# P5 verification report: public benchmark evaluation and eval trend report

- Phase: P5
- Date: 2026-10-31
- HEAD: `5fb45ca1a565d4b0af9a533655ab0ab7709dbb38` (branch `p5-architecture`)
- Uncommitted changes: yes. The whole phase is uncommitted (12 modified, 15 untracked entries; list at the end).
- Verifier: independent re-run in a fresh shell with `.venv/bin/...`. `.pytest_cache`, `.mypy_cache` and `.ruff_cache` were deleted first (git-ignored, inside the repo).

## Verdict: VERIFIED

Every gate passed and every acceptance criterion is met. Two limits are stated openly below: the machine network could not be switched off, and `mypy` does not cover `eval/`.

## Gate table

| Check | Command | Real result |
|---|---|---|
| Tests and coverage | `.venv/bin/python -m pytest -q --cov` | 253 passed in 9.42s. TOTAL 92% (91.84%). "Required test coverage of 85.0% reached." |
| Lint | `.venv/bin/ruff check .` | All checks passed! |
| Format | `.venv/bin/ruff format --check .` | 92 files already formatted |
| Types | `.venv/bin/mypy` | Success: no issues found in 22 source files |
| Invisible characters | `python scripts/check_invisible_chars.py; echo exit=$?` | no output, exit=0 |
| Offline eval | `source .venv/bin/activate && make eval-free` | exit 0, "Claude calls made: 0" (details below) |
| Docker | `docker compose build` | Image trustrag:latest Built, exit 0 (`.dockerignore` changed) |
| Secrets | `gitleaks detect --no-git` on a copy of all changed and untracked files | no leaks found (262 KB scanned) |
| Secrets, whole tree | `gitleaks detect --no-git --source .` | 2 hits, both `generic-api-key` in `.venv/share/jupyter/nbextensions/pydeck/index.js.map`. This is a third-party file in the git-ignored venv, not project content. |

Coverage baseline: no pre-phase coverage figure is recorded in the files I checked. I did not run the old tree, so I cannot state "not lower than before". The 91.84% is well above the 85% gate. `eval/` is not in the coverage or mypy scope.

### `make eval-free` checks
- `public_benchmark.status == "ok"`: Qasper v0.3, dev split, CC BY 4.0, 50 papers, 138 scored questions, Recall@4 0.529, MRR 0.362, 9 unanswerable. Excluded: mixed_answerability 10, figure_or_table_only 6, no_evidence 1. Rejected records: 0. This equals the earlier T7 run (138 questions, 0.529 / 0.362).
- Self-authored B8 retrieval equals `git show HEAD:eval/results/results.json`: 56 questions, Recall@4 1.0, MRR 0.973. The `ablation`, `settings`, `prompt_versions` and model fields are also equal (checked in Python).
- Injection `rules_only` block is identical to HEAD (precision 0.909, recall 0.667, false-alarm 0.067). The free suite does not run the `ai_only` and `combined` sections, so those are absent from the new file (SPEC-GAP-028).
- `history.jsonl`: `load_history` parsed all 3 lines, so all are valid. Line 1 (the backfill) is byte-identical to the copy I took before the run.
- `TRENDS.md`: `render_trends(load_history(...))` equals the file on disk (`True`).

## Acceptance criteria

| Criterion | Tests | Status | Note |
|---|---|---|---|
| P5-AC1: `make eval-free` runs the public benchmark with no network calls after download | `test_public_benchmark_retrieval_runs_with_sockets_blocked`, `test_public_benchmark_retrieval_with_real_chroma_store_and_sockets_blocked`, `test_eval_process_turns_chroma_telemetry_off`, plus the loader, evidence-hit, exclusion and metric tests in `tests/test_public_benchmark.py` | met | All pass. Manual part: the real `make eval-free` run gave `status: ok` with exit 0. **I cannot disable the machine network.** Evidence for "no network": the sockets-blocked tests (`socket.socket.connect` patched to raise), including the real `ChromaStore` variant, and the telemetry-off test. The live run is not proof of an offline run on its own. |
| P5-AC2: results stored with commit, prompt versions and models | `tests/test_eval_history.py` (one line per run, append-only, dirty flag and dataset hashes, backfill matches committed results), `test_results_json_public_benchmark_block_has_dataset_licence_and_hash` | met | `results.json` keeps `commit`, `prompt_versions`, `answer_model` and `checker_model`. The `history.jsonl` entries carry `commit`, `commit_dirty`, `datasets`, models and metrics. |
| P5-AC3: trend report with at least the last 10 runs per metric, one command | `test_trend_report_lists_last_10_runs_per_metric`, `..._metric_absent_from_a_run_is_skipped...`, `..._trend_command_regenerates_report_deterministically`, `test_makefile_has_eval_trend_target`, `test_malformed_history_line_fails_loudly` | met | All pass. The command is `make eval-trend`. The real file regenerates identically from `history.jsonl`. History holds only 3 runs so far, so the real report cannot yet show 10. The 12-run synthetic test covers that. |
| P5-AC4: existing baseline metrics (B8) unchanged | `tests/test_eval_baseline.py`: `test_results_markdown_without_public_benchmark_is_byte_identical`, `..._section_is_appended_after_existing_sections`, `..._default_relevance_is_unchanged...`, `..._does_not_change_config_or_settings` | met | Fresh-run comparison: B8 retrieval 56 / 1.0 / 0.973, ablation and rules-only injection equal to HEAD (see above). |
| P5-AC5: README "Measured, not claimed" shows Qasper numbers, dataset name and licence | `test_measured_section_shows_qasper_numbers_matching_results_json`, `test_measured_section_names_dataset_licence_and_credit` | met | README row: "Qasper retrieval recall@4 / MRR (138 answerable questions, 50 papers): 53% / 0.36". It names the dataset, v0.3, dev split and CC BY 4.0. |

Run of the AC test files: `pytest tests/test_public_benchmark.py tests/test_eval_history.py tests/test_eval_baseline.py tests/test_readme_benchmark.py tests/test_qasper_download.py --no-cov` gave 108 passed.

## Definition of Done

- [x] Every P5 acceptance criterion has a passing test (table above).
- [x] Full gate green. I ran it, and the output is recorded here.
- [x] Coverage at least 85% (91.84%). Not lower than before: not checkable, no recorded baseline.
- [x] No open critical or high review findings. Mediums fixed. Lows: see below.
- [x] Threat model: rows 20-25 are present in `THREAT_MODEL.md` (tampered archive, malicious archive, data-format code execution, dataset text in reports, injection inside benchmark papers, offline data leak). Regression tests are in `tests/test_qasper_download.py`, `tests/test_public_benchmark.py` and `tests/test_eval_history.py`. I did not map every row to a single test by name. Row 25 is covered by the sockets-blocked and telemetry tests, and row 21 mentions symlinks.
- [x] ADRs 0009 and 0010 have `Status: Accepted (P5; approved by the human)`. `ARCHITECTURE.md` (+33 lines), `README.md` (+30/-4) and `CHANGELOG.md` (+12) are updated.
- [x] `make eval-free` shows no regression: B8 and injection rules-only are identical to the baseline (SPEC-GAP-028 explains the dropped sections).
- [x] No secrets: the gitleaks scan of changed files was clean. `.env` is git-ignored and does not appear in `git status`. No `breakpoint`, `pdb`, `TODO` or `FIXME` in the new modules. The only `print` calls are CLI output in `eval/qasper_download.py`, `eval/trend.py` and `eval/qasper.py`. The invisible-character check exited 0.

## Review findings and status

- code-reviewer and security-reviewer: no critical or high findings (as reported to me; I did not re-review).
- Round-1 mediums (trend dataset-changed note, paid-run loss) and all lows, plus round-2 items NEW-1, N1, N2 and N3: reported fixed. Their regression tests are in the 253 passing tests. I did not review each fix individually.
- Open, low, not fixed:
  - Security L1: a mid-run `QasperDataError` shows only the exception type name on stderr (`eval/run_eval.py:503`, `type(exc).__name__`). The exit is still non-zero.
  - Security L2: `THREAT_MODEL.md` row 23 mitigation text does not mention symlink refusal or the re-check before write (the grep for "symlink" only matched row 21). A test exists.
  - Code-review NEW-5: a stale temp file after an interrupted download makes the next download refuse the directory.
  - Code-review NEW-3 (partly): the subset is hashed twice per run.
- Pre-existing, outside P5: `mypy --strict` flags a bare `dict` at `eval/metrics.py:38`. `eval/` is not in the mypy gate (the gate checked 22 source files), so the pass above does not cover `eval/` typing.

## SPEC-GAPs

- 016-029: resolved. I read 028 in `docs/SPEC-GAPS.md` (default accepted by the human on 2026-10-31). I did not check each of the others.
- 030: **open**. It asks the human to confirm the Python interface names of the new eval modules (`eval.qasper`, `eval.history`, `eval.trend`, `eval.qasper_download`, `evidence_hit`). The implementers followed the names the tests proposed. Renaming later is cheap.

## Human decisions needed

1. Which `eval/results/` files to commit (SPEC-GAP-028). `make eval-free` just overwrote `results.json` and `RESULTS.md` with a free-suite result that lacks the rerank, answers, trust and AI-injection sections the HEAD version has. The HEAD copies are the last full run. Options: commit the new free-suite files (with `history.jsonl` and `TRENDS.md`), or keep the HEAD `results.json` and `RESULTS.md` and commit only the history and trends. Note that `tests/data/results_full_v1.json` and `RESULTS_full_v1.md` hold a v1 full-run copy for the baseline tests. `README.md` quotes the Qasper numbers, which match the current `results.json`. The README test `test_measured_section_shows_qasper_numbers_matching_results_json` reads `results.json`, and asserts a `public_benchmark` block with `status: ok` (`tests/test_readme_benchmark.py:22-25`). HEAD's `results.json` has no such block, so restoring it would fail that test. Committing the new `results.json` is therefore required for the tests to pass; `RESULTS.md` is the freer choice.
2. The citation in `eval/data/qasper_manifest.json` is still listed under `"unverified"`. Verify it before release.
3. Optional paid `make eval` (plan task T8). It costs quota and was not run.
4. Accept or fix security L1 and L2 and the code-review nits NEW-5 and NEW-3.
5. Confirm the interface names (SPEC-GAP-030).

## Files for the commit (`git status --short`)

```
 M .dockerignore
 M .gitignore
 M ARCHITECTURE.md
 M CHANGELOG.md
 M Makefile
 M README.md
 M THREAT_MODEL.md
 M docs/SPEC-GAPS.md
 M eval/metrics.py
 M eval/results/RESULTS.md
 M eval/results/results.json
 M eval/run_eval.py
?? eval/data/qasper_manifest.json
?? eval/history.py
?? eval/qasper.py
?? eval/qasper_download.py
?? eval/results/TRENDS.md
?? eval/results/history.jsonl
?? eval/trend.py
?? tests/data/
?? tests/qasper_support.py
?? tests/test_eval_baseline.py
?? tests/test_eval_history.py
?? tests/test_public_benchmark.py
?? tests/test_qasper_download.py
?? tests/test_readme_benchmark.py
```

The run also needs to be listed: `docs/phases/p5-report.md` (this file) and the ADR files `docs/adr/0009-public-benchmark-data-handling.md` and `docs/adr/0010-eval-history-jsonl-and-static-trend-report.md` and `docs/phases/p5-plan.md`. These did not appear in `git status --short` taken at the start. They may already be committed (HEAD message: "approve plan, accept ADRs 0009 and 0010"). The report file is new.

## Human gate decisions (2026-10-31)

Recorded by the engineering manager after the verifier's run; the verifier's findings above are unchanged.

- **Security L2: fixed.** `THREAT_MODEL.md` row 23 now also says the data directory must not be a symlink and that the downloader re-checks it immediately before writing the subset (covered by `test_data_dir_swapped_during_download_is_refused`).
- **Security L1, code-review NEW-3 and NEW-5: accepted by the human** as low / nit, not fixed in P5.
- **Citation: verified by the human** against the Qasper web page's BibTeX (authors Dasigi, Lo, Beltagy, Cohan, Smith, Gardner; 2021). The page names no venue, so "NAACL 2021" was removed from the manifest and README; the manifest no longer has an `unverified` list. Item 2 under "Decisions for you" is therefore closed.
- `tests/test_readme_benchmark.py::test_measured_section_names_dataset_licence_and_credit` now takes the first author's surname from the citation (same check: "Dasigi" must appear next to the numbers).
- Re-run after these edits: `.venv/bin/python -m pytest -q --no-cov` gave `253 passed`; `ruff check .` passed; `scripts/check_invisible_chars.py` exit 0.
- Optional paid `make eval` (T8): not run.
