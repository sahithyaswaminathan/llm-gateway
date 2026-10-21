"""Semantic cache stub. Plug in pgvector / Qdrant for production."""

class SemanticCache:
    def __init__(self):
        self._store: dict[str, str] = {}

    def _key(self, prompt: str) -> str:
        # TODO: replace with embedding similarity lookup
        return prompt.strip().lower()[:200]

    def get(self, prompt: str) -> str | None:
        return self._store.get(self._key(prompt))

    def set(self, prompt: str, response: str) -> None:
        self._store[self._key(prompt)] = response
