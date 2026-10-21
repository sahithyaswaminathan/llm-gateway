"""Aggregate metrics for dashboard: cost per day, hit rates, p90 latency."""

def summarize(calls: list[dict]) -> dict:
    total = len(calls)
    if not total:
        return {}
    total_cost = sum(c["cost_usd"] for c in calls)
    cache_hits = sum(1 for c in calls if c["cache_hit"])
    filtered = sum(1 for c in calls if c["filtered"])
    latencies = sorted(c["latency_ms"] for c in calls)
    p90 = latencies[int(0.9 * len(latencies))]
    return {
        "total_calls": total,
        "total_cost_usd": round(total_cost, 4),
        "cache_hit_rate": round(cache_hits / total, 3),
        "filtered_rate": round(filtered / total, 3),
        "p90_latency_ms": p90,
        "avg_cost_per_call": round(total_cost / total, 6),
    }
