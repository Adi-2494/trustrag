"""
Record real Claude responses once, replay them in tests.

Responses are keyed by what makes them unique, not by the exact prompt bytes, so tiny, harmless differences
between machines (for example the order of equally-ranked search results) don't break the replay:
- answer:  the question;            verify: the answer being checked;
- scan:    the chunk texts in the batch (the random tag names are ignored);
- other:   the whole prompt.
OCR is not replayed through the client at all: rendered page images differ by a few bytes between platforms,
so the recorded OCR *results* are seeded into the app's OCR cache instead (keyed by file hash and page).
"""

import hashlib
import json
import pathlib
import re
from typing import Any

from src.llm import LLMClient


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def response_key(purpose: str, prompt: Any) -> str:
    text = prompt if isinstance(prompt, str) else json.dumps(prompt, sort_keys=True)
    if purpose == "answer":
        match = re.search(r"\nQuestion: (.*)\Z", text, re.S)
        return f"answer:{_digest(match.group(1) if match else text)}"
    if purpose == "verify":
        match = re.search(r"<answer>\n(.*)\n</answer>", text, re.S)
        return f"verify:{_digest(match.group(1) if match else text)}"
    if purpose == "scan":
        chunks = re.findall(r'<chunk-[0-9a-f]+ id="\d+">\n(.*?)\n</chunk-', text, re.S)
        return f"scan:{_digest(json.dumps(chunks))}"
    return f"{purpose}:{_digest(text)}"


class RecordingLLM:
    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.responses: dict[str, str] = {}

    def __call__(
        self,
        model: str,
        system: str,
        prompt: Any,
        max_tokens: int,
        effort: str | None = None,
        *,
        purpose: str = "other",
    ) -> str:
        reply = self.inner(model, system, prompt, max_tokens, effort, purpose=purpose)
        self.responses[response_key(purpose, prompt)] = reply
        return reply

    def save(self, path: pathlib.Path, ocr_cache: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        responses = {k: v for k, v in self.responses.items() if not k.startswith("ocr:")}
        data = {"responses": responses, "ocr_cache": ocr_cache}
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ReplayLLM:
    def __init__(self, path: pathlib.Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self.responses: dict[str, str] = data["responses"]
        self.ocr_cache: dict[str, Any] = data["ocr_cache"]
        self.used: set[str] = set()

    def __call__(
        self,
        model: str,
        system: str,
        prompt: Any,
        max_tokens: int,
        effort: str | None = None,
        *,
        purpose: str = "other",
    ) -> str:
        key = response_key(purpose, prompt)
        if key not in self.responses:
            raise AssertionError(
                f"No recorded response for {key} - a prompt or input changed. "
                "Re-record with: python -m scripts.record_cassette"
            )
        self.used.add(key)
        return self.responses[key]
