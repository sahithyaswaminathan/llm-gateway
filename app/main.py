from fastapi import FastAPI, Header
from pydantic import BaseModel
from typing import Optional
import time

from app.gateway.prefilter import should_call_llm, rule_based_response
from app.gateway.cache import SemanticCache
from app.gateway.router import route_model, call_model
from app.observability.logger import log_call

app = FastAPI(title="Cost-Aware LLM Gateway")
cache = SemanticCache()

class CompleteRequest(BaseModel):
    prompt: str
    task_type: str = "general"  # triage, summarize, qa, general
    max_cost_usd: Optional[float] = 0.05
    user_id: Optional[str] = None

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/v1/complete")
def complete(req: CompleteRequest):
    start = time.time()
    prompt_hash = str(hash(req.prompt))

    # 1. Pre-filter: can we avoid LLM entirely?
    if not should_call_llm(req.prompt, req.task_type):
        resp = rule_based_response(req.prompt)
        log_call(
            prompt_hash=prompt_hash, model="rule-based", task_type=req.task_type,
            tokens_in=0, tokens_out=0, latency_ms=int((time.time()-start)*1000),
            cost_usd=0.0, cache_hit=False, filtered=True,
        )
        return {"response": resp, "model": "rule-based", "cost_usd": 0.0, "filtered": True}

    # 2. Semantic cache
    cached = cache.get(req.prompt)
    if cached:
        log_call(
            prompt_hash=prompt_hash, model="cache", task_type=req.task_type,
            tokens_in=0, tokens_out=0, latency_ms=int((time.time()-start)*1000),
            cost_usd=0.0, cache_hit=True, filtered=False,
        )
        return {"response": cached, "model": "cache", "cost_usd": 0.0, "cache_hit": True}

    # 3. Route to cheapest capable model
    model = route_model(req.task_type, req.max_cost_usd)
    response_text, tokens_in, tokens_out, cost = call_model(model, req.prompt)

    # 4. Cache and log
    cache.set(req.prompt, response_text)
    latency_ms = int((time.time() - start) * 1000)
    log_call(
        prompt_hash=prompt_hash, model=model, task_type=req.task_type,
        tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms,
        cost_usd=cost, cache_hit=False, filtered=False,
    )

    return {
        "response": response_text,
        "model": model,
        "cost_usd": round(cost, 6),
        "latency_ms": latency_ms,
    }
