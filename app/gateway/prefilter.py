"""Pre-filter: avoid LLM calls for duplicates, health-checks, or rule-handlable inputs."""

import re

DEDUP_TTL_HINT = "deduplicate identical prompts within a short window"

def should_call_llm(prompt: str, task_type: str) -> bool:
    p = prompt.strip().lower()
    # Examples: skip LLM for empty, ping/health, or exact template matches
    if not p:
        return False
    if p in {"ping", "health", "test"}:
        return False
    # TODO: add your domain rules here, e.g.:
    # - if pod is in known-good state, return rule-based "healthy"
    # - if error matches known regex, return runbook link without LLM
    return True

def rule_based_response(prompt: str) -> str:
    return "Handled by rule-based pre-filter (no LLM call needed)."
