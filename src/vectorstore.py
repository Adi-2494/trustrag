"""
Steps 6-7 of the pipeline: store chunks in ChromaDB and find the most relevant ones.
ChromaDB turns text into embeddings (lists of numbers that capture meaning) using its
built-in all-MiniLM-L6-v2 model. It runs locally, is free and needs no API key.
"""

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from src import config

# Created once and reused.
_collection = None


def _get_collection():
    """Open (or create) the ChromaDB collection stored in the chroma_db/ folder."""
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=config.CHROMA_DIR)
        _collection = client.get_or_create_collection(
            name=config.COLLECTION_NAME,
            embedding_function=DefaultEmbeddingFunction(),
            # "cosine" measures how similar two meanings are (0 = identical, 2 = opposite).
            configuration={"hnsw": {"space": "cosine"}},
        )
    return _collection


def add_chunks(chunks: list[dict]) -> None:
    """Embed and save chunks. Chunks with an existing ID are replaced, not duplicated."""
    if not chunks:
        return
    _get_collection().upsert(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[{"file_name": c["file_name"], "page": c["page"]} for c in chunks],
    )


def retrieve(question: str, top_k: int = config.TOP_K) -> list[dict]:
    """Return the top_k chunks most similar in meaning to the question."""
    collection = _get_collection()
    if collection.count() == 0:
        return []

    results = collection.query(query_texts=[question], n_results=min(top_k, collection.count()))

    # Chroma returns parallel lists (one list per question). We asked one question, so take [0].
    chunks = []
    for chunk_id, text, meta, distance in zip(
        results["ids"][0], results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        chunks.append({
            "id": chunk_id,
            "text": text,
            "file_name": meta["file_name"],
            "page": meta["page"],
            "distance": round(distance, 3),  # smaller = more similar
        })
    return chunks


def list_files() -> list[str]:
    """Names of all files currently stored (for the sidebar)."""
    metadatas = _get_collection().get(include=["metadatas"])["metadatas"]
    return sorted({m["file_name"] for m in metadatas})


def count() -> int:
    """How many chunks are stored."""
    return _get_collection().count()


def clear() -> None:
    """Delete every stored chunk (the 'Clear all documents' button)."""
    global _collection
    client = chromadb.PersistentClient(path=config.CHROMA_DIR)
    client.delete_collection(config.COLLECTION_NAME)
    _collection = None  # will be re-created empty on next use
