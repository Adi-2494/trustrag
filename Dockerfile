# TrustRAG - production container (multi-architecture: linux/amd64 + linux/arm64)
# Build (this machine):   docker build -t trustrag .
# Build (both chip types): docker buildx build --platform linux/amd64,linux/arm64 -t <user>/trustrag:latest --push .
# Run (only reachable from this computer):
#   docker run --rm --env-file .env -p 127.0.0.1:7860:7860 \
#     -v "$HOME/.trustrag/claude_token:/run/secrets/claude_code_oauth_token:ro" trustrag
# Secrets are passed at runtime and are never copied into the image. Mounting the token as a file keeps it
# out of `docker inspect`. Set APP_PASSWORD in .env before exposing the app beyond 127.0.0.1.

# Stage 1: official Node.js image, used only to borrow its small, ready-made Node binary.
FROM node:22-bookworm-slim AS node

# Stage 2: the actual app image. Both stages are Debian "bookworm", so the Node binary is compatible.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    CHROMA_DIR=/app/chroma_db

# Node.js + Claude Code CLI: lets the app use a Claude subscription (LLM_PROVIDER=claude_code).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @anthropic-ai/claude-code@2.1.310 \
    && npm cache clean --force \
    && claude --version

# Run as a normal user, not root (safer).
RUN useradd --create-home appuser
WORKDIR /app
# The app folder (including the database folder) must belong to appuser, so it can save documents.
RUN mkdir -p /app/chroma_db && chown -R appuser:appuser /app

# Install Python libraries first, so Docker can cache this layer between code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

USER appuser

# Download the local embedding model (~80 MB) at build time, so the first question is fast.
# Retries on slow networks; if it still fails, the build continues and the app downloads it on first use.
RUN for attempt in 1 2 3 4 5; do \
        python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm up'])" \
        && echo "Embedding model ready" && break; \
        echo "Download attempt $attempt failed, retrying..."; rm -rf /home/appuser/.cache/chroma; sleep 5; \
    done; true

COPY --chown=appuser:appuser . .

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7860/_stcore/health')" || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]
