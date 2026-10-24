"""
Central configuration.

Two kinds of values live here:
- deployment settings (provider, limits, paths, policy) come from the environment via `settings.Settings`,
  which validates them at import time, so a bad .env fails fast with a clear message;
- product tuning (chunk size, prompts' token limits, thresholds) are constants, chosen and justified by the
  evaluation suite (eval/results/RESULTS.md).
No secrets are stored here.
"""

import os
from typing import TypedDict

from dotenv import load_dotenv

from src.settings import load_settings

load_dotenv()  # API keys / tokens are read from the environment by llm.py and claude_code.py
SETTINGS = load_settings()

# ---------------------------------------------------------------------------
# Claude access
# ---------------------------------------------------------------------------
PROVIDER = SETTINGS.llm_provider
AWS_REGION = SETTINGS.aws_region


# "answer" writes the reply; "checker" does the cheap, high-volume jobs (fact-checking, scanning, OCR).
# effort None means "don't send the parameter" (Haiku 4.5 rejects it).
class ModelChoice(TypedDict):
    answer: str
    answer_effort: str | None
    checker: str
    checker_effort: str | None


MODELS: dict[str, ModelChoice] = {
    "claude_code": {
        "answer": "claude-sonnet-5-5",
        "answer_effort": "low",
        "checker": "claude-haiku-5-5",
        "checker_effort": "low",
    },
    "anthropic": {
        "answer": "claude-sonnet-5-5",
        "answer_effort": "low",
        "checker": "claude-haiku-5-5",
        "checker_effort": "low",
    },
    "bedrock": {
        "answer": "anthropic.claude-opus-5-5",
        "answer_effort": "low",
        "checker": "anthropic.claude-haiku-4-5",
        "checker_effort": None,
    },
}
ANSWER_MODEL = MODELS[PROVIDER]["answer"]
ANSWER_EFFORT = MODELS[PROVIDER]["answer_effort"]
CHECKER_MODEL = MODELS[PROVIDER]["checker"]
CHECKER_EFFORT = MODELS[PROVIDER]["checker_effort"]

# Thinking tokens count toward these limits, so they are generous enough to avoid truncated answers.
ANSWER_MAX_TOKENS = 4000
VERIFY_MAX_TOKENS = 3000
SCAN_MAX_TOKENS = 2000
MAX_RETRIES = 3
CLAUDE_CODE_TIMEOUT = 120  # seconds per subscription-mode call

# ---------------------------------------------------------------------------
# Chunking and retrieval (values chosen by the evaluation ablation)
# ---------------------------------------------------------------------------
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
CHUNK_STRATEGY = "fixed"  # "fixed" or "section" (heading-aware)
TOP_K = 4
RETRIEVAL_MODE = SETTINGS.retrieval_mode  # "semantic" or "hybrid" (semantic + BM25 keyword search)
CANDIDATES_FOR_RERANK = 12  # how many candidates hybrid search / the reranker choose from
RERANK = SETTINGS.rerank

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
CHROMA_DIR = SETTINGS.chroma_dir
STATE_DB = os.path.join(CHROMA_DIR, "trustrag_state.sqlite")  # caches and rate limits, shared across processes

# ---------------------------------------------------------------------------
# OCR for scanned PDFs
# ---------------------------------------------------------------------------
OCR_MIN_CHARS = 50  # a page with less extractable text than this is treated as scanned
OCR_MAX_PAGES = 50  # cost cap per file
OCR_MAX_TOKENS = 3000
OCR_WORKERS = 4

# ---------------------------------------------------------------------------
# Answers and trust score
# ---------------------------------------------------------------------------
REFUSAL_TEXT = "The documents do not contain this information."
TRUST_GREEN = 80
TRUST_ORANGE = 50

# ---------------------------------------------------------------------------
# Security and abuse limits
# ---------------------------------------------------------------------------
APP_PASSWORD = SETTINGS.app_password
API_KEY = SETTINGS.api_key
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60
MAX_CALLS_PER_HOUR = SETTINGS.max_calls_per_hour  # all users together - protects the plan / bill
MAX_QUESTIONS_PER_HOUR = SETTINGS.max_questions_per_hour
MAX_FILES_PER_SESSION = SETTINGS.max_files_per_session
MAX_UPLOAD_MB = 25  # keep in sync with .streamlit/config.toml
MAX_PDF_PAGES = 300
OCR_MAX_RENDER_PX = 1568
WORKSPACE_MAX_AGE_HOURS = 24
CLAUDE_TOKEN_FILE = SETTINGS.claude_token_file  # mounted secret keeps the token out of `docker inspect`
LOG_LEVEL = SETTINGS.log_level

# ---------------------------------------------------------------------------
# Injection scanner
# ---------------------------------------------------------------------------
SCAN_BATCH_SIZE = 10
SCAN_WORKERS = 4
SEAM_CHARS = 300  # characters each side of a chunk join checked for split attacks
DEFAULT_EXCLUDED_RISKS = SETTINGS.scan_excluded_risks  # always removed; users can only add stricter levels

# Prompt versions are logged with every call and stored with evaluation results, so a change in behaviour
# can be traced to the prompt change that caused it.
PROMPT_VERSIONS = {
    "answer": "answer-v3",
    "verify": "verify-v3",
    "scan": "scan-v3",
    "ocr": "ocr-v1",
    "rerank": "rerank-v1",
}
