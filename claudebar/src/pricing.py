"""Claude model pricing tables and cost calculations."""

from dataclasses import dataclass

from models import TokenUsage


@dataclass
class ModelPricing:
    """Pricing for a Claude model (per million tokens)."""
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_creation_per_mtok: float


# USD per million tokens, September 2026.
# Source: Anthropic pricing, cross-checked against the LiteLLM pricing dataset.
MODEL_PRICING: dict[str, ModelPricing] = {
    # Fable 5 / 5.1 - top tier. 5.1 reads cache at a quarter the 5 rate.
    "claude-fable-5-1": ModelPricing(10.0, 50.0, 0.25, 12.5),
    "claude-fable-5": ModelPricing(10.0, 50.0, 1.0, 12.5),
    "claude-mythos-5-1": ModelPricing(10.0, 50.0, 0.25, 12.5),
    "claude-mythos-5": ModelPricing(10.0, 50.0, 1.0, 12.5),

    # Opus - 4.5 through 5 all share a price point.
    "claude-opus-5": ModelPricing(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": ModelPricing(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": ModelPricing(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": ModelPricing(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-5": ModelPricing(5.0, 25.0, 0.5, 6.25),

    # Sonnet - 5 is cheaper than the 4.x it replaced.
    "claude-sonnet-5": ModelPricing(2.0, 10.0, 0.2, 2.5),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5": ModelPricing(3.0, 15.0, 0.3, 3.75),

    "claude-haiku-4-5": ModelPricing(1.0, 5.0, 0.1, 1.25),
    "claude-3-5-haiku": ModelPricing(0.8, 4.0, 0.08, 1.0),
}

# Newest known model of each family, for a release this table has not caught up
# with. Anthropic has only ever cut prices within a family, so the current entry
# is the safe over-estimate.
FAMILY_FALLBACK = {
    "fable": "claude-fable-5-1",
    "mythos": "claude-mythos-5-1",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

DEFAULT_PRICING = MODEL_PRICING["claude-sonnet-5"]


def get_model_pricing(model: str) -> ModelPricing:
    """Get pricing for a model, with fallback for unknown models."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    # Longest prefix wins, so a dated "claude-opus-4-5-20251101" cannot be
    # captured by a shorter key that happens to share its opening.
    model_lower = model.lower()
    for known_model in sorted(MODEL_PRICING, key=len, reverse=True):
        if model_lower.startswith(known_model):
            return MODEL_PRICING[known_model]

    for family, newest in FAMILY_FALLBACK.items():
        if family in model_lower:
            return MODEL_PRICING[newest]

    return DEFAULT_PRICING


def calculate_cost(model: str, tokens: TokenUsage) -> float:
    """Calculate cost in USD for token usage on a specific model.

    Anthropic reports input_tokens exclusive of both cache figures, so all four
    are charged separately with no overlap.
    """
    pricing = get_model_pricing(model)

    return (
        tokens.input_tokens / 1_000_000 * pricing.input_per_mtok
        + tokens.output_tokens / 1_000_000 * pricing.output_per_mtok
        + tokens.cache_read_input_tokens / 1_000_000 * pricing.cache_read_per_mtok
        + tokens.cache_creation_input_tokens / 1_000_000 * pricing.cache_creation_per_mtok
    )


def format_tokens(count: int) -> str:
    """Format token count for display (e.g., 1.2M, 500K)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)
