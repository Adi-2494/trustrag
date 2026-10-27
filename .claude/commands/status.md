---
description: Show TrustRAG project status — phases, open SPEC-GAPs, last verification, uncommitted work, next step.
---

Report TrustRAG's status. Read only; change nothing; no agents needed.

Gather:
1. Phase table from `docs/SPEC.md`, plus for each phase whether `docs/phases/pNN-plan.md` and
   `docs/phases/pNN-report.md` exist and the report's verdict.
2. Phase tags: `git log --oneline --decorate -n 30` (look for `pNN-verified` and `vX.Y.Z`).
3. Open rows in `docs/SPEC-GAPS.md` (count, and list any that block the next phase).
4. Working tree: `git status --short` — group into "phase work not yet committed" and "unexpected".
5. Latest eval baseline from the header of `eval/results/RESULTS.md`.

Output (short):
- One table: phase | plan | built | verified | committed/tagged.
- Open SPEC-GAPs needing a human decision.
- Uncommitted files, and whether they need the human to run git commands.
- **Next step**: the single command to run next (e.g. `/architecture 5`, `/build-phase 5`, or "human: run the
  commit commands from the P5 gate").
