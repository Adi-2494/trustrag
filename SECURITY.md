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
