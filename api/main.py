"""
TrustRAG HTTP API - a thin layer over `TrustRAGService`.

    uvicorn api.main:app --host 127.0.0.1 --port 8000

Workspace ids are random 128-bit values that act as capabilities: only someone who holds the id can read or
change that workspace. Set API_KEY to require an X-API-Key header on every call (do this before exposing
the API beyond localhost).
"""

import hmac
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from src import config
from src.llm import LLMError
from src.loader import LoaderError
from src.models import AskResult, UploadResult
from src.observability import METRICS, event, request_id_var, setup_logging
from src.service import TrustRAGService, UploadRejected

setup_logging(config.LOG_LEVEL)
app = FastAPI(
    title="TrustRAG API",
    version="1.0.0",
    description="Hallucination-aware document Q&A with fact-checking and prompt-injection scanning.",
)
service = TrustRAGService()


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if config.API_KEY and not hmac.compare_digest((x_api_key or "").encode(), config.API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


Auth = Depends(require_api_key)


@app.middleware("http")
async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request_id_var.set(request_id[:64])
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id_var.get()
    route = request.scope.get("route")
    METRICS.inc("http_requests_total", path=getattr(route, "path", "unmatched"), status=str(response.status_code))
    return response


@app.exception_handler(ValueError)
async def invalid_workspace(_: Request, error: ValueError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Workspace not found."})


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class WorkspaceCreated(BaseModel):
    workspace: str


class DocumentList(BaseModel):
    files: list[str]
    chunks: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", dependencies=[Auth], response_class=PlainTextResponse)
def metrics() -> str:
    return METRICS.render_prometheus()


@app.post("/v1/workspaces", dependencies=[Auth], status_code=201)
def create_workspace() -> WorkspaceCreated:
    return WorkspaceCreated(workspace=service.new_workspace())


@app.get("/v1/workspaces/{workspace}/documents", dependencies=[Auth])
def list_documents(workspace: str) -> DocumentList:
    return DocumentList(files=service.list_files(workspace), chunks=service.count(workspace))


@app.post("/v1/workspaces/{workspace}/documents", dependencies=[Auth], status_code=201)
async def upload_document(
    workspace: str, file: Annotated[UploadFile, File()], also_remove: Annotated[str, Form()] = ""
) -> UploadResult:
    data = await file.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    extra = [r.strip().upper() for r in also_remove.split(",") if r.strip().upper() in ("HIGH", "MEDIUM", "LOW")]
    try:
        return service.upload(workspace, file.filename or "document.pdf", data, extra, request_id=request_id_var.get())
    except UploadRejected as error:
        raise HTTPException(status_code=413, detail=str(error)) from None
    except LoaderError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except LLMError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


@app.post("/v1/workspaces/{workspace}/questions", dependencies=[Auth])
def ask_question(workspace: str, body: Question) -> AskResult:
    if service.count(workspace) == 0:
        raise HTTPException(status_code=409, detail="Upload at least one PDF to this workspace first.")
    try:
        return service.ask(workspace, body.question, request_id=request_id_var.get())
    except LLMError as error:
        event("ask.failed")
        raise HTTPException(status_code=503, detail=str(error)) from None


@app.delete("/v1/workspaces/{workspace}", dependencies=[Auth], status_code=204)
def delete_workspace(workspace: str) -> Response:
    service.clear(workspace)
    return Response(status_code=204)
