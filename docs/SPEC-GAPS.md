# Spec gaps

Questions the spec does not answer, or places where sources conflict. Each has a conservative default that is
used until the human decides. New gaps get the next number. Resolved gaps keep their row, with the decision.

| ID | Phase | Gap / conflict | Conservative default | Status |
|---|---|---|---|---|
| 001 | all | No standalone spec existed; requirements were spread over README, ARCHITECTURE, THREAT_MODEL, ADRs | `docs/SPEC.md` written from them; v1.0 behaviour is the baseline contract | open — confirm SPEC.md |
| 002 | P5–P8 | Roadmap items had no acceptance criteria | Draft criteria in SPEC.md; each phase plan needs human approval before tests are written | open |
| 003 | P6 | Account method not chosen (local passwords, magic link, or SSO-only) | Build nothing account-related until an ADR is approved; keep shared password / API key | open |
| 004 | P6 | Retention for persistent workspaces undefined | Anonymous: keep 24 h. Account workspaces: never auto-deleted; deletion only by the owner | open |
| 005 | P5 | Which public benchmark, and is its licence compatible? | Download nothing until the human approves a dataset and its licence | open |
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
