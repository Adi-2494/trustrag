"""Tests for the Claude subscription (Claude Code CLI) connector. The CLI is replaced with a fake: no real calls."""

import json
import subprocess

import pytest

from src import claude_code, config
from src.claude_code import ClaudeCodeError, _find_result


def cli_output(*events):
    return "\n".join(json.dumps(e) for e in events)


def test_find_result_picks_the_result_line():
    out = cli_output({"type": "system"}, {"type": "assistant"}, {"type": "result", "subtype": "success", "result": "Hi"})
    assert _find_result(out)["result"] == "Hi"
    assert _find_result("not json\n") is None


def fake_run(stdout, returncode=0):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")
    return run


def test_ask_returns_reply_text(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(cli_output(
        {"type": "result", "subtype": "success", "is_error": False, "result": "  24 days  "})))
    assert claude_code.ask("claude-haiku-5-5", "system", "question") == "24 days"


def test_ask_sends_text_and_disables_tools(monkeypatch):
    seen = {}

    def run(command, input, **kwargs):
        seen["command"], seen["input"], seen["env"] = command, json.loads(input), kwargs["env"]
        return subprocess.CompletedProcess(command, 0, cli_output({"type": "result", "subtype": "success", "result": "ok"}), "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-passed")
    claude_code.ask("claude-sonnet-5-5", "my system prompt", "hello", effort="low")

    cmd = seen["command"]
    assert cmd[cmd.index("--tools") + 1] == ""                       # no tools
    assert cmd[cmd.index("--system-prompt") + 1] == "my system prompt"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert seen["input"]["message"]["content"] == [{"type": "text", "text": "hello"}]
    assert "ANTHROPIC_API_KEY" not in seen["env"]                     # always uses the subscription login


@pytest.mark.parametrize("error_text, expected", [
    ("Please run /login. Not logged in.", "not logged in"),
    ("Claude usage limit reached", "usage limit"),
])
def test_friendly_errors(monkeypatch, error_text, expected):
    monkeypatch.setattr(subprocess, "run", fake_run(cli_output(
        {"type": "result", "subtype": "error", "is_error": True, "result": error_text}), returncode=1))
    with pytest.raises(ClaudeCodeError, match=expected):
        claude_code.ask("claude-haiku-5-5", "system", "question")


def test_retries_when_overloaded(monkeypatch):
    replies = [
        cli_output({"type": "result", "subtype": "error", "is_error": True, "result": "API Error: 529 overloaded"}),
        cli_output({"type": "result", "subtype": "success", "is_error": False, "result": "done"}),
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, replies.pop(0), ""))
    monkeypatch.setattr(claude_code.time, "sleep", lambda seconds: None)  # don't actually wait in tests
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    assert claude_code.ask("claude-haiku-5-5", "system", "question") == "done"
