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

from src import config


class ClaudeCodeError(Exception):
    """A problem talking to Claude Code, with a friendly message for the UI."""


def is_installed() -> bool:
    return shutil.which("claude") is not None


def _find_result(output: str) -> dict | None:
    """Claude Code prints one JSON object per line; the last one with type 'result' holds the reply."""
    result = None
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
    return result


def ask(model: str, system: str, content: str | list, effort: str | None = None) -> str:
    """Send one message (text, or a list of content blocks such as an image + text) and return the reply text."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    message = {"type": "user", "message": {"role": "user", "content": content}}

    command = [
        "claude", "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",                       # no tools: answer from the message only
        "--input-format", "stream-json",     # lets us send images as well as text
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--setting-sources", "",
        "--strict-mcp-config",
    ]
    if effort:
        command += ["--effort", effort]

    # Make sure Claude Code uses the subscription login, not an API key that might be set elsewhere.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

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
            )
        except FileNotFoundError:
            raise ClaudeCodeError("Claude Code is not installed. Install it, run `claude` once and log in with your subscription.")
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError("Claude took too long to reply. Please try again.")

        result = _find_result(process.stdout)
        if result and result.get("subtype") == "success" and not result.get("is_error"):
            return (result.get("result") or "").strip()

        # Something went wrong: work out what, from the error text.
        error_text = str((result or {}).get("result") or process.stderr or process.stdout[-500:])
        lower = error_text.lower()
        if "overloaded" in lower or "529" in lower or "internal server error" in lower:
            if attempt < config.MAX_RETRIES:
                time.sleep(2 * 2 ** attempt)  # wait 2s, 4s, 8s... (exponential backoff), then retry
                continue
            raise ClaudeCodeError("Claude is overloaded right now. Please try again in a minute.")
        if "log in" in lower or "login" in lower or "not logged" in lower or "authenticat" in lower:
            raise ClaudeCodeError("Claude Code is not logged in. Run `claude` in a terminal and use /login with your subscription.")
        if "limit" in lower:
            raise ClaudeCodeError("You've reached your Claude subscription usage limit. It resets after a few hours.")
        raise ClaudeCodeError(f"Claude Code returned an error: {error_text[:200]}")
