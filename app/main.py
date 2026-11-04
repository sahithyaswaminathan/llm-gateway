import hashlib
import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from typing import Optional

from app.gateway.prefilter import Prefilter
from app.gateway.cache import SemanticCache
from app.gateway.router import route_model, call_model
from app.agent.debug_agent import DebugAgent
from app.observability.logger import log_call

app = FastAPI(title="Cost-Aware LLM Gateway")
prefilter = Prefilter()
cache = SemanticCache()
agent = DebugAgent(cache=cache, runbooks=prefilter.runbooks)


class CompleteRequest(BaseModel):
    prompt: str
    task_type: str = "general"  # triage, summarize, qa, general
    max_cost_usd: Optional[float] = 0.05
    user_id: Optional[str] = None


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/v1/complete")
def complete(req: CompleteRequest):
    start = time.time()
    # Python's hash() is salted per process; use a stable digest instead.
    prompt_hash = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()

    # 1. Pre-filter: runbook or trivial rule can answer without the LLM?
    needs_llm, resolution, source = prefilter.check(req.prompt)
    if not needs_llm:
        model = f"prefilter:{source}"
        log_call(
            prompt_hash=prompt_hash, model=model, task_type=req.task_type,
            tokens_in=0, tokens_out=0, latency_ms=int((time.time()-start)*1000),
            cost_usd=0.0, cache_hit=False, filtered=True,
        )
        return {"response": resolution, "model": model, "source": source,
                "cost_usd": 0.0, "filtered": True}

    # 2. Semantic knowledge base (normalized + embedded)
    cached = cache.get(req.prompt)
    if cached:
        log_call(
            prompt_hash=prompt_hash, model="cache", task_type=req.task_type,
            tokens_in=0, tokens_out=0, latency_ms=int((time.time()-start)*1000),
            cost_usd=0.0, cache_hit=True, filtered=False,
        )
        return {"response": cached, "model": "cache", "cost_usd": 0.0, "cache_hit": True}

    # 3. Agent resolves the error and writes back to the knowledge base
    response_text, parsed_error = agent.resolve(req.prompt, model=route_model(req.task_type, req.max_cost_usd))
    
    # Estimate tokens (agent doesn't return them; use text length)
    tokens_in = len(req.prompt.split())
    tokens_out = len(response_text.split())
    cost = 0.0  # open-source models are free
    
    latency_ms = int((time.time() - start) * 1000)
    log_call(
        prompt_hash=prompt_hash, model="agent", task_type=req.task_type,
        tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms,
        cost_usd=cost, cache_hit=False, filtered=False,
    )

    return {
        "response": response_text,
        "model": "agent",
        "error_source": parsed_error.source,
        "error_type": parsed_error.error_type,
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
    }
