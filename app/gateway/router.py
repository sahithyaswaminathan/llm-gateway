"""Route requests to the cheapest capable open-source model.

Supports:
- Ollama (local server at http://localhost:11434)
- Hugging Face Transformers (fallback if Ollama unavailable)

No API keys needed; all models run locally.
"""

import os
from typing import Optional

import httpx

_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "mistral")
_CHEAP_MODEL = os.environ.get("CHEAP_MODEL", "phi")

# Model pricing (for cost estimation; local models are free to run)
MODEL_PRICES = {
    "mistral": (0.0, 0.0),
    "phi": (0.0, 0.0),
    "neural-chat": (0.0, 0.0),
    "orca-mini": (0.0, 0.0),
}

TASK_MODEL_PREFERENCE = {
    "triage": ["phi", "mistral"],
    "summarize": ["phi", "mistral"],
    "qa": ["mistral", "phi"],
    "general": ["phi", "mistral"],
}


def route_model(task_type: str, max_cost_usd: Optional[float] = None) -> str:
    """Pick the cheapest preferred model for the task."""
    prefs = TASK_MODEL_PREFERENCE.get(task_type, TASK_MODEL_PREFERENCE["general"])
    return prefs[0]


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Local models are free; return 0."""
    return 0.0


def call_model(model: str, prompt: str) -> tuple[str, int, int, float]:
    """Call an open-source model via Ollama or Hugging Face.

    Returns (response_text, tokens_in, tokens_out, cost_usd).
    """
    try:
        return _call_ollama(model, prompt)
    except Exception as e:
        # Fallback to Hugging Face if Ollama fails
        return _call_huggingface(model, prompt)


def _call_ollama(model: str, prompt: str) -> tuple[str, int, int, float]:
    """Call Ollama server (must be running at OLLAMA_URL)."""
    with httpx.Client(timeout=300) as client:
        resp = client.post(
            f"{_OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
        )
        resp.raise_for_status()
        data = resp.json()
        response_text = data.get("response", "")
        # Ollama doesn't return token counts; estimate from text length
        tokens_in = len(prompt.split())
        tokens_out = len(response_text.split())
        return response_text, tokens_in, tokens_out, 0.0


def _call_huggingface(model: str, prompt: str) -> tuple[str, int, int, float]:
    """Fallback: load a Hugging Face model locally (slower, but no server needed)."""
    try:
        from transformers import pipeline
    except ImportError:
        raise RuntimeError(
            "Hugging Face transformers not installed. "
            "Install with: pip install transformers torch, or run Ollama."
        )

    # Use a small, fast model for fallback
    generator = pipeline("text-generation", model="gpt2", device=-1)
    outputs = generator(prompt, max_length=200, do_sample=True, top_p=0.9)
    response_text = outputs[0]["generated_text"][len(prompt) :].strip()
    tokens_in = len(prompt.split())
    tokens_out = len(response_text.split())
    return response_text, tokens_in, tokens_out, 0.0
