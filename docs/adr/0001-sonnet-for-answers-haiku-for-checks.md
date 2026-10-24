# 0001 - Claude Sonnet for answers, Claude Haiku for checks

**Status:** Accepted

## Context
Each question needs one answer and one fact-check; each upload needs a scan (and OCR for scanned pages).
The checking jobs are high-volume, structured (classify, extract, label) and latency-sensitive.

## Decision
Claude Sonnet 5.5 writes answers. Claude Haiku 5.5 does fact-checking, injection scanning, OCR and optional
reranking. Model names live in `src/config.py` per provider; prompts are versioned (`PROMPT_VERSIONS`).

## Consequences
- A question costs about $0.009 with an API key (measured: answer ~$0.008 on Sonnet, fact-check ~$0.0006 on Haiku).
- Checks are batched (one fact-check call per answer, 10 chunks per scan call) to keep calls per task low.
- If Haiku's judgement proved too weak for a job, that job can move to Sonnet by changing one setting.

## Evidence
Full evaluation (eval/results/RESULTS.md): 100% answer accuracy and citation accuracy; the Haiku fact-checker
separates correct from falsified answers perfectly (AUROC 1.0; mean trust 99% vs 5%); the Haiku scanner layer
had 100% precision and recall on the injection benchmark.
