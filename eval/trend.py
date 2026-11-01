"""Render eval/results/TRENDS.md from the run history (ADR 0010).

Why static and deterministic: the report is committed, so the same history must give the same bytes (no generation
time). Only numbers, validated commit hashes, the suite enum and ISO timestamps are written (load_history enforces it).
A malformed history line makes the command fail without writing anything.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from eval.history import HISTORY_PATH, METRIC_NAMES, load_history

TRENDS_PATH = Path(__file__).parent / "results" / "TRENDS.md"


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def render_trends(entries: list[dict[str, Any]], last: int = 10) -> str:
    lines = [
        "# Evaluation trends",
        "",
        f"Last {last} runs per metric, newest first. `*` marks a run on uncommitted code.",
        "",
    ]
    for name in METRIC_NAMES:
        series = [e for e in entries if isinstance(e["metrics"].get(name), int | float)]
        if not series:
            continue
        lines += [f"## {name}", "", "| Run | Commit | Suite | Value | Change |", "| --- | --- | --- | --- | --- |"]
        first = max(len(series) - last, 0)
        for i in range(len(series) - 1, first - 1, -1):
            e, value = series[i], series[i]["metrics"][name]
            change = "-"
            if i > 0:
                prev = series[i - 1]
                change = f"{value - prev['metrics'][name]:+.3f}"
                key = "qasper" if name.startswith("qasper.") else "self_authored"
                before, after = (prev.get("datasets") or {}).get(key), (e.get("datasets") or {}).get(key)
                if before is not None and before != after:
                    change += " (dataset changed)"
            commit = e["commit"] + ("*" if e["commit_dirty"] else "")
            lines.append(f"| {e['run_at']} | {commit} | {e['suite']} | {_fmt(value)} | {change} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the evaluation trend report from history.jsonl")
    parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    parser.add_argument("--out", type=Path, default=TRENDS_PATH)
    parser.add_argument("--last", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        markdown = render_trends(load_history(args.history), last=args.last)
    except (OSError, ValueError) as exc:
        print(f"eval.trend: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(markdown, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
