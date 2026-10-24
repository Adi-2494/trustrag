# 0003 - Embedded ChromaDB with local embeddings

**Status:** Accepted

## Context
The product must run on a laptop with one command, keep document embeddings private, and isolate each user's
documents. Alternatives considered: pgvector (Postgres), a hosted vector database, FAISS.

## Decision
Embedded ChromaDB with its built-in all-MiniLM-L6-v2 ONNX embeddings, one collection per workspace, accessed only
through the `VectorStore` protocol (`src/interfaces.py`).

## Consequences
- Zero operations: no database server, no embedding API, embeddings never leave the machine.
- An embedded store is owned by one process. Running several API instances would require a server-based store
  (pgvector is the natural next step) - implemented as another `VectorStore`, with no pipeline changes.
- MiniLM is small and fast but English-centric; multilingual documents would need a different embedding model.

## Evidence
Retrieval on the evaluation corpus: recall@4 100%, MRR 0.97 with hybrid search (eval/results/RESULTS.md).
