"""
Recorder for the P6 anonymous-mode golden file (`tests/data/p6_anonymous_golden.json`, P6-AC3, SPEC-GAP-042).

HOW THE GOLDEN FILE WAS GENERATED
    The file was recorded from the pre-P6 code (v1.0 plus the P5 eval work; no P6 production code existed) with
        .venv/bin/python -m tests.p6_golden --record
    `--record` refuses to overwrite an existing file, so a later phase cannot silently re-record it from changed code.
    `--print` shows what the current code would record (used to prove determinism and to diff against the file).

WHAT IT RECORDS (all through `fastapi.testclient.TestClient` + `FakeStore` + `FakeLLM`, never Claude or the network)
    anonymous_flows  status, selected headers and body of anonymous v1.0 API traffic. Must be identical with
                     ACCOUNTS_ENABLED false AND true.
    disabled_only    behaviour of paths that P6 adds (`/v1/auth/*`, `GET /v1/workspaces`) and of the `Authorization`
                     header. Must match v1.0 only while ACCOUNTS_ENABLED is false.
    openapi          the existing operations and the component schemas they use. P6 may only ADD to these.

Volatile values are normalised: workspace ids -> "<WS>", `seconds` -> "<SECONDS>", `request_id` -> "<RID>";
headers `x-request-id`, `content-length`, `date`, `server` are not recorded (SPEC-GAP-042: "except X-Request-ID").
`collect(accounts_enabled=None)` does not touch the flag at all (used for the pre-P6 recording, where the flag does
not exist); `True` / `False` set `config.ACCOUNTS_ENABLED` for the duration of the call.
"""

import argparse
import io
import json
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

GOLDEN_PATH = Path(__file__).parent / "data" / "p6_anonymous_golden.json"
_SKIP_HEADERS = {"x-request-id", "content-length", "date", "server"}
_FIXED_ACCT_ID = "acct-" + "ab" * 16


def make_pdf(*lines: str) -> bytes:
    """A deterministic PDF (reportlab `invariant=1` fixes the creation date and the document id)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)
    y = 800
    for line in lines:
        pdf.drawString(50, y, line)
        y -= 20
    pdf.save()
    return buffer.getvalue()


HANDBOOK = make_pdf(
    "Leave policy",
    "Full-time employees receive 25 days of paid annual leave per year.",
    "Unused days may be carried over until 31 March.",
)
TRAVEL = make_pdf("Travel policy", "Flights above 6 hours may be booked in business class.")


def _normalise(value: Any, ws_ids: set[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "seconds":
                out[key] = "<SECONDS>"
            elif key == "request_id" and item != "trace-1":
                out[key] = "<RID>"
            else:
                out[key] = _normalise(item, ws_ids)
        return out
    if isinstance(value, list):
        return [_normalise(item, ws_ids) for item in value]
    if isinstance(value, str):
        for ws in ws_ids:
            value = value.replace(ws, "<WS>")
        return value
    return value


class _Recorder:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.ws_ids: set[str] = set()
        self.steps: list[dict[str, Any]] = []

    def call(self, name: str, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        try:
            body: Any = response.json() if response.content else None
        except ValueError:
            body = {"non_json_length_class": "short" if len(response.content) < 100 else "long"}
        if name.startswith("create") and isinstance(body, dict) and "workspace" in body:
            self.ws_ids.add(body["workspace"])
        headers = {k.lower(): v for k, v in response.headers.items() if k.lower() not in _SKIP_HEADERS}
        shown_path = path
        for ws in self.ws_ids:
            shown_path = shown_path.replace(ws, "<WS>")
        self.steps.append(
            {
                "name": name,
                "request": f"{method} {shown_path}",
                "status": response.status_code,
                "headers": headers,
                "body": _normalise(body, self.ws_ids),
                "x_request_id_echoed": kwargs.get("headers", {}).get("X-Request-ID") == response.headers["x-request-id"]
                if "X-Request-ID" in kwargs.get("headers", {})
                else None,
            }
        )
        return response


def _anonymous_flows(client: Any) -> list[dict[str, Any]]:
    from src import config

    r = _Recorder(client)
    r.call("health", "GET", "/health")
    ws = r.call("create", "POST", "/v1/workspaces").json()["workspace"]
    docs, questions = f"/v1/workspaces/{ws}/documents", f"/v1/workspaces/{ws}/questions"
    r.call("docs_empty", "GET", docs)
    r.call("ask_empty_workspace_409", "POST", questions, json={"question": "How much leave?"})
    r.call("ask_blank_422", "POST", questions, json={"question": ""})
    r.call("ask_too_long_422", "POST", questions, json={"question": "q" * 501})
    r.call("ask_missing_body_422", "POST", questions, json={})
    r.call("upload", "POST", docs, files={"file": ("handbook.pdf", HANDBOOK, "application/pdf")})
    r.call(
        "upload_also_remove",
        "POST",
        docs,
        files={"file": ("travel.pdf", TRAVEL, "application/pdf")},
        data={"also_remove": "MEDIUM,low,bogus"},
    )
    r.call("upload_not_a_pdf_422", "POST", docs, files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    r.call("upload_no_file_422", "POST", docs)
    r.call("docs_after_upload", "GET", docs)
    r.call("ask", "POST", questions, json={"question": "How much annual leave?"}, headers={"X-Request-ID": "trace-1"})
    for label, bad in (
        ("unknown_hex", "0" * 32),
        ("malformed", "not-a-real-id"),
        ("uppercase_hex", "A" * 32),
        ("acct_form", _FIXED_ACCT_ID),
    ):
        r.call(f"docs_{label}_404", "GET", f"/v1/workspaces/{bad}/documents")
        r.call(
            f"upload_{label}_404",
            "POST",
            f"/v1/workspaces/{bad}/documents",
            files={"file": ("handbook.pdf", HANDBOOK, "application/pdf")},
        )
        r.call(f"ask_{label}_404", "POST", f"/v1/workspaces/{bad}/questions", json={"question": "x"})
        if label != "acct_form":  # P6 deliberately answers DELETE of an unowned account id with 404 (v1.0: 204)
            r.call(f"delete_{label}", "DELETE", f"/v1/workspaces/{bad}")
    r.call("delete", "DELETE", f"/v1/workspaces/{ws}")
    r.call("docs_after_delete", "GET", docs)
    r.call("put_workspaces_405", "PUT", "/v1/workspaces")
    r.call("root_404", "GET", "/")
    with mock.patch.object(config, "MAX_UPLOAD_MB", 1):
        ws2 = r.call("create_for_413", "POST", "/v1/workspaces").json()["workspace"]
        big = b"\0" * (1024 * 1024 + 1)
        r.call("upload_too_large_413", "POST", f"/v1/workspaces/{ws2}/documents", files={"file": ("big.pdf", big, "x")})
    with mock.patch.object(config, "API_KEY", "golden-key"):
        r.call("api_key_missing_401", "POST", "/v1/workspaces")
        r.call("api_key_wrong_401", "POST", "/v1/workspaces", headers={"X-API-Key": "wrong"})
        r.call("create_with_api_key", "POST", "/v1/workspaces", headers={"X-API-Key": "golden-key"})
        r.call("health_open_with_api_key", "GET", "/health")
        r.call("metrics_needs_key_401", "GET", "/metrics")
    metrics = client.get("/metrics")
    r.steps.append({"name": "metrics", "status": metrics.status_code, "content_type": metrics.headers["content-type"]})
    return r.steps


def _disabled_only(client: Any) -> list[dict[str, Any]]:
    """Paths P6 adds, and the Authorization header. v1.0 answers for them only while accounts are disabled."""
    r = _Recorder(client)
    r.call("list_workspaces_405", "GET", "/v1/workspaces")
    r.call("login_404", "POST", "/v1/auth/login", json={"username": "alice", "password": "x" * 12})
    r.call("login_get_404", "GET", "/v1/auth/login")
    r.call("logout_404", "POST", "/v1/auth/logout")
    r.call("me_404", "GET", "/v1/auth/me")
    garbage = {"Authorization": "Bearer not-a-real-token"}
    r.call("create_with_garbage_bearer", "POST", "/v1/workspaces", headers=garbage)
    ws = r.call("create", "POST", "/v1/workspaces").json()["workspace"]
    r.call("docs_with_garbage_bearer", "GET", f"/v1/workspaces/{ws}/documents", headers=garbage)
    r.call(
        "upload_with_garbage_bearer",
        "POST",
        f"/v1/workspaces/{ws}/documents",
        files={"file": ("handbook.pdf", HANDBOOK, "application/pdf")},
        headers=garbage,
    )
    r.call(
        "ask_with_garbage_bearer",
        "POST",
        f"/v1/workspaces/{ws}/questions",
        json={"question": "annual leave?"},
        headers=garbage,
    )
    r.call("docs_with_basic_auth", "GET", f"/v1/workspaces/{ws}/documents", headers={"Authorization": "Basic YTpi"})
    r.call("acct_with_garbage_bearer_404", "GET", f"/v1/workspaces/{_FIXED_ACCT_ID}/documents", headers=garbage)
    r.call("delete_with_garbage_bearer", "DELETE", f"/v1/workspaces/{ws}", headers=garbage)
    return r.steps


_EXISTING_OPERATIONS = {
    "/health": ["get"],
    "/metrics": ["get"],
    "/v1/workspaces": ["post"],
    "/v1/workspaces/{workspace}/documents": ["get", "post"],
    "/v1/workspaces/{workspace}/questions": ["post"],
    "/v1/workspaces/{workspace}": ["delete"],
}


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value.rsplit("/", 1)[-1]
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _openapi(app: Any) -> dict[str, Any]:
    spec = json.loads(json.dumps(app.openapi()))  # deep copy; FastAPI caches the dict
    paths = {p: {m: spec["paths"][p][m] for m in methods} for p, methods in _EXISTING_OPERATIONS.items()}
    names: set[str] = set()
    pending = list(_refs(paths))
    while pending:
        name = pending.pop()
        if name not in names:
            names.add(name)
            pending.extend(_refs(spec["components"]["schemas"][name]))
    return {
        "info": spec["info"],
        "paths": paths,
        "components": {n: spec["components"]["schemas"][n] for n in sorted(names)},
    }


@contextmanager
def _patched(accounts_enabled: bool | None) -> Iterator[Any]:
    from api import main
    from src import config
    from src.service import TrustRAGService
    from tests.fakes import FakeLLM, FakeStore

    with (
        tempfile.TemporaryDirectory() as tmp,
        mock.patch.object(config, "STATE_DB", str(Path(tmp) / "state.sqlite")),
        mock.patch.object(main, "service", TrustRAGService(store=FakeStore(), llm=FakeLLM())),
    ):
        if accounts_enabled is None:
            yield main
        else:
            with mock.patch.object(config, "ACCOUNTS_ENABLED", accounts_enabled, create=True):
                yield main


def collect(*, accounts_enabled: bool | None = None) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    with _patched(accounts_enabled) as main:
        flows = _anonymous_flows(TestClient(main.app))
    with _patched(accounts_enabled) as main:
        disabled_only = _disabled_only(TestClient(main.app))
        openapi = _openapi(main.app)
    return {"anonymous_flows": flows, "disabled_only": disabled_only, "openapi": openapi}


def load_golden() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="write the golden file (refuses to overwrite)")
    mode.add_argument("--print", action="store_true", help="print what the current code produces")
    args = parser.parse_args(argv)
    text = json.dumps(collect(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if args.print:
        sys.stdout.write(text)
        return 0
    if GOLDEN_PATH.exists():
        sys.stderr.write(f"{GOLDEN_PATH} exists; refusing to overwrite a recording.\n")
        return 1
    GOLDEN_PATH.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
