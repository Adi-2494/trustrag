"""
P6 workspace ownership and retention (ADR 0012, plan sections 2.5-2.7 and 3).

Service-level tests use `FakeStore` / `FakeLLM` / `FakeAuthenticator` (tests/fakes.py): tokens are opaque strings and
the service resolves them through its injected `Authenticator`. Ownership rows live in the real (tmp) state DB.
Interfaces fixed by these tests are listed in tests/p6_support.py (SPEC-GAP-057).

    service.new_workspace(*, session=None) -> id        anonymous: `<32 hex>`; with a session: `acct-<32 hex>` + row
    service.list_workspaces(*, session) -> {"workspaces": [{"workspace", "created_at"}]}   NotAuthenticated if no session
    service.<method>(..., *, session=None)              WorkspaceNotFound (a ValueError) when not authorised;
                                                        NotAuthenticated when the session is invalid, expired, revoked
    state.workspace_record(workspace) -> (account_id, deleted) | None
"""

import functools
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import config, state
from tests.fakes import FakeLLM, FakeStore
from tests.p6_support import (
    HANDBOOK,
    NOT_FOUND,
    SESSION_EXPIRED,
    add_account,
    bearer,
    comparable,
    fake_world,
    install_api,
    make_service,
    sign_in,
)

HEX = "0123456789abcdef" * 2


def _errors():
    from src.service import NotAuthenticated, WorkspaceLimitReached, WorkspaceNotFound

    return NotAuthenticated, WorkspaceLimitReached, WorkspaceNotFound


def _world_with_docs():
    """alice owns a workspace holding a document; bob is a second signed-in user; `anon` is an anonymous workspace."""
    w = fake_world("alice", "bob")
    w.alice_ws = w.service.new_workspace(session=w.alice.token)
    w.service.upload(w.alice_ws, "handbook.pdf", HANDBOOK, session=w.alice.token)
    w.anon_ws = w.service.new_workspace()
    return w


# ---------------------------------------------------------------------------- P6-AC1 service level
def test_service_user_lists_only_own_workspaces():
    """P6-AC1: list_workspaces returns the caller's live workspaces only."""
    NotAuthenticated, _, _ = _errors()
    w = fake_world("alice", "bob")
    a1, a2 = (w.service.new_workspace(session=w.alice.token) for _ in range(2))
    b1 = w.service.new_workspace(session=w.bob.token)
    w.service.new_workspace()  # an anonymous workspace belongs to nobody

    alice_list = w.service.list_workspaces(session=w.alice.token)["workspaces"]
    assert {item["workspace"] for item in alice_list} == {a1, a2}
    assert all(item["workspace"].startswith("acct-") and isinstance(item["created_at"], float) for item in alice_list)
    assert [i["workspace"] for i in w.service.list_workspaces(session=w.bob.token)["workspaces"]] == [b1]

    w.service.clear(a2, session=w.alice.token)  # deleted workspaces disappear from the list
    assert [i["workspace"] for i in w.service.list_workspaces(session=w.alice.token)["workspaces"]] == [a1]
    with pytest.raises(NotAuthenticated):
        w.service.list_workspaces(session=None)


def test_service_user_cannot_query_other_users_workspace():
    """P6-AC1: asking in someone else's workspace is refused and never reaches retrieval or the model."""
    _, _, WorkspaceNotFound = _errors()
    w = _world_with_docs()
    calls_before = len(w.llm.calls)
    with pytest.raises(WorkspaceNotFound):
        w.service.ask(w.alice_ws, "How much annual leave?", session=w.bob.token)
    assert len(w.llm.calls) == calls_before
    answer = w.service.ask(w.alice_ws, "How much annual leave?", session=w.alice.token)  # the owner still can
    assert answer["answer"] == w.llm.answer


def test_service_user_cannot_delete_other_users_workspace():
    """P6-AC1 / threat 31: another user (or an anonymous caller) cannot clear an account workspace."""
    _, _, WorkspaceNotFound = _errors()
    w = _world_with_docs()
    with pytest.raises(WorkspaceNotFound):
        w.service.clear(w.alice_ws, session=w.bob.token)
    with pytest.raises(WorkspaceNotFound):
        w.service.clear(w.alice_ws)
    assert w.service.list_files(w.alice_ws, session=w.alice.token) == ["handbook.pdf"]
    assert state.workspace_record(w.alice_ws) == (w.alice.account_id, 0)


def test_service_user_cannot_list_files_count_or_upload_to_other_users_workspace():
    """P6-AC1: the other read and write methods refuse a foreign workspace too, and nothing is written."""
    _, _, WorkspaceNotFound = _errors()
    w = _world_with_docs()
    chunks_before = w.store.count(w.alice_ws)
    with pytest.raises(WorkspaceNotFound):
        w.service.list_files(w.alice_ws, session=w.bob.token)
    with pytest.raises(WorkspaceNotFound):
        w.service.count(w.alice_ws, session=w.bob.token)
    with pytest.raises(WorkspaceNotFound):
        w.service.upload(w.alice_ws, "evil.pdf", HANDBOOK, session=w.bob.token)
    assert w.store.count(w.alice_ws) == chunks_before
    assert w.service.count(w.alice_ws, session=w.alice.token) == chunks_before


def test_service_anonymous_caller_cannot_reach_account_workspace():
    """P6-AC1 / threat 32: an account workspace id is not an anonymous capability (every method, no session)."""
    _, _, WorkspaceNotFound = _errors()
    w = _world_with_docs()
    ws = w.alice_ws
    for call in (
        lambda: w.service.list_files(ws),
        lambda: w.service.count(ws),
        lambda: w.service.ask(ws, "annual leave?"),
        lambda: w.service.upload(ws, "x.pdf", HANDBOOK),
        lambda: w.service.clear(ws),
    ):
        with pytest.raises(WorkspaceNotFound):
            call()
    assert w.service.count(ws, session=w.alice.token) >= 1


def test_service_signed_in_user_cannot_reach_unowned_workspace():
    """P6-AC1 / SPEC-GAP-040: a session reaches only workspaces it owns, not anonymous or unknown ids."""
    _, _, WorkspaceNotFound = _errors()
    w = _world_with_docs()
    for ws in (w.anon_ws, "f" * 32, "acct-" + "f" * 32, "not-a-real-id"):
        for call in (
            lambda ws=ws: w.service.list_files(ws, session=w.alice.token),
            lambda ws=ws: w.service.count(ws, session=w.alice.token),
            lambda ws=ws: w.service.ask(ws, "x", session=w.alice.token),
            lambda ws=ws: w.service.upload(ws, "x.pdf", HANDBOOK, session=w.alice.token),
            lambda ws=ws: w.service.clear(ws, session=w.alice.token),
        ):
            with pytest.raises(WorkspaceNotFound):
                call()
    assert w.service.list_files(w.anon_ws) == []  # the anonymous v1.0 path still works for anonymous callers


def test_service_rejects_expired_revoked_and_unknown_sessions():
    """P6-AC1: a bad session is NotAuthenticated, never a silent fallback to anonymous access."""
    NotAuthenticated, _, _ = _errors()
    w = _world_with_docs()
    revoked = w.auth.add_user("carol")[1]
    w.auth.revoke(revoked)
    for token in ("unknown-token", revoked, "x" * 513):
        for ws in (w.alice_ws, w.anon_ws, "f" * 32):  # even an anonymous workspace id: no fallback
            with pytest.raises(NotAuthenticated):
                w.service.list_files(ws, session=token)
            with pytest.raises(NotAuthenticated):
                w.service.ask(ws, "x", session=token)
            with pytest.raises(NotAuthenticated):
                w.service.upload(ws, "x.pdf", HANDBOOK, session=token)
        with pytest.raises(NotAuthenticated):
            w.service.new_workspace(session=token)
        with pytest.raises(NotAuthenticated):
            w.service.list_workspaces(session=token)
    w.auth.expire(w.alice.token)  # the owner's own session ends
    with pytest.raises(NotAuthenticated):
        w.service.list_files(w.alice_ws, session=w.alice.token)


def test_service_workspace_cap_per_account(monkeypatch):
    """P6-AC1 / SPEC-GAP-041: at most MAX_WORKSPACES_PER_ACCOUNT live workspaces; deleting frees a slot."""
    _, WorkspaceLimitReached, _ = _errors()
    monkeypatch.setattr(config, "MAX_WORKSPACES_PER_ACCOUNT", 3, raising=False)
    w = fake_world("alice", "bob")
    owned = [w.service.new_workspace(session=w.alice.token) for _ in range(3)]
    with pytest.raises(WorkspaceLimitReached, match="3"):
        w.service.new_workspace(session=w.alice.token)
    assert w.service.new_workspace(session=w.bob.token)  # the cap is per account
    w.service.clear(owned[0], session=w.alice.token)
    assert w.service.new_workspace(session=w.alice.token)  # a tombstoned workspace no longer counts
    assert len(w.service.list_workspaces(session=w.alice.token)["workspaces"]) == 3


def test_upload_or_ask_to_foreign_workspace_makes_zero_llm_calls(monkeypatch):
    """P6-AC1 / threat 40: authorisation runs before size check, loader, OCR, scanner and retrieval."""
    _, _, WorkspaceNotFound = _errors()
    w = _world_with_docs()
    w.llm.calls.clear()
    retrievals = []
    real_retrieve = w.store.retrieve
    monkeypatch.setattr(w.store, "retrieve", lambda *a, **k: retrievals.append(a) or real_retrieve(*a, **k))
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    oversized, garbage = b"\0" * (1024 * 1024 + 1), b"this is not a pdf"
    for session in (w.bob.token, None):
        with pytest.raises(WorkspaceNotFound):  # NOT UploadRejected (413) for an oversized file
            w.service.upload(w.alice_ws, "big.pdf", oversized, session=session)
        with pytest.raises(WorkspaceNotFound):  # NOT LoaderError (422) for a file that is not a PDF
            w.service.upload(w.alice_ws, "x.pdf", garbage, session=session)
        with pytest.raises(WorkspaceNotFound):
            w.service.upload(w.alice_ws, "ok.pdf", HANDBOOK, session=session)
        with pytest.raises(WorkspaceNotFound):
            w.service.ask(w.alice_ws, "annual leave?", session=session)
    assert w.llm.calls == [] and retrievals == []

    from src.service import UploadRejected

    with pytest.raises(UploadRejected):  # the size check still exists for the owner
        w.service.upload(w.alice_ws, "big.pdf", oversized, session=w.alice.token)


def test_acct_id_without_live_row_is_refused():
    """P6-AC1 / threat 32 (fail closed): an `acct-` id with no live ownership row is never treated as anonymous."""
    _, _, WorkspaceNotFound = _errors()
    w = fake_world("alice")
    orphan = w.store.new_workspace(persistent=True)  # e.g. the state DB was lost or a row was never written
    assert orphan.startswith("acct-") and state.workspace_record(orphan) is None
    for session in (None, w.alice.token):
        with pytest.raises(WorkspaceNotFound):
            w.service.list_files(orphan, session=session)
        with pytest.raises(WorkspaceNotFound):
            w.service.clear(orphan, session=session)
    with pytest.raises(WorkspaceNotFound):
        w.service.upload(orphan, "x.pdf", HANDBOOK)


# ---------------------------------------------------------------------------- P6-AC1 API level
@pytest.fixture
def web(monkeypatch, accounts_on):
    """Factory (so P6 import errors surface inside the test): the API wired to a fake-authenticator service."""

    def build():
        w = _world_with_docs()
        w.main = install_api(monkeypatch, w.service)
        w.client = TestClient(w.main.app)
        return w

    return build


def test_api_user_lists_only_own_workspaces(web):
    """P6-AC1: GET /v1/workspaces returns 200 {"workspaces": [...]} with the caller's workspaces only."""
    web = web()
    response = web.client.get("/v1/workspaces", headers=bearer(web.alice.token))
    assert response.status_code == 200
    assert [i["workspace"] for i in response.json()["workspaces"]] == [web.alice_ws]
    assert set(response.json()["workspaces"][0]) == {"workspace", "created_at"}
    assert web.client.get("/v1/workspaces", headers=bearer(web.bob.token)).json() == {"workspaces": []}
    created = web.client.post("/v1/workspaces", headers=bearer(web.bob.token))
    assert created.status_code == 201 and created.json()["workspace"].startswith("acct-")
    mine = web.client.get("/v1/workspaces", headers=bearer(web.bob.token)).json()["workspaces"]
    assert [i["workspace"] for i in mine] == [created.json()["workspace"]]


_ROUTES = [
    ("GET", "/v1/workspaces/{ws}/documents", {}),
    ("POST", "/v1/workspaces/{ws}/documents", {"files": {"file": ("a.pdf", HANDBOOK, "application/pdf")}}),
    ("POST", "/v1/workspaces/{ws}/questions", {"json": {"question": "annual leave?"}}),
    ("DELETE", "/v1/workspaces/{ws}", {}),
    ("DELETE", "/v1/workspaces/{ws}/documents", {}),
]
_ROUTE_IDS = ["list_documents", "upload", "ask", "delete", "clear_documents"]


@pytest.mark.parametrize(("method", "path", "kwargs"), _ROUTES, ids=_ROUTE_IDS)
def test_api_other_users_workspace_is_404_on_every_route(web, method, path, kwargs):
    """P6-AC1 / threat 31: another user's workspace answers exactly like an unknown id (status, body, headers)."""
    web = web()
    unknown = web.client.request(method, path.format(ws="acct-" + "e" * 32), headers=bearer(web.bob.token), **kwargs)
    foreign = web.client.request(method, path.format(ws=web.alice_ws), headers=bearer(web.bob.token), **kwargs)
    assert foreign.status_code == 404 and foreign.json() == NOT_FOUND
    assert comparable(foreign) == comparable(unknown)
    assert web.service.count(web.alice_ws, session=web.alice.token) >= 1  # nothing was deleted or added


@pytest.mark.parametrize(("method", "path", "kwargs"), _ROUTES, ids=_ROUTE_IDS)
def test_api_anonymous_request_to_account_workspace_is_404(web, method, path, kwargs):
    """P6-AC1 / threat 32: no Authorization header + an `acct-` id = the standard 404 on every route."""
    web = web()
    response = web.client.request(method, path.format(ws=web.alice_ws), **kwargs)
    assert response.status_code == 404 and response.json() == NOT_FOUND
    assert web.service.count(web.alice_ws, session=web.alice.token) >= 1


def test_api_question_to_foreign_empty_workspace_is_404_not_409(web):
    """P6-AC1: the empty-workspace 409 must not reveal that someone else's workspace exists."""
    web = web()
    empty_owned = web.service.new_workspace(session=web.alice.token)
    question = {"json": {"question": "anything?"}}
    foreign = web.client.post(f"/v1/workspaces/{empty_owned}/questions", headers=bearer(web.bob.token), **question)
    anonymous = web.client.post(f"/v1/workspaces/{empty_owned}/questions", **question)
    assert foreign.status_code == 404 and anonymous.status_code == 404
    own = web.client.post(f"/v1/workspaces/{empty_owned}/questions", headers=bearer(web.alice.token), **question)
    assert own.status_code == 409  # the owner still gets the helpful message


def test_invalid_bearer_gives_identical_401_for_owned_unknown_and_malformed_ids(web):
    """P6-AC1 / threat 31 (review #15): the token is checked before the id, so the 401 is the same for every id."""
    web = web()
    web.auth.revoke(web.bob.token)
    bad_headers = {
        "unknown token": {"Authorization": "Bearer not-a-real-token"},
        "revoked token": bearer(web.bob.token),
        "over-long token": bearer("x" * 513),
        "wrong scheme": {"Authorization": "Basic YWxpY2U6cGFzcw=="},
        "empty token": {"Authorization": "Bearer "},
    }
    ids = [web.alice_ws, "acct-" + "e" * 32, "e" * 32, "not-a-real-id"]
    for label, headers in bad_headers.items():
        seen = set()
        for ws in ids:
            for method, path, kwargs in _ROUTES:
                response = web.client.request(method, path.format(ws=ws), headers=headers, **kwargs)
                assert response.status_code == 401, (label, ws, method, response.text)
                assert response.json() == SESSION_EXPIRED and response.headers["www-authenticate"] == "Bearer"
                status, _body, response_headers = comparable(response)
                seen.add((status, str(sorted(response_headers.items()))))
        assert len(seen) == 1, label


def test_api_list_workspaces_requires_session(web):
    """P6-AC1: GET /v1/workspaces without a valid session is 401 (never an empty list)."""
    web = web()
    for headers in (None, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        response = web.client.get("/v1/workspaces", headers=headers)
        assert response.status_code == 401 and response.json() == SESSION_EXPIRED
        assert response.headers["www-authenticate"] == "Bearer"
    assert web.client.get("/v1/workspaces", headers=bearer(web.alice.token)).status_code == 200


def test_api_client_forwards_session_and_maps_errors(monkeypatch, accounts_on, fast_kdf):
    """P6-AC1: ApiClient(session=...) sends the bearer token; 401/429/409 map to typed errors (plan 2.4)."""
    from src.accounts import LoginFailed, LoginRateLimited
    from src.api_client import ApiClient
    from src.service import NotAuthenticated, WorkspaceLimitReached

    monkeypatch.setattr(config, "MAX_WORKSPACES_PER_ACCOUNT", 2, raising=False)
    main = install_api(monkeypatch, make_service(store=FakeStore(), llm=FakeLLM()))
    add_account("alice")
    add_account("bobby")
    client = ApiClient("http://testserver")
    client._http = TestClient(main.app)

    session = client.login("alice", "correct horse battery staple")
    assert session["username"] == "alice" and session["token"]
    ws = client.new_workspace(session=session["token"])
    assert ws.startswith("acct-")
    assert client.upload(ws, "handbook.pdf", HANDBOOK, session=session["token"])["chunks_indexed"] >= 1
    assert client.list_files(ws, session=session["token"]) == ["handbook.pdf"] and client.count(
        ws, session=session["token"]
    )
    assert client.ask(ws, "annual leave?", session=session["token"])["verification"]["trust_score"] == 100.0
    assert [i["workspace"] for i in client.list_workspaces(session=session["token"])["workspaces"]] == [ws]
    client.new_workspace(session=session["token"])
    with pytest.raises(WorkspaceLimitReached):
        client.new_workspace(session=session["token"])

    other = client.login("bobby", "correct horse battery staple")
    with pytest.raises(Exception, match="not found"):  # 404: the existing mapping (an LLMError with the detail)
        client.list_files(ws, session=other["token"])

    client.logout(session["token"])
    with pytest.raises(NotAuthenticated):
        client.list_files(ws, session=session["token"])
    with pytest.raises(NotAuthenticated):
        client.list_workspaces(session="bogus")

    for _ in range(5):
        with pytest.raises(LoginFailed):
            client.login("alice", "wrong password entirely")
    with pytest.raises(LoginRateLimited):
        client.login("alice", "wrong password entirely")


def _recording_client(monkeypatch):
    """An ApiClient whose HTTP layer is a TestClient that records every (method, path) it sends."""
    from src.api_client import ApiClient

    main = install_api(monkeypatch, make_service(store=FakeStore(), llm=FakeLLM()))
    client = ApiClient("http://testserver")
    http = TestClient(main.app)
    sent: list[tuple[str, str]] = []
    real_request = http.request

    def recording(method, url, *args, **kwargs):
        sent.append((str(method), str(url)))
        return real_request(method, url, *args, **kwargs)

    monkeypatch.setattr(http, "request", recording)
    client._http = http
    return client, sent


def test_api_client_clear_documents_without_session_uses_v1_delete(monkeypatch):
    """P6-AC3 (review N1): no session -> the v1.0 route DELETE /v1/workspaces/{ws}; so the UI works with the flag off."""
    client, sent = _recording_client(monkeypatch)
    ws = client.new_workspace()
    client.clear_documents(ws)
    assert [call for call in sent if call[0] == "DELETE"] == [("DELETE", f"/v1/workspaces/{ws}")]


def test_api_client_clear_documents_with_session_uses_documents_delete(monkeypatch, accounts_on, fast_kdf):
    """P6-AC5 (review N1): with a session the account route DELETE /v1/workspaces/{ws}/documents keeps the workspace."""
    client, sent = _recording_client(monkeypatch)
    add_account("alice")
    token = client.login("alice", "correct horse battery staple")["token"]
    ws = client.new_workspace(session=token)
    client.clear_documents(ws, session=token)
    assert [call for call in sent if call[0] == "DELETE"] == [("DELETE", f"/v1/workspaces/{ws}/documents")]


# ---------------------------------------------------------------------------- P6-AC5 retention
def _real_chroma(monkeypatch, tmp_path):
    from src import vectorstore

    monkeypatch.setattr(config, "CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(vectorstore, "_collections", {})
    return vectorstore, vectorstore.ChromaStore()


def _put_chunk(vectorstore, workspace):
    """Index one chunk with a precomputed embedding, so these tests never load the embedding model."""
    vectorstore._get_collection(workspace).add(
        ids=["c1"],
        embeddings=[[0.1] * 384],
        documents=["Annual leave is 25 days."],
        metadatas=[{"file_name": "h.pdf", "page": 1}],
    )


def _collection_names(vectorstore):
    return {c.name for c in vectorstore._get_client().list_collections()}


def test_account_workspaces_survive_cleanup_past_24h(monkeypatch, tmp_path):
    """P6-AC5 / threat 33: the age rule deletes anonymous `ws_*` only; `acct_*` survives; purge removes tombstoned."""
    vectorstore, store = _real_chroma(monkeypatch, tmp_path)
    anon, kept, doomed = (
        store.new_workspace(),
        store.new_workspace(persistent=True),
        store.new_workspace(persistent=True),
    )
    anon_keep = store.new_workspace()
    for ws in (anon, kept, doomed, anon_keep):
        _put_chunk(vectorstore, ws)
    assert kept.startswith("acct-") and not anon.startswith("acct-")
    assert _collection_names(vectorstore) == {f"ws_{anon}", f"ws_{anon_keep}", f"acct_{kept[5:]}", f"acct_{doomed[5:]}"}

    real_time = time.time
    with monkeypatch.context() as later:  # 25 hours later, as the age rule sees it
        later.setattr(time, "time", lambda: real_time() + 25 * 3600)
        removed = store.cleanup(keep=frozenset({kept, anon_keep}), purge=frozenset({doomed}))
    assert removed == 2  # the aged anonymous workspace and the tombstoned account workspace
    assert _collection_names(vectorstore) == {f"ws_{anon_keep}", f"acct_{kept[5:]}"}
    assert store.count(kept) == 1  # data intact

    with monkeypatch.context() as much_later:  # even with an empty keep set, age alone never deletes acct_*
        much_later.setattr(time, "time", lambda: real_time() + 30 * 24 * 3600)
        store.cleanup()
    assert f"acct_{kept[5:]}" in _collection_names(vectorstore)


def test_service_passes_owned_and_tombstoned_ids_to_cleanup(monkeypatch):
    """P6-AC5 / threat 33: every new_workspace() calls store.cleanup(keep=live owned ids, purge=tombstoned ids)."""
    w = fake_world("alice", "bob")
    w.service.new_workspace()
    assert w.store.cleanup_calls[-1] == {"keep": frozenset(), "purge": frozenset()}
    live, dead = w.service.new_workspace(session=w.alice.token), w.service.new_workspace(session=w.alice.token)
    other = w.service.new_workspace(session=w.bob.token)
    w.service.clear(dead, session=w.alice.token)
    w.service.new_workspace()  # anonymous callers trigger the same cleanup
    assert w.store.cleanup_calls[-1] == {"keep": frozenset({live, other}), "purge": frozenset({dead})}
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", False, raising=False)  # regardless of the flag (ADR 0011/0012)
    w.service.new_workspace()
    assert w.store.cleanup_calls[-1] == {"keep": frozenset({live, other}), "purge": frozenset({dead})}


def test_disabling_accounts_neither_deletes_nor_exposes_owned_workspaces(monkeypatch, tmp_path):
    """P6-AC5 / threat 38 (review #2): flag on -> create -> flag off -> aged cleanup + anonymous access -> intact + 404."""
    NotAuthenticated, _, WorkspaceNotFound = _errors()
    vectorstore, store = _real_chroma(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", True, raising=False)
    w = fake_world("alice")
    service = make_service(store=store, llm=FakeLLM(), authenticator=w.auth)
    owned = service.new_workspace(session=w.alice.token)
    _put_chunk(vectorstore, owned)
    anonymous = service.new_workspace()
    _put_chunk(vectorstore, anonymous)

    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", False, raising=False)
    # make every cleanup behave as if all workspaces were older than 24 h
    monkeypatch.setattr(
        vectorstore, "cleanup_old_workspaces", functools.partial(vectorstore.cleanup_old_workspaces, max_age_hours=0)
    )
    service.new_workspace()  # runs cleanup with keep/purge from the state DB
    assert _collection_names(vectorstore) >= {f"acct_{owned[5:]}"}
    assert f"ws_{anonymous}" not in _collection_names(vectorstore)  # proves the aged cleanup really ran

    with pytest.raises(WorkspaceNotFound):  # anonymous access with the flag off
        service.list_files(owned)
    with pytest.raises(WorkspaceNotFound):
        service.ask(owned, "annual leave?")
    assert store.count(owned) == 1  # the data is intact

    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", True, raising=False)
    assert service.list_files(owned, session=w.alice.token) == ["h.pdf"]  # and the owner gets it back
    with pytest.raises(NotAuthenticated):
        service.list_files(owned, session="nope")


def test_v1_cleanup_does_not_touch_owned_collections(monkeypatch, tmp_path):
    """P6-AC5 / threat 38 (review #2): the v1.0-style call (no keep/purge) leaves `acct_*`; v1.0 id check rejects it."""
    vectorstore, store = _real_chroma(monkeypatch, tmp_path)
    owned, anon = store.new_workspace(persistent=True), store.new_workspace()
    _put_chunk(vectorstore, owned)
    _put_chunk(vectorstore, anon)
    assert vectorstore.cleanup_old_workspaces(max_age_hours=0) == 1  # exactly v1.0's call: only `ws_*`
    assert _collection_names(vectorstore) == {f"acct_{owned[5:]}"}
    # what a v1.0 process would do with the id: its validator (unchanged pattern) refuses `acct-...`
    assert vectorstore._WORKSPACE_ID.pattern == r"^[a-f0-9]{32}$"
    assert not vectorstore._WORKSPACE_ID.match(owned)


def test_collection_names_accept_only_the_two_id_forms():
    """P6-AC5 / SPEC-GAP-053: `<32 hex>` -> ws_, `acct-<32 hex>` -> acct_, everything else is refused."""
    from src import vectorstore

    assert vectorstore._collection_name(HEX) == f"ws_{HEX}"
    assert vectorstore._collection_name(f"acct-{HEX}") == f"acct_{HEX}"
    for bad in (
        "",
        f"acct_{HEX}",
        f"ws_{HEX}",
        f"ACCT-{HEX}",
        f"acct-{HEX[:-1]}",
        f"acct-{HEX}0",
        f"acct-{HEX.upper()}",
        f"acct--{HEX}",
        f"acct-{HEX}\n",
        "acct-",
        "../other",
        f"{HEX}\n",
    ):
        with pytest.raises(ValueError, match="Invalid workspace id"):
            vectorstore._collection_name(bad)
    from src.vectorstore import new_workspace_id

    assert len(new_workspace_id(persistent=False)) == 32
    assert new_workspace_id(persistent=True).startswith("acct-") and len(new_workspace_id(persistent=True)) == 37


def test_deleted_owned_workspace_id_is_404_for_anonymous_and_owner():
    """P6-AC5 / threat 32 (review #8): delete tombstones the row; the id never becomes anonymous or reusable."""
    _, _, WorkspaceNotFound = _errors()
    w = fake_world("alice")
    ws = w.service.new_workspace(session=w.alice.token)
    w.service.upload(ws, "handbook.pdf", HANDBOOK, session=w.alice.token)
    w.service.clear(ws, session=w.alice.token)

    record = state.workspace_record(ws)
    assert record is not None and record[0] == w.alice.account_id and record[1]  # tombstone, not a removed row
    assert w.store.count(ws) == 0  # vectors cleared
    for session in (None, w.alice.token):
        with pytest.raises(WorkspaceNotFound):
            w.service.list_files(ws, session=session)
        with pytest.raises(WorkspaceNotFound):
            w.service.upload(ws, "again.pdf", HANDBOOK, session=session)
    assert w.service.list_workspaces(session=w.alice.token) == {"workspaces": []}
    assert w.service.new_workspace(session=w.alice.token) != ws  # ids are never reused


def test_upload_racing_owner_delete_never_leaves_reachable_data(monkeypatch):
    """P6-AC5 / threat 32 (review #8, SPEC-GAP-056): delete injected between authorisation and add_chunks."""
    _, _, WorkspaceNotFound = _errors()
    w = fake_world("alice")
    ws = w.service.new_workspace(session=w.alice.token)
    real_add, raced = w.store.add_chunks, []

    def add_after_owner_deletes(workspace, chunks):
        if not raced:
            raced.append(True)
            w.service.clear(ws, session=w.alice.token)  # the owner deletes while the upload is in flight
        real_add(workspace, chunks)

    monkeypatch.setattr(w.store, "add_chunks", add_after_owner_deletes)
    with pytest.raises(WorkspaceNotFound):
        w.service.upload(ws, "handbook.pdf", HANDBOOK, session=w.alice.token)
    assert raced and w.store.count(ws) == 0  # the late write was cleared again
    with pytest.raises(WorkspaceNotFound):
        w.service.list_files(ws)  # unreachable anonymously
    with pytest.raises(WorkspaceNotFound):
        w.service.list_files(ws, session=w.alice.token)  # and by the former owner


def test_disabled_account_keeps_workspaces(fast_kdf):
    """P6-AC5 / SPEC-GAP-004, -037: disabling revokes sessions but keeps workspaces; enabling brings them back."""
    from src import accounts
    from src.accounts import LoginFailed
    from src.service import NotAuthenticated

    service = make_service(store=FakeStore(), llm=FakeLLM())
    principal = add_account("alice")
    token = sign_in(service, "alice")
    ws = service.new_workspace(session=token)
    service.upload(ws, "handbook.pdf", HANDBOOK, session=token)

    accounts.set_disabled("alice", True)
    with pytest.raises(NotAuthenticated):
        service.list_files(ws, session=token)
    with pytest.raises(LoginFailed):
        sign_in(service, "alice")
    assert state.workspace_record(ws) == (principal["account_id"], 0)  # row kept, not tombstoned
    assert service.store.count(ws) >= 1

    accounts.set_disabled("alice", False)
    token = sign_in(service, "alice")
    assert [i["workspace"] for i in service.list_workspaces(session=token)["workspaces"]] == [ws]
    assert service.list_files(ws, session=token) == ["handbook.pdf"]


def test_state_file_for_ownership_lives_in_the_state_db():
    """P6 / ADR 0012: ownership is durable SQLite state (tables from plan 2.3), not process memory."""
    import sqlite3

    w = fake_world("alice")
    ws = w.service.new_workspace(session=w.alice.token)
    with sqlite3.connect(config.STATE_DB) as db:
        rows = db.execute("SELECT workspace, account_id, deleted FROM workspaces").fetchall()
    assert rows == [(ws, w.alice.account_id, 0)]
    assert Path(config.STATE_DB).exists()


# ---------------------------------------------------------------------------- review fixes (fix step, P6)
def test_upload_racing_session_revocation_leaves_no_file(monkeypatch):
    """P6-AC5 / threat 32 (review F5): the session ends between authorisation and add_chunks; the late file must not stay."""
    from tests.p6_support import TRAVEL

    w = fake_world("alice")
    ws = w.service.new_workspace(session=w.alice.token)
    w.service.upload(ws, "travel.pdf", TRAVEL, session=w.alice.token)  # an earlier, unrelated upload
    real_add, raced = w.store.add_chunks, []

    def add_after_logout(workspace, chunks):
        if not raced:
            raced.append(True)
            w.service.logout(w.alice.token)  # sign-out / expiry / disable lands while the upload is in flight
        real_add(workspace, chunks)

    monkeypatch.setattr(w.store, "add_chunks", add_after_logout)
    with pytest.raises(Exception) as refused:  # NotAuthenticated (the session is gone); the exact type is not pinned
        w.service.upload(ws, "handbook.pdf", HANDBOOK, session=w.alice.token)
    assert raced and type(refused.value).__name__ in ("NotAuthenticated", "WorkspaceNotFound"), refused.value
    files = w.store.list_files(ws)
    assert "handbook.pdf" not in files, "the upload that raced the session ending was left indexed"
    assert "travel.pdf" in files, "the cleanup must remove only this upload's file"


def test_clear_documents_keeps_the_workspace_but_removes_documents():
    """P6-AC5 / SPEC-GAP-058: `clear_documents` empties the workspace without tombstoning it; `clear` still tombstones."""
    from src.service import NotAuthenticated, WorkspaceNotFound

    w = fake_world("alice", "bob")
    ws = w.service.new_workspace(session=w.alice.token)
    w.service.upload(ws, "handbook.pdf", HANDBOOK, session=w.alice.token)
    assert w.store.count(ws) > 0

    w.service.clear_documents(ws, session=w.alice.token)
    assert w.store.count(ws) == 0 and w.store.list_files(ws) == []
    assert state.workspace_record(ws) == (w.alice.account_id, 0)  # row kept, not deleted
    assert [i["workspace"] for i in w.service.list_workspaces(session=w.alice.token)["workspaces"]] == [ws]
    w.service.upload(ws, "handbook.pdf", HANDBOOK, session=w.alice.token)  # still usable
    assert w.service.list_files(ws, session=w.alice.token) == ["handbook.pdf"]

    # authorise first: a stranger, an anonymous caller and a dead session clear nothing
    for session, error in ((w.bob.token, WorkspaceNotFound), (None, WorkspaceNotFound), ("tok-nope", NotAuthenticated)):
        with pytest.raises(error):
            w.service.clear_documents(ws, session=session)
        assert w.store.count(ws) > 0

    w.service.clear(ws, session=w.alice.token)  # the API's DELETE keeps its meaning
    assert state.workspace_record(ws) == (w.alice.account_id, 1)


def test_clear_documents_on_an_anonymous_workspace_clears_it_as_in_v1():
    """P6-AC3 / SPEC-GAP-058: for anonymous callers `clear_documents` is the v1.0 'Clear all documents'."""
    w = fake_world("alice")
    ws = w.service.new_workspace()
    w.service.upload(ws, "handbook.pdf", HANDBOOK)
    w.service.clear_documents(ws)
    assert w.store.count(ws) == 0 and state.workspace_record(ws) is None
