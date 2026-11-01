"""Shared helpers for the P5 public-benchmark tests (synthetic Qasper-shaped data only; no network, no real data)."""

import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MINI = pathlib.Path(__file__).parent / "data" / "qasper_mini.json"

# Hand-computed from tests/data/qasper_mini.json with the FakeStore word-overlap ranking, semantic mode, top-4:
#   P001-q1 -> gold at rank 1, P001-q2 -> rank 2, P002-q1 -> rank 1, P003-q1 -> not in the top 4.
EXPECTED_ANSWERABLE = 4
EXPECTED_RECALL = 0.75
EXPECTED_MRR = 0.625
EXPECTED_UNANSWERABLE = 2
EXPECTED_EXCLUDED = {"mixed_answerability": 1, "figure_or_table_only": 1, "no_evidence": 1}
EXPECTED_REJECTED = 2  # "../evil" (bad paper id) and "P004" (full_text is not a list)
GOOD_PAPERS = ["P001", "P002", "P003"]
CANARIES = ["CANARY_TITLE_7731", "CANARY_ABSTRACT_5521", "CANARY_Q_9913", "<script>", "[click]", "Lamport", "Zorg"]


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_manifest(subset_path: pathlib.Path, **overrides) -> dict:
    manifest = {
        "dataset": "Qasper",
        "version": "test-fixture",
        "split": "dev",
        "licence": "CC BY 4.0",
        "source_url": "https://example.invalid/qasper-fixture",
        "archive_member": "qasper-dev-fixture.json",
        "archive_sha256": None,
        "subset_sha256": sha256_file(subset_path),
        "paper_ids": GOOD_PAPERS,
    }
    manifest.update(overrides)
    return manifest


def write_subset(tmp_path: pathlib.Path, data, name: str = "subset.json") -> tuple[pathlib.Path, dict]:
    """Write `data` (dict or raw text/bytes) as a subset file and return it with a matching manifest."""
    path = tmp_path / name
    if isinstance(data, bytes):
        path.write_bytes(data)
    elif isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data), encoding="utf-8")
    return path, make_manifest(path)


def mini_subset(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict]:
    path = tmp_path / "subset.json"
    path.write_bytes(MINI.read_bytes())
    return path, make_manifest(path)


def mini_data() -> dict:
    return json.loads(MINI.read_text(encoding="utf-8"))


def paper(title="T", abstract="A", sections=None, qas=None) -> dict:
    return {"title": title, "abstract": abstract, "full_text": sections or [], "qas": qas or []}
