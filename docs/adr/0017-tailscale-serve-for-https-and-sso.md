# 0017 - HTTPS and SSO through `tailscale serve`: trusted identity header, one allowed login, behind the existing `Authenticator`

**Status:** Proposed (P8). Drafted 2026-11-06 by the architect. The SSO provider (Tailscale free personal plan) and
the HTTPS approach (`tailscale serve` on the free `*.ts.net` name, Tailscale-issued certificates, no Funnel) were
decided by the human on 2026-11-06 (SPEC-GAP-008). This record decides how the app consumes that identity and where
Tailscale runs. It adds a security control (THREAT_MODEL rows 64-68 and 78-82 draft), so it needs human approval. Revised 2026-11-06
after the security review of the plan (`docs/phases/p8-plan.md` section 8, findings H1, H2, M1, M3, M5, M6, L1, L6).

## Context
- SPEC P8-AC1: HTTPS only; HTTP redirects; security headers set. P8-AC2: SSO login maps to P6 accounts.
- ADR 0011: `Authenticator` protocol (`resolve`, `start_session`, `logout`) in `src/interfaces.py`; "P8 SSO adds
  another login class that calls the same `start_session`". `SessionAuthenticator` stores only `sha256(token)`.
  Accounts are admin-created only (SPEC-GAP-031). `src/service.py` has a `LoginProvider` protocol shaped for a
  username/password body (`login(raw_body, client_key)`).
- SPEC-GAP-048 (resolved, P6): the app never reads `X-Forwarded-For`; uvicorn runs with `--no-proxy-headers`; "a
  trusted-proxy list is a P8 decision".
- Streamlit 1.66.1 (installed, read): `st.context.headers` returns the headers of the initial (WebSocket) request as
  a `StreamlitHeaders` mapping with `get_all(key)`; for a repeated key, `[key]` returns the first value in the code
  (`_headers[key][0]`) although the docstring says the last, and `get_all` returns all values. `server.trustedUserHeaders` (hidden, "experimental API subject to change") copies the **first**
  value of a configured header into `st.user`. The WebSocket origin check allows a missing `Origin`, same-origin, and
  a built-in list that includes `localhost` and `127.0.0.1` when `server.enableCORS` is true.
- **What I could not verify** (no web access in this session): the exact name and value format of the identity
  headers that `tailscale serve` adds when proxying; whether it removes or overwrites a client-supplied header of
  the same name; whether it adds them on proxied WebSocket upgrades; whether it adds any security headers (HSTS);
  whether it listens on or redirects plain HTTP; the exact CLI syntax of `tailscale serve` on the version that will
  be installed. From memory (unverified), the header is `Tailscale-User-Login`, set to the login name of the
  connecting user, and not set for tagged source devices or Funnel traffic. Each of these is a SPEC-GAP (093-095)
  with a verification step on the VM.
- Threat that decides most of this design: **identity header spoofing**. A header is only as trustworthy as the one
  hop that sets it. Anything that can reach the app without passing through `tailscale serve` can claim to be the
  owner.

## Decision

**Where Tailscale runs: on the VM host, from Tailscale's signed package repository** (SPEC-GAP-097)

| Option | For | Against |
|---|---|---|
| Host package (chosen) | Tailscale's documented install path; OS package manager applies security updates; no auth key to store (interactive `tailscale up` login); no new image or pin in the repo; repo stays provider-neutral | Not hash-pinned by the repo (vendor-signed repo instead, residual row 76); the app container must listen on its own network namespace and publish to host loopback, so any host process could spoof the header (covered by "a compromised host") |
| Sidecar container `tailscale/tailscale` pinned by digest, app in its network namespace | App can bind 127.0.0.1 in the shared namespace (nothing else can connect); everything in compose | New image to pin and update (human gate); needs an auth key secret and a state volume; serve config via container environment whose exact form is not verified; digest updates lag security fixes |

**Exposure** (SPEC-GAP-099)
- Only the Streamlit UI is served, as one container running the in-process `TrustRAGService` (no `TRUSTRAG_API_URL`).
  The FastAPI API is not run and not exposed in the reference setup. The API **never** reads the identity header in
  any mode (a test pins this), so the UI-to-API hop of the default compose can never carry a trusted identity.
- The container publishes `127.0.0.1:7861:7860` only, on a dedicated compose network with no other service. Port
  7861 is used by no other shape, so a default compose started on the VM by mistake is never what serve proxies
  (SPEC-GAP-119). `tailscale serve` on the host proxies HTTPS on the tailnet name to `http://127.0.0.1:7861`. No
  plain-HTTP listener exists on the tailnet or the public interface (SPEC-GAP-095). Funnel is never enabled, and the
  tailnet policy removes/denies the `funnel` node attribute (SPEC-GAP-123). OCI ingress rules allow nothing after
  setup (SPEC-GAP-109).
- **The VM is a tagged node** (`tag:trustrag`, `tagOwners` = the owner only). As I recall (not verified,
  SPEC-GAP-116), serve adds no user identity for requests from tagged sources, so anything that originates on the VM
  (a host process, the container, an SSRF in any component) reaching serve is `missing` and refused. The policy gives
  no grant from `tag:trustrag` to itself. Trade-off: tagged nodes may have key expiry off (not verified), which differs
  from the earlier SPEC-GAP-110 default; the key-expiry state is recorded at setup.
- **Residual, accepted for a single-user VM:** any local process on the VM can connect to `127.0.0.1:7861` directly
  and send any header. This is part of "a compromised host". A unix-socket target would narrow it if serve supports
  one (not verified, SPEC-GAP-121).

**Trusted identity sign-in** (`SSO_MODE=tailscale`; default `off`)
- With `SSO_MODE=off` (every existing shape) the identity header is ignored entirely.
- Header name is a code constant, `config.SSO_IDENTITY_HEADER = "Tailscale-User-Login"`, not an environment setting
  (a typo in an environment value must not silently trust another header). It changes only with a code change after
  the VM verification step (SPEC-GAP-093).
- Parsing (`src/sso.py`, `identity_from_headers(headers) -> str | None`), fail closed:
  - exactly one value, read **only** through `get_all` (never `[]` or `get`), header names compared
    case-insensitively: zero or two-plus values = refused (defeats "proxy appends, app reads first/last");
  - ASCII only, printable, no whitespace or comma, 3-254 characters, then lower-cased;
  - anything else = refused. The header value is never logged, echoed or stored (SPEC-GAP-113).
- **Mapping to the P6 account** (SPEC-GAP-100): settings `SSO_ALLOWED_LOGIN` (exactly one login; compared with
  `hmac.compare_digest` after lower-casing) and `SSO_ACCOUNT` (a P6 canonical username). The account is created by
  the admin with the existing CLI. **No account is ever created by sign-in** (no auto sign-up).
- New login class `TrustedHeaderLogin` in `src/sso.py` (not a new interface): checks mode, identity, allow-list,
  the ADR 0016 single-account rule, that the account exists and is enabled, then calls the injected
  `Authenticator.start_session(account_id, username)`. The session is an ordinary P6 server-side session (256-bit
  token, sha256 at rest, idle 2 h / absolute 24 h, cap 10). `TrustRAGService` gains
  `login_trusted_identity(identity: str) -> SessionInfo` that delegates to it. `Authenticator`, `VectorStore`,
  `LLMClient` and `src/state.py` interfaces are unchanged.
- No password check, so no KDF and no login rate-limit reservation on this path: there is no secret to guess. The
  session cap bounds session rows; sign-in happens once per Streamlit session.
- **UI in SSO mode** (SPEC-GAP-101): the identity gate runs before anything else (after the optional `APP_PASSWORD`
  gate, which stays unchanged). Refused identity = fixed page, no anonymous fallback, no password form. Accepted
  identity = signed in to the mapped account. "Continue without an account" stays available behind the identity
  gate (P6-AC3: anonymous behaviour unchanged once chosen). The password sign-in form is hidden. "Sign out" ends the
  TrustRAG session and shows "Signed out. Reload the page to sign in again." (no automatic re-sign-in in that
  Streamlit session). Session expiry clears state (THREAT_MODEL row 39) and shows the identity gate again.

**HTTPS and security headers: which layer does what** (SPEC-GAP-095, -096, -098)

| Concern | Layer | Testable in CI? |
|---|---|---|
| TLS certificate for `<machine>.<tailnet>.ts.net` and HTTPS termination | `tailscale serve` (host) | No; manual check on the VM (plan V1) |
| No plain HTTP | Nothing listens on HTTP; container publishes loopback only | Compose file test in CI; port check on the VM (V2) |
| `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy: frame-ancestors 'none'`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy` | App: a pure ASGI middleware (`src/web_security.py`) around Streamlit's ASGI app (`st.App`, ADR 0018) | Yes (Starlette `TestClient`); header presence through serve checked on the VM (V1) |
| Cross-origin requests riding the network identity (cross-site WebSocket, upload POST) | Same middleware: a request with an `Origin` header that is not byte-for-byte `https://` + `PUBLIC_HOSTNAME` (`null`, suffixes, case, trailing dot, explicit port and duplicate `Origin` headers all refused) gets a fixed 403 (HTTP) or a close before accept (WebSocket); lifespan scopes pass, unknown scope types are refused; Streamlit XSRF and CORS stay on | Yes |
| Client address / proxy headers | Not used for identity; uvicorn `--no-proxy-headers`; `X-Forwarded-For` never read (SPEC-GAP-048 stays as is, SPEC-GAP-114) | Yes (existing test pattern) |

**Verification before SSO is switched on.** The human runs V3 (forged header on a plain GET), V3b (forged header on
the `/_stcore/stream` WebSocket handshake with a correct `Origin`), V3c (from a tagged or shared-in source, or policy
evidence) and V9 (from the VM host and from inside the container). Any 200 or `auth.sso_login_succeeded` = fail:
`SSO_MODE` stays `off`. These checks use the diagnostic route `GET /_trustrag/identity`, which exists only while
`SSO_DIAGNOSTICS=true` (off by default) and reports account state only for the allowed login (SPEC-GAP-115, -124).

**Revocation.** Removing a device or user from the tailnet does not end a live TrustRAG session (up to 2 h idle / 24 h
absolute). The runbook says to disable and re-enable the account (revokes all sessions) or restart the container.

- A full `Content-Security-Policy` is **not** set: Streamlit's needs for inline scripts and styles are not verified,
  and a wrong policy breaks the UI. Only `frame-ancestors 'none'`. HSTS: `max-age=31536000`, no `includeSubDomains`,
  no `preload` (a shared `ts.net` parent domain is not ours).

## Consequences
- SSO with $0 and no code that talks to an identity provider: login, MFA and account recovery are the identity
  provider's and Tailscale's job. TrustRAG's own sign-in path in this mode is a header check plus `start_session`.
- The security of sign-in rests on two facts the app cannot check itself: (1) only `tailscale serve` can reach the
  app port, (2) serve overwrites or strips a client-supplied identity header. (1) is enforced by deployment files and
  tested in CI; (2) is a VM verification step that must pass before the human enables `SSO_MODE=tailscale`
  (SPEC-GAP-093). If (2) fails, SSO stays off and P8-AC2 is blocked: stop and ask.
- Ambient authority: any web page open in the owner's browser on a tailnet device can make that browser connect to
  the app, and serve will attach the owner's identity. The strict `Origin` check and frame denial are the controls
  (row 67 draft).
- Anyone holding the owner's identity-provider login or a device signed in to the tailnet has full access (row 77
  draft). Device hygiene, IdP MFA and tailnet ACLs are documented in the runbook, not enforced by code.
- Public certificate transparency logs will show the machine and tailnet name (my recollection of Tailscale's docs;
  not verified). The runbook says to choose names with no personal information (row 75 draft).
- New settings: `SSO_MODE`, `SSO_ALLOWED_LOGIN`, `SSO_ACCOUNT`, `PUBLIC_HOSTNAME`. New modules: `src/sso.py`,
  `src/web_security.py`. No new dependency.

## Evidence
Read while planning: `src/interfaces.py`, `src/accounts.py`, `src/service.py`, `api/main.py`, `app.py` (sign-in flow),
`src/settings.py`, `src/config.py`, ADR 0011, SPEC-GAP-031, -035, -048. Streamlit 1.66.1 in `.venv` (read):
`streamlit/runtime/context.py` (`StreamlitHeaders.get_all`, `headers` docstring "headers sent in the initial
request"), `streamlit/config.py` (`server.trustedUserHeaders`, `server.allowedHosts`, `server.enableXsrfProtection`
descriptions), `streamlit/web/server/starlette/starlette_websocket.py` (`_gather_user_info` takes the first value;
`_is_origin_allowed` allows a missing Origin), `streamlit/web/server/server_util.py` (`is_url_from_allowed_origins`
list includes localhost and 127.0.0.1). Versions: streamlit 1.66.1, fastapi 0.162.0, starlette 1.10.0, uvicorn
0.54.0. Not verified: every Tailscale behaviour listed in Context.
