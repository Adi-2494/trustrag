"""
The TrustRAG pipeline as one service, used by both the Streamlit UI and the HTTP API.

    upload: sanitise name -> load (+OCR) -> chunk -> injection scan -> filter -> index (private workspace)
    ask:    retrieve -> generate grounded answer -> fact-check -> trust score

Dependencies (vector store, LLM client) are injected, so the same code runs against ChromaDB + Claude in
production and against in-memory fakes or recorded responses in tests.

Accounts (ADR 0011 / 0012): every workspace method takes an optional bearer `session`. `_authorize` is the first
thing each one does, so a caller who may not touch a workspace cannot cost an LLM call, an OCR run or a retrieval.
The ownership check is always on (the ACCOUNTS_ENABLED flag only gates sign-in in the API): with no account rows an
`acct-` id simply has no owner and is refused. An invalid session is an error, never a silent fallback to anonymous.
"""

import hashlib
import json
import time
from collections.abc import Callable
from typing import Protocol

from src import config, retrieval, state
from src.chunker import chunk_pages
from src.generator import generate_answer, is_refusal
from src.injection_scanner import filter_chunks, scan_chunks
from src.interfaces import Authenticator, VectorStore
from src.llm import LLMClient, LLMError, call_claude
from src.loader import load_pdf
from src.models import AskResult, Principal, SessionInfo, UploadResult, WorkspaceList
from src.observability import METRICS, event, new_request_id, stage
from src.safety import sanitize_filename
from src.verifier import verify_answer


class UploadRejected(Exception):
    """The upload breaks a limit (size, count); the message is safe to show to users."""


class WorkspaceNotFound(ValueError):
    """Unknown workspace, or one the caller may not use (deliberately the same error: no enumeration)."""


class NotAuthenticated(Exception):
    """The session token is missing where required, invalid, expired or revoked."""


class WorkspaceLimitReached(Exception):
    """The account already owns the maximum number of live workspaces."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"You already have the maximum of {limit} workspaces. Delete one first.")
        self.limit = limit


class LoginProvider(Protocol):
    """What the service needs from a login mechanism (`PasswordLogin` today, SSO in P8)."""

    def login(self, raw_body: bytes, client_key: str | None) -> SessionInfo: ...


class TrustRAGService:
    def __init__(
        self,
        store: VectorStore | None = None,
        llm: LLMClient | None = None,
        authenticator: Authenticator | None = None,
        password_login: LoginProvider | None = None,
    ) -> None:
        if store is None:
            from src import storage  # imported lazily: ChromaDB / psycopg are heavy and fakes don't need them

            store = storage.make_vector_store()
        self.store = store
        self.llm: LLMClient = llm or call_claude
        self._authenticator = authenticator
        self._password_login = password_login

    # ------------------------------------------------------------------ accounts
    @property
    def authenticator(self) -> Authenticator:
        if self._authenticator is None:
            # lazy: SQLite-backed sessions are not needed by anonymous use
            from src.accounts import SessionAuthenticator

            self._authenticator = SessionAuthenticator()
        return self._authenticator

    @property
    def password_login(self) -> LoginProvider:
        if self._password_login is None:
            from src.accounts import PasswordLogin

            # _authenticator None: PasswordLogin builds its own SessionAuthenticator
            self._password_login = PasswordLogin(self._authenticator)
        return self._password_login

    def _principal(self, session: str | None) -> Principal | None:
        """None for an anonymous caller (no token); NotAuthenticated for any token that does not resolve."""
        if session is None:
            return None
        principal = self.authenticator.resolve(session) if isinstance(session, str) else None
        if principal is None:
            raise NotAuthenticated("Your session has expired. Please sign in again.")
        return principal

    def _authorize(self, workspace: str, session: str | None) -> Principal | None:
        """
        The single authorisation rule (plan 2.6). Raises NotAuthenticated, then WorkspaceNotFound; returns the caller.

        Token first (so the answer does not depend on the id), then ownership. An id with an ownership row is
        usable only by its live owner; `acct-` ids without a live row fail closed; a signed-in caller never reaches
        an unowned plain id; only an anonymous caller reaches a plain id with no row (the v1.0 path).
        """
        principal = self._principal(session)
        record = state.workspace_record(workspace) if isinstance(workspace, str) else None
        if record is not None:
            owner, deleted = record
            if not deleted and principal is not None and principal["account_id"] == owner:
                return principal
        elif principal is None and isinstance(workspace, str) and not workspace.startswith("acct-"):
            return None
        raise WorkspaceNotFound("Workspace not found.")

    # ------------------------------------------------------------------ workspaces
    def new_workspace(self, *, session: str | None = None) -> str:
        principal = self._principal(session)
        # drop abandoned anonymous workspaces and purge deleted account ones, whatever the ACCOUNTS_ENABLED flag
        if getattr(self.store, "resolves_cleanup_ids", False):
            self.store.cleanup()  # server store: throttled, resolves live / deleted ids itself in SQL
        else:
            self.store.cleanup(keep=frozenset(state.live_owned_ids()), purge=frozenset(state.tombstoned_ids()))
        if principal is None:
            return self.store.new_workspace(persistent=False)
        workspace = self.store.new_workspace(persistent=True)
        limit = config.MAX_WORKSPACES_PER_ACCOUNT
        if not state.add_owned_workspace(workspace, principal["account_id"], limit):
            self.store.clear(workspace)
            raise WorkspaceLimitReached(limit)
        event("workspace.created", account_id=principal["account_id"])
        return workspace

    def list_workspaces(self, *, session: str | None) -> WorkspaceList:
        principal = self._principal(session)
        if principal is None:
            raise NotAuthenticated("Please sign in.")
        rows = state.list_owned_workspace_info(principal["account_id"])
        return {"workspaces": [{"workspace": w, "created_at": c} for w, c in rows]}

    def list_files(self, workspace: str, *, session: str | None = None) -> list[str]:
        self._authorize(workspace, session)
        return self.store.list_files(workspace)

    def count(self, workspace: str, *, session: str | None = None) -> int:
        self._authorize(workspace, session)
        return self.store.count(workspace)

    def clear_documents(self, workspace: str, *, session: str | None = None) -> None:
        """Remove every document but keep the workspace (and its owner): the UI's "Clear all documents"."""
        self._authorize(workspace, session)
        self.store.clear(workspace)

    def clear(self, workspace: str, *, session: str | None = None) -> None:
        """Delete the workspace: an owner's one is tombstoned (its id is never reused), an anonymous one is emptied."""
        principal = self._authorize(workspace, session)
        if principal is not None:
            state.tombstone_workspace(workspace, principal["account_id"])  # first, so a racing upload sees it
            event("workspace.deleted", account_id=principal["account_id"])
        self.store.clear(workspace)

    def login_raw(self, raw_body: bytes, client_key: str | None = None) -> SessionInfo:
        """Sign in from a raw request body (the API reads it by hand). Raises LoginFailed / LoginRateLimited."""
        return self.password_login.login(raw_body, client_key)

    def login(self, username: str, password: str, *, client_key: str | None = None) -> SessionInfo:
        body = json.dumps({"username": username, "password": password}).encode()
        return self.login_raw(body, client_key)

    def logout(self, session: str) -> None:
        principal = self.authenticator.resolve(session)  # before the delete; unknown tokens log nothing
        self.authenticator.logout(session)
        if principal is not None:
            event("auth.logout", account_id=principal["account_id"])

    # ------------------------------------------------------------------ upload
    def upload(
        self,
        workspace: str,
        file_name: str,
        data: bytes,
        excluded_risks: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        request_id: str | None = None,
        *,
        session: str | None = None,
    ) -> UploadResult:
        """Read, scan and index one PDF. Raises LoaderError / LLMError / UploadRejected with user-safe messages."""
        self._authorize(workspace, session)  # before the size check, loader, OCR and scanner: no work for strangers
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
            try:
                self._authorize(workspace, session)  # the owner may have deleted the workspace meanwhile
            except WorkspaceNotFound:
                self.store.clear(workspace)
                raise
            except NotAuthenticated:  # the session ended mid-upload: drop only this upload's file, keep the rest
                self.store.delete_file(workspace, name)
                raise
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
    def ask(
        self, workspace: str, question: str, request_id: str | None = None, *, session: str | None = None
    ) -> AskResult:
        """Answer from the workspace's documents and fact-check the answer. Raises LLMError if answering fails."""
        self._authorize(workspace, session)  # before retrieval
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
