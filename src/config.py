"""
All settings for TrustRAG live here, so they are easy to find and change.
Think of this like a Java class full of `public static final` constants.
No secrets are stored here - keys are read from the .env file by llm.py.
"""

import os

from dotenv import load_dotenv

# Read the .env file (if it exists) into environment variables.
load_dotenv()

# ---------------------------------------------------------------------------
# Which service we use to reach Claude: "anthropic" (direct API) or "bedrock" (AWS)
# ---------------------------------------------------------------------------
PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Model names differ per provider. "answer" writes the reply, "checker" does the
# cheap checking jobs (fact-checking, claim extraction, injection scanning).
# "effort" controls how hard the model thinks; None means "don't send it"
# (Haiku 4.5 does not support the effort setting).
MODELS = {
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

if PROVIDER not in MODELS:
    raise ValueError(f"LLM_PROVIDER must be 'anthropic' or 'bedrock', got '{PROVIDER}'")

ANSWER_MODEL = MODELS[PROVIDER]["answer"]
ANSWER_EFFORT = MODELS[PROVIDER]["answer_effort"]
CHECKER_MODEL = MODELS[PROVIDER]["checker"]
CHECKER_EFFORT = MODELS[PROVIDER]["checker_effort"]

# ---------------------------------------------------------------------------
# Token limits (max length of each reply). Thinking also counts toward these,
# so they are a bit generous to avoid cut-off answers.
# ---------------------------------------------------------------------------
ANSWER_MAX_TOKENS = 4000
VERIFY_MAX_TOKENS = 3000
SCAN_MAX_TOKENS = 2000

# How many times to retry when the API is busy (rate limit / overloaded).
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Chunking and retrieval
# ---------------------------------------------------------------------------
CHUNK_SIZE = 800       # characters per chunk
CHUNK_OVERLAP = 150    # characters shared between neighbouring chunks
TOP_K = 4              # how many chunks to retrieve per question

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
CHROMA_DIR = "chroma_db"                              # folder for the vector database
COLLECTION_NAME = "trustrag_docs"                     # like a table name inside ChromaDB
SCAN_CACHE_FILE = os.path.join(CHROMA_DIR, "scan_cache.json")  # saved injection-scan results

# ---------------------------------------------------------------------------
# Answer generation
# ---------------------------------------------------------------------------
REFUSAL_TEXT = "The documents do not contain this information."

# ---------------------------------------------------------------------------
# Trust score colours (percent)
# ---------------------------------------------------------------------------
TRUST_GREEN = 80   # >= 80% is green
TRUST_ORANGE = 50  # 50-79% is orange, below 50% is red

# ---------------------------------------------------------------------------
# Injection scanner
# ---------------------------------------------------------------------------
SCAN_BATCH_SIZE = 10                 # chunks per Haiku call
DEFAULT_EXCLUDED_RISKS = ["HIGH"]    # flagged chunks with these risk levels are left out
