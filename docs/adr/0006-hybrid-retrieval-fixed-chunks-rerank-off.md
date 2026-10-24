# 0006 - Hybrid retrieval, fixed 800-character chunks, reranker off by default

**Status:** Accepted

## Context
Retrieval options: chunk size, fixed vs heading-aware ("section") chunking, semantic vs hybrid (semantic + BM25)
search, and an optional Claude Haiku reranker. Each was implemented and measured on the evaluation corpus
(5 documents, 56 answerable questions with known evidence).

## Decision
Hybrid search (reciprocal rank fusion of semantic and BM25 top-12), fixed 800-character chunks with 150 overlap,
top-4, reranker available but off (`RERANK=true` enables it).

## Evidence (eval/results/RESULTS.md)
| Option | Recall@4 | MRR |
|---|---|---|
| 500 fixed, semantic | 95% | 0.89 |
| 800 fixed, semantic | 100% | 0.95 |
| **800 fixed, hybrid (chosen)** | **100%** | **0.97** |
| 800 section, hybrid | 100% | 0.95 |
| 800 fixed, hybrid + rerank | 100% | 0.99 |

- 500-character chunks cut some facts in half (recall 95%).
- Hybrid improves ranking at every size where there is room to improve.
- Section chunking did not help here: these pages have one heading each, so the heading prefix only adds noise.
  It remains available for documents with many headings.
- Reranking lifts MRR to 0.99 but adds a Claude call per question while recall is already 100%.
