# TrustRAG evaluation results

Run: 2026-10-31T20:48:21 · commit `5fb45ca` · answer model `claude-sonnet-5-5` · checker model `claude-haiku-5-5`

## Retrieval (default settings: hybrid, fixed chunks of 800)

| Questions | Recall@4 | MRR |
|---|---|---|
| 56 | 100% | 0.97 |

## Ablation: chunk size, chunking strategy and retrieval mode (top-4)

| Chunk size | Strategy | Retrieval | Recall@4 | MRR |
|---|---|---|---|---|
| 500 | fixed | semantic | 95% | 0.89 |
| 500 | fixed | hybrid | 95% | 0.91 |
| 500 | section | semantic | 93% | 0.89 |
| 500 | section | hybrid | 93% | 0.90 |
| 800 | fixed | semantic | 100% | 0.95 |
| 800 | fixed | hybrid | 100% | 0.97 |
| 800 | section | semantic | 100% | 0.94 |
| 800 | section | hybrid | 100% | 0.95 |
| 1200 | fixed | semantic | 100% | 0.97 |
| 1200 | fixed | hybrid | 100% | 0.97 |
| 1200 | section | semantic | 100% | 0.97 |
| 1200 | section | hybrid | 100% | 0.97 |

## Prompt-injection benchmark (30 attacks, 30 benign)

| Layer | Precision | Recall | False-alarm rate |
|---|---|---|---|
| Rules only (flagged) | 91% | 67% | 7% |

## Public benchmark: Qasper v0.3 (dev split, CC BY 4.0)

50 papers; one index per paper; default retrieval settings (hybrid, top-4).

| Metric | Value |
|---|---|
| Answerable questions scored | 138 |
| Recall@4 (evidence paragraph in top-4) | 53% |
| MRR | 0.36 |
| Unanswerable questions | 9 |

Excluded questions: mixed_answerability 10, figure_or_table_only 6, no_evidence 1. Rejected records: 0.
