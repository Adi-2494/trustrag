# 0011 - Local password accounts with server-side sessions, behind one `Authenticator` interface

**Status:** Accepted (P6; approved by the human, 2026-11-04). The account method was decided by the human on
2026-11-01 (SPEC-GAP-003). This record describes how it is built. It was revised on 2026-11-02 after the security
review of the plan (`docs/phases/p6-plan.md` section 9).

## Context
P6 lets users keep their documents across sessions without sharing a password (SPEC P6). On 2026-11-01 the human
chose **local passwords**: username and password, a slow password hash, rate-limited and timing-safe login, and
server-side sessions (SPEC-GAP-003). P8 adds SSO later, so authentication must sit behind one interface that SSO can
plug into (SPEC-GAP-008). Other constraints:

- P6-AC2: credentials are never stored or logged in plain text, and login is rate-limited and timing-safe.
- P6-AC4: the threat model gains rows for account takeover, enumeration and session theft, each with a test.
- P6-AC3: anonymous mode keeps B5 behaviour.
- Stack: Python 3.11, and no new dependency without an ADR and a human gate. All storage goes through `src/state.py`
  (SQLite WAL, ADR 0005).
- Streamlit 1.66.1 can **read** cookies but cannot **set** them. Its own docstring says `st.context.ip_address` "should
  not be used for security measures because it can easily be spoofed" and is `None` on localhost.
- uvicorn 0.54.0 defaults to `proxy_headers=True` with `forwarded_allow_ips` = `$FORWARDED_ALLOW_IPS` or
  `"127.0.0.1,::1"`. So by default a loopback client can set its own client address with `X-Forwarded-For`.
- FastAPI 0.162.0 echoes the rejected value (`"input"`) in 422 bodies.

Options for the password hash: Argon2id (`argon2-cffi`, not installed, so a new dependency); `bcrypt` (only a
transitive dependency of chromadb, and it limits passwords to 72 bytes); `hashlib.scrypt` from the standard library
(available in `.venv`, OpenSSL 3.6.4).

## Decision

**Accounts**
- Accounts are created by the administrator only, with the CLI `python -m src.accounts create|set-password|disable|enable|list`.
  There is no sign-up endpoint (SPEC-GAP-031).
- How the CLI reads passwords (SPEC-GAP-055):
  - Passwords come from `getpass` and must be typed twice to confirm.
  - If stdin is not a TTY, the CLI refuses unless `--password-stdin` is given (one line is read).
  - A password is never taken from argv or the environment.
  - Errors are fixed strings.
  - The CLI must run as the service user, so that the 0600 state file keeps its owner.
- **Username canonicalisation** (SPEC-GAP-033):
  - Non-ASCII input is rejected first (`str.isascii()`), before `lower()`. This blocks case folding such as the
    Kelvin sign `K` becoming `k`.
  - Then `lower()`, then `re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}")`.
  - The resulting single canonical value is used for the rate-limit key, the account lookup and the CLI.
- Password policy: 12 to 1024 characters, not equal to the username (SPEC-GAP-032).
- `ACCOUNTS_ENABLED` (default `false`) controls **only** these: sign-in, sessions, and the new routes and UI. The
  ownership checks and the retention keep-set are **always on**, whatever the flag says (ADR 0012).

**Password hashing (standard library, no new dependency)**
- `hashlib.scrypt` with N = 2^17, r = 8 and p = 1, a 16-byte salt from `secrets`, a 32-byte key and
  `maxmem` = 256 MiB. A probe on the dev machine took 0.307 s per hash.
- Hashes are stored as `scrypt$n=…,r=…,p=…$<salt>$<key>`.
- **Rehash on login:** after a successful check, a hash stored with different parameters is replaced by one made with
  the current parameters.
- `DUMMY_HASH` is built at import with the current parameters. A test asserts that its parameters equal the
  configured ones, and this is the **primary** proof that login is timing-safe. A wall-clock test is secondary.
- The scrypt parameters are from my memory of OWASP and are **not verified**. Argon2id is still available as a human
  choice, but it is a dependency gate.

**Login order (each step is fixed)**
1. **Read the body yourself.** At most 4 KiB, using the declared `Content-Length` and a capped read. There is no
   pydantic body model. Parse with `json.loads`, then require an object with string `username` and `password`, and a
   password of at most 1024 characters. Any other shape, including an oversized body, gets the **same**
   `401 {"detail":"Wrong username or password."}`, and no input is echoed. Bad-shape attempts still go through
   step 2, using only the client and global keys.
2. **Reserve the attempt atomically.** One `BEGIN IMMEDIATE` transaction prunes expired rows across the whole table,
   counts the rows for the user key, the client key and the global key, and either refuses or **inserts the
   reservation rows**. Concurrent wrong guesses therefore cannot all pass the check before any of them is recorded.
   - Limits: 5 per user and 20 per client in a rolling 15 minutes, and a **global cap of 5 login attempts per
     second** across processes (SPEC-GAP-049).
   - A refusal returns `429 {"detail":"Too many sign-in attempts. Please try again later."}` with `Retry-After`, and
     no hash is run.
3. Look up the account.
4. Acquire the hashing semaphore (`KDF_MAX_CONCURRENT = 2`) with a 5-second timeout. On timeout, the reservation is
   refunded and the client gets the same 429 body with `Retry-After`.
5. Run exactly one scrypt, against the stored hash or `DUMMY_HASH`, then `hmac.compare_digest`.
6. **Success** (correct password and account enabled): one transaction re-checks `disabled`, deletes this attempt's
   reservation rows for the user and client keys (a refund), creates the session, enforces the per-account session
   cap, and rehashes if needed. **Failure:** the reservation rows stay and count as failures.

Unknown user, wrong password, disabled account and bad shape all return the same status, body and headers. Response
**time** is the same up to the cost of one indexed SQLite lookup. Equal hashing work is guaranteed. Equal time is
not guaranteed and is not claimed.

**Rate-limit keys** (SPEC-GAP-050)
- The keys are `sha256("user:" + canonical)`, `sha256("client:" + address)` and `"global"`.
- These are pseudonyms, not secrets: someone with a username list can reverse them. The earlier claim that "raw
  usernames are not stored" is withdrawn.
- **Client address** (SPEC-GAP-048):
  - The API uses `request.client.host`, and uvicorn is launched with `--no-proxy-headers` in `docker-compose.yml` and
    in the README run commands. The application itself never reads `X-Forwarded-For`.
  - The Streamlit UI applies its own per-client check to `st.context.ip_address` when that value is not `None`. When
    it is `None` (localhost), the per-client check is skipped. There is no shared `"unknown"` bucket.

**Sessions (server-side, hashed at rest)**
- Each successful login issues a new token, `secrets.token_urlsafe(32)`. Tokens chosen by the client are never
  accepted.
- `sessions` stores only `sha256(token)`, `account_id`, `created`, `last_seen` and `expires`. The idle timeout is
  2 h and the absolute limit is 24 h (SPEC-GAP-034).
- At most 10 sessions per account; the oldest is revoked when the cap is reached. Expired sessions and expired
  attempt rows are pruned on each login (SPEC-GAP-051).
- `resolve(token)` **joins `accounts`**. It rejects the session if the account is disabled, or if
  `sessions.created < accounts.password_changed`. This closes the gap where a login racing `disable` or
  `set-password` would leave a live session.
- Logout deletes the row. `set-password` and `disable` also delete all of the account's sessions.
- The token never appears in a URL, a log, an error message, a cookie or the database (only its hash is stored).

**How clients carry the session**
- **API:** `Authorization: Bearer <token>`, read from `request.headers` without declaring a header parameter, so
  existing OpenAPI operations do not change. Cookies are never read. The token is validated **before** any workspace
  lookup, so an invalid token gives the same 401 whatever workspace id it is used with. `X-API-Key` stays the
  outer gate (SPEC-GAP-038).
- **Streamlit:** the token lives only in `st.session_state`. On sign-in, sign-out or session expiry, every
  session-state key is cleared except the `APP_PASSWORD` keys (`authenticated`, `failed_logins`, `locked_until`). An
  expired session shows the sign-in screen. It never falls back to anonymous mode.

**Auth interface for P8 (SPEC-GAP-008)**
- `Principal` (`account_id`, `username`) and an `Authenticator` Protocol in `src/interfaces.py`:
  `resolve(token) -> Principal | None`, `start_session(account_id) -> SessionInfo`, `logout(token) -> None`.
- `PasswordLogin` (`src/accounts.py`) verifies credentials and calls `start_session`. P8 SSO adds another login class
  that calls the same `start_session`.
- `TrustRAGService` receives the authenticator by injection.

**Logging and metrics**
- These log events carry no submitted username: `auth.login_failed`, `auth.rate_limited`, `auth.busy`.
  `auth.login_succeeded`, `auth.logout` and the workspace events carry `account_id`. Passwords and tokens (including
  their hashes) are never logged.
- `/metrics` records `/v1/auth/*` requests with `status="-"`, so success and failure counts are not exposed
  (SPEC-GAP-052).

## Consequences
- No new dependency and no lock regeneration, if the human accepts scrypt.
- `docker-compose.yml` (the `api` command) and the README run commands gain `--no-proxy-headers`. That is a change to
  deployment configuration, which is a human gate and triggers `docker compose build` in the quality gate.
- A login costs about 0.3 s and 128 MiB. Under a flood, the global cap and the semaphore timeout fail fast with 429
  instead of stalling the worker threads.
- Accepted limits:
  - Targeted lockout of a known username for up to 15 minutes.
  - A global lockout of sign-in for at most a second at a time under a flood.
  - In compose, all UI sign-ins share the UI container's address at the API.
  - The Streamlit client address can be spoofed or is `None`, so the per-username limit is the main control.
  - Plain HTTP until P8.
  - No MFA.
- The state database holds password and session hashes. The directory is 0700 and the files are 0600 (ADR 0012,
  SPEC-GAP-044).

## Evidence
Commands I ran while planning (read-only):
- **scrypt:** `hashlib.scrypt` exists; N = 2^17 took 0.307 s. `argon2`, `passlib`, `pwdlib` and `cryptography` are
  absent. `bcrypt` is installed only because `chromadb` requires it.
- **uvicorn:** `uvicorn.config.Config` defaults are `proxy_headers=True` and `forwarded_allow_ips` =
  `FORWARDED_ALLOW_IPS` or `"127.0.0.1,::1"`. `uvicorn --help` lists `--proxy-headers / --no-proxy-headers` and
  `--forwarded-allow-ips`.
- **Streamlit:** the `ip_address` docstring is quoted above.
- **FastAPI:** 422 bodies echo the input.

Not verified: scrypt in the Docker image; the OWASP figures.
Tests are to be added in P6 (`docs/phases/p6-plan.md` section 3).
