# 0002 - Fail closed when the AI check is unavailable

**Status:** Accepted

## Context
The injection scanner combines regex rules with a Claude Haiku classifier. Weak rules (e.g. "you are now")
alone produce false alarms, so when the AI layer says a chunk is safe, weak rule matches are dropped. But the AI
layer can be unavailable (rate limits, outages) - and an attacker who exhausts the usage limit could switch it off
on purpose.

## Decision
Failed scan batches are retried once. If the AI still cannot check a chunk, the scanner **fails closed**: any rule
match on that chunk counts as HIGH and the chunk is removed by the default policy. The report states how many
sections the AI checked.

## Consequences
- Switching off the AI layer makes the scanner stricter, never weaker.
- During an outage, some legitimate text that resembles attack wording may be removed; the security report shows
  exactly what, and re-uploading after the outage re-checks only the failed sections (cached results are reused).

## Evidence
`tests/test_security.py::test_without_ai_any_rule_match_counts_as_high`,
`tests/test_injection_scanner.py::test_one_failed_batch_does_not_disable_the_ai_for_the_whole_file`.
