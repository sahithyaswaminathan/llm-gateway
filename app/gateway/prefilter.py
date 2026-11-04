"""Pre-filter: avoid LLM calls for trivial or runbook-known inputs.

Tier 0 of the gateway. Checks are cheapest-first:
1. trivial requests (empty / ping / health) -> generic rule response
2. runbook match on the normalized text    -> known resolution, no LLM
3. everything else                         -> pass to the semantic cache tier
"""

from app.gateway.runbooks import RunbookStore

_TRIVIAL = {"", "ping", "health", "test"}
_TRIVIAL_RESPONSE = "No action needed (trivial request)."


class Prefilter:
    def __init__(self, runbooks: RunbookStore | None = None):
        self.runbooks = runbooks or RunbookStore()

    def check(self, prompt: str) -> tuple[bool, str | None, str | None]:
        """Return (needs_llm, resolution, source).

        needs_llm is False when the request can be answered without the LLM;
        then resolution holds the answer and source is "rule" or "runbook".
        """
        if prompt.strip().lower() in _TRIVIAL:
            return False, _TRIVIAL_RESPONSE, "rule"
        runbook = self.runbooks.match(prompt)
        if runbook is not None:
            return False, runbook, "runbook"
        return True, None, None
