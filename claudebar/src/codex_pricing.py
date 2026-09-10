"""Codex/OpenAI model pricing for cost calculations."""

# A request whose input exceeds this is billed on the "long" tier for every
# component (OpenAI: input/cached/cache-write 2x, output 1.5x).
LONG_CONTEXT_INPUT_TOKENS = 272_000

_LONG_MULTIPLIERS = {"input": 2.0, "output": 1.5, "cached_input": 2.0, "cache_write": 2.0}


def _row(input_, output, cached_input, cache_write=None, long=None) -> dict:
    row = {
        "input": input_, "output": output, "cached_input": cached_input,
        "cache_write": input_ * 1.25 if cache_write is None else cache_write,
    }
    row["long"] = long or {k: row[k] * m for k, m in _LONG_MULTIPLIERS.items()}
    return row


# Bundled fallback, USD per 1 million tokens, September 2026 (OpenAI pricing
# page). model_catalog.apply() overlays models.dev rates at runtime.
CODEX_PRICING = {
    "gpt-6-astra": _row(10.00, 50.00, 1.00, 12.50,
                        long={"input": 20.00, "output": 75.00, "cached_input": 2.00, "cache_write": 25.00}),
    "gpt-5.6-sol": _row(4.00, 20.00, 0.40, 5.00,
                        long={"input": 8.00, "output": 30.00, "cached_input": 0.80, "cache_write": 10.00}),
    "gpt-5.6-terra": _row(2.00, 12.00, 0.20, 2.50,
                          long={"input": 4.00, "output": 18.00, "cached_input": 0.40, "cache_write": 5.00}),
    "gpt-5.6-luna": _row(0.20, 1.20, 0.02, 0.25,
                         long={"input": 0.40, "output": 1.80, "cached_input": 0.04, "cache_write": 0.50}),
    "gpt-5.5": _row(2.50, 15.00, 0.25),
    "gpt-5.4": _row(2.50, 15.00, 0.25),
    "gpt-5.1-codex": _row(1.25, 10.00, 0.125),
    "gpt-5.1": _row(1.25, 10.00, 0.125),
    "gpt-5-codex": _row(1.25, 10.00, 0.125),
    "gpt-5": _row(1.25, 10.00, 0.125),
}
CODEX_PRICING["gpt-5.6"] = CODEX_PRICING["gpt-5.6-sol"]
# An OpenAI model this table has not caught up with: price at the top tier, the
# safe over-estimate. Non-OpenAI providers never reach it (see codex_log_parser).
CODEX_PRICING["default"] = CODEX_PRICING["gpt-6-astra"]


def load_catalog(models: dict) -> None:
    """Overlay models.dev rates: {model_id: {"cost": {input, output, cache_read, cache_write?,
    tiers?: [{tier: {type: "context", size}, input, output, cache_read, cache_write}]}}}.

    Entries missing input/output/cache_read are skipped; cache_write and the
    long tier are derived when absent.
    """
    for model_id, entry in models.items():
        cost = entry.get("cost") if isinstance(entry, dict) else None
        if not isinstance(cost, dict):
            continue
        try:
            input_, output = float(cost["input"]), float(cost["output"])
            cached_input = float(cost["cache_read"])
        except (KeyError, TypeError, ValueError):
            continue
        cache_write = float(cost.get("cache_write") or input_ * 1.25)

        long = None
        tiers = cost.get("tiers")
        for tier in tiers if isinstance(tiers, list) else []:
            if not isinstance(tier, dict) or (tier.get("tier") or {}).get("type") != "context":
                continue
            try:
                long = {
                    "input": float(tier["input"]), "output": float(tier["output"]),
                    "cached_input": float(tier["cache_read"]),
                    "cache_write": float(tier.get("cache_write") or float(tier["input"]) * 1.25),
                }
            except (KeyError, TypeError, ValueError):
                long = None
            break

        CODEX_PRICING[model_id] = _row(input_, output, cached_input, cache_write, long)

    CODEX_PRICING["default"] = CODEX_PRICING["gpt-6-astra"]


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
    cache_write_input_tokens: int = 0,
) -> float:
    """Cost in USD of ONE request (a Codex turn's `last_token_usage`).

    Codex reports input_tokens inclusive of the cached and cache-write slices,
    and output_tokens inclusive of reasoning tokens. Every token is charged
    once: cached at the cache-read rate, cache writes at their rate, the rest
    at the input rate. Requests above LONG_CONTEXT_INPUT_TOKENS use the long
    tier for every component, which is why this must be applied per request
    and never to a session total.
    """
    pricing = get_model_pricing(model)
    if input_tokens > LONG_CONTEXT_INPUT_TOKENS:
        pricing = pricing["long"]

    cached = min(max(cached_input_tokens, 0), input_tokens)
    remaining = input_tokens - cached
    cache_write = min(max(cache_write_input_tokens, 0), remaining)
    non_cached = remaining - cache_write

    return (
        non_cached * pricing["input"]
        + cached * pricing["cached_input"]
        + cache_write * pricing["cache_write"]
        + output_tokens * pricing["output"]
    ) / 1_000_000
