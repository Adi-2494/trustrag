"""
The single entry point to Claude.

Three ways to reach Claude (LLM_PROVIDER):
- "claude_code": a Claude subscription, through the Claude Code CLI (no API key) - see claude_code.py
- "anthropic":   the Anthropic API with an API key
- "bedrock":     AWS Bedrock with a Bedrock API key

Every call goes through the app-wide rate limit (shared across processes), is counted, and is logged with
its purpose, prompt version, model, token usage, cost and latency - never with prompt or answer text.
Pipeline modules receive the client as an `LLMClient`, so tests and alternative providers can be injected.
"""

import json
import os
import re
import threading
import time
from typing import Any, Protocol

import anthropic

from src import claude_code, config, state
from src.observability import METRICS, event

# USD per million tokens (input, output) - used for cost reporting only.
_PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "anthropic.claude-opus-5-5": (4.0, 20.0),
    "anthropic.claude-haiku-4-5": (1.0, 5.0),
}


class LLMError(Exception):
    """An error with a message that is safe and useful to show to end users."""


class LLMClient(Protocol):
    """Anything that can answer a prompt: the real client below, a fake in tests, or a recorded-replay client."""

    def __call__(
        self,
        model: str,
        system: str,
        prompt: str | list[Any],
        max_tokens: int,
        effort: str | None = None,
        *,
        purpose: str = "other",
    ) -> str: ...


_count_lock = threading.Lock()
_call_count = 0
_client: Any = None  # one SDK client per process: it holds the HTTP connection pool


def get_call_count() -> int:
    with _count_lock:
        return _call_count


def _count_call() -> None:
    global _call_count  # noqa: PLW0603 - process-wide counter, guarded by _count_lock
    with _count_lock:
        _call_count += 1


def missing_key_message() -> str | None:
    """A friendly message if the chosen provider has no credentials configured, else None."""
    if config.PROVIDER == "claude_code" and not claude_code.is_installed():
        return "Claude Code is not installed. Install it, run `claude` once and log in with your Claude subscription."
    if config.PROVIDER == "bedrock" and not os.getenv("AWS_BEARER_TOKEN_BEDROCK"):
        return "No Bedrock API key found. Add AWS_BEARER_TOKEN_BEDROCK to your .env file and restart the app."
    if config.PROVIDER == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        return "No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file and restart the app."
    return None


def _get_client() -> Any:
    global _client  # noqa: PLW0603 - one SDK client (connection pool) per process
    if _client is not None:
        return _client
    if config.PROVIDER == "bedrock":
        if not os.getenv("AWS_BEARER_TOKEN_BEDROCK"):
            raise LLMError("No Bedrock API key found. Add AWS_BEARER_TOKEN_BEDROCK to your .env file.")
        # The SDK reads AWS_BEARER_TOKEN_BEDROCK itself and retries rate limits / overloads with backoff.
        _client = anthropic.AnthropicBedrockMantle(aws_region=config.AWS_REGION, max_retries=config.MAX_RETRIES)
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise LLMError("No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file.")
        _client = anthropic.Anthropic(max_retries=config.MAX_RETRIES)
    return _client


def _call_api(
    model: str, system: str, prompt: str | list[Any], max_tokens: int, effort: str | None
) -> tuple[str, int, int]:
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if effort:
        request["output_config"] = {"effort": effort}
    try:
        response = _get_client().messages.create(**request)
    # Subclasses before their parents (e.g. RateLimitError before APIStatusError), or the general handler wins.
    except anthropic.AuthenticationError:
        raise LLMError("Your API key was rejected. Check the key in your .env file.") from None
    except anthropic.PermissionDeniedError:
        raise LLMError("Your key does not have access to the configured model.") from None
    except anthropic.NotFoundError:
        raise LLMError("The configured model was not found. Check the model names in src/config.py.") from None
    except anthropic.RateLimitError:
        raise LLMError("Too many requests right now (rate limit). Please wait a minute and try again.") from None
    except (anthropic.OverloadedError, anthropic.InternalServerError):
        raise LLMError("Claude is overloaded or having problems right now. Please try again shortly.") from None
    except anthropic.BadRequestError as error:
        if "credit balance" in str(error).lower():
            raise LLMError("Your API credit balance is too low. Add credit in your account billing page.") from None
        raise LLMError("Claude rejected the request. Please try again.") from None
    except anthropic.APITimeoutError:
        raise LLMError("The request to Claude timed out. Please try again.") from None
    except anthropic.APIConnectionError:
        raise LLMError("Could not connect to Claude. Check your internet connection.") from None

    if response.stop_reason == "refusal":  # the safety system declined; say so instead of returning nothing
        raise LLMError("Claude declined to answer this request.")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text, response.usage.input_tokens, response.usage.output_tokens


def call_claude(
    model: str,
    system: str,
    prompt: str | list[Any],
    max_tokens: int,
    effort: str | None = None,
    *,
    purpose: str = "other",
) -> str:
    """Send one message to Claude and return its text reply (prompt may be text or content blocks)."""
    wait = state.try_acquire_call(config.MAX_CALLS_PER_HOUR)
    if wait:
        METRICS.inc("llm_rate_limited_total")
        raise LLMError(
            f"This app has reached its usage limit for now. Please try again in about {int(wait // 60) + 1} minutes."
        )
    _count_call()
    started = time.perf_counter()
    cost: float | None = None
    try:
        if config.PROVIDER == "claude_code":
            try:
                result = claude_code.ask_detailed(model, system, prompt, effort)
            except claude_code.ClaudeCodeError as error:
                raise LLMError(str(error)) from None
            text = str(result["text"])
            tokens_in, tokens_out = int(result["input_tokens"]), int(result["output_tokens"])
            cost = result["cost_usd"]  # notional for subscriptions: what the same usage would cost via the API
        else:
            text, tokens_in, tokens_out = _call_api(model, system, prompt, max_tokens, effort)
    except LLMError:
        METRICS.inc("llm_errors_total", purpose=purpose)
        event(
            "llm.call_failed", purpose=purpose, model=model, duration_ms=round((time.perf_counter() - started) * 1000)
        )
        raise

    if cost is None and model in _PRICES:
        price_in, price_out = _PRICES[model]
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    seconds = time.perf_counter() - started
    METRICS.inc("llm_calls_total", purpose=purpose, model=model)
    METRICS.inc("llm_input_tokens_total", tokens_in, purpose=purpose)
    METRICS.inc("llm_output_tokens_total", tokens_out, purpose=purpose)
    METRICS.inc("llm_cost_usd_total", cost or 0.0, purpose=purpose)
    METRICS.observe(f"llm_{purpose}", seconds)
    event(
        "llm.call",
        purpose=purpose,
        prompt_version=config.PROMPT_VERSIONS.get(purpose, "-"),
        model=model,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=round(cost, 6) if cost is not None else None,
        duration_ms=round(seconds * 1000),
    )
    return text


def _parse_json(text: str) -> Any:
    """Turn a model reply into Python data, tolerating code fences and stray prose. None if it isn't JSON."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def call_claude_json(
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    effort: str | None = None,
    *,
    purpose: str = "other",
    client: LLMClient | None = None,
) -> Any:
    """Like call_claude, but returns parsed JSON. Retries once with a firmer reminder if the JSON is broken."""
    ask = client or call_claude
    system = system + "\n\nRespond with valid JSON only. No explanations, no markdown."
    data = _parse_json(ask(model, system, prompt, max_tokens, effort, purpose=purpose))
    if data is not None:
        return data
    METRICS.inc("llm_json_retries_total", purpose=purpose)
    retry_prompt = prompt + "\n\nIMPORTANT: Your previous reply was not valid JSON. Reply with ONLY valid JSON."
    data = _parse_json(ask(model, system, retry_prompt, max_tokens, effort, purpose=purpose))
    if data is None:
        raise LLMError("Claude's reply could not be read as JSON. Please try again.")
    return data
