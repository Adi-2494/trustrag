"""
P5 evaluation history and trend report (ADR 0010, P5-AC2 / P5-AC3). Synthetic runs only; no model calls.

Interface proposed by these tests (see SPEC-GAP-030):
  eval.history: entry_from_results(results, *, commit_dirty=False, datasets=None, source="run"),
                append_entry(path, entry), load_history(path) -> list[dict]  (ValueError on any malformed line)
  eval.trend:   render_trends(entries, last=10) -> str;  CLI: python -m eval.trend [--history P] [--out P] [--last N]
"""

import copy
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Frozen v1.0 full-run results (commit bf3c288): the source of the committed history backfill line (SPEC-GAP-028).
RESULTS = json.loads((ROOT / "tests" / "data" / "results_full_v1.json").read_text(encoding="utf-8"))
DATASETS = {"self_authored": "a" * 64, "qasper": "b" * 64}


def _entry(results=None, **kw):
    from eval import history

    return history.entry_from_results(copy.deepcopy(results or RESULTS), datasets=kw.pop("datasets", DATASETS), **kw)


def _synthetic(i: int, with_qasper: bool = True) -> dict:
    results = {
        "run_at": f"2026-02-{i + 1:02d}T10:00:00",
        "commit": f"{i + 1:07x}",
        "suite": "free",
        "answer_model": "m-a",
        "checker_model": "m-c",
        "prompt_versions": {"answer": "answer-v3"},
        "settings": {"chunk_size": 800},
        "retrieval": {
            "questions": 56,
            "recall_at_k": 0.5 + i / 100,
            "mrr": 0.4 + i / 100,
            "top_k": 4,
            "mode": "hybrid",
        },
    }
    if with_qasper:
        results["public_benchmark"] = {
            "status": "ok",
            "retrieval": {
                "questions": 10,
                "recall_at_k": 0.3 + i / 100,
                "mrr": 0.2 + i / 100,
                "top_k": 4,
                "mode": "hybrid",
            },
        }
    return results


def _write_history(path: pathlib.Path, runs: list[dict], **kw) -> list[dict]:
    from eval import history

    entries = [history.entry_from_results(r, datasets=DATASETS, **kw) for r in runs]
    for e in entries:
        history.append_entry(path, e)
    return entries


def _section(markdown: str, metric: str) -> list[str]:
    """Table body rows of the '## <metric>' section."""
    block = markdown.split(f"## {metric}", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in block.splitlines() if ln.startswith("|")]
    return rows[2:]  # header + separator


# ---- P5-AC2 -------------------------------------------------------------------------------------------------
def test_each_run_appends_one_line_with_commit_prompt_versions_and_models(tmp_path):
    """P5-AC2: one appended line per run, carrying commit, prompt versions and both models."""
    from eval import history

    path = tmp_path / "history.jsonl"
    first = copy.deepcopy(RESULTS)
    second = copy.deepcopy(RESULTS) | {"commit": "deadbee", "run_at": "2026-03-01T00:00:00", "suite": "free"}
    history.append_entry(path, history.entry_from_results(first, datasets=DATASETS))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    history.append_entry(path, history.entry_from_results(second, datasets=DATASETS))
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for line, src in zip(lines, (first, second), strict=True):
        assert line["schema"] == 1
        assert line["commit"] == src["commit"] and line["run_at"] == src["run_at"]
        assert line["prompt_versions"] == src["prompt_versions"]
        assert line["answer_model"] == src["answer_model"] and line["checker_model"] == src["checker_model"]
    assert lines[1]["suite"] == "free" and lines[1]["source"] == "run"
    assert [e["commit"] for e in history.load_history(path)] == [first["commit"], "deadbee"]


def test_append_never_rewrites_existing_lines(tmp_path):
    """P5-AC2 / ADR 0010: appending leaves every earlier byte untouched (file is opened in append mode)."""
    from eval import history

    path = tmp_path / "history.jsonl"
    history.append_entry(path, _entry())
    before = path.read_bytes()
    history.append_entry(path, _entry(commit_dirty=True))
    after = path.read_bytes()
    assert after.startswith(before) and len(after) > len(before) and after.endswith(b"\n")
    assert before.endswith(b"\n")


def test_entry_records_dirty_flag_suite_settings_and_dataset_hashes():
    """P5-AC2: commit_dirty, suite, settings and dataset hashes are recorded; metrics are a flat dotted map."""
    entry = _entry(commit_dirty=True)
    assert entry["commit_dirty"] is True and _entry()["commit_dirty"] is False
    assert entry["suite"] == RESULTS["suite"] and entry["settings"] == RESULTS["settings"]
    assert entry["datasets"] == DATASETS and entry["source"] == "run"
    assert all(isinstance(k, str) and "." in k for k in entry["metrics"])
    assert all(v is None or isinstance(v, int | float) for v in entry["metrics"].values())


def test_backfill_entry_matches_committed_results_json():
    """P5-AC2 (SPEC-GAP-024): a backfilled entry reproduces the committed results.json figures."""
    entry = _entry(source="backfill:results.json")
    m = entry["metrics"]
    assert entry["source"] == "backfill:results.json"
    assert m["retrieval.recall_at_k"] == RESULTS["retrieval"]["recall_at_k"]
    assert m["retrieval.mrr"] == RESULTS["retrieval"]["mrr"]
    rules = RESULTS["injection"]["rules_only"]["removed"]
    for name in ("precision", "recall", "false_alarm_rate"):
        assert m[f"injection.rules_only.removed.{name}"] == rules[name]
    if "combined" in RESULTS["injection"]:
        assert m["injection.combined.removed.recall"] == RESULTS["injection"]["combined"]["removed"]["recall"]
    for name in (
        "answer_accuracy",
        "citation_accuracy",
        "correct_refusal_rate",
        "false_refusal_rate",
        "unanswerable_with_unsupported_claims",
    ):
        assert m[f"answers.{name}"] == RESULTS["answers"][name]
    assert m["trust.auroc_correct_vs_falsified"] == RESULTS["trust"]["auroc_correct_vs_falsified"]
    assert not [k for k in m if k.startswith("ablation")]  # SPEC-GAP-025: ablations are not tracked
    assert not [k for k in m if k.startswith("qasper")]  # no public_benchmark block in the old results


def test_qasper_metrics_come_from_the_public_benchmark_block():
    """P5-AC2 (SPEC-GAP-025): qasper.* metrics are read from results['public_benchmark']."""
    run = _synthetic(3) | {"suite": "all"}
    run["public_benchmark"]["answers"] = {"correct_refusal_rate": 0.8, "false_refusal_rate": 0.1}
    m = _entry(run)["metrics"]
    assert m["qasper.retrieval.recall_at_k"] == 0.33 and m["qasper.retrieval.mrr"] == 0.23
    assert m["qasper.answers.correct_refusal_rate"] == 0.8 and m["qasper.answers.false_refusal_rate"] == 0.1
    skipped = _synthetic(3)
    skipped["public_benchmark"] = {"status": "skipped", "skipped_reason": "no data"}
    assert not [k for k in _entry(skipped)["metrics"] if k.startswith("qasper")]


def test_committed_history_starts_with_backfilled_entry():
    """P5-AC2 (SPEC-GAP-024): eval/results/history.jsonl is committed and starts with one backfilled entry."""
    from eval import history

    path = ROOT / "eval" / "results" / "history.jsonl"
    assert path.exists(), "history.jsonl missing (T7)"
    entries = history.load_history(path)
    assert entries and entries[0]["source"] == "backfill:results.json"
    assert entries[0]["metrics"]["retrieval.recall_at_k"] == 1.0 and entries[0]["metrics"]["retrieval.mrr"] == 0.973
    expected = history.entry_from_results(
        copy.deepcopy(RESULTS), datasets=entries[0]["datasets"], source="backfill:results.json"
    )
    assert entries[0] == expected, "backfill line no longer matches the frozen v1.0 full-run results"


def test_history_lines_are_ascii_and_strict_json(tmp_path):
    """M5: ensure_ascii and allow_nan=False, so the invisible-character gate cannot be bypassed via .jsonl."""
    from eval import history

    path = tmp_path / "history.jsonl"
    entry = _entry()
    entry["answer_model"] = "modél\u200b"
    history.append_entry(path, entry)
    raw = path.read_bytes()
    assert raw.isascii() and b"\\u200b" in raw
    bad = _entry()
    bad["metrics"]["retrieval.mrr"] = float("nan")
    with pytest.raises(ValueError):
        history.append_entry(path, bad)
    assert path.read_bytes() == raw  # nothing was written by the failed append


# ---- load_history validation (M4) ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,value",
    [
        ("commit", "abc1234; <b>x</b>"),
        ("commit", "[x](http://evil)"),
        ("commit", "ZZZZ"),
        ("suite", "<script>"),
        ("suite", "everything"),
        ("run_at", "yesterday"),
        ("run_at", "2026-01-01 | **bold**"),
        ("schema", 2),
    ],
)
def test_history_line_with_markdown_or_html_in_fields_is_rejected(tmp_path, field, value):
    """M4: load_history validates commit regex, suite enum, ISO timestamp and schema, and fails loudly."""
    from eval import history

    entry = _entry()
    entry[field] = value
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        history.load_history(path)


def test_history_metric_values_must_be_finite_numbers_or_null(tmp_path):
    """M4: a metric that is a string (markup) or non-finite is rejected."""
    from eval import history

    for value in ("<b>1</b>", "NaN", True, [1]):
        entry = _entry()
        entry["metrics"]["retrieval.mrr"] = value
        path = tmp_path / "h.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            history.load_history(path)
    path.write_text(json.dumps(_entry() | {"metrics": {"retrieval.mrr": None}}) + "\n", encoding="utf-8")
    assert history.load_history(path)[0]["metrics"]["retrieval.mrr"] is None
    path.write_text(json.dumps(_entry() | {"commit": "unknown"}) + "\n", encoding="utf-8")
    assert history.load_history(path)[0]["commit"] == "unknown"


def test_malformed_history_line_fails_loudly(tmp_path):
    """P5-AC3 / ADR 0010: a bad line raises (never skipped silently); the CLI exits non-zero and writes nothing."""
    from eval import history

    path = tmp_path / "history.jsonl"
    _write_history(path, [_synthetic(0)])
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    with pytest.raises(ValueError):
        history.load_history(path)
    out = tmp_path / "TRENDS.md"
    done = _cli(path, out)
    assert done.returncode != 0 and not out.exists()


# ---- P5-AC3 -------------------------------------------------------------------------------------------------
def test_trend_report_lists_last_10_runs_per_metric(tmp_path):
    """P5-AC3: 12 runs -> exactly the 10 most recent rows per metric, newest first."""
    from eval import history, trend

    path = tmp_path / "history.jsonl"
    runs = [_synthetic(i) for i in range(12)]
    _write_history(path, runs)
    markdown = trend.render_trends(history.load_history(path))
    for metric in ("retrieval.recall_at_k", "retrieval.mrr", "qasper.retrieval.recall_at_k", "qasper.retrieval.mrr"):
        rows = _section(markdown, metric)
        assert len(rows) == 10, metric
        assert runs[11]["run_at"] in rows[0] and runs[2]["run_at"] in rows[-1]
        assert not any(runs[1]["run_at"] in r or runs[0]["run_at"] in r for r in rows)
    assert runs[11]["commit"] in _section(markdown, "retrieval.mrr")[0]
    assert "| Run | Commit | Suite | Value | Change |" in markdown
    assert len(_section(trend.render_trends(history.load_history(path), last=3), "retrieval.mrr")) == 3


def test_dirty_commits_are_marked_with_a_star(tmp_path):
    """P5-AC3 (plan 2.6): '*' after the commit of a run made on uncommitted code."""
    from eval import history, trend

    path = tmp_path / "history.jsonl"
    _write_history(path, [_synthetic(0)], commit_dirty=True)
    _write_history(path, [_synthetic(1)], commit_dirty=False)
    rows = _section(trend.render_trends(history.load_history(path)), "retrieval.mrr")
    assert f"{_synthetic(0)['commit']}*" in rows[1] and "*" not in rows[0].split("|")[2]


def test_metric_absent_from_a_run_is_skipped_for_that_metric_only(tmp_path):
    """P5-AC3: runs without a metric are left out of that metric's table but stay in the others."""
    from eval import history, trend

    path = tmp_path / "history.jsonl"
    runs = [_synthetic(i, with_qasper=i >= 9) for i in range(12)]
    _write_history(path, runs)
    markdown = trend.render_trends(history.load_history(path))
    assert len(_section(markdown, "retrieval.mrr")) == 10
    qasper_rows = _section(markdown, "qasper.retrieval.mrr")
    assert len(qasper_rows) == 3
    assert not any(runs[8]["run_at"] in r for r in qasper_rows)


def _cli(history_path, out_path, *extra):
    return subprocess.run(
        [sys.executable, "-m", "eval.trend", "--history", str(history_path), "--out", str(out_path), *extra],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_trend_command_regenerates_report_deterministically(tmp_path):
    """P5-AC3: `python -m eval.trend` twice on one history gives byte-identical output with no generation time."""
    path = tmp_path / "history.jsonl"
    _write_history(path, [_synthetic(i) for i in range(12)])
    out = tmp_path / "TRENDS.md"
    first = _cli(path, out)
    assert first.returncode == 0, first.stderr[-500:]
    content = out.read_bytes()
    assert content.startswith(b"# ") and content.endswith(b"\n")
    out.unlink()
    assert _cli(path, out).returncode == 0
    assert out.read_bytes() == content
    assert _cli(path, tmp_path / "t3.md", "--last", "3").returncode == 0
    assert (tmp_path / "t3.md").read_bytes() != content


def test_makefile_has_eval_trend_target():
    """P5-AC3: `make eval-trend` regenerates the report with python -m eval.trend."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "eval-trend:" in makefile
    recipe = makefile.split("eval-trend:", 1)[1].split("\n\n", 1)[0]
    assert "python -m eval.trend" in recipe
    assert "eval-trend" in makefile.splitlines()[0]  # listed in .PHONY


# ---- P5 fix 1 and 2 ------------------------------------------------------------------------------------------
def _with_datasets(i: int, datasets: dict) -> dict:
    from eval import history

    return history.entry_from_results(_synthetic(i), datasets=datasets)


def _render(entries):
    from eval import trend

    return trend.render_trends(entries)


def test_dataset_changed_note_only_when_relevant_hash_differs():
    """P5 fix 1: qasper.* metrics compare datasets['qasper']; every other metric compares datasets['self_authored']."""
    base = {"self_authored": "a" * 64, "qasper": "b" * 64}
    qasper_changed = _render([_with_datasets(0, base), _with_datasets(1, base | {"qasper": "c" * 64})])
    assert "(dataset changed)" in _section(qasper_changed, "qasper.retrieval.mrr")[0]
    assert "(dataset changed)" not in _section(qasper_changed, "retrieval.mrr")[0]

    own_changed = _render([_with_datasets(0, base), _with_datasets(1, base | {"self_authored": "d" * 64})])
    assert "(dataset changed)" in _section(own_changed, "retrieval.mrr")[0]
    assert "(dataset changed)" not in _section(own_changed, "qasper.retrieval.mrr")[0]


def test_no_dataset_changed_note_when_previous_entry_lacks_the_key():
    """P5 fix 1: a previous run without a qasper hash (subset not downloaded) raises no note for qasper metrics."""
    own = {"self_authored": "a" * 64}
    rendered = _render([_with_datasets(0, own), _with_datasets(1, own | {"qasper": "b" * 64})])
    assert "(dataset changed)" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [("commit", "abcd\n"), ("commit", "abc1234\n"), ("run_at", "2026-01-01T10:00:00\n")],
)
def test_history_rejects_trailing_newline_in_commit_and_run_at(field, value):
    """P5 fix 2: `$` matches before a trailing newline; validate_entry must still reject it."""
    from eval import history

    entry = _entry()
    entry[field] = value
    with pytest.raises(ValueError):
        history.validate_entry(entry)
