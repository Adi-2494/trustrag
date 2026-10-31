"""
P5-AC4: the public benchmark must not regress the v1.0 evaluation (B8). Pure checks on the committed baseline;
these should already pass before the build and must keep passing after it.
"""

import copy
import json
import pathlib

from src import config
from tests.fakes import FakeStore
from tests.qasper_support import mini_subset

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = json.loads((ROOT / "eval" / "results" / "results.json").read_text(encoding="utf-8"))
# Frozen copy of the v1.0 full-run (suite "all", commit bf3c288) results.json and its RESULTS.md. Tests must not depend
# on which suite last overwrote the live files (SPEC-GAP-028).
BASELINE = json.loads((ROOT / "tests" / "data" / "results_full_v1.json").read_text(encoding="utf-8"))
BASELINE_MD = (ROOT / "tests" / "data" / "RESULTS_full_v1.md").read_bytes()


def _qasper_block() -> dict:
    return {
        "dataset": "Qasper",
        "version": "v-test",
        "split": "dev",
        "licence": "CC BY 4.0",
        "source_url": "https://example.invalid/q",
        "papers": 3,
        "subset_sha256": "c" * 64,
        "status": "ok",
        "retrieval": {"questions": 4, "recall_at_k": 0.75, "mrr": 0.625, "top_k": 4, "mode": "hybrid"},
        "unanswerable_questions": 2,
        "excluded_questions": {"mixed_answerability": 1},
        "rejected_records": 0,
    }


def test_results_markdown_without_public_benchmark_is_byte_identical():
    """P5-AC4: write_markdown(frozen v1.0 full-run results) still reproduces that run's RESULTS.md byte for byte."""
    from eval.run_eval import write_markdown

    committed = BASELINE_MD
    assert (write_markdown(copy.deepcopy(BASELINE)).rstrip("\n") + "\n").encode("utf-8") == committed


def test_public_benchmark_section_is_appended_after_existing_sections():
    """P5-AC4: the Qasper section is added at the END; everything before it is the unchanged B8 report."""
    from eval.run_eval import write_markdown

    base = write_markdown(copy.deepcopy(BASELINE))
    results = copy.deepcopy(BASELINE) | {"public_benchmark": _qasper_block()}
    extended = write_markdown(results)
    assert extended.startswith(base.rstrip("\n"))
    tail = extended[len(base.rstrip("\n")) :]
    assert "Qasper" in tail and "CC BY 4.0" in tail
    assert "75%" in tail and ("0.62" in tail or "0.63" in tail)


def test_skipped_public_benchmark_adds_a_note_but_leaves_the_rest_untouched():
    """P5-AC4 (SPEC-GAP-026): status skipped renders only a short note after the unchanged report."""
    from eval.run_eval import write_markdown

    base = write_markdown(copy.deepcopy(BASELINE))
    skipped = {"dataset": "Qasper", "status": "skipped", "skipped_reason": "Qasper subset not downloaded"}
    extended = write_markdown(copy.deepcopy(BASELINE) | {"public_benchmark": skipped})
    assert extended.startswith(base.rstrip("\n")) and "skipped" in extended[len(base) :].lower()


def test_default_relevance_is_unchanged_for_self_authored_questions(monkeypatch):
    """P5-AC4: eval_retrieval's default scoring (file + evidence substring) gives the same figures as before."""
    from eval import run_eval

    chunks = [
        {"id": "a-p1-c0", "text": "Staff get 25 days of leave.", "file_name": "a.pdf", "page": 1},
        {"id": "a-p2-c0", "text": "Remote work needs approval.", "file_name": "a.pdf", "page": 2},
        {"id": "b-p1-c0", "text": "Staff get 25 days of leave.", "file_name": "b.pdf", "page": 1},  # other file
    ]
    q = {"question": "leave days staff", "file": "a.pdf", "evidence": "25  days of LEAVE", "answerable": True}
    assert run_eval.first_relevant_rank(chunks, q) == 1
    assert run_eval.first_relevant_rank(chunks[1:], q) is None  # same text in another file does not count
    store = FakeStore()
    monkeypatch.setattr(run_eval, "STORE", store)
    ws = store.new_workspace()
    store.add_chunks(ws, chunks)
    questions = [q, q | {"question": "remote approval", "evidence": "Remote work needs approval.", "file": "a.pdf"}]
    unanswerable = {"question": "stock price", "answerable": False}
    result = run_eval.eval_retrieval(ws, [*questions, unanswerable], 2, "semantic")
    assert result == {"questions": 2, "recall_at_k": 1.0, "mrr": 1.0, "top_k": 2, "mode": "semantic"}


def test_public_benchmark_does_not_change_config_or_settings(tmp_path):
    """P5-AC4: running the benchmark changes no config value, and the baseline records the current settings."""
    from eval import qasper

    names = [
        "CHUNK_SIZE",
        "CHUNK_OVERLAP",
        "CHUNK_STRATEGY",
        "TOP_K",
        "RETRIEVAL_MODE",
        "RERANK",
        "PROMPT_VERSIONS",
        "ANSWER_MODEL",
        "CHECKER_MODEL",
        "REFUSAL_TEXT",
        "TRUST_GREEN",
    ]
    before = {n: copy.deepcopy(getattr(config, n)) for n in names}
    path, manifest = mini_subset(tmp_path)
    qasper.run_public_benchmark(FakeStore(), subset_path=path, manifest=manifest)
    assert {n: getattr(config, n) for n in names} == before
    s = LIVE["settings"]
    assert (s["chunk_size"], s["chunk_strategy"], s["retrieval_mode"], s["top_k"], s["rerank"]) == (
        config.CHUNK_SIZE,
        config.CHUNK_STRATEGY,
        config.RETRIEVAL_MODE,
        config.TOP_K,
        config.RERANK,
    )
    assert LIVE["prompt_versions"] == config.PROMPT_VERSIONS


def test_baseline_b8_figures_are_still_in_results_json():
    """P5-AC4: recall@4 1.0, MRR 0.973 and the rules-only injection figures of v1.0 are intact in the baseline."""
    assert LIVE["retrieval"]["recall_at_k"] == 1.0 and LIVE["retrieval"]["mrr"] == 0.973
    rules = LIVE["injection"]["rules_only"]["removed"]
    assert (rules["precision"], rules["recall"], rules["false_alarm_rate"]) == (0.909, 0.667, 0.067)


def test_makefile_eval_free_and_eval_lines_are_unchanged():
    """P5-AC4: the eval-free and eval recipes keep their exact commands; the new targets exist."""
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    assert "\tpython -m eval.run_eval --suite free" in lines
    assert "\tpython -m eval.run_eval --suite all" in lines
    assert any(ln.startswith("eval-free:") for ln in lines) and any(ln.startswith("eval:") for ln in lines)
    text = "\n".join(lines)
    assert "eval-download-qasper:" in text and "python -m eval.qasper_download" in text
