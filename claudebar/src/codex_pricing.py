"""Codex/OpenAI model pricing for cost calculations."""

# Pricing per 1 million tokens (January 2026)
# Source: OpenAI pricing page + LiteLLM pricing dataset
CODEX_PRICING = {
    # GPT-4o series
    "gpt-4o": {"input": 2.50, "output": 10.00, "cached_input": 1.25},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00, "cached_input": 1.25},
    "gpt-4o-2024-08-06": {"input": 2.50, "output": 10.00, "cached_input": 1.25},
    "gpt-4o-2024-05-13": {"input": 5.00, "output": 15.00, "cached_input": 2.50},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached_input": 0.075},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "output": 0.60, "cached_input": 0.075},

    # GPT-4 Turbo
    "gpt-4-turbo": {"input": 10.00, "output": 30.00, "cached_input": 5.00},
    "gpt-4-turbo-2024-04-09": {"input": 10.00, "output": 30.00, "cached_input": 5.00},
    "gpt-4-1106-preview": {"input": 10.00, "output": 30.00, "cached_input": 5.00},
    "gpt-4-0125-preview": {"input": 10.00, "output": 30.00, "cached_input": 5.00},

    # GPT-4
    "gpt-4": {"input": 30.00, "output": 60.00, "cached_input": 15.00},
    "gpt-4-0613": {"input": 30.00, "output": 60.00, "cached_input": 15.00},

    # o1 reasoning models
    "o1": {"input": 15.00, "output": 60.00, "cached_input": 7.50},
    "o1-2024-12-17": {"input": 15.00, "output": 60.00, "cached_input": 7.50},
    "o1-preview": {"input": 15.00, "output": 60.00, "cached_input": 7.50},
    "o1-preview-2024-09-12": {"input": 15.00, "output": 60.00, "cached_input": 7.50},
    "o1-mini": {"input": 1.10, "output": 4.40, "cached_input": 0.55},
    "o1-mini-2024-09-12": {"input": 1.10, "output": 4.40, "cached_input": 0.55},

    # o3 reasoning models
    "o3-mini": {"input": 1.10, "output": 4.40, "cached_input": 0.55},
    "o3-mini-2025-01-31": {"input": 1.10, "output": 4.40, "cached_input": 0.55},

    # GPT-3.5 Turbo (legacy but still used)
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50, "cached_input": 0.25},
    "gpt-3.5-turbo-0125": {"input": 0.50, "output": 1.50, "cached_input": 0.25},
    "gpt-3.5-turbo-1106": {"input": 1.00, "output": 2.00, "cached_input": 0.50},

    # Default fallback (use gpt-4o pricing as conservative estimate)
    "default": {"input": 2.50, "output": 10.00, "cached_input": 1.25},
}


def get_model_pricing(model: str) -> dict:
    """Get pricing for a model, falling back to default if unknown."""
    # Try exact match first
    if model in CODEX_PRICING:
        return CODEX_PRICING[model]

    # Try base model name (strip date suffix)
    base_model = model.rsplit("-", 1)[0] if "-20" in model else model
    if base_model in CODEX_PRICING:
        return CODEX_PRICING[base_model]

    # Try prefix matching for variants
    for known_model in CODEX_PRICING:
        if model.startswith(known_model):
            return CODEX_PRICING[known_model]

    return CODEX_PRICING["default"]


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    """Calculate cost in USD for token usage.

    Args:
        model: Model name (e.g., "gpt-4o", "o1-mini")
        input_tokens: Number of non-cached input tokens
        output_tokens: Number of output tokens
        cached_input_tokens: Number of cached input tokens (discounted)
        reasoning_tokens: Number of reasoning tokens (charged as output)

    Returns:
        Cost in USD
    """
    pricing = get_model_pricing(model)

    # Calculate costs (pricing is per 1M tokens)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    cached_cost = (cached_input_tokens / 1_000_000) * pricing.get("cached_input", pricing["input"] * 0.5)
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    # Reasoning tokens are charged at output rate
    reasoning_cost = (reasoning_tokens / 1_000_000) * pricing["output"]

    return input_cost + cached_cost + output_cost + reasoning_cost
