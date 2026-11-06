# 0016 - Claude subscription (`claude_code`) allowed in a private single-user cloud mode only (amends 0007)

**Status:** Proposed (P8). Drafted 2026-11-06 by the architect from a decision the human made on 2026-11-06
(SPEC-GAP-010). Revised 2026-11-06 after the security review of the plan (`docs/phases/p8-plan.md` section 8,
findings M2, M4, L8). Amends ADR 0007 if accepted. ADR 0007 is not superseded: its rule "a subscription is for the
subscriber's own use; deployments for other users need an API key" still holds. Changing the status of this ADR or
of ADR 0007 is a human action (CLAUDE.md section 10).

## Context
- ADR 0007: three providers behind `LLMClient`. `claude_code` (the owner's Claude subscription, through the Claude
  Code CLI) is for local use; "products serving other people should use API keys".
- SPEC P8-AC3 (provisional wording): "Cloud config uses an API-key provider; `claude_code` provider is refused
  (ADR 0007)". SPEC-GAP-010 recorded the same default.
- Human decision, 2026-11-06 (SPEC-GAP-007): budget is $0 in total: no paid services, no domain, **no Anthropic API
  key**. With the provisional P8-AC3, a $0 cloud deployment could not call Claude at all.
- Human decision, 2026-11-06 (SPEC-GAP-010): amend P8-AC3. `claude_code` is allowed only in an explicit "private
  single-user" cloud mode: reachable only through Tailscale, exactly one account, and startup refuses `claude_code`
  otherwise. Any multi-user or public deployment still needs `anthropic` or `bedrock`. Rationale given by the human:
  a VM that only its owner can reach and use is "the subscriber's own use" in the sense of ADR 0007.
- Code today (read): `src/settings.py` has `llm_provider: Literal["claude_code", "anthropic", "bedrock"]`, default
  `claude_code`, and no notion of a deployment mode. `src/llm.py` selects the provider from `config.PROVIDER`.
  Accounts are created only by the admin CLI (`src/accounts.py`, ADR 0011, SPEC-GAP-031). `src/state.py` has
  `list_accounts()`; there is no account count function.
- The app cannot observe "the network path is Tailscale". It can observe its own settings, the number of account
  rows, and whether a request carried the trusted identity header (ADR 0017).
- Whether Anthropic's terms allow a subscription to be used from a cloud VM that only the subscriber reaches is a
  legal question. **Not verified by me**; the human made the decision and owns it.

## Decision
- **A new setting `DEPLOYMENT_MODE`**: `local` (default), `private_cloud`, `cloud`. `local` keeps every v1.0/P6/P7
  behaviour unchanged, including `claude_code` as the default provider. The mode is **declared by the operator**; it
  is a guard against misconfiguration, not a control against the owner (who could set `local` on the VM).
- **`cloud`** (any multi-user or public deployment): `LLM_PROVIDER` must be `anthropic` or `bedrock`. `claude_code`
  fails settings validation with a fixed message. This is the provisional P8-AC3 rule, kept.
- **`private_cloud`** (the P8 reference setup, ADR 0017/0018). Settings validation requires all of:
  - `SSO_MODE=tailscale` (access only through `tailscale serve` with the trusted identity, ADR 0017);
  - `ACCOUNTS_ENABLED=true`;
  - exactly one value in `SSO_ALLOWED_LOGIN` and a valid canonical username in `SSO_ACCOUNT`;
  - local backends (`VECTOR_STORE=chroma`, `STATE_BACKEND=sqlite`): server mode exists for several hosts, which is
    not a single-user shape (SPEC-GAP-102).
  A runtime check (needs the state DB, so it cannot live in pydantic validation) requires **exactly one account row,
  enabled, whose username equals `SSO_ACCOUNT`** (SPEC-GAP-103). These rules apply whatever the provider; with
  `claude_code` they are what makes the subscription use "own use".
- **Where the runtime check runs** (fail closed at every point):
  1. at process start of the cloud UI server (ASGI lifespan, ADR 0018): the process exits non-zero with one fixed
     sentence, no account names, no paths. The FastAPI app (`api/main.py`) refuses to start at all when
     `DEPLOYMENT_MODE=private_cloud`: it offers password sign-in and anonymous workspaces without the identity gate,
     and exposing it is out of scope for the private shape (SPEC-GAP-099). In `cloud` mode the API starts as today
     (with an API-key provider);
  2. at every trusted-identity sign-in (ADR 0017), before a session is created;
  3. in the admin CLI: `create` is refused in `private_cloud` when any account row exists. This is check-then-insert,
     not one transaction; two concurrent admin runs could both pass. Accepted in writing (SPEC-GAP-128): two accounts
     make every other check refuse, so the race fails closed. `src/state.py` is unchanged;
  4. in `app.py` itself: in `private_cloud` it refuses to render unless the in-process marker
     `src.deployment.SERVED_BY_UI_SERVER` (a module attribute set by `src/ui_server.py`, never an environment
     variable) is true, and it runs the check once per process. So `streamlit run app.py`, the Dockerfile's default
     command, can never serve private mode without the security middleware and the lifespan check (SPEC-GAP-120).
- **Environment refusal** (SPEC-GAP-122): `private_cloud` refuses to start when any of these is set to a non-empty
  value: `CLAUDE_CODE_OAUTH_TOKEN` (the token must be the mounted file, not visible to `docker inspect`),
  `TRUSTRAG_API_URL` (the UI must use the in-process service; a remote API has no identity gate), `API_KEY` (the API
  does not run), and, with `claude_code`, `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`. The message is fixed and
  names no value.
  Account creation for the first boot still works: the CLI is a separate process (`docker compose run --rm ...`),
  not gated by the UI server's lifespan.
- **Refusal text** (fixed, shown in logs and on the page): "This deployment mode needs exactly one enabled account
  that matches SSO_ACCOUNT. See the deployment guide." Settings errors follow the existing pattern in
  `src/settings.py` (`InitErrorDetails` with empty `input`), so no value is echoed.
- No change to `LLMClient`, `src/llm.py` provider dispatch or `src/claude_code.py` token handling (token stays a
  read-only mounted file, passed only to the CLI process, THREAT_MODEL row 12).

## Consequences
- The $0 budget works: the owner's subscription answers questions on the private VM.
- A single-user deployment cannot quietly become a shared one: adding a second account (CLI refuses), adding a second
  allowed login (validation refuses), switching to server mode (validation refuses) or turning SSO off (validation
  refuses) all stop the app until the mode is changed, and changing to `cloud` then refuses `claude_code`.
- Residual: the owner can set `DEPLOYMENT_MODE=local` on the VM and bypass everything. Accepted: the guard protects
  against mistakes; misuse by the owner is out of scope (THREAT_MODEL row 69 draft).
- Residual: anyone who controls the owner's tailnet identity, or a device signed in to it, uses the subscription
  (ADR 0017, THREAT_MODEL row 77 draft).
- Usage still counts toward the plan's limits; `MAX_CALLS_PER_HOUR` is unchanged.
- README, `.env.example` and `ARCHITECTURE.md` must state the three modes; ADR 0007 gets an "amended by 0016" note
  when the human accepts this ADR.
- SPEC P8-AC3 wording changes (decided by the human 2026-11-06; final wording confirmed at the phase gate).

## Evidence
Read while planning: `docs/adr/0007-subscription-mode-for-local-use.md`, `docs/SPEC.md` P8, `docs/SPEC-GAPS.md` 010,
`src/settings.py`, `src/config.py`, `src/llm.py` (provider dispatch, grep), `src/claude_code.py` (token file handling,
grep), `src/accounts.py`, `src/state.py` (grep `list_accounts`, `create_account`).
Not verified: Anthropic's terms for subscription use on a cloud VM (human decision); the exact ASGI lifespan
behaviour of the cloud UI server (ADR 0018, SPEC-GAP-097). Tests are planned in `docs/phases/p8-plan.md` section 3.3.
