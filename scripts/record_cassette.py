"""
Record real Claude responses for the replay integration test (tests/test_integration_replay.py).

    python -m scripts.record_cassette        # uses your configured Claude access; about 10 calls

Re-run this after changing a prompt; commit the updated tests/cassettes/pipeline.json.
"""

import hashlib
import os
import pathlib
import tempfile

os.environ["CHROMA_DIR"] = tempfile.mkdtemp(prefix="trustrag-record-")

from src import config, state
from src.llm import call_claude
from src.service import TrustRAGService
from tests.recording import RecordingLLM
from tests.test_integration_replay import CASSETTE, DOCUMENTS, QUESTIONS


def main() -> None:
    recorder = RecordingLLM(call_claude)
    service = TrustRAGService(llm=recorder)
    workspace = service.new_workspace()
    for name in DOCUMENTS:
        result = service.upload(workspace, name, pathlib.Path("sample_docs", name).read_bytes())
        print(f"uploaded {name}: {result['chunks_indexed']} indexed, {result['chunks_removed']} removed")
    for question in QUESTIONS:
        answer = service.ask(workspace, question)
        print(
            f"Q: {question}\nA: {answer['answer'][:100]}  (trust: {answer['verification'] and answer['verification']['trust_score']})"
        )
    # OCR results, keyed exactly as the app's OCR cache keys them (prompt version : file hash : page).
    ocr_keys = []
    for name in DOCUMENTS:
        file_hash = hashlib.sha256(pathlib.Path("sample_docs", name).read_bytes()).hexdigest()
        ocr_keys += [f"{config.PROMPT_VERSIONS['ocr']}:{file_hash}:{page}" for page in range(1, 20)]
    recorder.save(CASSETTE, state.cache_get_many("ocr", ocr_keys))
    print(f"Recorded {len(recorder.responses)} responses to {CASSETTE}")


if __name__ == "__main__":
    main()
