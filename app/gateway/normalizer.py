"""Normalize raw log/error messages into a stable signature.

Real log lines differ in pod names, IPs, timestamps, and counters even when
they represent the *same* underlying error. Without normalization, an exact
or embedding-based cache rarely hits twice on production logs. This module
strips the volatile parts so near-duplicate errors map to the same entry in
the knowledge base.
"""

import hashlib
import re

_PATTERNS = [
    # timestamps: 2024-01-02T03:04:05.123Z or "2024-01-02 03:04:05"
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"), "<TS>"),
    # IPv4, optionally with port
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    # k8s-style pod/replicaset suffixes: api-7d9c4f6b9-x2vkd -> api-<POD>
    (re.compile(r"-[0-9a-f]{6,10}-[0-9a-z]{5}\b", re.I), "-<POD>"),
    # bare hex ids (container ids, hashes)
    (re.compile(r"\b[0-9a-f]{7,}\b", re.I), "<HEX>"),
    # any remaining standalone numbers (counters, ports, memory sizes, exit codes)
    (re.compile(r"\b\d+\b"), "<NUM>"),
    # collapse whitespace
    (re.compile(r"\s+"), " "),
]


def normalize(message: str) -> str:
    """Return a human-readable, template-like form of a log message."""
    msg = message.strip()
    for pattern, repl in _PATTERNS:
        msg = pattern.sub(repl, msg)
    return msg.strip().lower()


def signature(message: str) -> str:
    """Return a short stable id for the normalized message, used as a cache key."""
    return hashlib.sha256(normalize(message).encode("utf-8")).hexdigest()[:16]
