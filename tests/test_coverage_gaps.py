"""Tests for paths the other suites don't reach: the Anthropic API path, the HTTP client, section chunking, reranking."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from src import config, llm, retrieval
from src.api_client import ApiClient, backend_from_environment
from src.chunker import chunk_pages, is_heading, split_sections
from src.loader import LoaderError
from src.service import TrustRAGService, UploadRejected
from tests.fakes import FakeLLM, FakeStore
from tests.test_architecture import HANDBOOK


# ---------------------------------------------------------------------------- Anthropic API path
def fake_response(text="ok", stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", text=""), SimpleNamespace(type="text", text=f" {text} ")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


class FakeMessages:
    def __init__(self, result):
        self.result, self.requests = result, []

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def use_api(monkeypatch, result):
    messages = FakeMessages(result)
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "_client", SimpleNamespace(messages=messages))
    return messages


def test_api_path_returns_text_and_sends_effort(monkeypatch):
    messages = use_api(monkeypatch, fake_response("24 days"))
    assert llm.call_claude("claude-haiku-5-5", "sys", "q", 100, "low", purpose="answer") == "24 days"
    assert messages.requests[0]["output_config"] == {"effort": "low"}
    assert messages.requests[0]["messages"] == [{"role": "user", "content": "q"}]


def test_api_path_omits_effort_when_not_supported(monkeypatch):
    messages = use_api(monkeypatch, fake_response())
    llm.call_claude("anthropic.claude-haiku-4-5", "sys", "q", 100, None)
    assert "output_config" not in messages.requests[0]


def http_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom /internal/detail", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (http_error(anthropic.AuthenticationError, 401), "API key was rejected"),
        (http_error(anthropic.PermissionDeniedError, 403), "does not have access"),
        (http_error(anthropic.NotFoundError, 404), "model was not found"),
        (http_error(anthropic.RateLimitError, 429), "rate limit"),
        (http_error(anthropic.InternalServerError, 500), "overloaded"),
        (http_error(anthropic.BadRequestError, 400), "rejected the request"),
    ],
)
def test_api_errors_become_user_safe_messages(monkeypatch, error, message):
    use_api(monkeypatch, error)
    with pytest.raises(llm.LLMError, match=message) as raised:
        llm.call_claude("claude-haiku-5-5", "sys", "q", 100)
    assert "/internal/detail" not in str(raised.value)  # internal details never reach users


def test_api_connection_and_timeout_errors(monkeypatch):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    use_api(monkeypatch, anthropic.APITimeoutError(request=request))
    with pytest.raises(llm.LLMError, match="timed out"):
        llm.call_claude("claude-haiku-5-5", "sys", "q", 100)
    use_api(monkeypatch, anthropic.APIConnectionError(request=request))
    with pytest.raises(llm.LLMError, match="connect"):
        llm.call_claude("claude-haiku-5-5", "sys", "q", 100)


def test_low_credit_and_refusal(monkeypatch):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    use_api(
        monkeypatch,
        anthropic.BadRequestError(
            "Your credit balance is too low", response=httpx.Response(400, request=request), body=None
        ),
    )
    with pytest.raises(llm.LLMError, match="credit balance"):
        llm.call_claude("claude-haiku-5-5", "sys", "q", 100)
    use_api(monkeypatch, fake_response(stop_reason="refusal"))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.call_claude("claude-haiku-5-5", "sys", "q", 100)


def test_missing_credentials_messages(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" in llm.missing_key_message()
    monkeypatch.setattr(config, "PROVIDER", "bedrock")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    assert "AWS_BEARER_TOKEN_BEDROCK" in llm.missing_key_message()


def test_json_helper_retries_once_then_gives_up():
    replies = iter(["not json", '{"ok": true}'])
    assert llm.call_claude_json("m", "s", "p", 10, client=lambda *a, **k: next(replies)) == {"ok": True}
    with pytest.raises(llm.LLMError, match="JSON"):
        llm.call_claude_json("m", "s", "p", 10, client=lambda *a, **k: "still not json")


# ---------------------------------------------------------------------------- HTTP client
@pytest.fixture
def client_backend(monkeypatch):
    from api import main

    monkeypatch.setattr(main, "service", TrustRAGService(store=FakeStore(), llm=FakeLLM()))
    api_client = ApiClient("http://testserver")
    api_client._http = TestClient(main.app)
    return api_client


def test_api_client_mirrors_the_service(client_backend):
    ws = client_backend.new_workspace()
    upload = client_backend.upload(ws, "handbook.pdf", HANDBOOK, ["MEDIUM"])
    assert upload["chunks_indexed"] >= 1 and upload["report"]["excluded_risks"] == ["HIGH", "MEDIUM"]
    assert client_backend.list_files(ws) == ["handbook.pdf"] and client_backend.count(ws) >= 1
    assert client_backend.ask(ws, "annual leave?")["verification"]["trust_score"] == 100.0
    client_backend.clear(ws)
    assert client_backend.count(ws) == 0


def test_api_client_maps_errors_to_the_same_exceptions(client_backend, monkeypatch):
    ws = client_backend.new_workspace()
    with pytest.raises(LoaderError):
        client_backend.upload(ws, "bad.pdf", b"not a pdf")
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    with pytest.raises(UploadRejected):
        client_backend.upload(ws, "big.pdf", HANDBOOK)
    with pytest.raises(llm.LLMError):
        client_backend.ask(ws, "anything?")  # 409: nothing uploaded


def test_backend_selection(monkeypatch):
    monkeypatch.setenv("TRUSTRAG_API_URL", "http://localhost:9")
    assert isinstance(backend_from_environment(), ApiClient)


# ---------------------------------------------------------------------------- section chunking and reranking
def test_heading_detection():
    assert is_heading("Annual and sick leave") and is_heading("3. Flights")
    assert not is_heading("Employees receive 25 days of leave.") and not is_heading("a lowercase line")


def test_section_chunks_carry_their_heading():
    page = {
        "text": "Leave\nYou get 25 days.\nTravel\nBook 10 days ahead.",
        "file_name": "f.pdf",
        "page": 1,
        "ocr": False,
    }
    assert [h for h, _ in split_sections(page["text"])] == ["Leave", "Travel"]
    texts = [c["text"] for c in chunk_pages([page], strategy="section")]
    assert texts == ["Leave\nYou get 25 days.", "Travel\nBook 10 days ahead."]


def test_rerank_reorders_and_falls_back_safely():
    candidates = [
        {"id": str(i), "text": f"text {i}", "file_name": "f.pdf", "page": 1, "distance": 0.1 * i} for i in range(5)
    ]
    order = lambda *a, **k: '{"ranking": [3, 1]}'  # noqa: E731
    assert [c["id"] for c in retrieval.rerank("q", candidates, 3, order)] == ["3", "1", "0"]

    def broken(*a, **k):
        raise llm.LLMError("down")

    assert [c["id"] for c in retrieval.rerank("q", candidates, 2, broken)] == ["0", "1"]
