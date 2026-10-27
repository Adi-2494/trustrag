# TrustRAG — engineering constitution

This file is read at the start of every Claude Code session. It overrides default behaviour.
If anything here conflicts with a request, stop and ask the human.

## 1. Project summary

TrustRAG is a hallucination-aware document Q&A product. Users upload PDFs; TrustRAG answers **only** from those
documents, cites every fact as `[file, p.N]`, fact-checks its own answer claim by claim (trust score), and scans
every document for prompt-injection attacks before indexing it.

- **Released:** v1.0.0 (pipeline, FastAPI backend, Streamlit UI, eval suite, hybrid retrieval, observability,
  threat model, 8 ADRs). v1.0 behaviour is the baseline contract: do not regress it.
- **Next:** roadmap phases P5–P8 in [`docs/SPEC.md`](docs/SPEC.md).

## 2. Sources of truth (highest first)

1. [`docs/SPEC.md`](docs/SPEC.md) — what the product must do, phase scope, acceptance criteria.
2. [`docs/adr/`](docs/adr/) — decisions already made and why. A change to a decision needs a **new** ADR.
3. [`THREAT_MODEL.md`](THREAT_MODEL.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`SECURITY.md`](SECURITY.md).
4. The code and its tests.
5. `README.md` (user-facing; must be kept in sync, but is not a source of requirements).

Lower sources never override higher ones. If two sources conflict, log a SPEC-GAP (section 6) and stop.
Open questions live in [`docs/SPEC-GAPS.md`](docs/SPEC-GAPS.md).

## 3. Tech stack (do not add to it without an ADR)

| Area | Choice |
|---|---|
| Language | Python 3.11 only (`requires-python = ">=3.11,<3.12"`) |
| LLM | Claude via `src/llm.py` only (`LLMClient` protocol). Providers: `claude_code`, `anthropic`, `bedrock` |
| Models | Sonnet for answers, Haiku for checks (ADR 0001); IDs live in `src/config.py` only |
| Vector store | Embedded ChromaDB + all-MiniLM-L6-v2, behind `VectorStore` (`src/interfaces.py`) |
| Retrieval | Hybrid semantic + BM25 with RRF, optional Haiku rerank (ADR 0006) |
| Shared state | SQLite WAL (`src/state.py`, ADR 0005) |
| API / UI | FastAPI (`api/main.py`) / Streamlit (`app.py`), UI is a client of `TrustRAGService` |
| Models / settings | `TypedDict` in `src/models.py` (ADR 0004); pydantic-settings in `src/settings.py` |
| Quality | pytest, Hypothesis, pytest-cov (gate 85%), ruff, `mypy --strict`, pre-commit (gitleaks, invisible chars) |
| Delivery | Multi-arch Docker, docker-compose, GitHub Actions (actions pinned by SHA), hash-locked deps, Dependabot |

New dependencies: add to `pyproject.toml` with an exact pin **and** regenerate the hash locks. Regenerating locks
and installing packages are human gates.

## 4. Team

Agents live in `.claude/agents/`. The main session acts as **engineering manager**: it plans, delegates, merges
results and runs the gate. It does not skip roles.

| Agent | Model | Writes code? | Owns |
|---|---|---|---|
| `architect` | opus | docs only | ADRs, data model, API contracts, phase plans |
| `backend` | sonnet | yes | `src/`, `api/`, `app.py` |
| `llm-eval-engineer` | sonnet | yes | prompts, retrieval, `eval/`, benchmark results |
| `qa-engineer` | sonnet | tests only | `tests/`, fakes, cassettes, test-first specs |
| `devops` | sonnet | yes | Docker, compose, CI, pre-commit, lock files (proposals only for locks) |
| `security-reviewer` | opus | **no** (read-only) | threat model review, attack replay, secrets |
| `code-reviewer` | opus | **no** (read-only) | correctness, design, ADR conformance |
| `verifier` | sonnet | **no** (read-only) | independent re-run of every check, acceptance criteria |

## 5. Phase protocol

Every phase `PNN` follows these steps in order. No step may be skipped or merged.

1. **Plan** — `architect` writes `docs/phases/pNN-plan.md`: scope (from SPEC only), files touched, ADRs needed,
   acceptance criteria mapped to tests, risks, human gates expected.
2. **Tests first** — `qa-engineer` writes failing tests for every acceptance criterion. Run them; they must fail
   for the right reason. Record the output.
3. **Build** — `backend` / `llm-eval-engineer` / `devops` implement until those tests pass. Smallest change that
   satisfies the spec.
4. **Review** — `code-reviewer` and `security-reviewer` run **in parallel** on the phase diff.
5. **Fix** — owners fix every confirmed finding. Run only the affected tests during this loop.
6. **Independent verify** — `verifier` runs the full quality gate (section 8) from a clean state and checks each
   acceptance criterion. It writes `docs/phases/pNN-report.md`.
7. **Human gate** — stop. Present: the report, all SPEC-GAPs raised, all findings and their status, and the exact
   git commands (section 9). Wait for the human.
8. **Commit** — **the human** runs the commands. Claude never does.

## 6. Anti-hallucination rule

**If it is not in the spec, it does not get built. Never guess.**

When a requirement is missing, ambiguous or conflicting:
1. Add an entry to `docs/SPEC-GAPS.md`: `SPEC-GAP-NNN | phase | question | conservative default | status: open`.
2. Use the conservative default only if it is reversible and does not touch a human gate; otherwise stop.
3. Mention every new SPEC-GAP at the human gate.

Also: never invent model IDs, API fields, library functions, CLI flags or benchmark numbers. Check the installed
package, the docs, or ask. Agents report **only what they actually ran**, with the command and its real output.
"Should pass" is not a result.

## 7. Definition of Done (per phase)

- [ ] Every acceptance criterion in `docs/SPEC.md` for the phase has a test that passes.
- [ ] Full quality gate (section 8) green, run by `verifier`, output recorded in the phase report.
- [ ] Coverage ≥ 85% overall; not lower than before the phase.
- [ ] No open critical/high review findings; mediums fixed or accepted by the human in writing.
- [ ] New security-relevant behaviour added to `THREAT_MODEL.md` with a regression test.
- [ ] Significant decisions recorded as ADRs; `ARCHITECTURE.md`, `README.md`, `CHANGELOG.md` updated.
- [ ] `make eval-free` shows no regression versus `eval/results/RESULTS.md` (or the regression is explained and approved).
- [ ] No secrets, no `.env` contents, no invisible characters, no debug leftovers.

## 8. Commands

| Purpose | Command |
|---|---|
| Fast tests (affected only) | `python -m pytest -q tests/test_<area>.py -k <name>` |
| Full tests + coverage | `python -m pytest -q --cov` |
| Lint / format | `ruff check .` and `ruff format --check .` |
| Types | `mypy` |
| Invisible characters | `python scripts/check_invisible_chars.py` |
| Offline eval (free) | `make eval-free` |
| Full eval (**calls Claude — human gate**) | `make eval` |
| Containers | `docker compose build`, `docker compose up` |

Full quality gate = full tests + coverage, ruff check, ruff format --check, mypy, invisible characters,
`make eval-free`, and `docker compose build` when Docker/CI files changed.

## 9. Git rules

Claude **never** runs `git add`, `git commit`, `git tag`, `git push`, `git remote`, `git rebase`,
`git reset --hard`, `git commit --amend` or `git filter-branch`. Read-only git (`status`, `diff`, `log`, `show`)
is fine.

At the human gate, print the commands for the human to run, in this shape:

```bash
git switch -c pNN-<short-name>              # main is branch-protected
git add <explicit file list — never "git add ." or "-A">
git commit -m "pNN: <one-line summary>"
git tag pNN-verified
git push -u origin pNN-<short-name>         # then open a PR to main
git push origin pNN-verified
```

Commit messages: plain one-line summary. No co-author lines, trailers, signatures or AI attribution.
Release tags (`vX.Y.Z`) are created by the human only.

## 10. Human gates (always stop and ask)

- Changing `.claude/settings.json`, `CLAUDE.md`, CI workflows, branch rules or ADR status.
- Anything that costs money or uses quota: `make eval`, paid services, cloud resources, new API keys.
- Installing packages or regenerating `requirements*.lock`.
- Anything touching production, deployment, DNS, certificates or SSO configuration.
- Deleting data: `chroma_db/`, SQLite state, eval results, `docker compose down -v`, any `rm -rf`.
- Reading or editing real secrets (`.env`, token files). Use `.env.example` instead.
- Any change to a security control listed in `THREAT_MODEL.md`.

## 11. Speed rules

- During build and fix loops run **only the affected tests**. Run the full gate once per phase (step 6).
- Run independent agents in parallel (reviewers in step 4; independent build tasks in step 3).
- Use fakes (`tests/fakes.py`) and cassettes; never call Claude from tests.
- Collect all SPEC-GAPs and questions and present them together at the gate, unless one blocks progress.

## 12. Code conventions

- Match the surrounding code: small modules with one responsibility, module docstrings explaining *why*,
  type hints everywhere (`mypy --strict` must pass), friendly user-facing error messages.
- All Claude calls go through `LLMClient`; all storage through `VectorStore` / `src/state.py`.
- Treat document text, file names, questions and model output as untrusted (see `src/safety.py`).
- Bump `config.PROMPT_VERSIONS` whenever a prompt changes, and re-run the relevant eval.
