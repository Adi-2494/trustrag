# TrustRAG evaluation results

Run: 2026-10-24T21:33:57 · commit `bf3c288` · answer model `claude-sonnet-5-5` · checker model `claude-haiku-5-5`

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

## Reranking (default chunking)

| Retrieval | Recall@4 | MRR |
|---|---|---|
| semantic | 100% | 0.95 |
| hybrid | 100% | 0.97 |
| hybrid+rerank | 100% | 0.99 |

## Answer quality

| Metric | Value |
|---|---|
| Answer accuracy (56 answerable questions) | 100% |
| Cites the correct file and page | 100% |
| Correct refusals (15 unanswerable questions) | 93% |
| Unanswerable questions answered with unsupported claims (hallucinations) | 0% |
| False refusals (answerable questions refused) | 0% |
| Median time per question (answer + fact-check) | 6.6s |

## Trust score vs ground truth

| Metric | Value |
|---|---|
| Mean trust, correct answers | 99.2% |
| Mean trust, deliberately falsified answers | 4.7% |
| AUROC (correct vs falsified) | 1.0 |
| Falsified answers wrongly scored green (≥80%) | 0% |

## Prompt-injection benchmark (30 attacks, 30 benign)

| Layer | Precision | Recall | False-alarm rate |
|---|---|---|---|
| Rules only (flagged) | 91% | 67% | 7% |
| AI only (flagged) | 100% | 100% | 0% |
| Rules + AI (flagged) | 100% | 100% | 0% |
| Rules + AI (removed by default policy) | 100% | 100% | 0% |
