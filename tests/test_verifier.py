"""Tests for the trust-score maths and refusal handling (no API calls)."""

from src import config
from src.verifier import compute_trust_score, verify_answer


def claims(*statuses):
    return [{"status": s} for s in statuses]


def test_all_supported_is_100():
    assert compute_trust_score(claims("SUPPORTED", "SUPPORTED")) == 100.0


def test_partial_counts_as_half():
    # (2 + 0.5) / 4 = 62.5%
    assert compute_trust_score(claims("SUPPORTED", "SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED")) == 62.5


def test_nothing_supported_is_0():
    assert compute_trust_score(claims("NOT_SUPPORTED", "NOT_SUPPORTED")) == 0.0


def test_no_claims_gives_no_score():
    assert compute_trust_score([]) is None


def test_refusal_skips_verification_without_api_call():
    result = verify_answer(config.REFUSAL_TEXT, chunks=[])
    assert result == {"refused": True, "claims": [], "trust_score": None}
