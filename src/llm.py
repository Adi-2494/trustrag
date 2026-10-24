"""
The ONLY file that talks to Claude.
Other files call call_claude() or call_claude_json() and never touch the SDK directly.
This keeps API keys, retries and error handling in one place.

Three ways to reach Claude (chosen with LLM_PROVIDER in .env):
- "claude_code": your Claude subscription, through the Claude Code CLI (no API key) - see claude_code.py
- "anthropic":   the Anthropic API with an API key
- "bedrock":     AWS Bedrock with a Bedrock API key
"""

import json
import os
import re
import threading
import time
from collections import deque

import anthropic

from src import claude_code, config


class LLMError(Exception):
    """An error with a friendly message we can show directly in the UI."""


# Counts how many times we called Claude since the app started (shown in the sidebar).
_call_count = 0

# We create the client once and reuse it (like a singleton in Java).
_client = None


def get_call_count() -> int:
    return _call_count


# App-wide usage limit: timestamps of recent calls, shared by all users (and all threads, hence the lock).
_recent_calls = deque()
_rate_lock = threading.Lock()


def _check_rate_limit() -> None:
    """Refuse the call if the whole app already made MAX_CALLS_PER_HOUR calls in the last hour."""
    now = time.time()
    with _rate_lock:
        while _recent_calls and now - _recent_calls[0] > 3600:
            _recent_calls.popleft()
        if len(_recent_calls) >= config.MAX_CALLS_PER_HOUR:
            wait_minutes = int((3600 - (now - _recent_calls[0])) // 60) + 1
            raise LLMError(f"This app has reached its usage limit for now. Please try again in about {wait_minutes} minutes.")
        _recent_calls.append(now)


def missing_key_message() -> str | None:
    """Return a friendly message if the API key for the chosen provider is missing, else None."""
    if config.PROVIDER == "claude_code" and not claude_code.is_installed():
        return "Claude Code is not installed. Install it, run `claude` once and log in with your Claude subscription."
    if config.PROVIDER == "bedrock" and not os.getenv("AWS_BEARER_TOKEN_BEDROCK"):
        return "No Bedrock API key found. Add AWS_BEARER_TOKEN_BEDROCK to your .env file and restart the app."
    if config.PROVIDER == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        return "No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file and restart the app."
    return None


def _get_client():
    """Create the Claude client for the chosen provider (only once)."""
    global _client
    if _client is not None:
        return _client

    if config.PROVIDER == "bedrock":
        if not os.getenv("AWS_BEARER_TOKEN_BEDROCK"):
            raise LLMError("No Bedrock API key found. Add AWS_BEARER_TOKEN_BEDROCK to your .env file.")
        # The SDK reads AWS_BEARER_TOKEN_BEDROCK from the environment by itself.
        # max_retries: the SDK automatically retries rate limits / overloads with backoff.
        _client = anthropic.AnthropicBedrockMantle(
            aws_region=config.AWS_REGION, max_retries=config.MAX_RETRIES
        )
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise LLMError("No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file.")
        # The SDK reads ANTHROPIC_API_KEY from the environment by itself.
        _client = anthropic.Anthropic(max_retries=config.MAX_RETRIES)
    return _client


def call_claude(model: str, system: str, prompt: str | list, max_tokens: int, effort: str | None = None) -> str:
    """
    Send one message to Claude and return its text reply.
    `prompt` is normally a string; it can also be a list of content blocks (e.g. an image + text).
    """
    global _call_count
    _check_rate_limit()

    # Subscription mode: hand the request to Claude Code instead of the API.
    if config.PROVIDER == "claude_code":
        _call_count += 1
        try:
            return claude_code.ask(model, system, prompt, effort)
        except claude_code.ClaudeCodeError as e:
            raise LLMError(str(e))

    client = _get_client()

    # Build the request. "effort" is only sent when the model supports it.
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if effort:
        request["output_config"] = {"effort": effort}

    _call_count += 1
    try:
        response = client.messages.create(**request)
    # Most specific errors first (like ordering catch blocks in Java).
    except anthropic.AuthenticationError:
        raise LLMError("Your API key was rejected. Check the key in your .env file.")
    except anthropic.PermissionDeniedError:
        raise LLMError(f"Your key does not have access to the model '{model}'.")
    except anthropic.NotFoundError:
        raise LLMError(f"The model '{model}' was not found. Check the model names in src/config.py.")
    except anthropic.RateLimitError:
        raise LLMError("Too many requests right now (rate limit). Please wait a minute and try again.")
    except (anthropic.OverloadedError, anthropic.InternalServerError):
        raise LLMError("Claude is overloaded or having problems right now. Please try again shortly.")
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise LLMError("Your API credit balance is too low. Add credit in your account billing page.")
        raise LLMError(f"The request was rejected: {e.message}")
    except anthropic.APITimeoutError:
        raise LLMError("The request to Claude timed out. Please try again.")
    except anthropic.APIConnectionError:
        raise LLMError("Could not connect to Claude. Check your internet connection.")

    # Claude's safety system can decline a request; tell the user instead of showing nothing.
    if response.stop_reason == "refusal":
        raise LLMError("Claude declined to answer this request.")

    # The reply is a list of blocks (e.g. thinking + text). We only want the text blocks.
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _parse_json(text: str):
    """Try to turn Claude's reply into Python data. Returns None if it is not valid JSON."""
    # Remove ```json ... ``` fences if Claude added them.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} or [...] block found in the text.
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def call_claude_json(model: str, system: str, prompt: str, max_tokens: int, effort: str | None = None):
    """Like call_claude, but returns parsed JSON (dict or list). Retries once if the JSON is broken."""
    system = system + "\n\nRespond with valid JSON only. No explanations, no markdown."
    reply = call_claude(model, system, prompt, max_tokens, effort)
    data = _parse_json(reply)
    if data is not None:
        return data

    # One retry with a firmer reminder.
    retry_prompt = prompt + "\n\nIMPORTANT: Your previous reply was not valid JSON. Reply with ONLY valid JSON."
    reply = call_claude(model, system, retry_prompt, max_tokens, effort)
    data = _parse_json(reply)
    if data is None:
        raise LLMError("Claude's reply could not be read as JSON. Please try again.")
    return data
