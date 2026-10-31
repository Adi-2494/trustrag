"""
P5 public benchmark (Qasper): loader, scoring, offline retrieval run, refusal metrics and data-handling controls.
Uses only the synthetic fixture tests/data/qasper_mini.json and fakes: no real dataset, no network, no Claude.

Interface proposed by these tests (see SPEC-GAP-030):
  eval.qasper: QasperDataError, resolve_data_dir(), load_manifest(path), load_benchmark(subset_path, manifest),
               run_public_benchmark(store, *, subset_path, manifest, suite="free", llm=None, mode=..., top_k=...)
  eval.metrics: evidence_hit(chunk_text, paragraph, min_chars=200)
"""

# ruff: noqa: S311  (seeded random text for fixtures, not security)
import ast
import json
import os
import pathlib
import random
import socket
import subprocess
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src import config, vectorstore
from tests.fakes import FakeLLM, FakeStore
from tests.qasper_support import (
    CANARIES,
    EXPECTED_ANSWERABLE,
    EXPECTED_EXCLUDED,
    EXPECTED_MRR,
    EXPECTED_RECALL,
    EXPECTED_REJECTED,
    EXPECTED_UNANSWERABLE,
    GOOD_PAPERS,
    ROOT,
    make_manifest,
    mini_subset,
    paper,
    sha256_file,
    write_subset,
)

MODEL_CACHED = os.path.isdir(os.path.expanduser("~/.cache/chroma/onnx_models/all-MiniLM-L6-v2"))


def _benchmark(tmp_path):
    from eval import qasper

    path, manifest = mini_subset(tmp_path)
    return qasper.load_benchmark(path, manifest)


def _run(tmp_path, store=None, **kw):
    from eval import qasper

    path, manifest = mini_subset(tmp_path)
    return qasper.run_public_benchmark(store or FakeStore(), subset_path=path, manifest=manifest, **kw)


def _words(rng: random.Random, n: int) -> str:
    return " ".join(
        "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(4, 9))) for _ in range(n)
    )


# --------------------------------------------------------------------------------------------------------------
# Loader and classification
# --------------------------------------------------------------------------------------------------------------
def test_loader_builds_pages_and_questions_from_fixture(tmp_path):
    """P5-AC1: papers become Pages (title+abstract, then one page per section) and questions are classified."""
    bench = _benchmark(tmp_path)
    papers = {p["paper_id"]: p for p in bench["papers"]}
    assert sorted(papers) == GOOD_PAPERS

    p1 = papers["P001"]
    assert p1["file_name"] == "qasper-P001.pdf"
    assert [pg["page"] for pg in p1["pages"]] == list(range(1, 8))
    assert all(pg["file_name"] == "qasper-P001.pdf" and pg["ocr"] is False for pg in p1["pages"])
    assert p1["pages"][0]["text"].startswith("Fast Widget Learning")
    assert "quickly a widget can be learned" in p1["pages"][0]["text"]
    method = p1["pages"][3]["text"]
    assert method.startswith("Method")
    assert "We use a plain pipeline.\n\nThe widget encoder is trained" in method  # paragraphs joined by blank lines

    questions = {q["id"]: q for q in p1["questions"]}
    assert sorted(questions) == ["P001-q1", "P001-q2", "P001-q3"]
    assert questions["P001-q1"]["answerable"] is True
    assert questions["P001-q1"]["paper_id"] == "P001"
    assert questions["P001-q1"]["evidence"] and "Lamport optimizer" in questions["P001-q1"]["evidence"][0]
    assert questions["P001-q3"]["answerable"] is False and questions["P001-q3"]["evidence"] == []


def test_mixed_figure_only_and_no_evidence_questions_are_excluded_and_counted(tmp_path):
    """P5-AC1 (SPEC-GAP-019): disagreeing annotators, figure/table-only and evidence-less questions are excluded."""
    bench = _benchmark(tmp_path)
    ids = {q["id"] for p in bench["papers"] for q in p["questions"]}
    assert not ids & {"P001-q4", "P002-q3", "P002-q4"}
    assert bench["excluded_questions"] == EXPECTED_EXCLUDED
    assert bench["rejected_records"] == EXPECTED_REJECTED


def test_paper_ids_outside_allow_list_are_rejected(tmp_path):
    """Threat row 23 / L6: paper ids must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$; others are rejected and counted."""
    from eval import qasper

    bad = ["../evil", "-lead", ".hidden", "a b", "x" * 65, "", "a/b", "<b>", "café"]
    data = {pid: paper() for pid in bad}
    data.update({"OK.1_a-b": paper(), "A" * 64: paper()})
    path, manifest = write_subset(tmp_path, data)
    bench = qasper.load_benchmark(path, manifest)
    assert sorted(p["paper_id"] for p in bench["papers"]) == sorted(["OK.1_a-b", "A" * 64])
    assert bench["rejected_records"] == len(bad)


def test_malformed_records_are_skipped_and_counted(tmp_path, capsys):
    """Threat row 22 / L3: wrong types are skipped and counted; messages never echo record content."""
    from eval import qasper

    canary = "SECRET-CANARY-4417"
    good = paper(sections=[{"section_name": "S", "paragraphs": ["para one"]}])
    data = {
        "G1": good,
        "B1": {"title": canary, "abstract": 5, "full_text": [], "qas": []},
        "B2": {"title": "t", "abstract": "a", "full_text": [{"section_name": canary, "paragraphs": "oops"}], "qas": []},
        "B3": canary,
        "B4": {"title": "t", "abstract": "a", "full_text": [], "qas": [{"question": canary, "answers": 7}]},
    }
    path, manifest = write_subset(tmp_path, data)
    bench = qasper.load_benchmark(path, manifest)
    assert [p["paper_id"] for p in bench["papers"]] == ["G1"]
    assert bench["rejected_records"] == 4
    out = capsys.readouterr()
    assert canary not in out.out + out.err


# --------------------------------------------------------------------------------------------------------------
# Evidence matching (pure)
# --------------------------------------------------------------------------------------------------------------
def test_evidence_hit_matches_whole_and_split_paragraphs():
    """P5-AC1 (SPEC-GAP-020): whole-paragraph containment, normalisation, and a paragraph split across chunks."""
    from eval.metrics import evidence_hit

    rng = random.Random(1)
    short = _words(rng, 12)
    assert evidence_hit("intro text. " + short + " trailing words", short)
    assert evidence_hit("INTRO\n" + short.replace(" ", "\n", 2).upper(), short)  # case and whitespace are normalised

    long_paragraph = _words(rng, 90)  # well over 400 characters
    first_chunk = long_paragraph[: len(long_paragraph) * 2 // 3]
    second_chunk = long_paragraph[len(long_paragraph) // 3 :]
    assert evidence_hit(first_chunk, long_paragraph)  # long shared run (>= 200 chars) counts
    assert evidence_hit(second_chunk, long_paragraph)
    assert not evidence_hit(_words(random.Random(2), 90), long_paragraph)


def test_overlap_tail_alone_is_not_a_hit():
    """P5-AC1: a chunk that only shares the 150-character chunk overlap with the paragraph does not count."""
    from eval.metrics import evidence_hit

    rng = random.Random(3)
    paragraph = _words(rng, 80)
    tail = paragraph[:150]
    chunk = _words(random.Random(4), 40) + " " + tail
    assert len(paragraph) > 150
    assert not evidence_hit(chunk, paragraph)
    # but a short paragraph that is entirely inside the chunk still hits (min(len, 200) applies)
    small = _words(random.Random(5), 6)
    assert len(small) < 200 and evidence_hit("x " + small + " y", small)
    assert not evidence_hit("x " + small[: len(small) // 2] + " y", small)


@given(
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=0x24F), min_size=1, max_size=60
    ),
    st.text(max_size=40),
    st.text(max_size=40),
)
def test_evidence_hit_is_true_when_the_paragraph_is_contained(paragraph, before, after):
    """P5-AC1: containment always hits, whatever surrounds it."""
    from eval.metrics import evidence_hit, norm

    if not norm(paragraph):
        return
    assert evidence_hit(before + " " + paragraph + " " + after, paragraph)


# --------------------------------------------------------------------------------------------------------------
# Retrieval run on the fixture
# --------------------------------------------------------------------------------------------------------------
def test_recall_and_mrr_on_fixture_match_hand_computed_values(tmp_path):
    """P5-AC1: recall@4 and MRR on the fixture equal the hand-computed values (see tests/qasper_support.py)."""
    block = _run(tmp_path, mode="semantic", top_k=4)
    assert block["status"] == "ok"
    r = block["retrieval"]
    assert r["questions"] == EXPECTED_ANSWERABLE
    assert r["recall_at_k"] == EXPECTED_RECALL
    assert r["mrr"] == EXPECTED_MRR
    assert r["top_k"] == 4 and r["mode"] == "semantic"
    assert block["unanswerable_questions"] == EXPECTED_UNANSWERABLE
    assert block["excluded_questions"] == EXPECTED_EXCLUDED
    assert block["rejected_records"] == EXPECTED_REJECTED
    assert block["papers"] == len(GOOD_PAPERS)


def test_public_benchmark_retrieval_runs_with_sockets_blocked(tmp_path, monkeypatch):
    """P5-AC1 / threat row 25: the offline run never opens a network connection (FakeStore, default settings)."""
    from eval import qasper

    path, manifest = mini_subset(tmp_path)

    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    block = qasper.run_public_benchmark(FakeStore(), subset_path=path, manifest=manifest)
    assert block["status"] == "ok"
    assert block["retrieval"]["mode"] == config.RETRIEVAL_MODE and block["retrieval"]["top_k"] == config.TOP_K


@pytest.mark.skipif(not MODEL_CACHED, reason="needs the local embedding model (as tests/test_integration_replay.py)")
def test_public_benchmark_retrieval_with_real_chroma_store_and_sockets_blocked(tmp_path, monkeypatch):
    """P5-AC1 / threat row 25: same run with the real ChromaStore (hybrid, 800 fixed, top-4)."""
    from eval import qasper

    monkeypatch.setattr(config, "CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(vectorstore, "_collections", {})
    path, manifest = mini_subset(tmp_path)

    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    block = qasper.run_public_benchmark(vectorstore.ChromaStore(), subset_path=path, manifest=manifest)
    assert block["status"] == "ok"
    r = block["retrieval"]
    assert r["questions"] == EXPECTED_ANSWERABLE and r["mode"] == "hybrid" and r["top_k"] == 4
    assert 0.0 <= r["mrr"] <= r["recall_at_k"] <= 1.0


def test_missing_subset_is_reported_as_skipped_with_warning(tmp_path, capsys):
    """P5-AC1 (SPEC-GAP-026): no data -> status skipped + fixed reason + warning on stderr, no exception."""
    from eval import qasper

    manifest = make_manifest(pathlib.Path(__file__))  # hash is irrelevant: the file does not exist
    block = qasper.run_public_benchmark(FakeStore(), subset_path=tmp_path / "missing.json", manifest=manifest)
    assert block["status"] == "skipped"
    assert block["skipped_reason"] and "retrieval" not in block
    err = capsys.readouterr().err
    assert "warning" in err.lower() and str(tmp_path) not in err


def test_whitespace_only_evidence_is_no_evidence(tmp_path):
    """P5 fix 8: blank text evidence without any FLOAT SELECTED item is no_evidence, not figure_or_table_only."""

    def qa(qid, evidence):
        return {
            "question_id": qid,
            "question": "q?",
            "answers": [{"answer": {"unanswerable": False, "evidence": evidence}}],
        }

    qas = [qa("w1", ["   ", "\n\t"]), qa("w2", ["  ", "FLOAT SELECTED: Table 1"]), qa("w3", ["FLOAT SELECTED: Fig 2"])]
    path, manifest = write_subset(tmp_path, {"P1": paper(qas=qas)})
    from eval import qasper

    bench = qasper.load_benchmark(path, manifest)
    assert bench["excluded_questions"] == {"mixed_answerability": 0, "figure_or_table_only": 2, "no_evidence": 1}


def test_tampered_subset_is_refused(tmp_path):
    """Threat row 20: a subset whose SHA-256 differs from the manifest is never used."""
    from eval import qasper

    path, manifest = mini_subset(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"Lamport", b"Mallory"))
    with pytest.raises(qasper.QasperDataError) as exc:
        qasper.load_benchmark(path, manifest)
    assert "Mallory" not in str(exc.value)
    with pytest.raises(qasper.QasperDataError):
        qasper.run_public_benchmark(FakeStore(), subset_path=path, manifest=manifest)


@pytest.mark.parametrize(
    "raw",
    [b"[" * 200000 + b"]" * 200000, b'{"P1": {"title": NaN}}', b'{"P1": Infinity}', b"not json", b"\xff\xfe"],
    ids=["deep-nesting", "nan", "infinity", "garbage", "bad-bytes"],
)
def test_deeply_nested_or_nan_json_is_reported_as_corrupt(tmp_path, raw):
    """L2: hash is checked on the bytes read once; recursion, NaN/Infinity and bad JSON raise QasperDataError."""
    from eval import qasper

    path, manifest = write_subset(tmp_path, raw)
    with pytest.raises(qasper.QasperDataError):
        qasper.load_benchmark(path, manifest)


def test_data_dir_inside_repo_is_refused(tmp_path, monkeypatch):
    """L1 / SPEC-GAP-017: default ~/.cache/trustrag/qasper; env override; a directory inside the repo is refused."""
    from eval import qasper

    monkeypatch.delenv("TRUSTRAG_EVAL_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert pathlib.Path(qasper.resolve_data_dir()) == tmp_path / "home" / ".cache" / "trustrag" / "qasper"

    target = tmp_path / "custom"
    monkeypatch.setenv("TRUSTRAG_EVAL_DATA_DIR", str(target))
    assert pathlib.Path(qasper.resolve_data_dir()) == target  # the read path only resolves; the downloader creates

    for inside in (ROOT / "eval" / "data" / "cache", ROOT):
        monkeypatch.setenv("TRUSTRAG_EVAL_DATA_DIR", str(inside))
        with pytest.raises(qasper.QasperDataError):
            qasper.resolve_data_dir()


def _scrub_repo_dir(path: pathlib.Path) -> None:
    """Remove a directory a (buggy) data-dir check may have created inside the repo."""
    if path.is_dir():
        path.rmdir()


def test_data_dir_inside_repo_is_refused_by_identity(tmp_path, monkeypatch):
    """P5 fix 4: a case-swapped spelling of the repo path (case-insensitive filesystems) is still the repo."""
    from eval import qasper

    swapped = pathlib.Path(str(ROOT).swapcase())
    if swapped == ROOT or not swapped.exists() or not os.path.samefile(swapped, ROOT):
        pytest.skip("case-sensitive filesystem: a case-swapped path is a different directory")
    child = swapped / "eval" / "data" / "_qtest_case_swap"
    monkeypatch.setenv("TRUSTRAG_EVAL_DATA_DIR", str(child))
    try:
        with pytest.raises(qasper.QasperDataError):
            qasper.ensure_private_dir(child)
    finally:
        _scrub_repo_dir(ROOT / "eval" / "data" / "_qtest_case_swap")


def test_data_dir_symlink_into_repo_is_refused(tmp_path):
    """P5 fix 4: a symlink in tmp pointing at a directory inside the repo does not smuggle the data dir in."""
    from eval import qasper

    link = tmp_path / "link"
    link.symlink_to(ROOT / "eval" / "data", target_is_directory=True)
    child = link / "_qtest_symlink_child"
    try:
        with pytest.raises(qasper.QasperDataError):
            qasper.ensure_private_dir(child)
    finally:
        _scrub_repo_dir(ROOT / "eval" / "data" / "_qtest_symlink_child")


def test_read_path_does_not_create_or_chmod_data_dir(tmp_path, monkeypatch):
    """P5 fix 5: resolving/benchmarking never creates the data dir or changes an existing dir's mode."""
    from eval import qasper

    missing = tmp_path / "absent"
    monkeypatch.setenv("TRUSTRAG_EVAL_DATA_DIR", str(missing))
    resolved = qasper.resolve_data_dir()
    assert pathlib.Path(resolved) == missing and not missing.exists()
    manifest = make_manifest(pathlib.Path(__file__))
    block = qasper.run_public_benchmark(
        FakeStore(), subset_path=pathlib.Path(resolved) / qasper.SUBSET_NAME, manifest=manifest
    )
    assert block["status"] == "skipped" and not missing.exists()

    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    monkeypatch.setenv("TRUSTRAG_EVAL_DATA_DIR", str(existing))
    qasper.resolve_data_dir()
    assert (existing.stat().st_mode & 0o777) == 0o755


_BAD_TEXT = ["x](http://e)<b>|", "", "a" * 65, "v1\nx", "caf\u00e9", "a`b", "a*b*", "v0.3\n"]
_BAD_URLS = [
    "http://example.org/a.tgz",
    "https://example.org/a b.tgz",
    "https://example.org/a|b",
    "https://example.org/<b>",
    "https://example.org/[x]",
    "https://example.org/(x)",
    "https://example.org/\x01",
    "https://example.org/caf\u00e9",
    "https://example.org/a.tgz\n",
]


def test_manifest_free_text_fields_are_charset_validated(tmp_path):
    """P5 fix 3: dataset/version/split/licence are 1-64 chars of [A-Za-z0-9 ._()-]; source_url is a clean https URL.

    The `citation` field is deliberately left unconstrained: the committed text has commas and is never rendered.
    """
    from eval import qasper

    base = json.loads((ROOT / "eval" / "data" / "qasper_manifest.json").read_text(encoding="utf-8"))
    qasper.load_manifest(ROOT / "eval" / "data" / "qasper_manifest.json")  # the committed manifest stays valid

    def load(**changes):
        path = tmp_path / "m.json"
        path.write_text(json.dumps({**base, **changes}), encoding="utf-8")
        return qasper.load_manifest(path)

    assert load(dataset="Qasper (dev)", version="v0.3-rc_1", split="dev", licence="CC BY 4.0")["split"] == "dev"
    for field in ("dataset", "version", "split", "licence"):
        for bad in _BAD_TEXT:
            with pytest.raises(qasper.QasperDataError):
                load(**{field: bad})
    for url in _BAD_URLS:
        with pytest.raises(qasper.QasperDataError):
            load(source_url=url)


def test_committed_manifest_has_required_keys_and_https_url():
    """P5-AC1 / ADR 0009: eval/data/qasper_manifest.json is committed with the pinned fields."""
    path = ROOT / "eval" / "data" / "qasper_manifest.json"
    assert path.exists(), "manifest skeleton missing (T3a)"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "source_url",
        "version",
        "split",
        "archive_member",
        "archive_sha256",
        "subset_sha256",
        "paper_ids",
        "licence",
        "citation",
    ):
        assert key in manifest, key
    assert manifest["source_url"].startswith("https://")
    assert manifest["licence"] == "CC BY 4.0" and manifest["split"] == "dev"


# --------------------------------------------------------------------------------------------------------------
# Results block, answers, data hygiene
# --------------------------------------------------------------------------------------------------------------
def test_results_json_public_benchmark_block_has_dataset_licence_and_hash(tmp_path):
    """P5-AC2: the public_benchmark block names the dataset, version, split, licence, source and subset hash."""
    path, manifest = mini_subset(tmp_path)
    from eval import qasper

    block = qasper.run_public_benchmark(FakeStore(), subset_path=path, manifest=manifest)
    assert block["dataset"] == "Qasper"
    assert block["licence"] == "CC BY 4.0" and block["split"] == "dev" and block["version"] == "test-fixture"
    assert block["source_url"] == manifest["source_url"]
    assert block["subset_sha256"] == sha256_file(path)
    assert block["status"] == "ok" and set(block["retrieval"]) >= {"questions", "recall_at_k", "mrr", "top_k", "mode"}
    json.dumps(block, allow_nan=False)  # serialisable, finite numbers only


def test_qasper_answers_store_only_id_answerable_refused_and_skip_fact_check(tmp_path):
    """L4 / threat row 24 (SPEC-GAP-023): suite all generates answers only; stores id/answerable/refused, no text."""
    llm = FakeLLM(answer=config.REFUSAL_TEXT)
    block = _run(tmp_path, suite="all", llm=llm)
    assert {c["purpose"] for c in llm.calls} == {"answer"}  # no verify, scan or rerank call
    rows = block["per_question"]
    assert all(set(r) == {"id", "answerable", "refused"} for r in rows)
    assert len(rows) == EXPECTED_ANSWERABLE + EXPECTED_UNANSWERABLE
    a = block["answers"]
    assert a["answerable_questions"] == EXPECTED_ANSWERABLE and a["unanswerable_questions"] == EXPECTED_UNANSWERABLE
    assert a["correct_refusal_rate"] == 1.0 and a["false_refusal_rate"] == 1.0
    assert set(a) <= {"answerable_questions", "unanswerable_questions", "correct_refusal_rate", "false_refusal_rate"}
    text = json.dumps(block)
    assert not [c for c in CANARIES if c in text]


def test_qasper_refusal_rates_when_the_model_answers_everything(tmp_path):
    """P5-AC2 (SPEC-GAP-023): a model that never refuses has 0 correct refusals and 0 false refusals."""
    block = _run(tmp_path, suite="all", llm=FakeLLM(answer="Something [qasper-P001.pdf, p.1]."))
    assert block["answers"]["correct_refusal_rate"] == 0.0 and block["answers"]["false_refusal_rate"] == 0.0


def test_free_suite_makes_no_llm_calls_and_has_no_answers(tmp_path):
    """P5-AC1: suite free never touches the model."""
    llm = FakeLLM()
    block = _run(tmp_path, suite="free", llm=llm)
    assert llm.calls == [] and "answers" not in block and "per_question" not in block


def _written_artifacts(tmp_path, block):
    """Everything P5 writes to disk for a run, rendered from the fixture-based block."""
    from eval import history, run_eval, trend

    results = {
        "run_at": "2026-01-01T00:00:00",
        "commit": "abc1234",
        "suite": "free",
        "answer_model": "m-answer",
        "checker_model": "m-check",
        "prompt_versions": {"answer": "answer-v3"},
        "settings": {"chunk_size": 800},
        "retrieval": {"questions": 1, "recall_at_k": 1.0, "mrr": 1.0, "top_k": 4, "mode": "hybrid"},
        "public_benchmark": block,
    }
    entry = history.entry_from_results(results, commit_dirty=True, datasets={"self_authored": "0" * 64})
    hist = tmp_path / "history.jsonl"
    history.append_entry(hist, entry)
    return {
        "results.json": json.dumps(results),
        "RESULTS.md": run_eval.write_markdown(results),
        "history.jsonl": hist.read_text(encoding="utf-8"),
        "TRENDS.md": trend.render_trends(history.load_history(hist)),
    }


def test_results_and_history_contain_no_dataset_text(tmp_path):
    """Threat row 23 / M4: no paper, question, evidence or markup text, and no invisible characters, in any output."""
    block = _run(tmp_path, suite="all", llm=FakeLLM(answer=config.REFUSAL_TEXT))
    forbidden_chars = {"\u200b", "\u00ad", "\ufeff", "\u202e"}
    for name, text in _written_artifacts(tmp_path, block).items():
        assert not [c for c in CANARIES if c in text], name
        assert not [c for c in forbidden_chars if c in text], name
        assert "<script" not in text and "](http" not in text, name


def test_no_home_path_in_written_files(tmp_path, monkeypatch):
    """L8: no home directory, cache path or temp path appears in results, history or trends."""
    monkeypatch.setenv("HOME", str(tmp_path / "homedir"))
    block = _run(tmp_path, suite="free")
    for name, text in _written_artifacts(tmp_path, block).items():
        assert str(tmp_path) not in text and str(pathlib.Path.home()) not in text, name
        assert ".cache" not in text, name


# --------------------------------------------------------------------------------------------------------------
# Telemetry and code-safety scans
# --------------------------------------------------------------------------------------------------------------
def test_eval_process_turns_chroma_telemetry_off():
    """P5-AC1 / threat row 25 (SPEC-GAP-027): importing the eval runner switches ChromaDB telemetry off."""
    code = (
        "import eval.run_eval\n"
        "from chromadb.config import Settings\n"
        "print('TELEMETRY=' + str(Settings().anonymized_telemetry))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert "TELEMETRY=False" in done.stdout


BENCHMARK_MODULES = ["qasper.py", "qasper_download.py", "history.py", "trend.py", "metrics.py"]
NETWORK_LIBS = {"urllib", "http", "socket", "httpx", "requests", "huggingface_hub", "ftplib", "ssl", "aiohttp"}
UNSAFE_LOADERS = {"pickle", "cPickle", "dill", "shelve", "marshal", "yaml", "datasets", "pyarrow", "joblib"}


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _calls(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


@pytest.mark.parametrize("module", BENCHMARK_MODULES)
def test_benchmark_modules_import_no_unsafe_loaders(module):
    """Threat row 22: JSON only; no pickle/yaml/datasets and no eval()/exec() in benchmark modules."""
    path = ROOT / "eval" / module
    assert path.exists(), f"eval/{module} missing"
    assert not _imports(path) & UNSAFE_LOADERS
    assert not _calls(path) & {"eval", "exec", "compile", "__import__"}


@pytest.mark.parametrize("module", [*BENCHMARK_MODULES, "run_eval.py"])
def test_only_download_module_imports_network_libraries(module):
    """M4 / threat row 25: network code lives only in eval/qasper_download.py."""
    path = ROOT / "eval" / module
    assert path.exists(), f"eval/{module} missing"
    found = _imports(path) & NETWORK_LIBS
    if module == "qasper_download.py":
        assert "urllib" in found
    else:
        assert not found, found


def test_new_modules_are_not_importing_chroma_or_llm_at_import_time_in_history_and_trend():
    """P5-AC3: the trend/history tools are light (no model, no vector store) so `make eval-trend` is instant."""
    for module in ("history.py", "trend.py"):
        path = ROOT / "eval" / module
        assert path.exists(), f"eval/{module} missing"
        assert not _imports(path) & {"chromadb", "anthropic", "sentence_transformers", "onnxruntime"}


def test_fixture_is_ascii_and_has_no_invisible_characters():
    """The synthetic fixture must pass the invisible-character gate (hostile characters are written as escapes)."""
    raw = (ROOT / "tests" / "data" / "qasper_mini.json").read_bytes()
    assert raw.isascii()
    assert b"\\u200b" in raw and b"\\u00ad" in raw  # the hostile characters are really there, as escapes


# --------------------------------------------------------------------------------------------------------------
# run_eval.main wiring (P5 fix 7). Interface assumed: run_eval.main() reads sys.argv; RESULTS_DIR, history.HISTORY_PATH
# and trend.HISTORY_PATH/TRENDS_PATH are module globals looked up at call time; no new production hook is needed.
# --------------------------------------------------------------------------------------------------------------
def _patch_run_eval(monkeypatch, tmp_path, argv):
    from eval import history, qasper, run_eval, trend

    paid: list[str] = []
    monkeypatch.setattr(sys, "argv", ["run_eval", *argv])
    monkeypatch.setattr(run_eval.llm, "missing_key_message", lambda: None)
    monkeypatch.setattr(run_eval, "load_questions", lambda: [])
    monkeypatch.setattr(run_eval, "load_corpus_pages", lambda: [])
    monkeypatch.setattr(run_eval, "git_dirty", lambda: False)
    monkeypatch.setattr(run_eval, "build_index", lambda *a, **k: "ws")
    monkeypatch.setattr(run_eval, "eval_ablation", lambda *a, **k: [])
    monkeypatch.setattr(run_eval, "write_markdown", lambda results: "md")

    def retrieval(*args, **kw):
        if args[4] if len(args) > 4 else kw.get("rerank"):
            paid.append("eval_retrieval(rerank)")
        return {}

    def answers(*args, **kw):
        paid.append("eval_answers")
        return {}, [], {}

    def injection(*args, **kw):
        if args[0] if args else kw.get("use_ai"):
            paid.append("eval_injection(use_ai)")
        return {}

    monkeypatch.setattr(run_eval, "eval_retrieval", retrieval)
    monkeypatch.setattr(run_eval, "eval_answers", answers)
    monkeypatch.setattr(run_eval, "eval_injection", injection)
    results_dir = tmp_path / "results"
    monkeypatch.setattr(run_eval, "RESULTS_DIR", str(results_dir))
    for module in (history, trend):
        monkeypatch.setattr(module, "HISTORY_PATH", results_dir / "history.jsonl")
    monkeypatch.setattr(trend, "TRENDS_PATH", results_dir / "TRENDS.md")
    monkeypatch.setattr(qasper, "resolve_data_dir", lambda: tmp_path / "nodata")
    return run_eval, qasper, paid, results_dir


def test_qasper_data_error_fails_before_paid_suites(tmp_path, monkeypatch):
    """P5 fix 7: a Qasper data error stops `--suite all` before any Claude-using suite and writes no result files."""
    run_eval, qasper, paid, results_dir = _patch_run_eval(monkeypatch, tmp_path, ["--suite", "all"])

    def broken(*args, **kw):
        raise qasper.QasperDataError("manifest broken")

    monkeypatch.setattr(qasper, "load_manifest", broken)
    monkeypatch.setattr(qasper, "load_benchmark", broken)
    with pytest.raises(SystemExit) as exc:
        run_eval.main()
    assert exc.value.code not in (None, 0)
    assert paid == []
    assert not results_dir.exists() or not list(results_dir.iterdir())


def test_qasper_failure_after_paid_suites_still_writes_results(tmp_path, monkeypatch):
    """P5 fix round 2 (NEW-1): a Qasper crash after paid suites keeps results.json/RESULTS.md/history (status error,
    fixed reason, no exception text) and then exits non-zero. Choice: the history entry IS appended, without qasper."""
    from eval import history

    run_eval, qasper, _paid, results_dir = _patch_run_eval(monkeypatch, tmp_path, ["--suite", "all"])

    def boom(*args, **kw):
        raise RuntimeError("boom /Users/x secret")

    monkeypatch.setattr(qasper, "run_public_benchmark", boom)
    with pytest.raises(SystemExit) as exc:
        run_eval.main()
    assert exc.value.code not in (None, 0)
    results = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    block = results["public_benchmark"]
    assert block["status"] == "error"
    assert isinstance(block["reason"], str) and block["reason"]
    assert (results_dir / "RESULTS.md").is_file()
    for path in results_dir.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "boom" not in text and "/Users" not in text and "secret" not in text
    lines = (results_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    history.validate_entry(entry)
    assert "qasper" not in entry.get("datasets", {})


def test_run_eval_free_exits_zero_when_subset_missing(tmp_path, monkeypatch):
    """P5 fix 7 (SPEC-GAP-026): `--suite free` with no downloaded subset still exits 0 and records status skipped."""
    run_eval, _qasper, paid, results_dir = _patch_run_eval(monkeypatch, tmp_path, ["--suite", "free"])
    try:
        run_eval.main()
    except SystemExit as exc:
        assert exc.code in (None, 0)
    assert paid == []
    results = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    assert results["public_benchmark"]["status"] == "skipped"


def test_symlink_loop_data_dir_is_refused_cleanly(tmp_path):
    """P5 fix round 2 (security N1): a symlink loop in the data dir raises QasperDataError, not RuntimeError."""
    from eval import qasper

    a, b = tmp_path / "a", tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    with pytest.raises(qasper.QasperDataError):
        qasper.check_outside_repo(a / "x")
