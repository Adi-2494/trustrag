---
description: Build an approved TrustRAG phase end to end (tests first → build → review → fix → verify → human gate).
argument-hint: <phase number, e.g. 5>
---

Build phase **P$ARGUMENTS** of TrustRAG following the phase protocol in `CLAUDE.md` section 5. You are the
engineering manager: delegate to agents, don't skip steps.

Preconditions (stop if any fails):
- `docs/phases/p$ARGUMENTS-plan.md` exists and the human approved it in this or an earlier session.
- No open SPEC-GAP blocks this phase.
- `git status` is clean or only contains this phase's planned files.

Steps:
1. **Tests first** — `qa-engineer` writes failing tests for every acceptance criterion in the plan. Confirm each
   fails for the right reason; keep the output.
2. **Build** — assign plan tasks to `backend`, `llm-eval-engineer`, `devops`. Run independent tasks in parallel.
   Loop until the new tests pass; run only affected tests in this loop.
3. **Review** — run `code-reviewer` and `security-reviewer` **in parallel** on `git diff`.
4. **Fix** — send confirmed findings to the owning agent. Re-run only affected tests. Re-review only changed parts.
   Stop after 3 fix rounds on the same finding and bring it to the human.
5. **Verify** — run the `verifier` agent. It writes `docs/phases/p$ARGUMENTS-report.md`.
6. **Human gate** — present:
   - Verdict (VERIFIED / NOT VERIFIED) and the gate table from the report.
   - Findings and how each was resolved.
   - All SPEC-GAPs raised in this phase.
   - Anything needing approval (full `make eval`, dependencies, CI changes).
   - The exact commands from `CLAUDE.md` section 9, with the explicit file list from `git status`, commit message
     `p$ARGUMENTS: <summary>` and tag `p$ARGUMENTS-verified`. Give the tag only if the verdict is VERIFIED.

Never run `git add`, `commit`, `tag` or `push` yourself.
