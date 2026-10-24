"""
End-to-end integration test with REAL components (PDF parsing, OCR path, chunking, ChromaDB, hybrid search,
scanner, generator, verifier) and RECORDED Claude responses, so it is free and deterministic.
Re-record after changing prompts: python -m scripts.record_cassette
"""

import os
import pathlib

import pytest

from src import config, state, vectorstore
from src.service import TrustRAGService
from tests.recording import ReplayLLM

CASSETTE = pathlib.Path(__file__).parent / "cassettes" / "pipeline.json"
DOCUMENTS = ["company_handbook.pdf", "scanned_leave_policy.pdf", "injection_demo.pdf"]
QUESTIONS = [
    "How many days of sick leave do I get?",
    "Can I fly business class?",
    "What is the company's stock price?",
]
MODEL_CACHED = os.path.isdir(os.path.expanduser("~/.cache/chroma/onnx_models/all-MiniLM-L6-v2"))

pytestmark = pytest.mark.skipif(
    not (MODEL_CACHED and CASSETTE.exists()), reason="needs the local embedding model and a recorded cassette"
)


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(vectorstore, "_collections", {})
    replay = ReplayLLM(CASSETTE)
    state.cache_set_many("ocr", replay.ocr_cache)  # recorded OCR results, independent of image bytes
    return TrustRAGService(llm=replay), replay


def test_full_pipeline_replays_recorded_claude_responses(service):
    svc, replay = service
    ws = svc.new_workspace()
    uploads = {name: svc.upload(ws, name, pathlib.Path("sample_docs", name).read_bytes()) for name in DOCUMENTS}

    assert uploads["scanned_leave_policy.pdf"]["pages"] == 2  # read through the OCR path
    assert uploads["injection_demo.pdf"]["chunks_removed"] == 2  # both hidden attacks removed
    assert uploads["company_handbook.pdf"]["chunks_removed"] == 0  # no false alarms

    sick = svc.ask(ws, QUESTIONS[0])
    assert "10 days" in sick["answer"] and sick["verification"]["trust_score"] >= 80

    business = svc.ask(ws, QUESTIONS[1])
    assert "director" in business["answer"].lower()
    assert "every flight" not in business["answer"].lower()  # the planted lie did not get through

    stock = svc.ask(ws, QUESTIONS[2])
    assert stock["refused"] and stock["verification"]["refused"]

    assert replay.used == set(replay.responses)  # every recorded call was exercised
