# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | ✅ |
| < 1.0 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
(**Security → Report a vulnerability** on this repository), or contact [@Adi-2494](https://github.com/Adi-2494).

Include what you found, how to reproduce it, and the impact you expect. You can expect an acknowledgement within
5 working days and a fix or mitigation plan within 30 days for confirmed issues.

## How TrustRAG handles security

- **Threat model:** [`THREAT_MODEL.md`](THREAT_MODEL.md) lists attackers, attacks, mitigations, the tests that prove
  them, and what is not defended against.
- **Secrets:** API keys and tokens are read from `.env` or a mounted secret file only; they are never logged,
  displayed, or copied into the Docker image. `.env` is git-ignored, and `gitleaks` runs as a pre-commit hook.
- **Untrusted content:** documents, file names and model output are treated as untrusted at every boundary
  (scanning, escaping inside prompts, verbatim-quote verification, safe rendering).
- **Supply chain:** dependencies are hash-locked, base images pinned by digest, CI actions pinned by commit, and
  Dependabot proposes updates weekly.
- **Defaults:** the app and the Docker examples listen on localhost only. Set `APP_PASSWORD` and/or `API_KEY`
  before exposing TrustRAG to a network.
- **Accounts (optional, `ACCOUNTS_ENABLED`):** accounts are created by the operator only (no sign-up). Passwords are
  stored as salted scrypt hashes; session tokens are 256-bit, stored only as SHA-256, accepted only in the
  `Authorization` header and expire after 2 hours idle or 24 hours. Sign-in failures look identical whatever the
  cause, and are rate limited per username, per client address and globally before any password hashing. The API
  must run with `--no-proxy-headers` (set in `docker-compose.yml`). The state database directory is owner-only.
  Users can only reach their own workspaces; anyone else gets "not found". Limits, such as targeted lockout and no
  MFA, are listed in [`THREAT_MODEL.md`](THREAT_MODEL.md).
- **Backups:** `chroma_db/` holds vectors and the state database (accounts, sessions). Back it up before upgrading;
  rolling back to v1.0 hides account workspaces but does not delete them.
