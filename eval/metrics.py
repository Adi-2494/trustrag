"""Pure scoring functions for the evaluation suite (no I/O, no model calls), so they can be unit-tested."""

import difflib
import re


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def has_keys(answer: str, key_groups: list[list[str]]) -> bool:
    """True if, for every group, the answer contains at least one of the group's strings."""
    text = norm(answer)
    return all(any(norm(key) in text for key in group) for group in key_groups)


def cites(answer: str, file_name: str, page: int) -> bool:
    """True if the answer contains a citation like [file.pdf, p.3] for this file and page."""
    return any(
        norm(f) == norm(file_name) and int(p) == page for f, p in re.findall(r"\[([^\],]+),\s*p\.?\s*(\d+)\]", answer)
    )


def auroc(positives: list[float], negatives: list[float]) -> float | None:
    """Probability that a random positive scores higher than a random negative (0.5 = chance, 1.0 = perfect)."""
    if not positives or not negatives:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return round(wins / (len(positives) * len(negatives)), 3)


def corrupt(sentence: str) -> str | None:
    """Change every number in a true sentence, producing a plausible but false claim (None if no numbers)."""
    changed = re.sub(r"\d+", lambda m: str(int(m.group()) * 2 + 3), sentence)
    return changed if changed != sentence else None


def classification_metrics(predicted: list[bool], actual: list[bool]) -> dict:
    tp = sum(p and a for p, a in zip(predicted, actual, strict=True))
    fp = sum(p and not a for p, a in zip(predicted, actual, strict=True))
    fn = sum(a and not p for p, a in zip(predicted, actual, strict=True))
    tn = sum(not p and not a for p, a in zip(predicted, actual, strict=True))
    return {
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "false_alarm_rate": round(fp / (fp + tn), 3) if fp + tn else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def evidence_hit(chunk_text: str, paragraph: str, min_chars: int = 200) -> bool:
    """
    True if a retrieved chunk holds (part of) a gold evidence paragraph.

    After `norm()`, the chunk hits when it contains the whole paragraph, or when the longest run of text they share is
    at least min(len(paragraph), min_chars) characters. 200 is above the 150-character chunk overlap, so a chunk that
    only repeats the overlap tail of a neighbour does not count (P5, SPEC-GAP-020).
    """
    chunk, para = norm(chunk_text), norm(paragraph)
    if not para:
        return False
    if para in chunk:
        return True
    matcher = difflib.SequenceMatcher(None, chunk, para, autojunk=False)
    return matcher.find_longest_match(0, len(chunk), 0, len(para)).size >= min(len(para), min_chars)
