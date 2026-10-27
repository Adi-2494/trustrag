---
description: Phase 0 for the next roadmap phase — design, ADRs, data model, API contracts, scaffold and CI plan. No feature code.
argument-hint: <phase number, e.g. 5>
---

Run the architecture step for phase **P$ARGUMENTS** of TrustRAG. Follow `CLAUDE.md` exactly.

TrustRAG v1.0 is already built, so this designs the next phase on top of the existing architecture. It does not
rebuild the repo.

1. Read `CLAUDE.md`, `docs/SPEC.md` (section for P$ARGUMENTS), `docs/SPEC-GAPS.md`, `docs/adr/`, `ARCHITECTURE.md`,
   `THREAT_MODEL.md`. If P$ARGUMENTS is not in SPEC.md, stop and say so.
2. Check for blocking SPEC-GAPs for this phase (status `open`, default says "build nothing until…"). List them and
   stop if any block the design.
3. Delegate to the `architect` agent: write `docs/phases/p$ARGUMENTS-plan.md` (output contract in its agent file),
   draft any new ADRs as `Status: Proposed`, and add new SPEC-GAPs.
4. If the phase needs scaffolding (new empty modules, a compose profile, a CI job), list it in the plan as tasks
   for `backend` / `devops`. Do not create it yet.
5. Run `security-reviewer` on the plan only (threat model delta) in read-only mode, and add its points to the plan.
6. **Human gate.** Present: plan summary, proposed ADRs, CI/dependency changes needing approval, all open
   SPEC-GAPs with their defaults, and the git commands from `CLAUDE.md` section 9 for the docs written (phase tag
   not used yet; commit message `p$ARGUMENTS: architecture plan`). Do not start `/build-phase` until the human approves.
