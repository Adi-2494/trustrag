"""Tests for the evaluation suite's scoring functions and ground truth (no model calls)."""

from eval.corpus import DOCS, FACTS, page_text
from eval.injection_data import SAMPLES
from eval.metrics import auroc, cites, classification_metrics, corrupt, has_keys


def test_has_keys_requires_every_group():
    keys = [["10:00", "10 am"], ["16:00", "4 pm"]]
    assert has_keys("Core hours are 10 am to 4 pm.", keys)
    assert not has_keys("Core hours start at 10:00.", keys)


def test_cites_matches_file_and_page():
    answer = "Leave is 25 days [hr_handbook.pdf, p.3] and carry-over expires [hr_handbook.pdf, p. 3]."
    assert cites(answer, "hr_handbook.pdf", 3)
    assert not cites(answer, "hr_handbook.pdf", 4)
    assert not cites("No citation here.", "hr_handbook.pdf", 3)


def test_auroc():
    assert auroc([90, 100], [10, 20]) == 1.0
    assert auroc([10, 20], [90, 100]) == 0.0
    assert auroc([50], [50]) == 0.5
    assert auroc([], [1]) is None


def test_corrupt_changes_every_number():
    assert (
        corrupt("Employees receive 25 days of leave and 12 sick days.")
        == "Employees receive 53 days of leave and 27 sick days."
    )
    assert corrupt("Alcohol is not reimbursed.") is None


def test_classification_metrics():
    m = classification_metrics([True, True, False, False], [True, False, True, False])
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (1, 1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["false_alarm_rate"] == 0.5


def test_ground_truth_evidence_is_on_the_stated_page():
    for question, file_name, page, evidence, _ in FACTS:
        assert evidence in page_text(file_name, page), question


def test_benchmark_is_balanced_and_labelled():
    labels = [label for label, _, _ in SAMPLES]
    assert set(labels) == {"attack", "benign"}
    assert abs(labels.count("attack") - labels.count("benign")) <= 5
    assert len(DOCS) >= 5 and len(FACTS) >= 50
