"""Codex cost accounting: every token is billed exactly once, at its own rate."""

import json

import codex_pricing
from codex_log_parser import parse_session_file


def test_cached_tokens_are_not_billed_twice():
    """input_tokens includes the cached slice - charging both inflates the bill."""
    pricing = codex_pricing.CODEX_PRICING["gpt-6-astra"]

    # 1M input of which 900K cached, 100K output.
    cost = codex_pricing.calculate_cost(
        model="gpt-6-astra",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cached_input_tokens=900_000,
    )

    expected = (
        0.1 * pricing["input"]      # 100K uncached input
        + 0.9 * pricing["cached_input"]
        + 0.1 * pricing["output"]
    )
    assert cost == expected


def test_longest_model_prefix_wins():
    """"gpt-5.1-codex-2026-01-01" must not fall through to the "gpt-5" entry."""
    assert codex_pricing.get_model_pricing("gpt-6-astra-2026-08-01") is \
        codex_pricing.CODEX_PRICING["gpt-6-astra"]
    assert codex_pricing.get_model_pricing("wingdings-9") is \
        codex_pricing.CODEX_PRICING["default"]


def test_session_is_priced_on_its_own_model_and_final_totals(tmp_path):
    """Model comes from turn_context; cumulative totals are taken, not summed."""
    def token_count(inp, cached, out):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached,
                "output_tokens": out, "reasoning_output_tokens": out // 2,
            }}}}

    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(400_000, 300_000, 5_000),
        token_count(1_000_000, 900_000, 100_000),   # cumulative, supersedes above
    ]) + "\n", encoding="utf-8")

    usage = parse_session_file(session)

    assert usage.input_tokens == 1_000_000
    assert usage.cached_input_tokens == 900_000
    assert usage.output_tokens == 100_000
    assert usage.cost_usd == codex_pricing.calculate_cost(
        "gpt-6-astra", 1_000_000, 100_000, 900_000)


def test_huge_session_file_is_still_counted(tmp_path):
    """A session bloated by base64 payloads must not drop out of the totals."""
    session = tmp_path / "rollout.jsonl"
    with open(session, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "turn_context",
                            "payload": {"model": "gpt-6-astra"}}) + "\n")
        f.write(json.dumps({"type": "response_item",
                            "payload": {"image": "A" * 20_000_000}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 500, "cached_input_tokens": 0,
                "output_tokens": 10, "reasoning_output_tokens": 0}}}}) + "\n")

    assert parse_session_file(session).input_tokens == 500
