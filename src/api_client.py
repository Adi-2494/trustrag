"""
HTTP client for the TrustRAG API with the same methods as `TrustRAGService`, so the Streamlit UI can run either
in-process or as a pure client of a separately deployed API (set TRUSTRAG_API_URL).
"""

import os
from collections.abc import Callable
from typing import Any, cast

import httpx

from src.llm import LLMError
from src.loader import LoaderError
from src.models import AskResult, UploadResult
from src.service import UploadRejected


class ApiClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 300.0) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def _check(self, response: httpx.Response) -> Any:
        if response.status_code < 400:
            return response.json() if response.content else None
        detail = response.json().get("detail", "The TrustRAG API returned an error.")
        if response.status_code == 413:
            raise UploadRejected(detail)
        if response.status_code == 422:
            raise LoaderError(detail)
        raise LLMError(str(detail))

    def new_workspace(self) -> str:
        return str(self._check(self._http.post("/v1/workspaces"))["workspace"])

    def list_files(self, workspace: str) -> list[str]:
        return cast(list[str], self._check(self._http.get(f"/v1/workspaces/{workspace}/documents"))["files"])

    def count(self, workspace: str) -> int:
        return int(self._check(self._http.get(f"/v1/workspaces/{workspace}/documents"))["chunks"])

    def clear(self, workspace: str) -> None:
        self._check(self._http.delete(f"/v1/workspaces/{workspace}"))

    def upload(
        self,
        workspace: str,
        file_name: str,
        data: bytes,
        excluded_risks: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> UploadResult:
        response = self._http.post(
            f"/v1/workspaces/{workspace}/documents",
            files={"file": (file_name, data, "application/pdf")},
            data={"also_remove": ",".join(excluded_risks or [])},
        )
        return cast(UploadResult, self._check(response))

    def ask(self, workspace: str, question: str) -> AskResult:
        return cast(
            AskResult,
            self._check(self._http.post(f"/v1/workspaces/{workspace}/questions", json={"question": question})),
        )


def backend_from_environment() -> Any:
    """The API client when TRUSTRAG_API_URL is set, otherwise the in-process service."""
    url = os.getenv("TRUSTRAG_API_URL", "")
    if url:
        return ApiClient(url, os.getenv("API_KEY", ""))
    from src.service import TrustRAGService

    return TrustRAGService()
