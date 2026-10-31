# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Public benchmark (P5):** retrieval recall@4 and MRR on a 50-paper subset of Qasper v0.3 dev (CC BY 4.0,
  Dasigi et al. 2021), reported in `eval/results/` and the README. The subset is downloaded on demand
  (`make eval-download-qasper`), pinned by SHA-256 and checked on every load; without it the suite reports
  `skipped`. Measured: 138 answerable questions, recall@4 53%, MRR 0.36.
- **Run history and trends:** every eval run appends a line to `eval/results/history.jsonl`;
  `make eval-trend` writes `eval/results/TRENDS.md` (last 10 runs per metric).
- **Threat model:** rows 20-25 for the benchmark download and data path (ADRs 0009 and 0010). Hardening: decompressed-size
  cap before tar parsing, identity-based data-directory check, charset-validated manifest, fail-fast benchmark checks.

## [1.0.0] - 2026-10-24

### Added
- **Evaluation suite** (`make eval`, `make eval-free`): a ground-truth corpus of 5 documents and 71 questions,
  retrieval metrics (recall@k, MRR), answer and citation accuracy, refusal and hallucination rates, trust score vs
  falsified answers (AUROC), a labelled 60-sample injection benchmark, and ablations. Results in `eval/results/`.
- **HTTP API** (FastAPI) with API-key auth, request IDs and `/metrics`; the Streamlit UI can run as its client
  (`TRUSTRAG_API_URL`); `docker-compose.yml` runs both.
- **Hybrid retrieval** (semantic + BM25 with reciprocal rank fusion), optional Claude Haiku reranker, and
  heading-aware chunking - each measured; defaults chosen from the results (ADR 0006).
- **Observability:** structured JSON logs with a request ID across stages and threads; per-call model, tokens,
  cost, latency and prompt version; Prometheus-format metrics.
- **Architecture:** `TrustRAGService` with injected `LLMClient` / `VectorStore` interfaces, typed domain models,
  settings validated at startup (pydantic-settings), SQLite for caches and a cross-process rate limit.
- **Engineering:** `pyproject.toml`, ruff, `mypy --strict`, pre-commit hooks (incl. gitleaks and a Trojan Source
  check), coverage gate (85%; currently 92%), Hypothesis property tests, recorded-replay integration test.
- **Docs:** `ARCHITECTURE.md`, `THREAT_MODEL.md`, `SECURITY.md`, ADRs in `docs/adr/`.

### Changed
- AI-layer "suspicious" verdicts of MEDIUM or above now count as HIGH (ADR 0008): attacks removed by the default
  policy rose from 93% to 100% with no false removals.
- Replaced `requirements.txt` with `pyproject.toml` plus hash-locked `requirements.lock` / `requirements-dev.lock`.

### Fixed
- Concurrent first use of the state database could fail with "database is locked".
- The HTTP `X-Request-ID` header now matches the request ID in the logs.
- Invisible / bidirectional characters in source files replaced with escapes.

## [0.4.0] - 2026-10-24

### Security
- Fixed all critical, high and medium findings of an internal red-team review: refusal-sentence bypass of the
  fact-check, file-name prompt injection, shared document store, network exposure and missing usage limits,
  disguised attack phrases, AI layer talked into "safe", delimiter breakout, fact-checker manipulation,
  data-leaking Markdown images, PDF bombs, token visible in `docker inspect`, split attacks, AI layer
  switch-off, user-weakened policy, unpinned dependencies. See `THREAT_MODEL.md`.
- Localhost-only binding, macOS firewall guidance, branch protection on `main`.

## [0.3.0] - 2026-10-24

### Added
- Two-layer prompt-injection scanner (rules + Claude Haiku) with a security report; demo attack PDF.
- Claude subscription mode via Claude Code (no API key); multi-architecture Docker image.

## [0.2.0] - 2026-10-24

### Added
- Claim-level fact-checker with colour-coded trust score.

## [0.1.0] - 2026-10-24

### Added
- PDF question answering with page citations, OCR for scanned PDFs, ChromaDB retrieval.

[1.0.0]: https://github.com/Adi-2494/trustrag/releases/tag/v1.0.0
