---
description: Fix a TrustRAG bug test-first — reproduce with a failing test, fix, review, verify affected tests.
argument-hint: <bug description, issue text or failing command>
---

Fix this bug in TrustRAG, test-first, following `CLAUDE.md`:

$ARGUMENTS

1. **Understand** — find the code involved and the spec or ADR that defines correct behaviour. If correct
   behaviour isn't defined anywhere, log a SPEC-GAP and ask before fixing.
2. **Reproduce** — `qa-engineer` writes a test that fails because of this bug. Run it and show the real failure.
   If you cannot reproduce it, stop and report what you tried.
3. **Fix** — the owning agent (`backend`, `llm-eval-engineer` or `devops`) makes the smallest fix. The new test
   passes; run the affected test files only.
4. **Review** — `code-reviewer` on the diff; also `security-reviewer` if the bug touches untrusted input, auth,
   isolation, secrets or anything in `THREAT_MODEL.md`. Security bugs also get a THREAT_MODEL row and a
   `CHANGELOG.md` entry.
5. **Check** — run `ruff check`, `mypy` and the affected test files. Run the full suite only if the fix touched
   shared code (`src/service.py`, `src/llm.py`, `src/safety.py`, `src/state.py`, `src/settings.py`).
6. **Report** — root cause (one or two sentences), the test that now guards it, commands run with real output,
   and the git commands from `CLAUDE.md` section 9 with commit message `fix: <summary>` (no phase tag).
   Do not commit.
