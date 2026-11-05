"""
Shared counters of server mode on Redis (a Redis-protocol server, Valkey in compose): the model-call window and
the login rate limit (ADR 0014; amends ADR 0005). `src/state.py` dispatches here when `config.STATE_BACKEND ==
"redis"`. Redis holds nothing else: accounts live in Postgres and caches in Postgres with a TTL, so Redis memory is
bounded by the rate limits, not by documents.

Design rules, each from a review finding of the P7 plan:
- One Lua script per multi-step rule, so check-then-record is atomic across threads, processes and hosts.
- The clock is Redis `TIME` unless `config.STATE_CLOCK == "caller"` (tests that freeze time). A host with a wrong
  clock can therefore neither wipe nor stretch another host's window.
- Scripts reply with strings and an explicit status, never a bare number: Redis turns Lua numbers into integers, and
  `src/llm.py` treats a falsy wait as "allowed". Python then applies `max(1.0, ...)` to every refusal.
- Every Redis or client error becomes `StorageUnavailable` (fail closed), with the driver error only as `__cause__`.
`redis` is imported inside functions only.
"""

import os
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from src import config
from src.state import StorageUnavailable

# KEYS[1] = calls ZSET. ARGV: limit, window seconds, caller time ("" = use Redis TIME), member id.
_CALL_SCRIPT = """
local t
if ARGV[3] == '' then
  local tm = redis.call('TIME')
  t = tonumber(tm[1]) + tonumber(tm[2]) / 1000000
else
  t = tonumber(ARGV[3])
end
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. string.format('%.6f', t - window))
local count = redis.call('ZCARD', KEYS[1])
if count >= limit then
  local oldest = t
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  if first[2] then oldest = tonumber(first[2]) end
  return {'refused', string.format('%.6f', window - (t - oldest))}
end
redis.call('ZADD', KEYS[1], string.format('%.6f', t), ARGV[4])
redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
return {'allowed', '0'}
"""

# KEYS = one ZSET per rate-limit key. ARGV: caller time ("" = Redis TIME), attempt id, then (max, window) per key.
_LOGIN_SCRIPT = """
local t
if ARGV[1] == '' then
  local tm = redis.call('TIME')
  t = tonumber(tm[1]) + tonumber(tm[2]) / 1000000
else
  t = tonumber(ARGV[1])
end
local retry = 0
local refused = false
for i = 1, #KEYS do
  local maximum = tonumber(ARGV[1 + 2 * i])
  local window = tonumber(ARGV[2 + 2 * i])
  local cutoff = string.format('%.6f', t - window)
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', '(' .. cutoff)
  local live = redis.call('ZRANGEBYSCORE', KEYS[i], '(' .. cutoff, '+inf', 'WITHSCORES', 'LIMIT', 0, maximum)
  if #live >= 2 * maximum then
    refused = true
    local wait = window
    if live[2] then wait = tonumber(live[2]) + window - t end
    if wait > retry then retry = wait end
  end
end
if refused then
  return {'refused', string.format('%.6f', retry)}
end
for i = 1, #KEYS do
  local window = tonumber(ARGV[2 + 2 * i])
  redis.call('ZADD', KEYS[i], string.format('%.6f', t), ARGV[2])
  redis.call('PEXPIRE', KEYS[i], math.ceil(window * 1000))
end
return {'reserved', ARGV[2]}
"""

_lock = threading.Lock()
_holder: dict[str, Any] = {"client": None, "key": None, "pid": None, "scripts": {}}


def _prefix() -> str:
    return str(config.REDIS_PREFIX)


def _caller_clock() -> bool:
    return config.STATE_CLOCK == "caller"


def _client() -> Any:
    """One client (and its connection pool) per process, rebuilt when the URL changes or after a fork."""
    import redis

    url = str(config.REDIS_URL)
    timeout = float(config.REDIS_TIMEOUT_SECONDS)
    key, pid = (url, timeout), os.getpid()
    with _lock:
        if _holder["client"] is None or _holder["key"] != key or _holder["pid"] != pid:
            if not url:
                raise StorageUnavailable
            try:
                client = redis.Redis.from_url(
                    url, decode_responses=True, socket_connect_timeout=timeout, socket_timeout=timeout
                )
            except Exception as error:  # a malformed URL: its text may hold the password, so it is only chained
                raise StorageUnavailable from error
            _holder.update(client=client, key=key, pid=pid, scripts={})
        return _holder["client"]


def _script(name: str, source: str) -> Any:
    client = _client()
    with _lock:
        scripts: dict[str, Any] = _holder["scripts"]
        if name not in scripts:
            scripts[name] = client.register_script(source)
        return scripts[name]


def _run(name: str, source: str, keys: list[str], args: list[str]) -> list[str]:
    try:
        reply = _script(name, source)(keys=keys, args=args)
        return [item.decode() if isinstance(item, bytes) else str(item) for item in reply]
    except StorageUnavailable:
        raise
    except Exception as error:  # every Redis / network / script error fails closed; the text is only chained
        raise StorageUnavailable from error


def reset_for_tests() -> None:
    """Forget the cached client and scripts so a new URL / prefix takes effect."""
    with _lock:
        client = _holder["client"]
        _holder.update(client=None, key=None, pid=None, scripts={})
    if client is not None:
        with suppress(Exception):  # closing a dead client may fail; nothing to do about it
            client.close()


def try_acquire_call(limit: int, window_seconds: int = 3600) -> float:
    """0.0 if the call is allowed and recorded, else the seconds to wait (always >= 1.0)."""
    caller = repr(time.time()) if _caller_clock() else ""
    reply = _run(
        "calls",
        _CALL_SCRIPT,
        [_prefix() + "calls"],
        [str(limit), str(window_seconds), caller, uuid.uuid4().hex],
    )
    if len(reply) == 2 and reply[0] == "allowed":
        return 0.0
    if len(reply) == 2 and reply[0] == "refused":
        return max(1.0, _seconds(reply[1]))
    raise StorageUnavailable  # an unexpected reply is not an "allowed"


def _seconds(text: str) -> float:
    try:
        return float(text)
    except ValueError as error:
        raise StorageUnavailable from error


def reserve_login_attempt(
    keys: Sequence[str], limits: Mapping[str, tuple[int, float]], now: float
) -> tuple[str | None, float]:
    """One atomic reservation over all keys (see `src.state.reserve_login_attempt`); `now` only counts with STATE_CLOCK=caller."""
    if not keys:
        return uuid.uuid4().hex, 0.0
    attempt_id = uuid.uuid4().hex
    args = [repr(float(now)) if _caller_clock() else "", attempt_id]
    for key in keys:
        maximum, window = limits[key]
        args += [str(int(maximum)), repr(float(window))]
    reply = _run("login", _LOGIN_SCRIPT, [_prefix() + "login:" + key for key in keys], args)
    if len(reply) == 2 and reply[0] == "reserved" and reply[1] == attempt_id:
        return attempt_id, 0.0
    if len(reply) == 2 and reply[0] == "refused":
        return None, max(1.0, _seconds(reply[1]))
    raise StorageUnavailable


def refund_login_attempt(attempt_id: str, keys: Sequence[str]) -> None:
    """Remove only this attempt's member from each key, in one MULTI/EXEC."""
    if not keys:
        return
    client = _client()
    try:
        pipe = client.pipeline(transaction=True)
        for key in keys:
            pipe.zrem(_prefix() + "login:" + key, attempt_id)
        pipe.execute()
    except Exception as error:  # fail closed; callers decide whether a refund failure matters
        raise StorageUnavailable from error
