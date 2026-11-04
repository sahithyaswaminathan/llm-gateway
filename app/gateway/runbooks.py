"""Runbook store: known error patterns answered with no LLM call.

Backed by a YAML file (default: runbooks.yaml at the repo root). Patterns are
matched against the *normalized* log text, so one entry covers every variant
of an error regardless of pod names, timestamps, or counters.
"""

import os
import re
from pathlib import Path

import yaml

from app.gateway.normalizer import normalize

_DEFAULT_PATH = os.environ.get("RUNBOOKS_PATH", "runbooks.yaml")


class RunbookStore:
    def __init__(self, path: str = _DEFAULT_PATH):
        self.path = Path(path)
        self._raw: list[dict] = []
        self._entries: list[tuple[re.Pattern, str]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = yaml.safe_load(self.path.read_text()) or []
        self._raw = data
        self._entries = [
            (re.compile(entry["pattern"], re.I), entry["resolution"]) for entry in data
        ]

    def match(self, prompt: str) -> str | None:
        """Return the runbook resolution for a known error, else None."""
        text = normalize(prompt)
        for pattern, resolution in self._entries:
            if pattern.search(text):
                return resolution
        return None

    def add(self, pattern: str, resolution: str) -> None:
        """Register a new runbook and persist it (agent write-back path)."""
        self._raw.append({"pattern": pattern, "resolution": resolution})
        self._entries.append((re.compile(pattern, re.I), resolution))
        self._save()

    def _save(self) -> None:
        self.path.write_text(yaml.safe_dump(self._raw, sort_keys=False))
