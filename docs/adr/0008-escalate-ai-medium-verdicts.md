# 0008 - AI "suspicious" verdicts of MEDIUM or above count as HIGH

**Status:** Accepted

## Context
The default policy removes HIGH-risk chunks. The first full evaluation showed two real attacks that the AI layer
flagged as suspicious but rated MEDIUM, so they were kept - while the AI layer had zero false alarms on the
benchmark.

## Decision
When the AI layer says SUSPICIOUS with risk MEDIUM or HIGH, the chunk counts as HIGH. LOW stays LOW.

## Evidence
Rules + AI, removed by the default policy: recall 93% -> 100%, precision 100%, false-alarm rate 0% before and
after (eval/results/RESULTS.md).

## Consequences
If the AI layer ever starts producing false alarms at MEDIUM, they would now remove text; the injection
benchmark in CI-run evaluations is the guard for that.
