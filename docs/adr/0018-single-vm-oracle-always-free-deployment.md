# 0018 - Reference cloud deployment: one Oracle Cloud Always Free Ampere A1 VM, one container, local storage, secrets as root-managed files

**Status:** Proposed (P8). Drafted 2026-11-06 by the architect. Host and budget decided by the human on 2026-11-06
(SPEC-GAP-007): Oracle Cloud "Always Free" Ampere A1 (ARM) VM, $0 in total. The secret-store choice below deviates
from the wording of P8-AC4 and needs human confirmation (SPEC-GAP-104). Revised 2026-11-06 after the security
review of the plan (`docs/phases/p8-plan.md` section 8, findings H3, M1, M3, L3, L4, L5, L7).

## Context
- SPEC P8-AC4: secrets come from the platform's secret store, never from the image or repo. P8-AC5: deployment is
  reproducible from the repo plus documented manual steps; no step is done by Claude.
- Free-tier size is uncertain (human, 2026-11-06): older Oracle documents say 4 OCPU / 24 GB for A1; 2026 reports say
  halved to 2 OCPU / 12 GB. **Not verified by me.** This design must fit 2 OCPU / 12 GB.
- The Docker image is multi-arch (`Dockerfile`, CI `docker` job builds `linux/amd64,linux/arm64` with QEMU). The
  Dockerfile runs `claude --version` after installing `@anthropic-ai/claude-code@2.1.310`, so a successful arm64
  build shows the CLI starts on arm64. I did not check the result of the latest CI run. The dev machine is arm64
  (`uname -m` → `arm64`), so the linux/arm64 image can be run natively there.
- Subscription token today: a read-only file mount at `/run/secrets/claude_code_oauth_token`, read by
  `src/claude_code.py` and passed only to the CLI process (THREAT_MODEL row 12).
- ADR 0003/0005: embedded ChromaDB + SQLite are owned by one host; P7 server mode (ADRs 0013-0015) exists for several
  hosts. ADR 0016: private single-user mode requires local backends.
- Streamlit 1.66.1 offers `st.App` (`streamlit.starlette.App`), an ASGI app that accepts `middleware=` and
  `lifespan=` and can be run by uvicorn (docstring read). It is the only way I found to add response headers to
  Streamlit without another proxy. Its parity with `streamlit run` (config.toml loading, `st.context`, XSRF) is
  **not verified** (SPEC-GAP-097).
- OCI Vault: from memory, Always Free includes a number of Vault secrets, but this is **not verified**, and reading
  them from the VM needs the OCI CLI or SDK on the host plus an instance-principal policy (a new identity that can
  read secrets; a new tool on the host).

## Decision
- **Host**: one OCI Always Free Ampere A1 VM (aarch64), Canonical Ubuntu LTS image (exact version chosen by the human
  at setup, SPEC-GAP-108). Sized for 2 OCPU / 12 GB; nothing in the design needs more.
- **Packages on the host** (human installs, runbook): Docker Engine with the compose plugin from Docker's signed apt
  repository; Tailscale from Tailscale's signed apt repository (ADR 0017); `unattended-upgrades` on. No
  `curl | sh` installers; vendor signing-key fingerprints recorded and compared. Not hash-pinned by the repo
  (residual, row 76 draft). `docker version` is recorded and must be a release with the loopback-publish fix (not
  verified which, SPEC-GAP-127).
- **Image**: built on the VM from a git checkout of the exact gate-A commit SHA (`git clone`, `git checkout <sha>`,
  `docker compose -f docker-compose.cloud.yml build`), not a tag or branch name, native arm64, same `Dockerfile`, same hash-locked `requirements.lock`. No
  registry, no push credentials (SPEC-GAP-111).
- **One container** (`docker-compose.cloud.yml`, a new file; the default `docker-compose.yml` is untouched): service
  `trustrag`, image `trustrag:latest` built from the repo, command `uvicorn src.ui_server:app --host 0.0.0.0 --port
  7860 --no-proxy-headers`, where `src/ui_server.py` sets the in-process marker and builds `st.App("app.py",
  middleware=[security middleware], lifespan=<ADR 0016 startup check>)`. Port `127.0.0.1:7861:7860` only (a host port
  no other shape uses, SPEC-GAP-119), on a dedicated named network with only this service. No API container, no
  server-profile services. `DEPLOYMENT_MODE=private_cloud`, `SSO_MODE=tailscale`, `ACCOUNTS_ENABLED=true`,
  `VECTOR_STORE=chroma`, `STATE_BACKEND=sqlite` set in `environment:` explicitly; the rest from `.env.cloud` (copied
  from `.env.cloud.example`; git- and docker-ignored), never `.env`, so tooling on a developer machine never reads the
  developer's real `.env` through this file (SPEC-GAP-118). The token path default is `/etc/trustrag/claude_token`,
  outside `$HOME` and the repo. The runbook forbids running the default compose or `docker run` on the VM.
  Fallback if `st.App` fails parity: stop and ask (the alternatives are a proxy image, a human gate, or accepting
  missing headers).
- **Storage**: local mode (ChromaDB + SQLite WAL) on a named Docker volume on the boot volume. Justification: one
  host, one user, no concurrency need; server mode would add two containers and two passwords for no benefit and is
  refused by ADR 0016 in private mode.
- **Secrets** (deviation from P8-AC4 wording, SPEC-GAP-104): the conservative default is **files on the VM, never in
  the image, the repo, `.env`, the container environment or `docker inspect`**:
  - `/etc/trustrag/` owned by root, mode 0700; `/etc/trustrag/claude_token` mode 0400, owned by the container's
    `appuser` uid (to be read with `docker run --rm trustrag id -u` at setup; not verified), bind-mounted read-only
    at `/run/secrets/claude_code_oauth_token` exactly as today. That uid may equal the host `ubuntu` user's uid
    (1000), who could then read the token; V6 checks it, and a fixed uid in the Dockerfile would be a separate gate
    (SPEC-GAP-125).
  - Optional `APP_PASSWORD` is not needed in this shape (SSO is the gate); if the human wants it, it goes in `.env`
    (0600, owned by the deploying user), as today.
  - The Tailscale node key stays in `tailscaled`'s own state on the host. No OCI API keys or instance-principal
    policies exist on or for the VM.
  - OCI Vault is not used. If the human wants it, a later ADR covers OCI CLI on the host, the instance-principal
    policy, and fetching the secret into the same 0400 file at boot.
- **Backups** (SPEC-GAP-107): manual only. Stop the container, archive the data volume (ChromaDB + SQLite state),
  copy it over the tailnet to the owner's machine (stored 0600 or encrypted), start again. Never to object storage;
  never includes `/etc/trustrag/`. OCI boot-volume backups and custom images of this VM are forbidden: they would
  contain `/etc/trustrag` and `/var/lib/tailscale`. Restore and deletion are human gates.
- **Resource limits** (SPEC-GAP-106): none set until measured. The verifier measures idle memory of the linux/arm64
  image on the dev machine with a scratch `.env.cloud` from the example and an empty token file, using
  `docker compose ... config -q` only (never printing resolved values) and making no upload and no question (no Claude
  call, no quota); the human measures under real load on the VM; a
  `mem_limit` is proposed afterwards.
- **Nothing is done by Claude** on OCI, Tailscale or the VM: no resource creation, no SSH, no commands. The runbook
  (`docs/deploy/oracle-tailscale.md`, devops) lists every manual step, including the verification steps V1-V8 in
  `docs/phases/p8-plan.md` section 3.

## Consequences
- $0: Always Free VM, free Tailscale plan, free `ts.net` certificate, no registry, no paid secret store.
- Reproducible from the repo plus the runbook; the image is the same one CI builds for arm64.
- Accepted risks (row 74 draft): Oracle may reclaim idle Always Free instances, and A1 capacity may be unavailable in
  the chosen region (both from memory, not verified); single VM = single point of failure; manual backups only.
- AC4 is met in substance (secrets never in image or repo, not in the container environment) but not literally
  ("platform's secret store"). The human confirms or rejects this at T0.
- The default local shapes (`streamlit run`, `docker run`, `docker compose up`, server profile) do not change.

## Evidence
Read: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.gitignore`, `.env.example`, `.github/workflows/tests.yml`
(`docker` job), `src/claude_code.py` (grep: token file read, env passed to the CLI), `src/settings.py`,
`src/config.py`, `.streamlit/config.toml`, `ARCHITECTURE.md` "Deployment shapes", Streamlit 1.66.1
`streamlit/web/server/starlette/starlette_app.py` (`App` docstring: `middleware`, `lifespan`, "User middleware runs
before Streamlit's internal middleware") and `streamlit/starlette.py` ("Run with uvicorn"). Ran: `uname -m` → `arm64`.
Not verified: Oracle free-tier limits, idle reclamation and capacity; OCI Vault free quota; the latest CI arm64 build
result; `st.App` parity; the container `appuser` uid.
