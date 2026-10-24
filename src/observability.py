"""
Structured logging and metrics.

- Every log line is one JSON object carrying the current request ID, so a single question can be followed
  through upload -> scan -> retrieval -> generation -> verification, even across worker threads.
- `stage()` times a pipeline step and records it both as a log event and as a metric.
- `METRICS` keeps counters and timings in memory and renders them in Prometheus text format (/metrics).
Document text and model output are never logged - only sizes, counts, timings, models and costs.
"""

import contextvars
import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

T = TypeVar("T")

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
logger = logging.getLogger("trustrag")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure JSON logging once (safe to call repeatedly, e.g. on every Streamlit re-run)."""
    if any(getattr(h, "_trustrag", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._trustrag = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def new_request_id(request_id: str | None = None) -> str:
    """Start a request: use the caller's ID (e.g. from the HTTP X-Request-ID header) or create one."""
    request_id = (request_id or uuid.uuid4().hex[:12])[:64]
    request_id_var.set(request_id)
    return request_id


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields})


def in_current_context(fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap a function so worker threads see the caller's request ID (threads don't inherit contextvars)."""
    context = contextvars.copy_context()
    return lambda *args, **kwargs: context.copy().run(fn, *args, **kwargs)


class Metrics:
    """Thread-safe counters and timing summaries, exported in Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._timings: dict[str, list[float]] = {}  # name -> [count, total_seconds, max_seconds]

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            count, total, peak = self._timings.get(name, [0.0, 0.0, 0.0])
            self._timings[name] = [count + 1, total + seconds, max(peak, seconds)]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {
                    f"{name}{dict(labels) if labels else ''}": v for (name, labels), v in self._counters.items()
                },
                "timings": {
                    n: {"count": int(c), "avg_seconds": round(t / c, 3), "max_seconds": round(m, 3)}
                    for n, (c, t, m) in self._timings.items()
                },
            }

    def render_prometheus(self) -> str:
        lines = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"trustrag_{name}{{{label_text}}} {value}" if label_text else f"trustrag_{name} {value}")
            for name, (count, total, peak) in sorted(self._timings.items()):
                lines += [
                    f'trustrag_stage_seconds_count{{stage="{name}"}} {int(count)}',
                    f'trustrag_stage_seconds_sum{{stage="{name}"}} {total:.6f}',
                    f'trustrag_stage_seconds_max{{stage="{name}"}} {peak:.6f}',
                ]
        return "\n".join(lines) + "\n"


METRICS = Metrics()


@contextmanager
def stage(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a pipeline stage. Callers may add result fields (counts, scores) to the yielded dict."""
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    try:
        yield extra
    except Exception as error:
        seconds = time.perf_counter() - started
        METRICS.inc("stage_errors_total", stage=name)
        event(
            f"{name}.failed",
            logging.WARNING,
            duration_ms=round(seconds * 1000),
            error_type=type(error).__name__,
            **fields,
        )
        raise
    seconds = time.perf_counter() - started
    METRICS.observe(name, seconds)
    event(f"{name}.done", duration_ms=round(seconds * 1000), **fields, **extra)
