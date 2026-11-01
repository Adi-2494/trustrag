"""
P5-AC5: README "Measured, not claimed" shows the Qasper numbers (matching results.json) with dataset name, licence
and credit. These stay red until the final docs task (T9) and a `make eval-free` run with the subset present (T7).
"""

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _measured_section() -> str:
    start = README.index("Measured, not claimed")
    after = README[start:]
    end = re.search(r"\n---\n", after)
    return after[: end.start()] if end else after


def _block() -> dict:
    results = json.loads((ROOT / "eval" / "results" / "results.json").read_text(encoding="utf-8"))
    assert "public_benchmark" in results, "results.json has no public_benchmark block yet (run make eval-free, T7)"
    block = results["public_benchmark"]
    assert block["status"] == "ok", "public benchmark was skipped in the committed results"
    return block


def test_measured_section_shows_qasper_numbers_matching_results_json():
    """P5-AC5: recall@4 as a percentage and MRR with two decimals, as in the existing README rows."""
    block = _block()
    section = _measured_section()
    r = block["retrieval"]
    row = next((ln for ln in section.splitlines() if "Qasper" in ln and ln.startswith("|")), "")
    assert row, "no Qasper row in the Measured table"
    assert f"{r['recall_at_k'] * 100:.0f}%" in row and f"{r['mrr']:.2f}" in row
    assert str(r["questions"]) in row and str(block["papers"]) in section


def test_measured_section_names_dataset_licence_and_credit():
    """P5-AC5: dataset name, CC BY 4.0, a link to the source and the authors' credit appear next to the numbers."""
    manifest = json.loads((ROOT / "eval" / "data" / "qasper_manifest.json").read_text(encoding="utf-8"))
    section = _measured_section()
    assert "Qasper" in section and "CC BY 4.0" in section
    assert manifest["source_url"].split("/")[2] in README or "allenai" in section.lower()
    first_author = manifest["citation"].split(",")[0].split()[-1]  # surname of the first listed author
    assert first_author in section


def test_known_limitations_no_longer_say_the_corpus_is_only_self_authored():
    """P5-AC5: 'Known limitations' mentions the public benchmark (what it does and does not cover)."""
    limits = README[README.index("Known limitations") :]
    assert "Qasper" in limits
