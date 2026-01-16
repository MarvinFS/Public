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


# Claude model pricing as of January 2026
# Prices in USD per million tokens
MODEL_PRICING: dict[str, ModelPricing] = {
    # Opus 4.5
    "claude-opus-4-5-20251101": ModelPricing(
        input_per_mtok=15.0,
        output_per_mtok=75.0,
        cache_read_per_mtok=1.5,
        cache_creation_per_mtok=18.75,
    ),
    # Sonnet 4.5
    "claude-sonnet-4-5-20250929": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.3,
        cache_creation_per_mtok=3.75,
    ),
    # Sonnet 4
    "claude-sonnet-4-20250514": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.3,
        cache_creation_per_mtok=3.75,
    ),
    # Haiku 3.5
    "claude-3-5-haiku-20241022": ModelPricing(
        input_per_mtok=0.8,
        output_per_mtok=4.0,
        cache_read_per_mtok=0.08,
        cache_creation_per_mtok=1.0,
    ),
}

# Fallback pricing for unknown models (use Sonnet pricing as default)
DEFAULT_PRICING = ModelPricing(
    input_per_mtok=3.0,
    output_per_mtok=15.0,
    cache_read_per_mtok=0.3,
    cache_creation_per_mtok=3.75,
)


def get_model_pricing(model: str) -> ModelPricing:
    """Get pricing for a model, with fallback for unknown models."""
    # Direct match
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    # Partial match (handle version variations)
    model_lower = model.lower()
    for known_model, pricing in MODEL_PRICING.items():
        if known_model in model_lower or model_lower in known_model:
            return pricing

    # Model family matching
    if "opus" in model_lower:
        return MODEL_PRICING.get("claude-opus-4-5-20251101", DEFAULT_PRICING)
    if "sonnet" in model_lower:
        return MODEL_PRICING.get("claude-sonnet-4-5-20250929", DEFAULT_PRICING)
    if "haiku" in model_lower:
        return MODEL_PRICING.get("claude-3-5-haiku-20241022", DEFAULT_PRICING)

    return DEFAULT_PRICING


def calculate_cost(model: str, tokens: TokenUsage) -> float:
    """Calculate cost in USD for token usage on a specific model."""
    pricing = get_model_pricing(model)

    # Convert tokens to millions and apply pricing
    input_cost = (tokens.input_tokens / 1_000_000) * pricing.input_per_mtok
    output_cost = (tokens.output_tokens / 1_000_000) * pricing.output_per_mtok
    cache_read_cost = (tokens.cache_read_input_tokens / 1_000_000) * pricing.cache_read_per_mtok
    cache_creation_cost = (tokens.cache_creation_input_tokens / 1_000_000) * pricing.cache_creation_per_mtok

    return input_cost + output_cost + cache_read_cost + cache_creation_cost


def format_cost(cost_usd: float) -> str:
    """Format cost for display."""
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:.2f}"


def format_tokens(count: int) -> str:
    """Format token count for display (e.g., 1.2M, 500K)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)
