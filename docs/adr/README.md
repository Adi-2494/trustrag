# Architecture Decision Records

Short records of significant decisions: the context, the decision, its consequences, and the evidence.
New decisions get the next number; superseded records are kept and marked as such.

| # | Decision | Status |
|---|---|---|
| [0001](0001-sonnet-for-answers-haiku-for-checks.md) | Claude Sonnet writes answers; Claude Haiku does fact-checking, scanning and OCR | Accepted |
| [0002](0002-fail-closed-when-ai-check-unavailable.md) | Fail closed when the AI scanner can't check a chunk | Accepted |
| [0003](0003-chromadb-over-pgvector.md) | Embedded ChromaDB with local embeddings, behind a `VectorStore` interface | Accepted |
| [0004](0004-typeddict-domain-models.md) | Domain models as `TypedDict`s | Accepted |
| [0005](0005-sqlite-for-shared-state.md) | SQLite (WAL) for caches and the rate limit instead of Redis | Accepted |
| [0006](0006-hybrid-retrieval-fixed-chunks-rerank-off.md) | Hybrid retrieval, fixed 800-character chunks, reranker off by default | Accepted |
| [0007](0007-subscription-mode-for-local-use.md) | Claude subscription (via Claude Code) for local use; API key for deployments | Accepted |
| [0008](0008-escalate-ai-medium-verdicts.md) | AI "suspicious" verdicts of MEDIUM or above count as HIGH | Accepted |
