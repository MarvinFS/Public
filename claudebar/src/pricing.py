"""Claude model pricing tables and cost calculations."""

from dataclasses import dataclass
from typing import Optional

from models import TokenUsage


@dataclass
class ModelPricing:
    """Pricing for a Claude model (per million tokens).

    cache_creation_per_mtok is the 5-minute write rate (1.25x input); the
    1-hour write rate is derived as 2x input in calculate_cost.
    """
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_creation_per_mtok: float


# Bundled fallback, USD per million tokens, September 2026 (Anthropic pricing
# page). model_catalog.apply() overlays models.dev rates at runtime.
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


def load_catalog(models: dict) -> None:
    """Overlay models.dev rates: {model_id: {"cost": {input, output, cache_read, cache_write}}}.

    Merges over the bundled table so bundled-only ids and the family fallback
    stay valid; entries without all four cost keys are skipped.
    """
    for model_id, entry in models.items():
        if "claude" not in model_id:
            continue
        cost = entry.get("cost") if isinstance(entry, dict) else None
        if not isinstance(cost, dict):
            continue
        try:
            MODEL_PRICING[model_id] = ModelPricing(
                float(cost["input"]), float(cost["output"]),
                float(cost["cache_read"]), float(cost["cache_write"]))
        except (KeyError, TypeError, ValueError):
            continue


def get_model_pricing(model: str) -> Optional[ModelPricing]:
    """Pricing for a Claude model; None for anything that is not one.

    `<synthetic>` and non-Anthropic routes carry no API price - a fallback
    rate there would invent money.
    """
    model_lower = model.lower()
    if "claude" not in model_lower:
        return None

    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    # Longest prefix wins, so a dated "claude-opus-4-5-20251101" cannot be
    # captured by a shorter key that happens to share its opening.
    for known_model in sorted(MODEL_PRICING, key=len, reverse=True):
        if model_lower.startswith(known_model):
            return MODEL_PRICING[known_model]

    for family, newest in FAMILY_FALLBACK.items():
        if family in model_lower:
            return MODEL_PRICING[newest]

    return None


def calculate_cost(model: str, tokens: TokenUsage) -> float:
    """Calculate cost in USD for token usage on a specific model.

    Anthropic reports input_tokens exclusive of both cache figures, so all four
    are charged separately with no overlap. Cache writes split by TTL: the
    1-hour share (Claude Code's default) costs 2x input, the rest 1.25x.
    """
    pricing = get_model_pricing(model)
    if pricing is None:
        return 0.0

    cache_1h = min(tokens.cache_creation_1h_input_tokens, tokens.cache_creation_input_tokens)
    cache_5m = tokens.cache_creation_input_tokens - cache_1h

    return (
        tokens.input_tokens * pricing.input_per_mtok
        + tokens.output_tokens * pricing.output_per_mtok
        + tokens.cache_read_input_tokens * pricing.cache_read_per_mtok
        + cache_5m * pricing.cache_creation_per_mtok
        + cache_1h * pricing.input_per_mtok * 2
    ) / 1_000_000


def format_tokens(count: int) -> str:
    """Format token count for display (e.g., 1.2B, 1.2M, 500K)."""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.2f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)
