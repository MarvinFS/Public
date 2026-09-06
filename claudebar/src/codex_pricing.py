"""Codex/OpenAI model pricing for cost calculations."""

# USD per 1 million tokens.
# Source: LiteLLM model_prices_and_context_window.json (September 2026).
CODEX_PRICING = {
    "gpt-6-astra": {"input": 10.00, "output": 50.00, "cached_input": 1.00},
    "gpt-5.6-sol": {"input": 4.00, "output": 20.00, "cached_input": 0.40},
    "gpt-5.1-codex": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
    "gpt-5.1": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
    "gpt-5-codex": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
    "gpt-5": {"input": 1.25, "output": 10.00, "cached_input": 0.125},

    # Fallback for a model Codex ships before this table catches up.
    "default": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
}


def get_model_pricing(model: str) -> dict:
    """Get pricing for a model, falling back to default if unknown."""
    if model in CODEX_PRICING:
        return CODEX_PRICING[model]

    # Longest prefix wins, so "gpt-5.1-codex-2026-01-01" does not match "gpt-5".
    for known_model in sorted(CODEX_PRICING, key=len, reverse=True):
        if model.startswith(known_model):
            return CODEX_PRICING[known_model]

    return CODEX_PRICING["default"]


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Calculate cost in USD for token usage.

    Codex reports input_tokens inclusive of cached_input_tokens, and
    output_tokens inclusive of reasoning tokens (its own total_tokens is
    input_tokens + output_tokens). Every token is therefore charged once:
    the cached slice at the cache-read rate, the rest at the input rate.

    Args:
        model: Model name (e.g., "gpt-6-astra")
        input_tokens: Total input tokens, cached ones included
        output_tokens: Total output tokens, reasoning ones included
        cached_input_tokens: Cache-read subset of input_tokens

    Returns:
        Cost in USD
    """
    pricing = get_model_pricing(model)
    uncached_input = max(input_tokens - cached_input_tokens, 0)

    return (
        uncached_input / 1_000_000 * pricing["input"]
        + cached_input_tokens / 1_000_000 * pricing["cached_input"]
        + output_tokens / 1_000_000 * pricing["output"]
    )
