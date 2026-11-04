"""Debug agent: resolve unknown errors and write back to the knowledge base.

When an error reaches the LLM (pre-filter and cache both miss), the agent:
1. Calls the LLM to generate a resolution
2. Parses the error to extract type + context
3. Writes the resolution back to the semantic cache
4. Returns the resolution to the caller

This closes the loop: the next similar error hits the cache for free.
"""

from app.gateway.cache import SemanticCache
from app.gateway.error_parser import ErrorParser, ParsedError
from app.gateway.router import call_model
from app.gateway.runbooks import RunbookStore


class DebugAgent:
    """Resolve errors and auto-populate the knowledge base."""

    def __init__(
        self,
        cache: SemanticCache | None = None,
        runbooks: RunbookStore | None = None,
        parser: ErrorParser | None = None,
    ):
        self.cache = cache or SemanticCache()
        self.runbooks = runbooks or RunbookStore()
        self.parser = parser or ErrorParser()

    def resolve(self, error_message: str, model: str = "phi") -> tuple[str, ParsedError]:
        """Resolve an error and write back to the knowledge base.

        Returns (resolution_text, parsed_error).
        """
        # Parse the error to extract type + context
        parsed = self.parser.parse(error_message)

        # Call the LLM to generate a resolution
        prompt = self._build_prompt(error_message, parsed)
        resolution, tokens_in, tokens_out, cost = call_model(model, prompt)

        # Clean up the resolution (small models often repeat the prompt)
        resolution = self._clean_resolution(resolution, prompt)

        # Write back to the knowledge base
        self._write_back(error_message, parsed, resolution)

        return resolution, parsed

    def _clean_resolution(self, resolution: str, prompt: str) -> str:
        """Remove prompt echoes and keep only the useful diagnosis."""
        # If the response starts with the prompt, skip it
        if resolution.lower().startswith(prompt.lower()[:50]):
            resolution = resolution[len(prompt) :].strip()
        
        # Take only the first paragraph (up to two newlines)
        paragraphs = resolution.split("\n\n")
        if paragraphs:
            resolution = paragraphs[0].strip()
        
        # Limit to 500 chars to avoid storing huge responses
        if len(resolution) > 500:
            resolution = resolution[:500].rsplit(" ", 1)[0] + "..."
        
        return resolution or "Unable to generate resolution."

    def _build_prompt(self, error_message: str, parsed: ParsedError) -> str:
        """Build a prompt that includes error context for better resolution."""
        context_str = ""
        if parsed.context:
            context_str = "\nContext: " + ", ".join(
                f"{k}={v}" for k, v in parsed.context.items()
            )

        return f"""You are a debugging expert. Given this error, provide a concise root-cause hypothesis and the single most useful next debugging step.

Source: {parsed.source}
Error Type: {parsed.error_type}
{context_str}

Error Message:
{error_message}

Provide a one-paragraph diagnosis and one actionable next step."""

    def _write_back(self, error_message: str, parsed: ParsedError, resolution: str) -> None:
        """Store the resolution in the semantic cache.

        Runbooks stay human-curated: auto-generated regexes were too broad and
        would answer every error of a class with one model-written resolution.
        """
        self.cache.set(error_message, resolution)
