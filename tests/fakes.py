"""In-memory implementations of the pipeline's interfaces, injected into TrustRAGService by tests."""

import json
import re
import uuid
from typing import Any

from src.models import Chunk, RetrievedChunk


class FakeStore:
    """A tiny VectorStore: ranks chunks by word overlap with the question."""

    def __init__(self) -> None:
        self.workspaces: dict[str, dict[str, Chunk]] = {}

    def new_workspace(self) -> str:
        workspace = uuid.uuid4().hex
        self.workspaces[workspace] = {}
        return workspace

    def _ws(self, workspace: str) -> dict[str, Chunk]:
        if workspace not in self.workspaces:
            raise ValueError("unknown workspace")
        return self.workspaces[workspace]

    def add_chunks(self, workspace: str, chunks: list[Chunk]) -> None:
        self._ws(workspace).update({c["id"]: c for c in chunks})

    def retrieve(self, workspace: str, question: str, top_k: int) -> list[RetrievedChunk]:
        words = set(re.findall(r"\w+", question.lower()))
        scored = sorted(
            self._ws(workspace).values(), key=lambda c: -len(words & set(re.findall(r"\w+", c["text"].lower())))
        )
        return [{**c, "distance": 0.5} for c in scored[:top_k]]  # type: ignore[typeddict-item]

    def all_chunks(self, workspace: str) -> list[Chunk]:
        return list(self._ws(workspace).values())

    def delete_file(self, workspace: str, file_name: str) -> None:
        ws = self._ws(workspace)
        for cid in [cid for cid, c in ws.items() if c["file_name"] == file_name]:
            del ws[cid]

    def list_files(self, workspace: str) -> list[str]:
        return sorted({c["file_name"] for c in self._ws(workspace).values()})

    def count(self, workspace: str) -> int:
        return len(self._ws(workspace))

    def clear(self, workspace: str) -> None:
        self.workspaces[workspace] = {}

    def cleanup(self) -> int:
        return 0


class FakeLLM:
    """Answers by purpose; records every call so tests can assert on what was asked."""

    def __init__(
        self,
        answer: str = "Employees receive 25 days of paid annual leave [handbook.pdf, p.1].",
        quote: str = "25 days of paid annual leave",
        suspicious_text: str | None = None,
    ) -> None:
        self.answer, self.quote, self.suspicious_text = answer, quote, suspicious_text
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        model: str,
        system: str,
        prompt: Any,
        max_tokens: int,
        effort: str | None = None,
        *,
        purpose: str = "other",
    ) -> str:
        self.calls.append({"purpose": purpose, "prompt": prompt})
        if purpose == "scan":
            chunks = re.findall(r'<chunk-[0-9a-f]+ id="(\d+)">\n(.*?)\n</chunk-', prompt, re.S)
            return json.dumps(
                {
                    "results": [
                        {
                            "id": int(i),
                            "verdict": "SUSPICIOUS"
                            if self.suspicious_text and self.suspicious_text in text
                            else "SAFE",
                            "risk": "HIGH" if self.suspicious_text and self.suspicious_text in text else "LOW",
                            "reason": "test",
                        }
                        for i, text in chunks
                    ]
                }
            )
        if purpose == "answer":
            return self.answer
        if purpose == "verify":
            return json.dumps(
                {
                    "claims": [
                        {
                            "claim": "Employees receive 25 days of paid annual leave",
                            "status": "SUPPORTED",
                            "quote": self.quote,
                            "file_name": "handbook.pdf",
                            "page": 1,
                            "reason": "",
                        }
                    ]
                }
            )
        if purpose == "rerank":
            return json.dumps({"ranking": []})
        return ""
