"""Tests for loading, chunking, refusal detection and JSON parsing (no API calls)."""

import pytest

from src import config
from src.chunker import chunk_pages
from src.generator import generate_answer, is_refusal
from src.llm import _parse_json
from src.loader import LoaderError, load_pdf

SAMPLE_PDF = "sample_docs/company_handbook.pdf"


# ---------- loader ----------


def test_loader_reads_every_page_with_metadata():
    pages = load_pdf(SAMPLE_PDF, "company_handbook.pdf")
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert all(p["file_name"] == "company_handbook.pdf" for p in pages)
    assert all(p["ocr"] is False for p in pages)  # real text PDF: OCR not needed
    assert "24 days of paid annual leave" in pages[1]["text"]


def test_loader_rejects_non_pdf(tmp_path):
    fake = tmp_path / "notes.pdf"
    fake.write_text("this is not a pdf")
    with pytest.raises(LoaderError):
        load_pdf(str(fake), "notes.pdf")


# ---------- chunker ----------


def test_chunks_respect_size_and_keep_metadata():
    long_text = " ".join(f"Sentence number {i} is here." for i in range(300))
    chunks = chunk_pages([{"text": long_text, "file_name": "a.pdf", "page": 7}])
    assert len(chunks) > 1
    assert all(len(c["text"]) <= config.CHUNK_SIZE for c in chunks)
    assert all(c["file_name"] == "a.pdf" and c["page"] == 7 for c in chunks)


def test_chunk_ids_are_unique_and_stable():
    pages = [{"text": "x " * 1000, "file_name": "a.pdf", "page": 1}]
    first, second = chunk_pages(pages), chunk_pages(pages)
    ids = [c["id"] for c in first]
    assert len(ids) == len(set(ids))
    assert ids == [c["id"] for c in second]


# ---------- generator ----------


def test_refusal_detection():
    assert is_refusal(config.REFUSAL_TEXT)
    assert is_refusal("The documents do not contain this information")
    assert not is_refusal("You get 24 days of annual leave [handbook.pdf, p.2].")


def test_no_chunks_returns_refusal_without_api_call():
    assert generate_answer("Anything?", chunks=[]) == config.REFUSAL_TEXT


# ---------- JSON parsing ----------


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here it is: {"a": 1} Hope that helps.', {"a": 1}),
        ("[1, 2, 3]", [1, 2, 3]),
        ("not json at all", None),
    ],
)
def test_parse_json_handles_messy_replies(reply, expected):
    assert _parse_json(reply) == expected
