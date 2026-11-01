"""Append-only run history (ADR 0010): one strict-ASCII JSON line per eval run in eval/results/history.jsonl.

Why a separate file: results.json is overwritten by every run (SPEC-GAP-028), so trends need their own durable log.
Why validation on load: the trend report is rendered into Markdown, so every field that reaches it must be a
number, a validated hash, an enum value or an ISO timestamp -- never free text that could carry markup.
"""

import json
import math
import re
from pathlib import Path
from typing import Any

SCHEMA = 1
HISTORY_PATH = Path(__file__).parent / "results" / "history.jsonl"
SUITES = ("free", "all")
_COMMIT = re.compile(r"[0-9a-f]{4,40}")
_RUN_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

_PRF = ("precision", "recall", "false_alarm_rate")
_ANSWERS = (
    "answer_accuracy",
    "citation_accuracy",
    "correct_refusal_rate",
    "false_refusal_rate",
    "unanswerable_with_unsupported_claims",
)
_TRUST = ("auroc_correct_vs_falsified", "mean_trust_correct", "mean_trust_falsified")

# Fixed metric order (plan 2.6, SPEC-GAP-025); ablation rows are deliberately not tracked.
METRIC_NAMES: tuple[str, ...] = (
    "retrieval.recall_at_k",
    "retrieval.mrr",
    *(f"injection.rules_only.removed.{n}" for n in _PRF),
    *(f"injection.combined.removed.{n}" for n in _PRF),
    *(f"answers.{n}" for n in _ANSWERS),
    *(f"trust.{n}" for n in _TRUST),
    "qasper.retrieval.recall_at_k",
    "qasper.retrieval.mrr",
    "qasper.answers.correct_refusal_rate",
    "qasper.answers.false_refusal_rate",
)


def _dig(data: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _collect_metrics(results: dict[str, Any]) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    bench = results.get("public_benchmark")
    for name in METRIC_NAMES:
        path = name.split(".")
        if path[0] == "qasper":
            if not isinstance(bench, dict) or bench.get("status") != "ok":
                continue
            value = _dig(bench, path[1:])
        else:
            value = _dig(results, path)
        if _is_number(value):
            metrics[name] = round(value, 6)  # drop float noise; run_eval already rounds to 3 places
    return metrics


def entry_from_results(
    results: dict[str, Any],
    *,
    commit_dirty: bool = False,
    datasets: dict[str, str] | None = None,
    source: str = "run",
) -> dict[str, Any]:
    """Build one history record from a results.json dict."""
    return {
        "schema": SCHEMA,
        "run_at": results["run_at"],
        "commit": results["commit"],
        "commit_dirty": bool(commit_dirty),
        "suite": results["suite"],
        "source": source,
        "answer_model": results["answer_model"],
        "checker_model": results["checker_model"],
        "prompt_versions": dict(results.get("prompt_versions", {})),
        "settings": dict(results.get("settings", {})),
        "datasets": dict(datasets or {}),
        "metrics": _collect_metrics(results),
    }


def validate_entry(entry: Any) -> None:
    """Raise ValueError unless the entry is safe to write and to render."""
    if not isinstance(entry, dict):
        raise ValueError("history entry is not an object")
    if entry.get("schema") != SCHEMA:
        raise ValueError(f"unsupported history schema: {entry.get('schema')!r}")
    commit = entry.get("commit")
    if commit != "unknown" and not (isinstance(commit, str) and _COMMIT.fullmatch(commit)):
        raise ValueError("invalid commit")
    if entry.get("suite") not in SUITES:
        raise ValueError("invalid suite")
    run_at = entry.get("run_at")
    if not (isinstance(run_at, str) and _RUN_AT.fullmatch(run_at)):
        raise ValueError("invalid run_at")
    if not isinstance(entry.get("commit_dirty"), bool):
        raise ValueError("invalid commit_dirty")
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("metrics is not an object")
    for name, value in metrics.items():
        if not isinstance(name, str) or not (value is None or _is_number(value)):
            raise ValueError(f"metric {name!r} is not a finite number or null")


def append_entry(path: Path, entry: dict[str, Any]) -> None:
    """Validate, then append one line (append mode: earlier bytes are never rewritten)."""
    validate_entry(entry)
    line = json.dumps(entry, ensure_ascii=True, allow_nan=False, sort_keys=True)
    with Path(path).open("a", encoding="ascii", newline="\n") as f:
        f.write(line + "\n")


def load_history(path: Path) -> list[dict[str, Any]]:
    """Read every line; any malformed line raises ValueError naming the line number."""
    entries: list[dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            entry = json.loads(line, parse_constant=_reject_constant)
            validate_entry(entry)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{path}: line {number}: {exc}") from exc
        entries.append(entry)
    return entries


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def backfill(results_path: Path, history_path: Path = HISTORY_PATH) -> None:
    """Write the first entry from a committed results.json (SPEC-GAP-024). Refuses to touch a non-empty history."""
    if Path(history_path).exists() and Path(history_path).stat().st_size:
        raise ValueError("history already exists; backfill only creates the first entry")
    results = json.loads(Path(results_path).read_text(encoding="utf-8"))
    append_entry(history_path, entry_from_results(results, source="backfill:results.json"))
