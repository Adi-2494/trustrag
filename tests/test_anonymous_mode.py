"""
P6-AC3: anonymous mode is unchanged (SPEC-GAP-042, ADR 0012, plan sections 2.5 and 3).

The reference is `tests/data/p6_anonymous_golden.json`, recorded from the pre-P6 code by `tests/p6_golden.py`
(`python -m tests.p6_golden --record`; see that module's docstring). These tests replay the same traffic against the
current code and require equality, with `config.ACCOUNTS_ENABLED` false and true. They must keep passing after P6 is built.
Tests that only assert v1.0 behaviour pass before P6 exists (marked "baseline" in their docstrings); the ones that need the
P6 flag or new parameters fail until it is built.
"""

import inspect
import time

import pytest
from fastapi.testclient import TestClient

from src import config
from tests import p6_golden
from tests.fakes import FakeLLM, FakeStore
from tests.p6_support import HANDBOOK, add_account, bearer, install_api, make_service, upload


@pytest.fixture(scope="module")
def golden():
    return p6_golden.load_golden()


def _first_difference(actual, expected):
    for index, (a, e) in enumerate(zip(actual, expected, strict=False)):
        if a != e:
            return index, a, e
    return len(actual), None, None


@pytest.mark.parametrize("accounts_enabled", [False, True], ids=["flag_off", "flag_on"])
def test_anonymous_api_responses_match_v1_golden(golden, accounts_enabled):
    """P6-AC3: the same anonymous requests give the same status, headers (minus X-Request-ID) and body as v1.0."""
    actual = p6_golden.collect(accounts_enabled=accounts_enabled)["anonymous_flows"]
    expected = golden["anonymous_flows"]
    assert len(actual) == len(expected), _first_difference(actual, expected)
    assert actual == expected, _first_difference(actual, expected)


def test_new_paths_answer_as_v1_when_accounts_disabled(golden):
    """P6-AC3 / SPEC-GAP-039: flag off -> /v1/auth/* are 404, GET /v1/workspaces is 405 + Allow: POST, as in v1.0."""
    actual = p6_golden.collect(accounts_enabled=False)["disabled_only"]
    expected = golden["disabled_only"]
    assert actual == expected, _first_difference(actual, expected)
    by_name = {step["name"]: step for step in expected}
    assert (
        by_name["list_workspaces_405"]["status"] == 405 and by_name["list_workspaces_405"]["headers"]["allow"] == "POST"
    )
    assert {by_name[n]["status"] for n in ("login_404", "login_get_404", "logout_404", "me_404")} == {404}


@pytest.mark.parametrize("api_key", [None, "secret-key"], ids=["no_api_key", "api_key_set"])
@pytest.mark.parametrize("key_header", [None, "secret-key", "wrong"], ids=["no_header", "right_key", "wrong_key"])
def test_documents_delete_route_answers_as_v1_when_accounts_disabled(monkeypatch, api_key, key_header):
    """P6-AC3 (review N1, plan 2.5): flag off -> DELETE /v1/workspaces/{ws}/documents is v1.0's 405, Allow: GET.

    Reference recorded by running the pre-P6 `api/main.py` (git show HEAD:api/main.py), with and without API_KEY and
    with right/wrong/missing X-API-Key: always 405, {"detail": "Method Not Allowed"}, `allow: GET` (route matching
    comes before auth), so the new route must not exist for anonymous deployments.
    """
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "API_KEY", api_key)
    main = install_api(monkeypatch, make_service(store=FakeStore(), llm=FakeLLM()))
    client = TestClient(main.app)
    headers = {"X-API-Key": key_header} if key_header is not None else {}
    for ws in ("a" * 32, "acct-" + "e" * 32):
        response = client.delete(f"/v1/workspaces/{ws}/documents", headers=headers)
        assert response.status_code == 405, (ws, response.status_code, response.text)
        assert response.json() == {"detail": "Method Not Allowed"}
        assert response.headers["allow"] == "GET"
    # nothing was cleared: a real anonymous workspace keeps its documents (set up with the right key, if one is needed)
    setup = {"X-API-Key": api_key} if api_key is not None else {}
    anon = client.post("/v1/workspaces", headers=setup).json()["workspace"]
    assert upload(client, anon, headers=setup).status_code == 201
    assert client.delete(f"/v1/workspaces/{anon}/documents", headers=headers).status_code == 405
    assert client.get(f"/v1/workspaces/{anon}/documents", headers=setup).json()["files"] == ["handbook.pdf"]


def test_authorization_header_ignored_when_accounts_disabled(monkeypatch, fast_kdf):
    """P6-AC3: with the flag off a (once valid) bearer token changes nothing: anonymous v1.0 behaviour, 404 for acct ids."""
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", True, raising=False)
    service = make_service(store=FakeStore(), llm=FakeLLM())
    client = TestClient(install_api(monkeypatch, service).app)
    add_account("alice")
    token = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "correct horse battery staple"}
    ).json()["token"]
    owned = client.post("/v1/workspaces", headers=bearer(token)).json()["workspace"]
    assert owned.startswith("acct-")

    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", False, raising=False)
    created = client.post("/v1/workspaces", headers=bearer(token))
    assert created.status_code == 201 and not created.json()["workspace"].startswith("acct-")  # anonymous, as v1.0
    anon = created.json()["workspace"]
    for headers in (bearer(token), bearer("garbage"), {"Authorization": "Basic YTpi"}, {"Authorization": ""}):
        assert client.get(f"/v1/workspaces/{anon}/documents", headers=headers).status_code == 200
        assert upload(client, anon, headers=headers).status_code == 201
        assert (
            client.post(
                f"/v1/workspaces/{anon}/questions", json={"question": "annual leave?"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.get(f"/v1/workspaces/{owned}/documents", headers=headers).status_code == 404
        )  # still not reachable
    assert client.get("/v1/auth/me", headers=bearer(token)).status_code == 404
    assert (
        client.post(
            "/v1/auth/login", json={"username": "alice", "password": "correct horse battery staple"}
        ).status_code
        == 404
    )
    assert client.get("/v1/workspaces", headers=bearer(token)).status_code == 405
    assert (
        client.delete(f"/v1/workspaces/{owned}", headers=bearer(token)).status_code == 404
    )  # no 204: not deletable either
    assert client.delete(f"/v1/workspaces/{anon}", headers=bearer(token)).status_code == 204


def test_existing_operations_openapi_schema_unchanged(golden):
    """P6-AC3: existing operations and their schemas are byte-equal to v1.0; P6 may only add paths and methods."""
    from api import main

    actual = p6_golden._openapi(main.app)
    expected = golden["openapi"]
    assert actual["info"] == expected["info"]
    for path, methods in expected["paths"].items():
        for method, operation in methods.items():
            assert actual["paths"][path][method] == operation, (method, path)
    for name, schema in expected["components"].items():
        assert actual["components"][name] == schema, name
    spec = main.app.openapi()
    assert "Authorization" not in str(expected), "v1.0 documents no Authorization parameter"
    for path in expected["paths"]:  # the bearer header is read from the request, never declared (plan 2.4)
        for operation in spec["paths"][path].values():
            assert "authorization" not in [p["name"].lower() for p in operation.get("parameters", [])], path


def test_anonymous_workspaces_still_expire_after_24h(monkeypatch, tmp_path):
    """P6-AC3 / SPEC-GAP-004 (baseline: v1.0 behaviour): anonymous `ws_*` collections go after 24 h, not before."""
    from src import vectorstore

    monkeypatch.setattr(config, "CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(vectorstore, "_collections", {})
    assert config.WORKSPACE_MAX_AGE_HOURS == 24
    store = vectorstore.ChromaStore()
    old, fresh = store.new_workspace(), store.new_workspace()
    for ws in (old, fresh):
        vectorstore._get_collection(ws)
    real = time.time
    with monkeypatch.context() as inside:
        inside.setattr(time, "time", lambda: real() + 23 * 3600)
        assert store.cleanup() == 0  # 23 h: still kept
    with monkeypatch.context() as inside:
        inside.setattr(time, "time", lambda: real() + 25 * 3600)
        assert store.cleanup() == 2  # 25 h: both gone
    assert not {c.name for c in vectorstore._get_client().list_collections()}


def test_service_default_signatures_unchanged_for_anonymous_callers():
    """P6-AC3: v1.0 call shapes still work; `session` is only ever an extra keyword-only argument defaulting to None."""
    from src.service import TrustRAGService

    v1 = {
        "new_workspace": [],
        "list_files": ["workspace"],
        "count": ["workspace"],
        "clear": ["workspace"],
        "upload": ["workspace", "file_name", "data", "excluded_risks", "on_progress", "request_id"],
        "ask": ["workspace", "question", "request_id"],
    }
    for name, positional in v1.items():
        parameters = list(inspect.signature(getattr(TrustRAGService, name)).parameters.values())[1:]  # drop self
        old = [p for p in parameters if p.name != "session"]
        assert [p.name for p in old] == positional, name
        assert all(p.kind is not inspect.Parameter.KEYWORD_ONLY for p in old), name
        session = next((p for p in parameters if p.name == "session"), None)
        assert session is not None and session.kind is inspect.Parameter.KEYWORD_ONLY and session.default is None, name
    assert inspect.signature(TrustRAGService.upload).parameters["excluded_risks"].default is None

    service = make_service(store=FakeStore(), llm=FakeLLM())  # and the v1.0 call style still works end to end
    ws = service.new_workspace()
    assert not ws.startswith("acct-") and len(ws) == 32
    assert service.upload(ws, "handbook.pdf", HANDBOOK)["chunks_indexed"] >= 1
    assert service.list_files(ws) == ["handbook.pdf"] and service.count(ws) >= 1
    assert service.ask(ws, "annual leave?")["answer"]
    service.clear(ws)


def test_new_paths_answer_as_v1_with_api_key_and_accounts_disabled(monkeypatch):
    """P6-AC3 / SPEC-GAP-039 (review CR8): v1.0 routing answers before the API-key check: 404 for /v1/auth/*, 405 for GET."""
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    client = TestClient(install_api(monkeypatch, make_service()).app)  # no X-API-Key header on any call below
    login = client.post("/v1/auth/login", json={"username": "alice", "password": "whatever long password"})
    assert (login.status_code, login.json()) == (404, {"detail": "Not Found"})
    assert (client.post("/v1/auth/logout").status_code, client.get("/v1/auth/me").status_code) == (404, 404)
    listing = client.get("/v1/workspaces")
    assert (listing.status_code, listing.json()) == (405, {"detail": "Method Not Allowed"})
    assert listing.headers["allow"] == "POST"
    assert client.post("/v1/workspaces").status_code == 401  # a real v1.0 route still demands the key
