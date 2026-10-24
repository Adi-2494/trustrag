"""
Record real Claude responses for the replay integration test (tests/test_integration_replay.py).

    python -m scripts.record_cassette        # uses your configured Claude access; about 10 calls

Re-run this after changing a prompt; commit the updated tests/cassettes/pipeline.json.
"""

import os
import pathlib
import tempfile

os.environ["CHROMA_DIR"] = tempfile.mkdtemp(prefix="trustrag-record-")

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
    recorder.save(CASSETTE)
    print(f"Recorded {len(recorder.responses)} responses to {CASSETTE}")


if __name__ == "__main__":
    main()
