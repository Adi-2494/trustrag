"""
TrustRAG evaluation runner.

  python -m eval.run_eval --suite free   # no Claude calls: retrieval, ablations, rule-only injection benchmark
  python -m eval.run_eval --suite all    # everything, including answer quality, trust score and the AI scanner

Results are written to eval/results/results.json and eval/results/RESULTS.md.
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Isolate the evaluation from the user's real data and app limits BEFORE the app modules read their config.
_TMP = tempfile.mkdtemp(prefix="trustrag-eval-")
os.environ["CHROMA_DIR"] = os.path.join(_TMP, "chroma")
os.environ.setdefault("MAX_CALLS_PER_HOUR", "5000")
# ChromaDB anonymized telemetry off for the eval process only, before chromadb is imported (SPEC-GAP-027).
os.environ["ANONYMIZED_TELEMETRY"] = "False"

from eval import history, qasper, trend  # noqa: E402
from eval.corpus import CORPUS_DIR, DOCS, QA_FILE  # noqa: E402
from eval.injection_data import SAMPLES  # noqa: E402
from eval.metrics import auroc, cites, classification_metrics, corrupt, has_keys, norm  # noqa: E402
from src import config, injection_scanner, llm, retrieval, vectorstore  # noqa: E402
from src.chunker import chunk_pages  # noqa: E402
from src.generator import generate_answer, is_refusal  # noqa: E402
from src.loader import load_pdf  # noqa: E402
from src.verifier import verify_answer  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
WORKERS = 4


def load_questions() -> list[dict]:
    with open(QA_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def load_corpus_pages() -> list[dict]:
    pages = []
    for file_name in DOCS:
        pages.extend(load_pdf(os.path.join(CORPUS_DIR, file_name), file_name))
    return pages


STORE = vectorstore.ChromaStore()


def build_index(pages: list[dict], chunk_size: int, chunk_overlap: int, strategy: str | None = None) -> str:
    workspace = vectorstore.new_workspace_id()
    vectorstore.add_chunks(workspace, chunk_pages(pages, chunk_size, chunk_overlap, strategy))
    return workspace


# ---------------------------------------------------------------------------
# Retrieval: does the chunk holding the evidence come back, and how high?
# ---------------------------------------------------------------------------
def first_relevant_rank(chunks: list[dict], q: dict) -> int | None:
    for rank, chunk in enumerate(chunks, start=1):
        if chunk["file_name"] == q["file"] and norm(q["evidence"]) in norm(chunk["text"]):
            return rank
    return None


def eval_retrieval(
    workspace: str, questions: list[dict], top_k: int, mode: str = config.RETRIEVAL_MODE, rerank: bool = False
) -> dict:
    def search(q: dict) -> list:
        return retrieval.retrieve(STORE, workspace, q["question"], top_k, mode=mode, use_rerank=rerank)

    answerable = [q for q in questions if q["answerable"]]
    if rerank:  # reranking calls Claude, so run the questions in parallel
        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(search, answerable))
    else:
        results = [search(q) for q in answerable]
    ranks = [first_relevant_rank(chunks, q) for chunks, q in zip(results, answerable, strict=True)]
    found = [r for r in ranks if r is not None]
    return {
        "questions": len(ranks),
        "recall_at_k": round(len(found) / len(ranks), 3),
        "mrr": round(sum(1 / r for r in found) / len(ranks), 3),
        "top_k": top_k,
        "mode": mode + ("+rerank" if rerank else ""),
    }


def eval_ablation(pages: list[dict], questions: list[dict]) -> list[dict]:
    """Free (no model calls): chunk size x chunking strategy x retrieval mode, at top_k 4."""
    rows = []
    for chunk_size in (500, 800, 1200):
        overlap = round(chunk_size * 0.1875)  # same overlap ratio as the default (150 / 800)
        for strategy in ("fixed", "section"):
            workspace = build_index(pages, chunk_size, overlap, strategy)
            for mode in ("semantic", "hybrid"):
                rows.append(
                    {
                        "chunk_size": chunk_size,
                        "overlap": overlap,
                        "strategy": strategy,
                        **eval_retrieval(workspace, questions, config.TOP_K, mode),
                    }
                )
    return rows


# ---------------------------------------------------------------------------
# Answers: correctness, citations, refusals, and how well the trust score tracks correctness
# ---------------------------------------------------------------------------


def run_one(workspace: str, q: dict) -> dict:
    started = time.time()
    chunks = retrieval.retrieve(STORE, workspace, q["question"])
    answer = generate_answer(q["question"], chunks)
    try:
        verification = verify_answer(answer, chunks)
    except llm.LLMError:
        verification = None
    refused = is_refusal(answer)
    row = {
        "id": q["id"],
        "question": q["question"],
        "answerable": q["answerable"],
        "answer": answer,
        "refused": refused,
        "trust": verification["trust_score"] if verification else None,
        "claims": [c["status"] for c in verification["claims"]] if verification else None,
        "seconds": round(time.time() - started, 1),
        "chunks": chunks,
    }
    if q["answerable"]:
        row.update(
            correct=(not refused) and has_keys(answer, q["answer_keys"]),
            cites_source=cites(answer, q["file"], q["page"]),
        )
    return row


def eval_answers(workspace: str, questions: list[dict]) -> tuple[dict, list[dict], dict]:
    with ThreadPoolExecutor(WORKERS) as pool:
        rows = list(pool.map(lambda q: run_one(workspace, q), questions))

    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    metrics = {
        "answerable_questions": len(answerable),
        "unanswerable_questions": len(unanswerable),
        "answer_accuracy": round(sum(r["correct"] for r in answerable) / len(answerable), 3),
        "citation_accuracy": round(sum(r["cites_source"] for r in answerable) / len(answerable), 3),
        "false_refusal_rate": round(sum(r["refused"] for r in answerable) / len(answerable), 3),
        "correct_refusal_rate": round(sum(r["refused"] for r in unanswerable) / len(unanswerable), 3),
        # The metric that matters for hallucination: a non-refusal is only a failure if it makes unsupported claims.
        "unanswerable_with_unsupported_claims": round(
            sum(
                (not r["refused"]) and (r["claims"] is None or "NOT_SUPPORTED" in r["claims"] or (r["trust"] or 0) < 80)
                for r in unanswerable
            )
            / len(unanswerable),
            3,
        ),
        "median_seconds_per_question": sorted(r["seconds"] for r in rows)[len(rows) // 2],
    }

    # Trust score vs ground truth: real correct answers should score high, deliberately falsified ones low.
    by_id = {q["id"]: q for q in questions}
    correct_scores = [r["trust"] for r in answerable if r["correct"] and r["trust"] is not None]
    falsified = []
    for r in answerable:
        q = by_id[r["id"]]
        sentence = next(s for s in DOCS[q["file"]][q["page"] - 1][1] if q["evidence"] in s)
        fake = corrupt(sentence)
        if fake:
            falsified.append((f"{fake} [{q['file']}, p.{q['page']}]", r["chunks"]))
    with ThreadPoolExecutor(WORKERS) as pool:
        fake_results = list(pool.map(lambda item: verify_answer(*item), falsified))
    fake_scores = [v["trust_score"] for v in fake_results if v["trust_score"] is not None]
    trust = {
        "correct_answers_scored": len(correct_scores),
        "falsified_answers_scored": len(fake_scores),
        "mean_trust_correct": round(sum(correct_scores) / len(correct_scores), 1) if correct_scores else None,
        "mean_trust_falsified": round(sum(fake_scores) / len(fake_scores), 1) if fake_scores else None,
        "auroc_correct_vs_falsified": auroc(correct_scores, fake_scores),
        "falsified_scored_green": round(sum(s >= config.TRUST_GREEN for s in fake_scores) / len(fake_scores), 3)
        if fake_scores
        else None,
    }
    for r in rows:
        r.pop("chunks")
    return metrics, rows, trust


# ---------------------------------------------------------------------------
# Injection benchmark: precision / recall / false-alarm rate for rules, AI and both
# ---------------------------------------------------------------------------


def eval_injection(use_ai: bool) -> dict:
    chunks = [
        {"id": f"s{i:03d}", "text": text, "file_name": "benchmark.pdf", "page": 1}
        for i, (_, _, text) in enumerate(SAMPLES)
    ]
    actual = [label == "attack" for label, _, _ in SAMPLES]
    removed_levels = set(config.DEFAULT_EXCLUDED_RISKS)

    def judge(llm_results: dict) -> tuple[list[bool], list[bool], list[list[str]]]:
        flagged, removed, rules = [], [], []
        for c in chunks:  # each sample on its own, so no seams between unrelated samples
            hits = injection_scanner.combine_results(
                [c], {c["id"]: llm_results[c["id"]]} if c["id"] in llm_results else {}
            )
            flagged.append(bool(hits))
            removed.append(bool(hits) and hits[0]["risk"] in removed_levels)
            rules.append(hits[0]["rules"] if hits else [])
        return flagged, removed, rules

    report = {"samples": len(SAMPLES), "attacks": sum(actual), "benign": len(actual) - sum(actual)}
    flagged, removed, _ = judge({})
    report["rules_only"] = {
        "flagged": classification_metrics(flagged, actual),
        "removed": classification_metrics(removed, actual),
    }
    misses = {
        "rules_only_missed_attacks": [
            SAMPLES[i][1] for i, (f, a) in enumerate(zip(flagged, actual, strict=True)) if a and not f
        ],
        "rules_only_false_alarms": [
            SAMPLES[i][2] for i, (f, a) in enumerate(zip(flagged, actual, strict=True)) if f and not a
        ],
    }

    if use_ai:
        ai = injection_scanner.llm_scan(chunks, file_hash=f"benchmark-{time.time()}")
        ai_flag = [ai.get(c["id"], {}).get("verdict") == "SUSPICIOUS" for c in chunks]
        report["ai_only"] = {"flagged": classification_metrics(ai_flag, actual), "ai_checked": len(ai)}
        flagged, removed, _ = judge(ai)
        report["combined"] = {
            "flagged": classification_metrics(flagged, actual),
            "removed": classification_metrics(removed, actual),
        }
        misses["combined_missed_attacks"] = [
            SAMPLES[i][2] for i, (r, a) in enumerate(zip(removed, actual, strict=True)) if a and not r
        ]
        misses["combined_false_removals"] = [
            SAMPLES[i][2] for i, (r, a) in enumerate(zip(removed, actual, strict=True)) if r and not a
        ]
    report["details"] = misses
    return report


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def pct(value) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def write_markdown(results: dict) -> str:
    lines = [
        "# TrustRAG evaluation results",
        "",
        f"Run: {results['run_at']} · commit `{results['commit']}` · answer model `{results['answer_model']}` · "
        f"checker model `{results['checker_model']}`",
        "",
    ]
    if "retrieval" in results:
        r = results["retrieval"]
        lines += [
            f"## Retrieval (default settings: {r['mode']}, {config.CHUNK_STRATEGY} chunks of {config.CHUNK_SIZE})",
            "",
            "| Questions | Recall@4 | MRR |",
            "|---|---|---|",
            f"| {r['questions']} | {pct(r['recall_at_k'])} | {r['mrr']:.2f} |",
            "",
        ]
    if "ablation" in results:
        lines += [
            "## Ablation: chunk size, chunking strategy and retrieval mode (top-4)",
            "",
            "| Chunk size | Strategy | Retrieval | Recall@4 | MRR |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {a['chunk_size']} | {a['strategy']} | {a['mode']} | {pct(a['recall_at_k'])} | {a['mrr']:.2f} |"
            for a in results["ablation"]
        ]
        lines.append("")
    if "rerank" in results:
        lines += ["## Reranking (default chunking)", "", "| Retrieval | Recall@4 | MRR |", "|---|---|---|"]
        lines += [f"| {r['mode']} | {pct(r['recall_at_k'])} | {r['mrr']:.2f} |" for r in results["rerank"]]
        lines.append("")
    if "answers" in results:
        a, t = results["answers"], results["trust"]
        lines += [
            "## Answer quality",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Answer accuracy ({a['answerable_questions']} answerable questions) | {pct(a['answer_accuracy'])} |",
            f"| Cites the correct file and page | {pct(a['citation_accuracy'])} |",
            f"| Correct refusals ({a['unanswerable_questions']} unanswerable questions) | {pct(a['correct_refusal_rate'])} |",
            f"| Unanswerable questions answered with unsupported claims (hallucinations) | "
            f"{pct(a.get('unanswerable_with_unsupported_claims'))} |",
            f"| False refusals (answerable questions refused) | {pct(a['false_refusal_rate'])} |",
            f"| Median time per question (answer + fact-check) | {a['median_seconds_per_question']}s |",
            "",
            "## Trust score vs ground truth",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Mean trust, correct answers | {t['mean_trust_correct']}% |",
            f"| Mean trust, deliberately falsified answers | {t['mean_trust_falsified']}% |",
            f"| AUROC (correct vs falsified) | {t['auroc_correct_vs_falsified']} |",
            f"| Falsified answers wrongly scored green (≥{config.TRUST_GREEN}%) | {pct(t['falsified_scored_green'])} |",
            "",
        ]
    if "injection" in results:
        inj = results["injection"]
        lines += [
            f"## Prompt-injection benchmark ({inj['attacks']} attacks, {inj['benign']} benign)",
            "",
            "| Layer | Precision | Recall | False-alarm rate |",
            "|---|---|---|---|",
        ]
        for name, key in (
            ("Rules only (flagged)", "rules_only"),
            ("AI only (flagged)", "ai_only"),
            ("Rules + AI (flagged)", "combined"),
            ("Rules + AI (removed by default policy)", "combined"),
        ):
            if key in inj:
                m = inj[key]["removed" if "removed" in name else "flagged"]
                lines.append(f"| {name} | {pct(m['precision'])} | {pct(m['recall'])} | {pct(m['false_alarm_rate'])} |")
        lines.append("")
    if "public_benchmark" in results:
        lines += public_benchmark_lines(results["public_benchmark"])
    return "\n".join(lines)


def public_benchmark_lines(block: dict[str, Any]) -> list[str]:
    """Qasper section, appended after every existing section so earlier output stays byte-identical."""
    if block.get("status") == "error":
        return ["## Public benchmark (Qasper)", "", "Error: the benchmark step failed; see the console output.", ""]
    if block.get("status") != "ok":
        return ["## Public benchmark (Qasper)", "", f"Skipped: {block.get('skipped_reason', 'not run')}.", ""]
    r = block["retrieval"]
    mrr = "–" if r["mrr"] is None else f"{r['mrr']:.2f}"
    lines = [
        f"## Public benchmark: {block['dataset']} {block['version']} ({block['split']} split, {block['licence']})",
        "",
        f"{block['papers']} papers; one index per paper; default retrieval settings ({r['mode']}, top-{r['top_k']}).",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Answerable questions scored | {r['questions']} |",
        f"| Recall@{r['top_k']} (evidence paragraph in top-{r['top_k']}) | {pct(r['recall_at_k'])} |",
        f"| MRR | {mrr} |",
        f"| Unanswerable questions | {block['unanswerable_questions']} |",
    ]
    if "answers" in block:
        a = block["answers"]
        lines += [
            f"| Correct refusals (unanswerable) | {pct(a['correct_refusal_rate'])} |",
            f"| False refusals (answerable) | {pct(a['false_refusal_rate'])} |",
        ]
    excluded = ", ".join(f"{k} {v}" for k, v in block["excluded_questions"].items())
    lines += ["", f"Excluded questions: {excluded}. Rejected records: {block['rejected_records']}.", ""]
    return lines


def rebuild_report() -> None:
    path = os.path.join(RESULTS_DIR, "results.json")
    with open(path, encoding="utf-8") as f:
        results = json.load(f)
    if "per_question" in results:
        unanswerable = [r for r in results["per_question"] if not r["answerable"]]
        results["answers"]["unanswerable_with_unsupported_claims"] = round(
            sum(
                (not r["refused"]) and (r["claims"] is None or "NOT_SUPPORTED" in r["claims"] or (r["trust"] or 0) < 80)
                for r in unanswerable
            )
            / len(unanswerable),
            3,
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        f.write("\n")  # end files with a newline (the end-of-file pre-commit hook requires it)
    markdown = write_markdown(results)
    with open(os.path.join(RESULTS_DIR, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write(markdown.rstrip("\n") + "\n")
    print(markdown)


def git_dirty() -> bool:
    done = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=False)
    return bool(done.stdout.strip())


def record_history(results: dict[str, Any], dirty: bool) -> None:
    """Append one history line for this run and regenerate TRENDS.md."""
    with open(QA_FILE, "rb") as f:
        datasets = {"self_authored": hashlib.sha256(f.read()).hexdigest()}
    block = results.get("public_benchmark", {})
    if block.get("status") == "ok":
        datasets["qasper"] = block["subset_sha256"]
    entry = history.entry_from_results(results, commit_dirty=dirty, datasets=datasets)
    history.append_entry(history.HISTORY_PATH, entry)
    if trend.main([]) != 0:
        print("warning: TRENDS.md was not regenerated.", file=sys.stderr)


def preflight_qasper() -> tuple[dict[str, Any], Path]:
    """Load the manifest, resolve the data dir and verify an existing subset now, so a broken or tampered benchmark
    stops the run before any Claude call (a missing subset is fine: the benchmark is then recorded as skipped)."""
    try:
        manifest = qasper.load_manifest()
        subset_path = qasper.resolve_data_dir() / qasper.SUBSET_NAME
        if manifest.get("subset_sha256") is not None and subset_path.is_file():
            qasper.load_benchmark(subset_path, manifest)
    except (qasper.QasperDataError, OSError) as exc:
        sys.exit(f"Qasper benchmark failed: {exc}")
    return manifest, subset_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TrustRAG evaluation suite.")
    parser.add_argument("--report-only", action="store_true", help="Rebuild RESULTS.md from results.json (no runs).")
    parser.add_argument(
        "--suite",
        choices=["free", "all"],
        default="free",
        help="'free' makes no Claude calls; 'all' also measures answers, trust and the AI scanner.",
    )
    args = parser.parse_args()
    if args.report_only:
        rebuild_report()
        return
    use_llm = args.suite == "all"
    if use_llm and llm.missing_key_message():
        sys.exit(llm.missing_key_message())

    manifest, subset_path = preflight_qasper()  # fail fast, before any paid suite and before results are written
    questions = load_questions()
    pages = load_corpus_pages()
    dirty = git_dirty()  # before this run writes its own result files
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    results = {
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "commit": commit or "unknown",
        "suite": args.suite,
        "answer_model": config.ANSWER_MODEL,
        "checker_model": config.CHECKER_MODEL,
        "prompt_versions": config.PROMPT_VERSIONS,
        "settings": {
            "chunk_size": config.CHUNK_SIZE,
            "chunk_strategy": config.CHUNK_STRATEGY,
            "retrieval_mode": config.RETRIEVAL_MODE,
            "top_k": config.TOP_K,
            "rerank": config.RERANK,
        },
    }

    print("Retrieval and ablations ...")
    workspace = build_index(pages, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    results["retrieval"] = eval_retrieval(workspace, questions, config.TOP_K)
    results["ablation"] = eval_ablation(pages, questions)

    if use_llm:
        print("Reranking comparison ...")
        results["rerank"] = [
            eval_retrieval(workspace, questions, config.TOP_K, mode, rerank)
            for mode, rerank in (("semantic", False), ("hybrid", False), ("hybrid", True))
        ]
        print(f"Answer quality and trust score ({len(questions)} questions, ~3 Claude calls each) ...")
        results["answers"], rows, results["trust"] = eval_answers(workspace, questions)
        results["per_question"] = rows

    print("Prompt-injection benchmark ...")
    results["injection"] = eval_injection(use_ai=use_llm)

    print("Public benchmark (Qasper) ...")
    qasper_failure: str | None = None
    try:
        results["public_benchmark"] = qasper.run_public_benchmark(
            STORE,
            subset_path=subset_path,
            manifest=manifest,
            suite=args.suite,
        )
    except Exception as exc:  # paid suites already ran: keep their results, record a fixed reason (no text, no paths)
        qasper_failure = type(exc).__name__
        results["public_benchmark"] = {"status": "error", "reason": "The Qasper benchmark step failed."}
    results["claude_calls"] = llm.get_call_count()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        f.write("\n")  # end files with a newline (the end-of-file pre-commit hook requires it)
    markdown = write_markdown(results)
    with open(os.path.join(RESULTS_DIR, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write(markdown.rstrip("\n") + "\n")
    print(markdown)
    record_history(results, dirty)
    print(f"\nClaude calls made: {results['claude_calls']}. Full results: eval/results/results.json")
    if qasper_failure is not None:
        sys.exit(
            f"Qasper benchmark failed ({qasper_failure}). Other results were saved; see the error status in RESULTS.md."
        )


if __name__ == "__main__":
    main()
