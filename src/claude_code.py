"""
Calls Claude through the Claude Code command-line tool, signed in with a Claude subscription.
No API key is needed: Claude Code uses the subscription you logged in with (run `claude` once and /login).

For each request we start `claude -p` (non-interactive "print" mode) with:
- our own system prompt (replacing Claude Code's default coding-assistant prompt)
- all tools disabled, so Claude can only read the message and reply with text
- no saved session, no user/project settings and no MCP servers, so every call is clean and independent
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any

from src import config


class ClaudeCodeError(Exception):
    """A problem talking to Claude Code, with a friendly message for the UI."""


def is_installed() -> bool:
    return shutil.which("claude") is not None


def _read_token_file() -> str:
    """Read the subscription token from the secret file, if one is mounted. Never logged or shown."""
    try:
        with open(config.CLAUDE_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _find_result(output: str) -> dict[str, Any] | None:
    """Claude Code prints one JSON object per line; the last one with type 'result' holds the reply."""
    result: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
    return result


def ask(model: str, system: str, content: str | list[Any], effort: str | None = None) -> str:
    """Send one message and return only the reply text."""
    return str(ask_detailed(model, system, content, effort)["text"])


def ask_detailed(model: str, system: str, content: str | list[Any], effort: str | None = None) -> dict[str, Any]:
    """Send one message; return {"text", "input_tokens", "output_tokens", "cost_usd"}."""
    """Send one message (text, or a list of content blocks such as an image + text) and return the reply text."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    message = {"type": "user", "message": {"role": "user", "content": content}}

    command = [
        "claude",
        "-p",
        "--model",
        model,
        "--system-prompt",
        system,
        "--tools",
        "",  # no tools: answer from the message only
        "--input-format",
        "stream-json",  # lets us send images as well as text
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
    ]
    if effort:
        command += ["--effort", effort]

    # Make sure Claude Code uses the subscription login, not an API key that might be set elsewhere.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    # In Docker, the login token can be a mounted secret file. We hand it to Claude Code only for this call,
    # so it never appears in the container's environment (and so not in `docker inspect`).
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        token = _read_token_file()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    for attempt in range(config.MAX_RETRIES + 1):
        try:
            process = subprocess.run(
                command,
                input=json.dumps(message) + "\n",
                capture_output=True,
                text=True,
                timeout=config.CLAUDE_CODE_TIMEOUT,
                cwd=tempfile.gettempdir(),  # neutral folder, so no project files are picked up
                env=env,
                check=False,  # the exit code is interpreted below, together with the JSON result
            )
        except FileNotFoundError:
            raise ClaudeCodeError(  # noqa: B904
                "Claude Code is not installed. Install it, run `claude` once and log in with your subscription."
            )
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError("Claude took too long to reply. Please try again.") from None

        result = _find_result(process.stdout)
        if result and result.get("subtype") == "success" and not result.get("is_error"):
            usage = result.get("usage") or {}
            return {
                "text": (result.get("result") or "").strip(),
                "input_tokens": int(usage.get("input_tokens", 0))
                + int(usage.get("cache_read_input_tokens", 0))
                + int(usage.get("cache_creation_input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "cost_usd": result.get("total_cost_usd"),
            }

        # Something went wrong: work out what, from the error text.
        error_text = str((result or {}).get("result") or process.stderr or process.stdout[-500:])
        lower = error_text.lower()
        if "overloaded" in lower or "529" in lower or "internal server error" in lower:
            if attempt < config.MAX_RETRIES:
                time.sleep(2 * 2**attempt)  # wait 2s, 4s, 8s... (exponential backoff), then retry
                continue
            raise ClaudeCodeError("Claude is overloaded right now. Please try again in a minute.")
        if "log in" in lower or "login" in lower or "not logged" in lower or "authenticat" in lower:
            raise ClaudeCodeError(
                "Claude Code is not logged in. Run `claude` in a terminal and use /login with your subscription."
            )
        if "limit" in lower:
            raise ClaudeCodeError("You've reached your Claude subscription usage limit. It resets after a few hours.")
        raise ClaudeCodeError("Claude could not answer this request. Please try again.")
    raise ClaudeCodeError("Claude could not answer this request. Please try again.")  # retries exhausted
