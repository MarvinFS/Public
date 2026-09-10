"""Codex cost accounting: every token is billed exactly once, at its own rate,
per request - OpenAI's long-context tier is decided request by request."""

import json

import pytest

import codex_pricing
from codex_pricing import LONG_CONTEXT_INPUT_TOKENS
from codex_log_parser import parse_session_file


def token_count(inp, cached, out, cache_write=0):
    """One turn. `last_token_usage` is this turn's own spend and is what the
    parser sums; the cumulative `total_token_usage` is a decoy that must be
    ignored (it restarts at zero on `codex resume`)."""
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {
            "input_tokens": 999_999_999, "cached_input_tokens": 999_999_999,
            "output_tokens": 999_999_999, "reasoning_output_tokens": 999_999_999,
        },
        "last_token_usage": {
            "input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "output_tokens": out, "reasoning_output_tokens": out // 2,
            "total_tokens": inp + out,
        }}}}


def session_meta(provider):
    return {"type": "session_meta", "payload": {"model_provider": provider,
                                                "cli_version": "0.153.4"}}


def write_session(tmp_path, entries):
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return session


def test_cached_tokens_are_not_billed_twice():
    """input_tokens includes the cached slice - charging both inflates the bill."""
    pricing = codex_pricing.CODEX_PRICING["gpt-6-astra"]

    # 200K input of which 150K cached, 10K output (standard tier).
    cost = codex_pricing.calculate_cost(
        model="gpt-6-astra", input_tokens=200_000, output_tokens=10_000,
        cached_input_tokens=150_000)

    expected = (
        0.05 * pricing["input"]      # 50K uncached input
        + 0.15 * pricing["cached_input"]
        + 0.01 * pricing["output"]
    )
    assert cost == pytest.approx(expected)

    # A cached count above the input it is a subset of is clamped, not negative.
    clamped = codex_pricing.calculate_cost("gpt-6-astra", 1_000, 0, cached_input_tokens=5_000)
    assert clamped == pytest.approx(0.001 * pricing["cached_input"])


def test_cache_write_tokens_are_billed_at_their_own_rate():
    """Codex reports cache writes inside input_tokens; they cost 1.25x input,
    not the plain input rate."""
    pricing = codex_pricing.CODEX_PRICING["gpt-5.5"]
    assert pricing["cache_write"] == pytest.approx(pricing["input"] * 1.25)

    cost = codex_pricing.calculate_cost(
        "gpt-5.5", input_tokens=100_000, output_tokens=0,
        cached_input_tokens=30_000, cache_write_input_tokens=20_000)

    assert cost == pytest.approx(0.05 * pricing["input"] + 0.03 * pricing["cached_input"]
                                 + 0.02 * pricing["cache_write"])


def test_requests_above_272k_input_use_the_long_tier_for_every_component():
    """OpenAI's threshold is per request: 272,000 is standard, 272,001 is long."""
    p = codex_pricing.CODEX_PRICING["gpt-6-astra"]
    assert p["long"] == {"input": 20.0, "output": 75.0, "cached_input": 2.0, "cache_write": 25.0}

    n = LONG_CONTEXT_INPUT_TOKENS
    standard = codex_pricing.calculate_cost("gpt-6-astra", n, 1_000, cached_input_tokens=100_000,
                                            cache_write_input_tokens=50_000)
    long = codex_pricing.calculate_cost("gpt-6-astra", n + 1, 1_000, cached_input_tokens=100_000,
                                        cache_write_input_tokens=50_000)

    assert standard == pytest.approx(
        ((n - 150_000) * p["input"] + 100_000 * p["cached_input"]
         + 50_000 * p["cache_write"] + 1_000 * p["output"]) / 1e6)
    assert long == pytest.approx(
        ((n + 1 - 150_000) * p["long"]["input"] + 100_000 * p["long"]["cached_input"]
         + 50_000 * p["long"]["cache_write"] + 1_000 * p["long"]["output"]) / 1e6)


def test_longest_model_prefix_wins():
    """"gpt-5.1-codex-2026-01-01" must not fall through to the "gpt-5" entry."""
    assert codex_pricing.get_model_pricing("gpt-6-astra-2026-08-01") is \
        codex_pricing.CODEX_PRICING["gpt-6-astra"]
    assert codex_pricing.get_model_pricing("gpt-5.1-codex-2026-01-01") is \
        codex_pricing.CODEX_PRICING["gpt-5.1-codex"]
    assert codex_pricing.get_model_pricing("wingdings-9") is \
        codex_pricing.CODEX_PRICING["default"]


def test_every_gpt_5_6_variant_has_its_own_row():
    """terra and luna used to fall to a gpt-5 default; sol is the bare alias."""
    for model, rate in (("gpt-5.6-sol", 4.0), ("gpt-5.6-terra", 2.0), ("gpt-5.6-luna", 0.2)):
        assert codex_pricing.get_model_pricing(model)["input"] == rate
    assert codex_pricing.get_model_pricing("gpt-5.6") is codex_pricing.CODEX_PRICING["gpt-5.6-sol"]


def test_turns_are_summed_and_each_priced_at_its_own_model(tmp_path):
    """A session that switches models mid-way bills each turn at the model in
    force for that turn, from last_token_usage - never the cumulative total."""
    session = write_session(tmp_path, [
        session_meta("openai"),
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(100_000, 60_000, 5_000),
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}},
        token_count(300_000, 250_000, 8_000, cache_write=10_000),   # long tier
    ])

    usage = parse_session_file(session)

    assert usage.input_tokens == 400_000
    assert usage.cached_input_tokens == 310_000
    assert usage.output_tokens == 13_000
    assert usage.reasoning_tokens == 2_500 + 4_000
    assert usage.cost_usd == pytest.approx(
        codex_pricing.calculate_cost("gpt-6-astra", 100_000, 5_000, 60_000)
        + codex_pricing.calculate_cost("gpt-5.6-luna", 300_000, 8_000, 250_000, 10_000))


def test_non_openai_provider_counts_tokens_but_costs_nothing(tmp_path):
    """A glm-5.3-flash session served by Ollama has no OpenAI price; billing
    it at a gpt default invented $14 in one day."""
    turns = [
        {"type": "turn_context", "payload": {"model": "glm-5.3-flash"}},
        token_count(80_000, 0, 500),
        token_count(90_000, 70_000, 300),
    ]

    ollama = parse_session_file(write_session(tmp_path, [session_meta("OLLAMA")] + turns))
    assert ollama.input_tokens == 170_000
    assert ollama.output_tokens == 800
    assert ollama.cost_usd == 0.0

    openai = parse_session_file(write_session(tmp_path, [session_meta("openai")] + turns))
    assert openai.input_tokens == 170_000
    assert openai.cost_usd > 0


def test_load_catalog_reads_context_tier_and_derives_missing_rates(monkeypatch):
    monkeypatch.setattr(codex_pricing, "CODEX_PRICING", dict(codex_pricing.CODEX_PRICING))

    codex_pricing.load_catalog({
        "gpt-6-astra": {"cost": {
            "input": 11, "output": 55, "cache_read": 1.1, "cache_write": 13.75,
            "tiers": [{"tier": {"type": "context", "size": 272000},
                       "input": 22, "output": 82.5, "cache_read": 2.2, "cache_write": 27.5}],
        }},
        "gpt-7": {"cost": {"input": 3, "output": 12, "cache_read": 0.3}},      # no write/tier
        "gpt-broken": {"cost": {"input": 3}},                                  # skipped
    })

    astra = codex_pricing.CODEX_PRICING["gpt-6-astra"]
    assert astra["cache_write"] == 13.75
    assert astra["long"] == {"input": 22, "output": 82.5, "cached_input": 2.2, "cache_write": 27.5}
    assert codex_pricing.CODEX_PRICING["default"] is astra

    gpt7 = codex_pricing.CODEX_PRICING["gpt-7"]
    assert gpt7["cache_write"] == pytest.approx(3.75)
    assert gpt7["long"] == {"input": 6, "output": 18, "cached_input": 0.6, "cache_write": 7.5}
    assert "gpt-broken" not in codex_pricing.CODEX_PRICING


def test_malformed_info_skips_the_record_not_the_file(tmp_path):
    """A junk info block must not raise past parse_session_file's handler."""
    session = write_session(tmp_path, [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": "junk"}},
        {"type": "event_msg", "payload": {"type": "token_count",
                                          "info": {"last_token_usage": []}}},
        token_count(500, 0, 10),
    ])

    assert parse_session_file(session).input_tokens == 500


def test_huge_session_file_is_still_counted(tmp_path):
    """A session bloated by base64 payloads must not drop out of the totals."""
    session = tmp_path / "rollout.jsonl"
    with open(session, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "turn_context",
                            "payload": {"model": "gpt-6-astra"}}) + "\n")
        f.write(json.dumps({"type": "response_item",
                            "payload": {"image": "A" * 20_000_000}}) + "\n")
        f.write(json.dumps(token_count(500, 0, 10)) + "\n")

    assert parse_session_file(session).input_tokens == 500
