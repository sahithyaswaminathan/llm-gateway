"""Decide which model to use based on task type and cost budget."""

MODEL_PRICES = {
    # $ per 1k tokens (input, output) — update with current pricing
    "claude-3-5-sonnet-20240620": (0.003, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "local-small": (0.0, 0.0),
}

TASK_MODEL_PREFERENCE = {
    "triage": ["gpt-4o-mini", "claude-3-5-sonnet-20240620"],
    "summarize": ["gpt-4o-mini", "claude-3-5-sonnet-20240620"],
    "qa": ["claude-3-5-sonnet-20240620", "gpt-4o-mini"],
    "general": ["gpt-4o-mini", "claude-3-5-sonnet-20240620"],
}

def route_model(task_type: str, max_cost_usd: float | None) -> str:
    prefs = TASK_MODEL_PREFERENCE.get(task_type, TASK_MODEL_PREFERENCE["general"])
    # Pick cheapest preferred model; if budget is very tight, prefer cheapest
    if max_cost_usd is not None and max_cost_usd < 0.005:
        return "gpt-4o-mini"
    return prefs[0]

def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = MODEL_PRICES.get(model, (0.003, 0.015))
    return (tokens_in / 1000) * p_in + (tokens_out / 1000) * p_out

def call_model(model: str, prompt: str) -> tuple[str, int, int, float]:
    """
    Stub: replace with real Anthropic/OpenAI calls.
    Returns (response_text, tokens_in, tokens_out, cost_usd).
    """
    # TODO: implement httpx calls to Anthropic / OpenAI with retries + fallback
    tokens_in = len(prompt.split()) * 2  # rough estimate
    tokens_out = 150
    response_text = f"[stub:{model}] Response to: {prompt[:120]}..."
    cost = estimate_cost(model, tokens_in, tokens_out)
    return response_text, tokens_in, tokens_out, cost
