"""
Helpers shared by the P6 tests (accounts, ownership, anonymous mode, UI, security).

P6 modules are imported inside the functions, never at module level, so that while P6 is unbuilt every test fails on
its own missing import / attribute (a clear reason per test) instead of one collection error for the whole file.

Interfaces the tests fix (the plan left them open; recorded as SPEC-GAP-057):
    src.passwords   hash_password(pw) -> str   verify_password(pw, encoded) -> bool   needs_rehash(encoded) -> bool
                    canonical_username(raw) -> str | None   check_password_policy(pw, username) -> None
                    (raises PasswordPolicyError(ValueError))   DUMMY_HASH   kdf_slot(timeout) (raises KdfBusy)
    src.accounts    create_account(username, pw) -> Principal   set_password(username, pw)   set_disabled(username, bool)
                    list_accounts() -> list[dict]   AccountError(ValueError)   SessionAuthenticator()
                    PasswordLogin(authenticator=None).login(raw_body: bytes, client_key: str | None) -> SessionInfo
                    LoginFailed   LoginRateLimited(retry_after)   main(argv, *, getpass=..., stdin=...) -> int
    src.service     TrustRAGService(store=, llm=, authenticator=None, password_login=None); every workspace method gets
                    a keyword-only `session: str | None = None`; new_workspace(*, session=None);
                    list_workspaces(*, session) -> WorkspaceList; login_raw(raw_body, client_key=None) -> SessionInfo;
                    login(username, password, *, client_key=None) -> SessionInfo; logout(session);
                    WorkspaceNotFound(ValueError)  NotAuthenticated  WorkspaceLimitReached
    config          ACCOUNTS_ENABLED, SESSION_IDLE_MINUTES, SESSION_MAX_HOURS, LOGIN_MAX_FAILURES_PER_USER,
                    LOGIN_MAX_FAILURES_PER_CLIENT, LOGIN_WINDOW_MINUTES, LOGIN_MAX_PER_SECOND,
                    MAX_WORKSPACES_PER_ACCOUNT, MAX_SESSIONS_PER_ACCOUNT, SCRYPT_N/R/P/MAXMEM, KDF_MAX_CONCURRENT,
                    KDF_WAIT_SECONDS, LOGIN_BODY_MAX_BYTES, PASSWORD_MIN_CHARS, PASSWORD_MAX_CHARS; all read at CALL
                    time (tests monkeypatch them); all expiry / window arithmetic uses `time.time()`.
"""

import json
from types import SimpleNamespace
from typing import Any

from tests.fakes import FakeAuthenticator, FakeLLM, FakeStore
from tests.p6_golden import HANDBOOK, TRAVEL, make_pdf  # noqa: F401 - re-exported for the test modules

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "another long passphrase 42"
NOT_FOUND = {"detail": "Workspace not found."}
BAD_LOGIN = {"detail": "Wrong username or password."}
RATE_LIMITED = {"detail": "Too many sign-in attempts. Please try again later."}
SESSION_EXPIRED = {"detail": "Your session has expired. Please sign in again."}


def login_body(username: str, password: str) -> bytes:
    return json.dumps({"username": username, "password": password}).encode()


def make_service(**kwargs: Any) -> Any:
    from src.service import TrustRAGService

    kwargs.setdefault("store", FakeStore())
    kwargs.setdefault("llm", FakeLLM())
    return TrustRAGService(**kwargs)


def fake_world(*names: str) -> SimpleNamespace:
    """A service wired to FakeStore / FakeLLM / FakeAuthenticator, with one signed-in user per name."""
    store, llm, auth = FakeStore(), FakeLLM(), FakeAuthenticator()
    service = make_service(store=store, llm=llm, authenticator=auth)
    users = {}
    for name in names or ("alice", "bob"):
        principal, token = auth.add_user(name)
        users[name] = SimpleNamespace(principal=principal, token=token, account_id=principal["account_id"])
    return SimpleNamespace(service=service, store=store, llm=llm, auth=auth, users=users, **users)


def add_account(username: str, password: str = PASSWORD) -> Any:
    from src import accounts

    return accounts.create_account(username, password)


def sign_in(service: Any, username: str, password: str = PASSWORD, client_key: str | None = None) -> str:
    return str(service.login_raw(login_body(username, password), client_key)["token"])


def install_api(monkeypatch: Any, service: Any) -> Any:
    from api import main

    monkeypatch.setattr(main, "service", service)
    return main


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def comparable(response: Any) -> tuple[int, Any, dict[str, str]]:
    """status, body and headers of a response, without the per-request id."""
    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() not in ("x-request-id", "content-length")}
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text
    return response.status_code, body, headers


def upload(client: Any, ws: str, data: bytes = HANDBOOK, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        f"/v1/workspaces/{ws}/documents", files={"file": ("handbook.pdf", data, "application/pdf")}, headers=headers
    )
