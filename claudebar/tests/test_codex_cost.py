"""Codex cost accounting: every token is billed exactly once, at its own rate."""

import json

import codex_pricing
from codex_log_parser import parse_session_file


def token_count(inp, cached, out, last=None):
    """`last` is this turn's own spend. It defaults to the whole total, which
    is what Codex writes on the first record of a run."""
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {
            "input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "reasoning_output_tokens": out // 2,
        },
        "last_token_usage": {
            "input_tokens": inp if last is None else last,
            "output_tokens": out if last is None else 0,
        }}}}


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
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(400_000, 300_000, 5_000, last=4_000),
        token_count(1_000_000, 900_000, 100_000, last=9_000),
    ]) + "\n", encoding="utf-8")

    usage = parse_session_file(session)

    assert usage.input_tokens == 1_000_000
    assert usage.cached_input_tokens == 900_000
    assert usage.output_tokens == 100_000
    assert usage.cost_usd == codex_pricing.calculate_cost(
        "gpt-6-astra", 1_000_000, 100_000, 900_000)


def test_resumed_session_sums_its_epochs(tmp_path):
    """`codex resume` restarts total_token_usage at zero in the same file.

    Taking only the last record would report the trailing 54K and discard the
    1.1M spent before the resume.
    """
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(400_000, 300_000, 5_000, last=4_000),
        token_count(1_000_000, 900_000, 100_000, last=9_000),
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(20_000, 10_000, 1_000),         # counter reset - new epoch
        token_count(50_000, 40_000, 4_000, last=30_000),
    ]) + "\n", encoding="utf-8")

    usage = parse_session_file(session)

    assert usage.input_tokens == 1_050_000
    assert usage.cached_input_tokens == 940_000
    assert usage.output_tokens == 104_000
    assert usage.cost_usd == codex_pricing.calculate_cost(
        "gpt-6-astra", 1_050_000, 104_000, 940_000)


def test_counter_dipping_mid_run_does_not_bank_an_epoch(tmp_path):
    """A resume that re-derives its total can correct slightly downwards.

    That is one run continuing, not a restart, and banking it would nearly
    double the session. A real restart has spent only the turn it just logged
    (total <= last); a mid-run dip sits far above its own last turn.
    """
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(1_000_000, 900_000, 100_000, last=5_000),
        token_count(990_000, 890_000, 99_000, last=4_000),   # 1% correction
        token_count(1_200_000, 1_000_000, 120_000, last=6_000),
    ]) + "\n", encoding="utf-8")

    usage = parse_session_file(session)

    assert usage.input_tokens == 1_200_000      # not 2_200_000


def test_epoch_reset_through_a_zero_record_still_banks(tmp_path):
    """Some restarts log an all-zero record before the first real turn."""
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        token_count(800_000, 700_000, 50_000, last=5_000),
        token_count(0, 0, 0),
        token_count(300_000, 250_000, 20_000, last=10_000),
    ]) + "\n", encoding="utf-8")

    assert parse_session_file(session).input_tokens == 1_100_000


def test_malformed_info_skips_the_record_not_the_file(tmp_path):
    """A junk info block must not raise past parse_session_file's handler."""
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": "junk"}},
        {"type": "event_msg", "payload": {"type": "token_count",
                                          "info": {"total_token_usage": []}}},
        token_count(500, 0, 10),
    ]) + "\n", encoding="utf-8")

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
