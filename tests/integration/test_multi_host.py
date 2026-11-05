"""
P7-AC2 integration tests (docs/phases/p7-plan.md 3.3, SPEC-GAP-072): two API containers on one Postgres and one Redis.

Not run in CI and skipped unless TRUSTRAG_IT_API_URLS names two base URLs. The verifier runs them against the compose
server profile:

    docker compose --profile server up -d --build
    TRUSTRAG_IT_API_URLS=http://127.0.0.1:8001,http://127.0.0.1:8002 python -m pytest -q -m integration tests/integration

An existing account is needed for the session tests (created by a human with `docker compose exec`); give it as
TRUSTRAG_IT_USERNAME / TRUSTRAG_IT_PASSWORD. No test here calls Claude.
"""

import os
import uuid

import httpx
import pytest

URLS = [u.strip().rstrip("/") for u in os.environ.get("TRUSTRAG_IT_API_URLS", "").split(",") if u.strip()]
USERNAME = os.environ.get("TRUSTRAG_IT_USERNAME", "")
PASSWORD = os.environ.get("TRUSTRAG_IT_PASSWORD", "")
RATE_LIMITED = {"detail": "Too many sign-in attempts. Please try again later."}
NOT_FOUND = {"detail": "Workspace not found."}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(len(URLS) != 2, reason="TRUSTRAG_IT_API_URLS does not name two API base URLs"),
]
needs_account = pytest.mark.skipif(
    not (USERNAME and PASSWORD), reason="TRUSTRAG_IT_USERNAME / TRUSTRAG_IT_PASSWORD are not set"
)


def _login(base, username, password):
    return httpx.post(f"{base}/v1/auth/login", json={"username": username, "password": password}, timeout=30)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_rate_limit_is_shared_across_two_api_containers():
    """P7-AC2 / threat 43: five failures alternating between containers -> 401 each; the 6th, on either, is the same 429."""
    username = "it-nobody-" + uuid.uuid4().hex[:12]
    statuses = [_login(URLS[i % 2], username, "a wrong password 12").status_code for i in range(5)]
    assert statuses == [401] * 5
    refused = [_login(URLS[0], username, "a wrong password 12"), _login(URLS[1], username, "a wrong password 12")]
    for response in refused:
        assert response.status_code == 429 and response.json() == RATE_LIMITED
        assert response.headers["Retry-After"].isdigit() and int(response.headers["Retry-After"]) >= 1
    assert refused[0].headers["Retry-After"] and refused[1].headers["Retry-After"]


@needs_account
def test_session_from_one_container_works_on_the_other():
    """P7-AC2 / threat 51: a token issued by container 1 is accepted by container 2 (sessions live in Postgres)."""
    login = _login(URLS[0], USERNAME, PASSWORD)
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    me = httpx.get(f"{URLS[1]}/v1/auth/me", headers=_bearer(token), timeout=30)
    assert me.status_code == 200 and me.json().get("username") == USERNAME
    httpx.post(f"{URLS[0]}/v1/auth/logout", headers=_bearer(token), timeout=30)
    assert (
        httpx.get(f"{URLS[1]}/v1/auth/me", headers=_bearer(token), timeout=30).status_code == 401
    )  # revoked everywhere


@needs_account
def test_account_workspace_created_on_one_container_is_usable_on_the_other():
    """P7-AC2 / threat 51 (review 11): an `acct-` workspace made on container 1 is owned on container 2; a random one is 404."""
    token = _login(URLS[0], USERNAME, PASSWORD).json()["token"]
    created = httpx.post(f"{URLS[0]}/v1/workspaces", headers=_bearer(token), timeout=60)
    assert created.status_code == 201, created.text
    workspace = created.json()["workspace"]
    try:
        assert workspace.startswith("acct-")
        other = httpx.get(f"{URLS[1]}/v1/workspaces/{workspace}/documents", headers=_bearer(token), timeout=30)
        assert other.status_code == 200, other.text
        stranger = httpx.get(
            f"{URLS[1]}/v1/workspaces/acct-{uuid.uuid4().hex}/documents", headers=_bearer(token), timeout=30
        )
        assert (stranger.status_code, stranger.json()) == (404, NOT_FOUND)
    finally:
        httpx.delete(f"{URLS[0]}/v1/workspaces/{workspace}", headers=_bearer(token), timeout=60)
        httpx.post(f"{URLS[0]}/v1/auth/logout", headers=_bearer(token), timeout=30)
