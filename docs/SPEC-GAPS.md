# Spec gaps

Questions the spec does not answer, or places where sources conflict. Each has a conservative default that is
used until the human decides. New gaps get the next number. Resolved gaps keep their row, with the decision.

| ID | Phase | Gap / conflict | Conservative default | Status |
|---|---|---|---|---|
| 001 | all | No standalone spec existed; requirements were spread over README, ARCHITECTURE, THREAT_MODEL, ADRs | `docs/SPEC.md` written from them; v1.0 behaviour is the baseline contract | open — confirm SPEC.md |
| 002 | P5–P8 | Roadmap items had no acceptance criteria | Draft criteria in SPEC.md; each phase plan needs human approval before tests are written | open |
| 003 | P6 | Account method not chosen (local passwords, magic link, or SSO-only) | Build nothing account-related until an ADR is approved; keep shared password / API key | open |
| 004 | P6 | Retention for persistent workspaces undefined | Anonymous: keep 24 h. Account workspaces: never auto-deleted; deletion only by the owner | open |
| 005 | P5 | Which public benchmark, and is its licence compatible? | Decided: Qasper (CC BY 4.0, credit it in README). Use 50 papers. Score retrieval by evidence paragraph. Unanswerable questions test refusals. | resolved |
| 006 | P5 | Where the eval trend "dashboard" lives | Static Markdown report in `eval/results/`, no hosted service | open |
| 007 | P8 | Cloud provider and budget unknown | Provider-neutral artefacts only (Dockerfile, compose, docs); no cloud resources created | open |
| 008 | P8 | SSO provider unknown | No SSO code until chosen; design keeps auth behind one interface in P6 | open |
| 009 | P7 | Should pgvector/Redis become the default backend? | No: ChromaDB + SQLite stay default; new backends opt-in via settings | open |
| 010 | P8 | Subscription (`claude_code`) mode is for personal use only (ADR 0007) | Cloud config must use `anthropic` or `bedrock`; startup refuses `claude_code` there | open |
| 011 | all | Prompt asks for `pNN-verified` tags; repo uses semver release tags (`v1.0.0`) | Use both: `pNN-verified` per phase, `vX.Y.Z` for releases, both created by the human | open |
| 012 | all | `main` has branch protection, so direct commits to main may be rejected on push | Gate commands use a phase branch + pull request | open |
| 013 | all | Full `make eval` calls Claude (quota/cost) | Phase loops use `make eval-free` only; full eval once per phase, with human approval | open |
| 014 | all | ADR 0001 says Haiku 5.5 for checks, but `config.py` uses `anthropic.claude-haiku-4-5` for Bedrock | Leave as is (may be a Bedrock availability limit); do not change model IDs without an ADR | open — confirm |
| 015 | all | Which kit files are committed | Commit `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, `.claude/commands/`, `docs/SPEC*.md`; ignore `.claude/settings.local.json` | open |
| 016 | P5 | How Qasper is obtained: one-time download or vendored subset | One-time human-approved download with the standard library (no new dependency); not vendored; HTTPS URL, archive SHA-256 and paper ids pinned in committed `eval/data/qasper_manifest.json`; URL, size and hash confirmed by the human at the first download (ADR 0009) | resolved — default accepted by the human (2026-10-31) |
| 017 | P5 | Where downloaded benchmark data is cached | `~/.cache/trustrag/qasper/`, override `TRUSTRAG_EVAL_DATA_DIR`; outside the repo because `scripts/check_invisible_chars.py` scans git-ignored folders too | resolved — default accepted by the human (2026-10-31) |
| 018 | P5 | Which Qasper split and which 50 papers | dev split; paper ids sorted; first 50 with at least one scorable question; list frozen in the manifest | resolved — default accepted by the human (2026-10-31) |
| 019 | P5 | Answerability when Qasper annotators disagree | Unanswerable only if every annotation says so; answerable only if every annotation is answerable and text evidence exists; otherwise excluded and counted by reason | resolved — default accepted by the human (2026-10-31) |
| 020 | P5 | How an evidence paragraph maps to a chunk-level hit for recall@k / MRR | Hit = chunk contains the normalised paragraph, or shares a common normalised substring of at least min(paragraph length, 200) characters; figure/table evidence dropped; union of annotators' evidence | resolved — default accepted by the human (2026-10-31) |
| 021 | P5 | One index per paper or one for all 50 papers | One workspace per paper (Qasper's own task setting) | resolved — default accepted by the human (2026-10-31) |
| 022 | P5 | How Qasper JSON maps to `Page`s | Page 1 = title + abstract; one page per section; no PDF rendering | resolved — default accepted by the human (2026-10-31) |
| 023 | P5 | Which answer-level metrics run on Qasper under `make eval` | Refusal metrics only (correct refusal on unanswerable, false refusal on answerable), answer generation only; no Answer-F1, no trust score / AUROC | resolved — default accepted by the human (2026-10-31) |
| 024 | P5 | History file format, location, whether committed, backfill | `eval/results/history.jsonl`, append-only, committed; both suites append; records `commit_dirty`; one entry backfilled from the current `results.json` (ADR 0010) | resolved — default accepted by the human (2026-10-31) |
| 025 | P5 | Which metrics the trend report tracks | List in `docs/phases/p5-plan.md` section 2.6; ablation rows excluded | resolved — default accepted by the human (2026-10-31) |
| 026 | P5 | Behaviour when the Qasper subset is missing (CI has no data) | `make eval-free` records `status: skipped`, warns, exits 0; CI workflow unchanged; P5-AC1 shown by the verifier on a machine with the data | resolved — default accepted by the human (2026-10-31) |
| 027 | P5 | ChromaDB `anonymized_telemetry` defaults to `True` (`chromadb/config.py`) and is not disabled anywhere; conflicts with "no network calls" (P5-AC1) | Turn telemetry off in the eval process only; a product-wide setting is a separate human decision (privacy) | resolved — default accepted by the human (2026-10-31) |
| 028 | P5 | `make eval-free` overwrites `results.json` / `RESULTS.md` and drops the last full run's answer and trust sections (existing behaviour) | Unchanged in P5; the history file is the durable record; the human chooses which result files to commit | resolved — default accepted by the human (2026-10-31) |
| 029 | P5 | No agent owns edits to `THREAT_MODEL.md` and `README.md` | llm-eval-engineer drafts, security-reviewer reviews the threat rows, human approves | resolved — default accepted by the human (2026-10-31) |
