"""
Vector store selection (ADR 0013).

One place decides which `VectorStore` the process uses, so the service and the eval harness agree. The server
implementation is imported lazily: local mode must never import psycopg.
"""

from src import config
from src.interfaces import VectorStore


def make_vector_store() -> VectorStore:
    if config.VECTOR_STORE == "chroma":
        from src.vectorstore import ChromaStore  # lazy: ChromaDB is heavy and fakes don't need it

        return ChromaStore()
    if config.VECTOR_STORE == "pgvector":
        from src.pgvector_store import PgVectorStore

        return PgVectorStore()
    raise ValueError("Unknown vector store setting; use 'chroma' or 'pgvector'.")
