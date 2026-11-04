"""Semantic knowledge base: normalized log signatures + embeddings in Chroma.

This replaces the exact-match stub with a real similarity lookup so that
near-duplicate errors (different pod names, timestamps, counters) hit the
knowledge base instead of falling through to the LLM every time.

Open-source, no external account: Chroma persists locally to disk and
embeddings are generated locally with sentence-transformers.
"""

import os

from app.gateway.normalizer import normalize, signature

_CHROMA_DIR = os.environ.get("VECTOR_DB_DIR", "./data/chroma")
_COLLECTION_NAME = "debug_knowledge_base"
# Chroma's default embedding space is cosine distance in [0, 2]; lower = more similar.
_DISTANCE_THRESHOLD = float(os.environ.get("CACHE_DISTANCE_THRESHOLD", "0.2"))


class SemanticCache:
    """Knowledge base of (normalized error signature -> resolution)."""

    def __init__(self):
        self._embedder = None
        self._collection = None

    def _ensure_ready(self):
        if self._collection is not None:
            return
        # NOTE: chromadb 0.5.5's telemetry call is broken against some posthog
        # versions and prints "Failed to send telemetry event ..." on stderr.
        # Harmless (no network effect, no exception raised) — not worth
        # pinning extra versions to silence; revisit if it becomes annoying.
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        import chromadb
        from sentence_transformers import SentenceTransformer

        self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=_CHROMA_DIR)
        self._collection = client.get_or_create_collection(
            _COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

    def _embed(self, prompt: str) -> list[float]:
        return self._embedder.encode(normalize(prompt)).tolist()

    def get(self, prompt: str) -> str | None:
        """Return a cached resolution if a similar error has been seen before."""
        self._ensure_ready()
        if self._collection.count() == 0:
            return None
        result = self._collection.query(query_embeddings=[self._embed(prompt)], n_results=1)
        distances = result.get("distances") or [[]]
        documents = result.get("documents") or [[]]
        if not distances[0] or distances[0][0] > _DISTANCE_THRESHOLD:
            return None
        return documents[0][0]

    def set(self, prompt: str, response: str) -> None:
        """Write a new (or updated) resolution into the knowledge base."""
        self._ensure_ready()
        sig = signature(prompt)
        self._collection.upsert(
            ids=[sig],
            embeddings=[self._embed(prompt)],
            documents=[response],
            metadatas=[{"normalized": normalize(prompt)}],
        )
