"""In-memory implementations of the pipeline's interfaces, injected into TrustRAGService by tests."""

import json
import re
import time
import uuid
from typing import Any

from src.models import Chunk, RetrievedChunk


class FakeStore:
    """A tiny VectorStore: ranks chunks by word overlap with the question."""

    def __init__(self) -> None:
        self.workspaces: dict[str, dict[str, Chunk]] = {}
        self.cleanup_calls: list[dict[str, frozenset[str]]] = []  # P6: what the service asked cleanup to keep / purge

    def new_workspace(self, *, persistent: bool = False) -> str:
        """Account workspaces (`persistent=True`) get the ADR 0012 id form `acct-<32 hex>`; anonymous ones `<32 hex>`."""
        workspace = ("acct-" if persistent else "") + uuid.uuid4().hex
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

    def cleanup(self, *, keep: frozenset[str] = frozenset(), purge: frozenset[str] = frozenset()) -> int:
        """Records its arguments (the 24 h age rule itself is covered with the real ChromaStore)."""
        self.cleanup_calls.append({"keep": frozenset(keep), "purge": frozenset(purge)})
        return 0


class FakeAuthenticator:
    """
    An `Authenticator` (src/interfaces.py) without passwords or SQLite: tokens are random strings kept in memory.
    `add_user` returns (principal, token). `revoke` / `expire` make the token unknown, as logout or expiry would.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, dict[str, str]] = {}
        self.resolve_calls: list[str] = []

    def add_user(self, username: str) -> tuple[dict[str, str], str]:
        principal = {"account_id": uuid.uuid4().hex, "username": username}
        return principal, self.start_session(principal["account_id"], username)["token"]

    def start_session(self, account_id: str, username: str = "fake", *, expected_hash: str | None = None) -> Any:
        token = "tok-" + uuid.uuid4().hex
        self._tokens[token] = {"account_id": account_id, "username": username}
        return {"token": token, "username": username, "expires_at": time.time() + 3600}

    def resolve(self, token: str) -> dict[str, str] | None:
        self.resolve_calls.append(token)
        found = self._tokens.get(token)
        return dict(found) if found else None

    def logout(self, token: str) -> None:
        self._tokens.pop(token, None)

    revoke = expire = logout


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
