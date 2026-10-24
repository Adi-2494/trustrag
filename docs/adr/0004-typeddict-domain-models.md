# 0004 - Domain models as TypedDicts

**Status:** Accepted

## Context
Chunks, claims, scan reports and answers flow through the pipeline, are cached as JSON in SQLite, returned by
the HTTP API and rendered by the UI. Options: dataclasses, Pydantic models, TypedDicts.

## Decision
`TypedDict` models in `src/models.py` (from `typing_extensions`, which FastAPI needs on Python 3.11), checked by
`mypy --strict`.

## Consequences
- No conversion code at the cache, API and UI boundaries: the objects already are JSON-shaped dicts.
- mypy catches misspelt keys and wrong types at development time; FastAPI derives response schemas from them.
- No runtime validation of model output inside the pipeline - that is done explicitly where untrusted data
  enters (e.g. the verifier normalises unknown claim statuses to NOT_SUPPORTED).
