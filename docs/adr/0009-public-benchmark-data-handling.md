# 0009 - Public benchmark data: downloaded once, hash-verified, JSON only, never vendored

**Status:** Accepted (P5; approved by the human)

## Context
P5 adds a public document-QA benchmark so quality is measured on text the author did not write (SPEC P5).
SPEC-GAP-005 chose Qasper (CC BY 4.0), 50 papers, with retrieval scored by evidence paragraph. P5-AC1 requires
`make eval-free` to run the benchmark with no network calls once the data has been downloaded.

Things that shaped the decision:
- Dataset text is third-party and **untrusted**, like an uploaded PDF (THREAT_MODEL "Document author" actor).
- The usual Python tools for public datasets (Hugging Face `datasets`, loading scripts, pickled caches) would add
  a dependency and/or run code that comes with the data.
- `scripts/check_invisible_chars.py` scans every `.json`/`.md`/`.txt` file under the repository with `rglob`,
  including git-ignored folders. Academic text often contains soft hyphens and zero-width characters, so a dataset
  stored inside the working tree would break the quality gate locally.
- Committing the data would redistribute third-party paper text inside a proprietary-licensed repository
  (`LICENSE`); downloading it avoids that question.

## Decision
- **Download once, by a human-approved command** (`make eval-download-qasper`), using the Python standard library
  only (`urllib.request`, `hashlib`, `tarfile`, `json`). No new dependency.
- **Source and integrity are pinned** in a committed manifest `eval/data/qasper_manifest.json`: HTTPS URL, archive
  SHA-256, archive member name, split, the 50 selected paper ids, and the SHA-256 of the derived subset file.
  The archive is rejected if its hash differs. The values are filled in at the approved first download and
  reviewed before commit.
- **Archive handling:** never `extractall`. Only the one expected member name is read, only if it is a regular
  file and under a size cap, straight into memory. Download size is capped.
- **JSON only.** Data is parsed with `json.loads`; no pickle, no `datasets` library, no loading scripts, no
  `eval`/`exec`, no YAML. Records are type-checked; malformed records are skipped and counted.
- **Cache outside the repository**: default `~/.cache/trustrag/qasper/`, overridable with
  `TRUSTRAG_EVAL_DATA_DIR`. Only the derived 50-paper subset is kept; it is re-verified against the manifest hash on
  every load.
- **No dataset text in committed files.** Results, history and trend report hold aggregate numbers and validated
  ids only.
- **Offline after download:** the eval process sets ChromaDB's telemetry off before importing it
  (`anonymized_telemetry` defaults to `True` in `chromadb/config.py`), and a test blocks sockets while the
  benchmark runs.
- When the subset is missing, `make eval-free` reports the benchmark as `skipped` and still succeeds, so CI keeps
  working without a CI change (SPEC-GAP-026).

## Consequences
- CI does not run the public benchmark until a human approves a CI change (for example a cached download step).
  Until then, P5-AC1 is shown by the verifier on a machine that has the data.
- Each machine needs one approved network download; a changed upstream archive is detected (hash mismatch) and is
  not used silently.
- The Hugging Face mirror and its loading script are not used.
- A local attacker who can edit both the cache and the manifest can change benchmark numbers; this falls under the
  existing "compromised host" limit in `THREAT_MODEL.md`.

## Evidence
To be added in P5: tests listed in `docs/phases/p5-plan.md` section 4 (hash mismatch, member allow-list, size cap,
no-network run, JSON-only import check).
