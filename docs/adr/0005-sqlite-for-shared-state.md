# 0005 - SQLite for shared state instead of Redis

**Status:** Accepted

## Context
OCR and scan results were cached in JSON files and the rate limit lived in process memory. That breaks with more
than one process (the UI and the API): limits are not shared, and two writers can corrupt a JSON file.

## Decision
A small SQLite database in WAL mode (`src/state.py`) holds the caches and the rate-limit window. Writes are
transactions; the rate-limit check-and-record runs under `BEGIN IMMEDIATE`.

## Consequences
- Atomic writes and a limit shared across processes on one host, with no extra server to run.
- Not shared across *hosts*; a multi-host deployment would swap this module for Redis behind the same functions.

## Evidence
`tests/test_architecture.py::test_rate_limit_is_shared_across_processes` (4 processes x 10 attempts, limit 15:
exactly 15 allowed) and `test_rate_limit_is_shared_across_threads_and_exact`. A concurrency bug in schema
initialisation ("database is locked", 3 of 8 runs) was found by these tests and fixed (0 of 15 runs).
