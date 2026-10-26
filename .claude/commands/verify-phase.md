---
description: Independently re-verify a TrustRAG phase (or the current v1.0 baseline) with the read-only verifier.
argument-hint: <phase number, or "baseline">
---

Run the `verifier` agent for **$ARGUMENTS**.

- If `$ARGUMENTS` is a phase number: verify against `docs/SPEC.md` P$ARGUMENTS and `docs/phases/p$ARGUMENTS-plan.md`;
  the report goes to `docs/phases/p$ARGUMENTS-report.md`.
- If `$ARGUMENTS` is `baseline`: verify the v1.0 baseline requirements B1–B9 in `docs/SPEC.md`; the report goes to
  `docs/phases/baseline-report.md`.

The verifier runs the full gate from `CLAUDE.md` section 8 itself and does not reuse other agents' results.
Do not fix anything in this command. Afterwards, show the verdict, the gate table and any failures, and suggest
`/fix-bug` for each failure. Do not run git write commands; if the verdict is VERIFIED and nothing is committed
yet, print the section 9 commands for the human.
