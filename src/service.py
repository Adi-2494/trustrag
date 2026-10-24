"""
The TrustRAG pipeline as one service, used by both the Streamlit UI and the HTTP API.

    upload: sanitise name -> load (+OCR) -> chunk -> injection scan -> filter -> index (private workspace)
    ask:    retrieve -> generate grounded answer -> fact-check -> trust score

Dependencies (vector store, LLM client) are injected, so the same code runs against ChromaDB + Claude in
production and against in-memory fakes or recorded responses in tests.
"""

import hashlib
import time
from collections.abc import Callable

from src import config, retrieval
from src.chunker import chunk_pages
from src.generator import generate_answer, is_refusal
from src.injection_scanner import filter_chunks, scan_chunks
from src.interfaces import VectorStore
from src.llm import LLMClient, LLMError, call_claude
from src.loader import load_pdf
from src.models import AskResult, UploadResult
from src.observability import METRICS, event, new_request_id, stage
from src.safety import sanitize_filename
from src.verifier import verify_answer


class UploadRejected(Exception):
    """The upload breaks a limit (size, count); the message is safe to show to users."""


class TrustRAGService:
    def __init__(self, store: VectorStore | None = None, llm: LLMClient | None = None) -> None:
        if store is None:
            from src.vectorstore import ChromaStore  # imported lazily: ChromaDB is heavy and fakes don't need it

            store = ChromaStore()
        self.store = store
        self.llm: LLMClient = llm or call_claude

    # ------------------------------------------------------------------ workspaces
    def new_workspace(self) -> str:
        self.store.cleanup()  # drop abandoned workspaces whenever a new one starts
        return self.store.new_workspace()

    def list_files(self, workspace: str) -> list[str]:
        return self.store.list_files(workspace)

    def count(self, workspace: str) -> int:
        return self.store.count(workspace)

    def clear(self, workspace: str) -> None:
        self.store.clear(workspace)

    # ------------------------------------------------------------------ upload
    def upload(
        self,
        workspace: str,
        file_name: str,
        data: bytes,
        excluded_risks: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        request_id: str | None = None,
    ) -> UploadResult:
        """Read, scan and index one PDF. Raises LoaderError / LLMError / UploadRejected with user-safe messages."""
        new_request_id(request_id)
        name = sanitize_filename(file_name)
        excluded = sorted(set(config.DEFAULT_EXCLUDED_RISKS) | set(excluded_risks or []))
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise UploadRejected(f"The file is larger than {config.MAX_UPLOAD_MB} MB.")
        with stage("upload", bytes=len(data)) as result:
            pages = load_pdf(data, name, self.llm)
            chunks = chunk_pages(pages)
            report = scan_chunks(chunks, hashlib.sha256(data).hexdigest(), on_progress, self.llm)
            report["excluded_risks"] = excluded
            safe = filter_chunks(chunks, report, excluded)
            self.store.delete_file(workspace, name)  # replace an older version of this file (this workspace only)
            self.store.add_chunks(workspace, safe)
            result.update(pages=len(pages), chunks=len(chunks), removed=len(chunks) - len(safe))
        METRICS.inc("uploads_total")
        METRICS.inc("chunks_removed_total", len(chunks) - len(safe))
        return {
            "file_name": name,
            "pages": len(pages),
            "chunks_indexed": len(safe),
            "chunks_removed": len(chunks) - len(safe),
            "report": report,
        }

    # ------------------------------------------------------------------ ask
    def ask(self, workspace: str, question: str, request_id: str | None = None) -> AskResult:
        """Answer from the workspace's documents and fact-check the answer. Raises LLMError if answering fails."""
        request_id = new_request_id(request_id)
        started = time.time()
        with stage("ask", question_chars=len(question)) as result:
            chunks = retrieval.retrieve(self.store, workspace, question, client=self.llm)
            answer = generate_answer(question, chunks, self.llm)
            try:
                verification = verify_answer(answer, chunks, self.llm)
            except LLMError:
                verification = None  # the fact-check is a safety net: still return the answer, flagged as unchecked
                event("verify.skipped")
            result.update(trust_score=verification["trust_score"] if verification else None)
        METRICS.inc("questions_total")
        return {
            "question": question,
            "answer": answer,
            "refused": is_refusal(answer),
            "chunks": chunks,
            "verification": verification,
            "seconds": round(time.time() - started, 1),
            "request_id": request_id,
        }
