# 0007 - Claude subscription for local use, API key for deployments

**Status:** Accepted

## Context
The project owner's team could not use API keys, but had a Claude subscription. A subscription is meant for the
subscriber's own use; products serving other people should use API keys.

## Decision
Three providers behind one `LLMClient`: `claude_code` (a subscription, via the Claude Code CLI in non-interactive
mode with all tools disabled and no project settings loaded), `anthropic` (API key) and `bedrock`.
`claude_code` is the default for local use; the README states that deployments for other users need an API key.

## Consequences
- Local use needs no API key and no extra cost; calls count toward the plan's usage limits.
- Subscription calls start a CLI process (~2-3 s overhead) and report notional cost.
- In Docker the subscription token is mounted as a read-only secret file and passed only to the CLI process, so it
  never appears in `docker inspect` or the container environment.
