"""Log every LLM call with cost/latency. Swap print for Postgres/ClickHouse insert."""

from datetime import datetime, timezone

from app.observability import prom

def log_call(*, prompt_hash: str, model: str, task_type: str, tokens_in: int,
             tokens_out: int, latency_ms: int, cost_usd: float,
             cache_hit: bool, filtered: bool):
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "prompt_hash": prompt_hash,
        "model": model,
        "task_type": task_type,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "cache_hit": cache_hit,
        "filtered": filtered,
    }
    prom.record(model=model, task_type=task_type, latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)
    # TODO: INSERT into postgres/clickhouse
    print(f"[llm-gateway] {row}")
    return row
