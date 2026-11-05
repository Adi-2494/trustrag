"""
TrustRAG HTTP API - a thin layer over `TrustRAGService`.

    uvicorn api.main:app --host 127.0.0.1 --port 8000

Workspace ids are random 128-bit values that act as capabilities: only someone who holds the id can read or
change that workspace. Set API_KEY to require an X-API-Key header on every call (do this before exposing
the API beyond localhost).
"""

import hmac
import math
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from src import config
from src.accounts import LoginFailed, LoginRateLimited
from src.llm import LLMError
from src.loader import LoaderError
from src.models import AskResult, SessionInfo, UploadResult, WorkspaceList
from src.observability import METRICS, event, request_id_var, setup_logging
from src.service import NotAuthenticated, TrustRAGService, UploadRejected, WorkspaceLimitReached
from src.state import StorageUnavailable

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

SESSION_EXPIRED = "Your session has expired. Please sign in again."
MAX_TOKEN_CHARS = 512


def accounts_enabled() -> bool:
    return bool(config.ACCOUNTS_ENABLED)  # read per request: the flag can be flipped without re-importing


def bearer_token(request: Request) -> str | None:
    """The bearer token from the Authorization header only (never query or cookie); NotAuthenticated if malformed."""
    header = request.headers.get("Authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_CHARS or not token.isascii():
        raise NotAuthenticated(SESSION_EXPIRED)
    if not token.isprintable() or any(c.isspace() for c in token):
        raise NotAuthenticated(SESSION_EXPIRED)
    return token


def optional_session(request: Request) -> str | None:
    """Session token for workspace routes: None = anonymous. Checked before the workspace id is looked at."""
    if not accounts_enabled():
        return None  # flag off: the header is ignored entirely
    return bearer_token(request)


Session = Annotated[str | None, Depends(optional_session)]


def require_accounts() -> None:
    if not accounts_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@app.middleware("http")
async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request_id_var.set(request_id[:64])
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id_var.get()
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")
    # auth routes: the status would reveal sign-in outcomes to anyone who can read /metrics (SPEC-GAP-052)
    status = "-" if path.startswith("/v1/auth/") else str(response.status_code)
    METRICS.inc("http_requests_total", path=path, status=status)
    return response


@app.exception_handler(ValueError)
async def invalid_workspace(_: Request, error: ValueError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Workspace not found."})


@app.exception_handler(StorageUnavailable)
async def storage_unavailable(_: Request, error: StorageUnavailable) -> JSONResponse:
    """Server mode only: Postgres/Redis failed. Fixed body; the driver error is never shown or logged here."""
    return JSONResponse(status_code=503, content={"detail": config.STORAGE_UNAVAILABLE_MESSAGE})


@app.exception_handler(NotAuthenticated)
async def not_authenticated(_: Request, error: NotAuthenticated) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": SESSION_EXPIRED}, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(LoginFailed)
async def login_failed(_: Request, error: LoginFailed) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "Wrong username or password."})


@app.exception_handler(LoginRateLimited)
async def login_rate_limited(_: Request, error: LoginRateLimited) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many sign-in attempts. Please try again later."},
        headers={"Retry-After": str(max(1, math.ceil(error.retry_after)))},
    )


@app.exception_handler(WorkspaceLimitReached)
async def workspace_limit(_: Request, error: WorkspaceLimitReached) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    """FastAPI's default body, with two routing quirks pinned so anonymous responses stay identical to v1.0."""
    path = request.url.path
    headers = dict(error.headers or {})
    if error.status_code == 405 and path.startswith("/v1/auth/") and not accounts_enabled():
        return JSONResponse(status_code=404, content={"detail": "Not Found"})  # flag off: auth routes do not exist
    if error.status_code == 405 and path == "/v1/workspaces":
        headers["Allow"] = "POST"  # the v1.0 answer for any other method, flag on or off
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail}, headers=headers or None)


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


class Me(BaseModel):
    username: str


async def read_login_body(request: Request) -> bytes:
    """The raw body, read at most LOGIN_BODY_MAX_BYTES + 1 bytes (the extra byte marks it oversized)."""
    limit = config.LOGIN_BODY_MAX_BYTES + 1
    declared = request.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        return b"x" * limit
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) >= limit:
            return bytes(body[:limit])
    return bytes(body)


# `require_accounts` runs BEFORE the API-key check: with the flag off the auth routes do not exist (404), as in v1.0.
AuthRoute = [Depends(require_accounts), Auth]


@app.post("/v1/auth/login", dependencies=AuthRoute)
async def login(request: Request) -> SessionInfo:
    # no pydantic model: a validation error would echo the submitted password back in a 422
    raw = await read_login_body(request)
    client_key = request.client.host if request.client else None  # never a forwarding header; the socket peer only
    # scrypt and SQLite are blocking: off the event loop, so a login cannot stall every other request
    return await run_in_threadpool(service.login_raw, raw, client_key)


@app.post("/v1/auth/logout", dependencies=AuthRoute, status_code=204)
def logout(request: Request) -> Response:
    try:
        token = bearer_token(request)
    except NotAuthenticated:
        token = None  # logout never reveals whether a token was valid
    if token is not None:
        service.logout(token)
    return Response(status_code=204)


@app.get("/v1/auth/me", dependencies=AuthRoute)
def me(request: Request) -> Me:
    token = bearer_token(request)
    principal = service.authenticator.resolve(token) if token is not None else None
    if principal is None:
        raise NotAuthenticated(SESSION_EXPIRED)
    return Me(username=principal["username"])


def require_listing() -> None:
    if not accounts_enabled():  # v1.0 had no GET here: the router answered 405 before any API-key check
        raise HTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "POST"})


def require_clear_route() -> None:
    if not accounts_enabled():  # v1.0 had only GET here: 405 before any API-key check
        raise HTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"})


@app.get("/v1/workspaces", dependencies=[Depends(require_listing), Auth])
def list_workspaces(request: Request) -> WorkspaceList:
    token = bearer_token(request)
    if token is None:
        raise NotAuthenticated(SESSION_EXPIRED)
    return service.list_workspaces(session=token)


@app.post("/v1/workspaces", dependencies=[Auth], status_code=201)
def create_workspace(session: Session) -> WorkspaceCreated:
    return WorkspaceCreated(workspace=service.new_workspace(session=session))


@app.get("/v1/workspaces/{workspace}/documents", dependencies=[Auth])
def list_documents(workspace: str, session: Session) -> DocumentList:
    return DocumentList(
        files=service.list_files(workspace, session=session), chunks=service.count(workspace, session=session)
    )


@app.post("/v1/workspaces/{workspace}/documents", dependencies=[Auth], status_code=201)
async def upload_document(
    workspace: str,
    session: Session,
    file: Annotated[UploadFile, File()],
    also_remove: Annotated[str, Form()] = "",
) -> UploadResult:
    data = await file.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    extra = [r.strip().upper() for r in also_remove.split(",") if r.strip().upper() in ("HIGH", "MEDIUM", "LOW")]
    try:
        return service.upload(
            workspace, file.filename or "document.pdf", data, extra, request_id=request_id_var.get(), session=session
        )
    except UploadRejected as error:
        raise HTTPException(status_code=413, detail=str(error)) from None
    except LoaderError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except LLMError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


@app.post("/v1/workspaces/{workspace}/questions", dependencies=[Auth])
def ask_question(workspace: str, body: Question, session: Session) -> AskResult:
    if service.count(workspace, session=session) == 0:  # authorises first, so strangers never see the 409
        raise HTTPException(status_code=409, detail="Upload at least one PDF to this workspace first.")
    try:
        return service.ask(workspace, body.question, request_id=request_id_var.get(), session=session)
    except LLMError as error:
        event("ask.failed")
        raise HTTPException(status_code=503, detail=str(error)) from None


@app.delete("/v1/workspaces/{workspace}/documents", dependencies=[Depends(require_clear_route), Auth], status_code=204)
def clear_documents(workspace: str, session: Session) -> Response:
    service.clear_documents(workspace, session=session)  # keeps the workspace; DELETE on the workspace removes it
    return Response(status_code=204)


@app.delete("/v1/workspaces/{workspace}", dependencies=[Auth], status_code=204)
def delete_workspace(workspace: str, session: Session) -> Response:
    service.clear(workspace, session=session)
    return Response(status_code=204)
