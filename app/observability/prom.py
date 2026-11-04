"""Prometheus metrics for the gateway tiers."""

from prometheus_client import Counter, Histogram

REQUESTS = Counter("gateway_requests_total", "Requests by answering tier", ["tier", "task_type"])
LATENCY = Histogram(
    "gateway_request_latency_seconds", "Request latency by answering tier", ["tier"],
    buckets=(0.005, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
TOKENS = Counter("gateway_llm_tokens_total", "Tokens sent to / produced by the LLM", ["direction"])
COST = Counter("gateway_cost_usd_total", "Estimated spend in USD")


def tier_for(model: str) -> str:
    if model.startswith("prefilter"):
        return "prefilter"
    if model == "cache":
        return "cache"
    return "agent"


def record(*, model: str, task_type: str, latency_ms: int, tokens_in: int,
           tokens_out: int, cost_usd: float) -> None:
    tier = tier_for(model)
    REQUESTS.labels(tier, task_type).inc()
    LATENCY.labels(tier).observe(latency_ms / 1000)
    if tier == "agent":
        TOKENS.labels("in").inc(tokens_in)
        TOKENS.labels("out").inc(tokens_out)
    COST.inc(cost_usd)
