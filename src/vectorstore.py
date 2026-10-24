"""
Steps 6-7 of the pipeline: store chunks in ChromaDB and find the most relevant ones.
ChromaDB turns text into embeddings (lists of numbers that capture meaning) using its
built-in all-MiniLM-L6-v2 model. It runs locally, is free and needs no API key.

Every user session gets its own private WORKSPACE (a separate ChromaDB collection), so users can never see,
search, delete or overwrite each other's documents. Workspaces unused for a while are deleted automatically.
"""

import contextlib
import re
import time
import uuid
from typing import Any, cast

import chromadb
import chromadb.errors
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from src import config
from src.models import Chunk, RetrievedChunk

_client: Any = None
_collections: dict[str, Any] = {}  # workspace id -> collection; opening a collection loads its index, so keep it open
_WORKSPACE_ID = re.compile(r"^[a-f0-9]{32}$")


def new_workspace_id() -> str:
    """A random, unguessable id for one user session."""
    return uuid.uuid4().hex


def _get_client() -> Any:
    global _client  # noqa: PLW0603 - one ChromaDB client per process
    if _client is None:
        _client = chromadb.PersistentClient(path=config.CHROMA_DIR)
    return _client


def _collection_name(workspace: str) -> str:
    if not _WORKSPACE_ID.match(workspace or ""):
        raise ValueError("Invalid workspace id")
    return f"ws_{workspace}"


def _get_collection(workspace: str) -> Any:
    """Open (or create) the collection that belongs to this workspace."""
    if workspace not in _collections:
        _collections[workspace] = _get_client().get_or_create_collection(
            name=_collection_name(workspace),
            embedding_function=DefaultEmbeddingFunction(),
            # "cosine" measures how similar two meanings are (0 = identical, 2 = opposite).
            configuration={"hnsw": {"space": "cosine"}},
            metadata={"created": time.time()},
        )
    return _collections[workspace]


def add_chunks(workspace: str, chunks: list[Chunk]) -> None:
    """Embed and save chunks. Chunks with an existing ID are replaced, not duplicated."""
    if not chunks:
        return
    _get_collection(workspace).upsert(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[{"file_name": c["file_name"], "page": c["page"]} for c in chunks],
    )


def retrieve(workspace: str, question: str, top_k: int = config.TOP_K) -> list[RetrievedChunk]:
    """Return the top_k chunks (from this workspace only) most similar in meaning to the question."""
    collection = _get_collection(workspace)
    if collection.count() == 0:
        return []

    results = collection.query(query_texts=[question], n_results=min(top_k, collection.count()))

    # Chroma returns parallel lists (one list per question). We asked one question, so take [0].
    chunks: list[RetrievedChunk] = []
    for chunk_id, text, meta, distance in zip(
        results["ids"][0], results["documents"][0], results["metadatas"][0], results["distances"][0], strict=True
    ):
        chunks.append(
            {
                "id": chunk_id,
                "text": text,
                "file_name": meta["file_name"],
                "page": int(meta["page"]),
                "distance": round(float(distance), 3),  # smaller = more similar
            }
        )
    return chunks


def delete_file(workspace: str, file_name: str) -> None:
    """Remove all chunks of one file in this workspace (used before re-adding it)."""
    _get_collection(workspace).delete(where={"file_name": file_name})


def all_chunks(workspace: str) -> list[Chunk]:
    """Every chunk in this workspace (used by keyword search, which scores the whole collection)."""
    data = _get_collection(workspace).get(include=["documents", "metadatas"])
    return [
        cast(Chunk, {"id": cid, "text": text, "file_name": meta["file_name"], "page": int(meta["page"])})
        for cid, text, meta in zip(data["ids"], data["documents"], data["metadatas"], strict=True)
    ]


def list_files(workspace: str) -> list[str]:
    """Names of all files in this workspace (for the sidebar)."""
    metadatas = _get_collection(workspace).get(include=["metadatas"])["metadatas"]
    return sorted({str(m["file_name"]) for m in metadatas})


def count(workspace: str) -> int:
    """How many chunks are stored in this workspace."""
    return int(_get_collection(workspace).count())


def clear(workspace: str) -> None:
    """Delete this workspace's documents (the 'Clear all documents' button). Other users are not affected."""
    with contextlib.suppress(chromadb.errors.NotFoundError, ValueError):  # nothing stored yet
        _get_client().delete_collection(_collection_name(workspace))
    _collections.pop(workspace, None)


def cleanup_old_workspaces(max_age_hours: float = config.WORKSPACE_MAX_AGE_HOURS) -> int:
    """Delete workspaces older than max_age_hours (left behind by closed browser tabs). Returns how many."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for collection in _get_client().list_collections():
        created = (collection.metadata or {}).get("created", 0)
        if collection.name.startswith("ws_") and created < cutoff:
            _get_client().delete_collection(collection.name)
            _collections.pop(collection.name[3:], None)
            removed += 1
    return removed


class ChromaStore:
    """The production `VectorStore` (see src/interfaces.py): a thin object wrapper around this module."""

    def new_workspace(self) -> str:
        return new_workspace_id()

    def add_chunks(self, workspace: str, chunks: list[Chunk]) -> None:
        add_chunks(workspace, chunks)

    def retrieve(self, workspace: str, question: str, top_k: int) -> list[RetrievedChunk]:
        return retrieve(workspace, question, top_k)

    def all_chunks(self, workspace: str) -> list[Chunk]:
        return all_chunks(workspace)

    def delete_file(self, workspace: str, file_name: str) -> None:
        delete_file(workspace, file_name)

    def list_files(self, workspace: str) -> list[str]:
        return list_files(workspace)

    def count(self, workspace: str) -> int:
        return count(workspace)

    def clear(self, workspace: str) -> None:
        clear(workspace)

    def cleanup(self) -> int:
        return cleanup_old_workspaces()
