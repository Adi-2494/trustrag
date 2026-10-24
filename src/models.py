"""
Domain models shared by the pipeline, the API and the UI.

TypedDicts rather than dataclasses: the same objects are cached as JSON, returned by the HTTP API and shown in
the UI, so keeping them as plain dicts avoids conversion code at every boundary, while mypy still checks every
field name and type (see docs/adr/0004-typeddict-domain-models.md).
"""

from typing import Literal, NotRequired

# typing_extensions.TypedDict: FastAPI/pydantic need it to build response schemas on Python < 3.12.
from typing_extensions import TypedDict

Risk = Literal["LOW", "MEDIUM", "HIGH"]
ClaimStatus = Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED"]


class Page(TypedDict):
    text: str
    file_name: str
    page: int
    ocr: bool


class Chunk(TypedDict):
    id: str
    text: str
    file_name: str
    page: int


class RetrievedChunk(Chunk):
    distance: float | None  # semantic distance; None when only keyword search found the chunk


class Claim(TypedDict):
    claim: str
    status: ClaimStatus
    quote: str
    file_name: str | None
    page: int | None
    reason: str
    quote_found: bool


class Verification(TypedDict):
    refused: bool
    claims: list[Claim]
    trust_score: float | None


class AIVerdict(TypedDict):
    verdict: Literal["SAFE", "SUSPICIOUS"]
    risk: Risk
    reason: str


class ScanFinding(TypedDict):
    chunk_id: str
    file_name: str
    page: int
    risk: Risk
    rules: list[str]
    reason: str
    text: str


class ScanReport(TypedDict):
    scanned: int
    ai_checked: int
    flagged: list[ScanFinding]
    llm_checked: bool
    excluded_risks: NotRequired[list[str]]


class UploadResult(TypedDict):
    file_name: str
    pages: int
    chunks_indexed: int
    chunks_removed: int
    report: ScanReport


class AskResult(TypedDict):
    question: str
    answer: str
    refused: bool
    chunks: list[RetrievedChunk]
    verification: Verification | None
    seconds: float
    request_id: str
