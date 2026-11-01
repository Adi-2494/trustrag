"""
Public benchmark (Qasper, CC BY 4.0): load the hash-pinned 50-paper subset and score retrieval and refusals on it.

Why this shape (ADR 0009, docs/phases/p5-plan.md): Qasper text is third-party and untrusted, like an uploaded PDF.
So the subset is JSON only, re-hashed against the committed manifest on every load, validated field by field
(malformed papers are skipped and counted, never echoed), and only aggregate numbers leave this module. This module
does no network I/O; the single download script is `eval/qasper_download.py`.
"""

import hashlib
import json
import os
import pathlib
import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from eval.metrics import evidence_hit
from src import config, retrieval, safety
from src.chunker import chunk_pages
from src.generator import generate_answer, is_refusal
from src.interfaces import LLMClient, VectorStore
from src.models import Chunk, Page, RetrievedChunk

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "eval" / "data" / "qasper_manifest.json"
SUBSET_NAME = "subset.json"
DATA_DIR_ENV = "TRUSTRAG_EVAL_DATA_DIR"
SUBSET_PAPERS = 50  # SPEC-GAP-018: first 50 sorted dev papers with at least one scorable question
WORKERS = 4

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FLOAT_PREFIX = "FLOAT SELECTED"  # figure/table evidence in Qasper (format not verified, see plan section 2.3)
EXCLUSION_REASONS = ("mixed_answerability", "figure_or_table_only", "no_evidence")
SKIPPED_REASON = "Qasper subset not downloaded; run make eval-download-qasper (needs approval)"
_TEXT_RE = re.compile(r"[A-Za-z0-9 ._()-]{1,64}")
_URL_RE = re.compile(r"https://[!-~]+")  # printable ASCII, no spaces
_MANIFEST_KEYS = ("dataset", "version", "split", "licence", "source_url", "archive_member")

Manifest = dict[str, Any]
Question = dict[str, Any]


class QasperDataError(Exception):
    """The manifest or the subset is missing, tampered with or unreadable. Messages never contain dataset text."""


# ---------------------------------------------------------------------------
# Paths and manifest
# ---------------------------------------------------------------------------
def check_outside_repo(path: pathlib.Path) -> pathlib.Path:
    """Refuse the repository or anything under it, judged by identity (not spelling): macOS is case-insensitive and
    symlinks/hardlinks can name the same directory. Checks the path and each existing ancestor against REPO_ROOT."""
    try:
        resolved = path.expanduser().resolve()
    except (RuntimeError, OSError) as exc:  # symlink loop, unreadable component
        raise QasperDataError("The benchmark data directory could not be resolved.") from exc
    for candidate in (resolved, *resolved.parents):
        try:
            inside = candidate == REPO_ROOT or os.path.samefile(candidate, REPO_ROOT)
        except OSError:
            continue  # this ancestor does not exist (yet) or is unreadable: it cannot be the repo
        if inside:
            raise QasperDataError("The benchmark data directory must be outside the repository.")
    return path


def ensure_private_dir(path: pathlib.Path) -> pathlib.Path:
    """Create the data directory (mode 0o700) for the downloader; refuse one inside the repository."""
    check_outside_repo(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def resolve_data_dir() -> pathlib.Path:
    """`TRUSTRAG_EVAL_DATA_DIR` if set, otherwise ~/.cache/trustrag/qasper (SPEC-GAP-017). Resolves and validates only."""
    override = os.environ.get(DATA_DIR_ENV)
    path = pathlib.Path(override).expanduser() if override else pathlib.Path.home() / ".cache" / "trustrag" / "qasper"
    return check_outside_repo(path)  # read path: validate only, never create or chmod


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load_manifest(path: pathlib.Path = MANIFEST_PATH) -> Manifest:
    """Read and validate the committed manifest (pinned URL, hashes and paper ids)."""
    try:
        manifest = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise QasperDataError("The Qasper manifest is missing or is not valid JSON.") from exc
    if not isinstance(manifest, dict) or any(not isinstance(manifest.get(k), str) for k in _MANIFEST_KEYS):
        raise QasperDataError("The Qasper manifest is missing required fields.")
    # These fields reach RESULTS.md; `citation` is never rendered and stays free text.
    if any(not _TEXT_RE.fullmatch(manifest[k]) for k in ("dataset", "version", "split", "licence")) or not (
        _URL_RE.fullmatch(manifest["source_url"]) and not re.search(r"[|<>\[\]()]", manifest["source_url"])
    ):
        raise QasperDataError("The Qasper manifest has a field with unexpected characters.")
    for key in ("archive_sha256", "subset_sha256"):
        if manifest.get(key) is not None and not _is_sha256(manifest[key]):
            raise QasperDataError(f"The Qasper manifest field {key} is not a SHA-256 hex digest.")
    ids = manifest.get("paper_ids", [])
    if not isinstance(ids, list) or any(not (isinstance(i, str) and ID_RE.fullmatch(i)) for i in ids):
        raise QasperDataError("The Qasper manifest has invalid paper ids.")
    return manifest


# ---------------------------------------------------------------------------
# Parsing one paper
# ---------------------------------------------------------------------------
def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _parse_pages(paper_id: str, record: dict[str, Any]) -> list[Page]:
    title, abstract, sections = record.get("title"), record.get("abstract"), record.get("full_text")
    if not isinstance(title, str) or not isinstance(abstract, str) or not isinstance(sections, list):
        raise ValueError("bad paper")
    file_name = safety.sanitize_filename(f"qasper-{paper_id}.pdf")
    texts = [f"{title}\n\n{abstract}"]
    for section in sections:
        if not isinstance(section, dict) or not _is_str_list(section.get("paragraphs")):
            raise ValueError("bad section")
        name = section.get("section_name")
        if name is not None and not isinstance(name, str):
            raise ValueError("bad section")
        texts.append("\n\n".join([name or "", *section["paragraphs"]]).strip())
    return [{"text": t, "file_name": file_name, "page": n, "ocr": False} for n, t in enumerate(texts, start=1)]


def _classify(annotations: list[Any]) -> tuple[str, list[str]]:
    """Return ("answerable" | "unanswerable", gold paragraphs) or (exclusion reason, [])."""
    answers = []
    for item in annotations:
        answer = item.get("answer") if isinstance(item, dict) else None
        if (
            not isinstance(answer, dict)
            or not isinstance(answer.get("unanswerable"), bool)
            or not _is_str_list(answer.get("evidence", []))
        ):
            raise ValueError("bad answer")
        answers.append(answer)
    flags = {a["unanswerable"] for a in answers}
    if flags == {True}:
        return "unanswerable", []
    if flags != {False}:
        return "mixed_answerability" if flags else "no_evidence", []
    evidence = [e for a in answers for e in a.get("evidence", [])]
    if not evidence:
        return "no_evidence", []
    if not any(e.strip() for e in evidence) and not any(e.startswith(FLOAT_PREFIX) for e in evidence):
        return "no_evidence", []
    text = list(dict.fromkeys(e for e in evidence if e.strip() and not e.startswith(FLOAT_PREFIX)))
    return ("answerable", text) if text else ("figure_or_table_only", [])


def _parse_paper(paper_id: str, record: object) -> tuple[list[Page], list[Question], dict[str, int]]:
    if not isinstance(record, dict) or not isinstance(record.get("qas"), list):
        raise ValueError("bad paper")
    pages = _parse_pages(paper_id, record)
    questions: list[Question] = []
    excluded = dict.fromkeys(EXCLUSION_REASONS, 0)
    for qa in record["qas"]:
        if not isinstance(qa, dict):
            raise ValueError("bad question")
        qid, text, annotations = qa.get("question_id"), qa.get("question"), qa.get("answers")
        if not (isinstance(qid, str) and ID_RE.fullmatch(qid)) or not isinstance(text, str):
            raise ValueError("bad question")
        if not isinstance(annotations, list):
            raise ValueError("bad question")
        kind, evidence = _classify(annotations)
        if kind in EXCLUSION_REASONS:
            excluded[kind] += 1
            continue
        questions.append(
            {
                "id": qid,
                "paper_id": paper_id,
                "question": text,
                "answerable": kind == "answerable",
                "evidence": evidence,
            }
        )
    return pages, questions, excluded


# ---------------------------------------------------------------------------
# Loading the subset
# ---------------------------------------------------------------------------
def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def parse_papers(raw: bytes) -> dict[str, Any]:
    """Parse subset or archive-member bytes as plain JSON (no NaN/Infinity); a top-level object of papers."""
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # includes bad bytes and invalid JSON
        raise QasperDataError("The Qasper subset is corrupt (not valid JSON).") from exc
    if not isinstance(data, dict):
        raise QasperDataError("The Qasper subset is corrupt (expected a JSON object of papers).")
    return data


def load_benchmark(subset_path: pathlib.Path, manifest: Manifest) -> dict[str, Any]:
    """Verify the subset against the manifest hash, then build papers (pages + questions) and exclusion counts."""
    expected = manifest.get("subset_sha256")
    if not _is_sha256(expected):
        raise QasperDataError("The manifest does not pin a subset hash; run the approved download first.")
    try:
        raw = pathlib.Path(subset_path).read_bytes()  # read once: hash and parse the same bytes
    except OSError as exc:
        raise QasperDataError("The Qasper subset could not be read.") from exc
    if hashlib.sha256(raw).hexdigest() != expected:
        raise QasperDataError("The Qasper subset does not match the SHA-256 in the manifest; it was not used.")
    data = parse_papers(raw)

    papers: list[dict[str, Any]] = []
    excluded = dict.fromkeys(EXCLUSION_REASONS, 0)
    rejected = 0
    for paper_id in sorted(data):
        try:
            if not ID_RE.fullmatch(paper_id):
                raise ValueError("bad id")
            pages, questions, paper_excluded = _parse_paper(paper_id, data[paper_id])
        except ValueError:
            rejected += 1
            continue
        for reason, count in paper_excluded.items():
            excluded[reason] += count
        papers.append(
            {
                "paper_id": paper_id,
                "file_name": pages[0]["file_name"],
                "pages": pages,
                "questions": questions,
            }
        )
    return {"papers": papers, "excluded_questions": excluded, "rejected_records": rejected}


def select_paper_ids(data: dict[str, Any], limit: int = SUBSET_PAPERS) -> list[str]:
    """Sorted ids of the first `limit` valid papers that have at least one answerable, scorable question."""
    chosen: list[str] = []
    for paper_id in sorted(data):
        if len(chosen) >= limit:
            break
        try:
            if not ID_RE.fullmatch(paper_id):
                raise ValueError("bad id")
            _, questions, _ = _parse_paper(paper_id, data[paper_id])
        except ValueError:
            continue
        if any(q["answerable"] for q in questions):
            chosen.append(paper_id)
    return chosen


# ---------------------------------------------------------------------------
# Running the benchmark
# ---------------------------------------------------------------------------
def _block_header(manifest: Manifest) -> dict[str, Any]:
    return {
        "dataset": manifest["dataset"],
        "version": manifest["version"],
        "split": manifest["split"],
        "licence": manifest["licence"],
        "source_url": manifest["source_url"],
    }


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 3) if total else None


def _retrieve_all(
    store: VectorStore, papers: list[dict[str, Any]], mode: str, top_k: int
) -> list[tuple[Question, list[RetrievedChunk]]]:
    """One workspace per paper (Qasper's own task setting); each question searches only its paper."""
    rows: list[tuple[Question, list[RetrievedChunk]]] = []
    for paper in papers:
        if not paper["questions"]:
            continue
        workspace = store.new_workspace()
        try:
            chunks: list[Chunk] = chunk_pages(paper["pages"])
            store.add_chunks(workspace, chunks)
            for q in paper["questions"]:
                found = retrieval.retrieve(store, workspace, q["question"], top_k, mode=mode, use_rerank=False)
                rows.append((q, found))
        finally:
            store.clear(workspace)
    return rows


def _first_hit_rank(found: list[RetrievedChunk], evidence: list[str], hit: Callable[[str, str], bool]) -> int | None:
    for rank, chunk in enumerate(found, start=1):
        if any(hit(chunk["text"], paragraph) for paragraph in evidence):
            return rank
    return None


def _answers(
    rows: list[tuple[Question, list[RetrievedChunk]]], llm: LLMClient | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Generate answers only (no fact-check, no dataset text stored); count refusals by answerability."""

    def one(row: tuple[Question, list[RetrievedChunk]]) -> bool:
        return is_refusal(generate_answer(row[0]["question"], row[1], client=llm))

    with ThreadPoolExecutor(WORKERS) as pool:
        refused = list(pool.map(one, rows))
    per_question = [
        {"id": q["id"], "answerable": q["answerable"], "refused": r} for (q, _), r in zip(rows, refused, strict=True)
    ]
    answerable = [r for r in per_question if r["answerable"]]
    unanswerable = [r for r in per_question if not r["answerable"]]
    summary = {
        "answerable_questions": len(answerable),
        "unanswerable_questions": len(unanswerable),
        "correct_refusal_rate": _rate(sum(r["refused"] for r in unanswerable), len(unanswerable)),
        "false_refusal_rate": _rate(sum(r["refused"] for r in answerable), len(answerable)),
    }
    return summary, per_question


def run_public_benchmark(
    store: VectorStore,
    *,
    subset_path: pathlib.Path,
    manifest: Manifest,
    suite: str = "free",
    llm: LLMClient | None = None,
    mode: str = config.RETRIEVAL_MODE,
    top_k: int = config.TOP_K,
) -> dict[str, Any]:
    """
    Build the `public_benchmark` results block. Suite "free" scores retrieval only (no model calls); suite "all" also
    generates answers for refusal metrics. A missing or unpinned subset gives status "skipped" (SPEC-GAP-026); a
    subset that fails its hash check raises QasperDataError.
    """
    block: dict[str, Any] = _block_header(manifest)
    if manifest.get("subset_sha256") is None or not pathlib.Path(subset_path).is_file():
        print("warning: Qasper subset not found; public benchmark skipped.", file=sys.stderr)
        return {**block, "status": "skipped", "skipped_reason": SKIPPED_REASON}

    bench = load_benchmark(subset_path, manifest)
    rows = _retrieve_all(store, bench["papers"], mode, top_k)
    answerable = [(q, found) for q, found in rows if q["answerable"]]
    ranks = [_first_hit_rank(found, q["evidence"], evidence_hit) for q, found in answerable]
    hits = [r for r in ranks if r is not None]
    block.update(
        papers=len(bench["papers"]),
        subset_sha256=manifest["subset_sha256"],
        status="ok",
        retrieval={
            "questions": len(ranks),
            "recall_at_k": _rate(len(hits), len(ranks)),
            "mrr": round(sum(1 / r for r in hits) / len(ranks), 3) if ranks else None,
            "top_k": top_k,
            "mode": mode,
        },
        unanswerable_questions=len(rows) - len(answerable),
        excluded_questions=bench["excluded_questions"],
        rejected_records=bench["rejected_records"],
    )
    if suite == "all":
        block["answers"], block["per_question"] = _answers(rows, llm)
    return block
