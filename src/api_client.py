"""
HTTP client for the TrustRAG API with the same methods as `TrustRAGService`, so the Streamlit UI can run either
in-process or as a pure client of a separately deployed API (set TRUSTRAG_API_URL).
"""

import os
from collections.abc import Callable
from typing import Any, cast

import httpx

from src.accounts import LoginFailed, LoginRateLimited
from src.llm import LLMError
from src.loader import LoaderError
from src.models import AskResult, SessionInfo, UploadResult, WorkspaceList
from src.service import NotAuthenticated, UploadRejected, WorkspaceLimitReached, WorkspaceNotFound


class ApiClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 300.0) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    @staticmethod
    def _headers(session: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {session}"} if session else {}

    def _check(self, response: httpx.Response, *, login: bool = False, create: bool = False) -> Any:
        if response.status_code < 400:
            return response.json() if response.content else None
        try:
            detail = str(response.json().get("detail", "The TrustRAG API returned an error."))
        except (ValueError, AttributeError):  # non-JSON or non-object error body (e.g. from a proxy)
            detail = "The TrustRAG API returned an error."
        status = response.status_code
        if status == 401 and login:
            raise LoginFailed()
        if status == 401:
            raise NotAuthenticated(detail)
        if status == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", "60"))
            except ValueError:
                retry_after = 60.0
            raise LoginRateLimited(max(1.0, retry_after))
        if status == 409 and create:
            raise WorkspaceLimitReached(self._limit_from(detail))
        if status == 404 and detail == "Workspace not found.":
            raise WorkspaceNotFound(detail)
        if status == 413:
            raise UploadRejected(detail)
        if status == 422:
            raise LoaderError(detail)
        raise LLMError(detail)

    @staticmethod
    def _limit_from(detail: str) -> int:
        digits = "".join(c for c in detail.split("maximum of", 1)[-1].split("workspaces", 1)[0] if c.isdigit())
        return int(digits) if digits else 0

    def login(self, username: str, password: str, *, client_key: str | None = None) -> SessionInfo:
        # client_key is accepted for signature parity with the service; the API derives it from the connection
        response = self._http.post("/v1/auth/login", json={"username": username, "password": password})
        return cast(SessionInfo, self._check(response, login=True))

    def logout(self, session: str) -> None:
        self._check(self._http.post("/v1/auth/logout", headers=self._headers(session)))

    def list_workspaces(self, *, session: str | None) -> WorkspaceList:
        return cast(WorkspaceList, self._check(self._http.get("/v1/workspaces", headers=self._headers(session))))

    def new_workspace(self, *, session: str | None = None) -> str:
        response = self._http.post("/v1/workspaces", headers=self._headers(session))
        return str(self._check(response, create=True)["workspace"])

    def list_files(self, workspace: str, *, session: str | None = None) -> list[str]:
        response = self._http.get(f"/v1/workspaces/{workspace}/documents", headers=self._headers(session))
        return cast(list[str], self._check(response)["files"])

    def count(self, workspace: str, *, session: str | None = None) -> int:
        response = self._http.get(f"/v1/workspaces/{workspace}/documents", headers=self._headers(session))
        return int(self._check(response)["chunks"])

    def clear(self, workspace: str, *, session: str | None = None) -> None:
        self._check(self._http.delete(f"/v1/workspaces/{workspace}", headers=self._headers(session)))

    def clear_documents(self, workspace: str, *, session: str | None = None) -> None:
        path = f"/v1/workspaces/{workspace}" + ("/documents" if session is not None else "")  # no session: v1.0 route
        self._check(self._http.delete(path, headers=self._headers(session)))

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
        response = self._http.post(
            f"/v1/workspaces/{workspace}/documents",
            files={"file": (file_name, data, "application/pdf")},
            data={"also_remove": ",".join(excluded_risks or [])},
            headers=self._headers(session),
        )
        return cast(UploadResult, self._check(response))

    def ask(
        self, workspace: str, question: str, request_id: str | None = None, *, session: str | None = None
    ) -> AskResult:
        response = self._http.post(
            f"/v1/workspaces/{workspace}/questions", json={"question": question}, headers=self._headers(session)
        )
        return cast(AskResult, self._check(response))


def backend_from_environment() -> Any:
    """The API client when TRUSTRAG_API_URL is set, otherwise the in-process service."""
    url = os.getenv("TRUSTRAG_API_URL", "")
    if url:
        return ApiClient(url, os.getenv("API_KEY", ""))
    from src.service import TrustRAGService

    return TrustRAGService()
