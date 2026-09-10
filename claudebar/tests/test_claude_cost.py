"""Claude cost accounting: one API call is billed once, at current rates."""

import json
import urllib.request

import pytest

import model_catalog
import pricing
from log_parser import aggregate_usage
from models import TokenUsage


def assistant(msg_id, request_id, block_type, model="claude-opus-5",
              output_tokens=1_128, iterations=None, cache_1h=None):
    """One JSONL line: Claude Code writes one per content block of a response.

    No `cache_1h` = no `cache_creation` block, i.e. every write is 5-minute.
    """
    usage = {
        "input_tokens": 2,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 78_023,
        "cache_creation_input_tokens": 4_654,
    }
    if cache_1h is not None:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": cache_1h,
                                   "ephemeral_5m_input_tokens": 4_654 - cache_1h}
    if iterations is not None:
        usage["iterations"] = iterations
    return {
        "type": "assistant",
        "timestamp": "2026-09-06T12:00:00.000Z",
        "requestId": request_id,
        "uuid": f"{msg_id}-{block_type}-{output_tokens}",
        "message": {
            "id": msg_id,
            "model": model,
            "content": [{"type": block_type}],
            "usage": usage,
        },
    }


def write_session(tmp_path, entries, name="session.jsonl"):
    project = tmp_path / "a-project"
    project.mkdir(exist_ok=True)
    (project / name).write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return tmp_path


def test_one_response_split_across_blocks_is_counted_once(tmp_path):
    """Four block lines repeat one usage object - billing it 4x is the bug.

    The mid-stream lines carry a placeholder output_tokens; only the last one,
    written when the response completes, has the real count. First-wins dedup
    keeps the placeholder, so the last line to claim a key has to win.
    """
    projects = write_session(tmp_path, [
        assistant("msg_1", "req_1", "thinking", output_tokens=1),
        assistant("msg_1", "req_1", "text", output_tokens=1),
        assistant("msg_1", "req_1", "tool_use", output_tokens=1),
        assistant("msg_1", "req_1", "tool_use", output_tokens=1_128),
    ])

    _, tokens, _ = aggregate_usage(projects)

    assert tokens.output_tokens == 1_128
    assert tokens.cache_read_input_tokens == 78_023


def test_advisor_iteration_tokens_are_counted(tmp_path):
    """An advisor turn's tokens live only in `iterations`.

    Top-level input/output cover the outer messages alone - reading them drops
    the advisor model's work entirely, while cache is already summed there.
    """
    projects = write_session(tmp_path, [
        assistant("msg_1", "req_1", "text", output_tokens=716, iterations=[
            {"input_tokens": 2, "output_tokens": 78,
             "cache_read_input_tokens": 112_117, "cache_creation_input_tokens": 2_043,
             "type": "message"},
            {"input_tokens": 115_477, "output_tokens": 6_417,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
             "type": "advisor_message", "model": "claude-fable-5-1"},
            {"input_tokens": 2, "output_tokens": 638,
             "cache_read_input_tokens": 114_160, "cache_creation_input_tokens": 1_654,
             "type": "message"},
        ]),
    ])

    models, tokens, _ = aggregate_usage(projects)

    assert tokens.input_tokens == 115_481          # not the top-level 2
    assert tokens.output_tokens == 7_133           # not the top-level 716
    assert tokens.cache_read_input_tokens == 226_277
    # The advisor's share is billed at the advisor's own model.
    assert {m.model for m in models.values()} == {"claude-opus-5", "claude-fable-5-1"}


def test_distinct_responses_still_both_count(tmp_path):
    """Dedup must key on the API call, not collapse everything that looks alike."""
    projects = write_session(tmp_path, [
        assistant("msg_1", "req_1", "text"),
        assistant("msg_2", "req_2", "text"),
    ])

    _, tokens, _ = aggregate_usage(projects)

    assert tokens.output_tokens == 2 * 1_128


def test_same_response_copied_into_a_second_file_counts_once(tmp_path):
    """Resuming a session re-writes earlier turns into the new transcript."""
    write_session(tmp_path, [assistant("msg_1", "req_1", "text")], "first.jsonl")
    write_session(tmp_path, [assistant("msg_1", "req_1", "text")], "second.jsonl")

    _, tokens, _ = aggregate_usage(tmp_path)

    assert tokens.output_tokens == 1_128


def test_current_models_are_priced_from_the_table_not_a_fallback():
    """Every model in real use must resolve to its own entry."""
    for model in ("claude-opus-5", "claude-fable-5", "claude-fable-5-1",
                  "claude-sonnet-5", "claude-opus-4-7", "claude-haiku-4-5"):
        assert pricing.get_model_pricing(model) is pricing.MODEL_PRICING[model]

    # Opus is $5/$25 - the old table charged $15/$75.
    opus = pricing.get_model_pricing("claude-opus-5")
    assert (opus.input_per_mtok, opus.output_per_mtok) == (5.0, 25.0)


def test_unknown_model_falls_back_to_its_family():
    """A release newer than this table should not be priced as a Sonnet."""
    assert pricing.get_model_pricing("claude-opus-9") is \
        pricing.MODEL_PRICING["claude-opus-5"]
    assert pricing.get_model_pricing("claude-opus-5-20260401") is \
        pricing.MODEL_PRICING["claude-opus-5"]
    # A Bedrock/Vertex id is prefixed, so only the family match can catch it.
    assert pricing.get_model_pricing("us.anthropic.claude-sonnet-5-v1:0") is \
        pricing.MODEL_PRICING["claude-sonnet-5"]


def test_non_claude_models_have_no_price_but_still_count_tokens(tmp_path):
    """`<synthetic>` and other providers carry no API price - a fallback rate
    would invent money. Their tokens are still real usage."""
    assert pricing.get_model_pricing("<synthetic>") is None
    assert pricing.get_model_pricing("glm-5.3-flash") is None
    assert pricing.calculate_cost("<synthetic>", TokenUsage(output_tokens=1_000_000)) == 0.0

    projects = write_session(tmp_path, [
        assistant("msg_1", "req_1", "text", model="<synthetic>"),
    ])
    models, tokens, cost = aggregate_usage(projects)

    assert cost == 0.0
    assert tokens.output_tokens == 1_128
    assert models["<synthetic>"].cost_usd == 0.0


def test_fable_5_1_is_not_captured_by_the_fable_5_entry():
    """Longest-prefix ordering: 5.1 reads cache at a quarter of the 5 rate.

    Exact keys are safe whatever the sort order, so the case that actually
    pins the ordering is a suffixed id that both entries could claim.
    """
    assert pricing.get_model_pricing("claude-fable-5-1").cache_read_per_mtok == 0.25
    assert pricing.get_model_pricing("claude-fable-5").cache_read_per_mtok == 1.0
    assert pricing.get_model_pricing("claude-fable-5-1-20260801").cache_read_per_mtok == 0.25
    assert pricing.get_model_pricing("claude-opus-4-5-20251101") is \
        pricing.MODEL_PRICING["claude-opus-4-5"]


def test_cache_tokens_are_charged_on_their_own_rates():
    """Anthropic reports input_tokens exclusive of cache - all four add up."""
    p = pricing.MODEL_PRICING["claude-opus-5"]
    cost = pricing.calculate_cost("claude-opus-5", TokenUsage(
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000))

    assert cost == pytest.approx(p.input_per_mtok + p.output_per_mtok
                                 + p.cache_read_per_mtok + p.cache_creation_per_mtok)


def test_one_hour_cache_writes_cost_twice_input_not_the_5m_rate():
    """Anthropic: 5m write 1.25x input, 1h write 2x input. Claude Code writes
    with the 1h TTL, so pricing every write at 1.25x under-counts."""
    p = pricing.MODEL_PRICING["claude-opus-5"]
    cost = pricing.calculate_cost("claude-opus-5", TokenUsage(
        cache_creation_input_tokens=1_000_000, cache_creation_1h_input_tokens=600_000))

    assert cost == pytest.approx(0.4 * p.cache_creation_per_mtok + 0.6 * p.input_per_mtok * 2)

    # The 1h share can never exceed the write total it is a subset of.
    clamped = pricing.calculate_cost("claude-opus-5", TokenUsage(
        cache_creation_input_tokens=1_000_000, cache_creation_1h_input_tokens=5_000_000))
    assert clamped == pytest.approx(p.input_per_mtok * 2)


def test_one_hour_share_is_read_from_top_level_and_iterations(tmp_path):
    """`cache_creation.ephemeral_1h_input_tokens` sits in the usage block and
    repeats inside every iterations[] item."""
    projects = write_session(tmp_path, [
        assistant("msg_1", "req_1", "text", cache_1h=4_000),
        assistant("msg_2", "req_2", "text", iterations=[
            {"input_tokens": 2, "output_tokens": 78,
             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 2_043,
             "cache_creation": {"ephemeral_1h_input_tokens": 2_043},
             "type": "message"},
            {"input_tokens": 10, "output_tokens": 20,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
             "type": "advisor_message", "model": "claude-fable-5-1"},
        ]),
        assistant("msg_3", "req_3", "text"),                 # no block: all 5m
    ])

    _, tokens, _ = aggregate_usage(projects)

    assert tokens.cache_creation_input_tokens == 4_654 + 2_043 + 4_654
    assert tokens.cache_creation_1h_input_tokens == 4_000 + 2_043


def test_oversized_transcript_is_still_counted(tmp_path):
    """A 20 MB pasted-image line used to trip a size cap that dropped the
    whole file, and its spend, from the totals."""
    project = tmp_path / "a-project"
    project.mkdir()
    with open(project / "big.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "A" * 20_000_000}}) + "\n")
        f.write(json.dumps(assistant("msg_1", "req_1", "text")) + "\n")

    _, tokens, _ = aggregate_usage(tmp_path)

    assert tokens.output_tokens == 1_128


def test_load_catalog_overrides_bundled_rates_and_skips_partial_entries(monkeypatch):
    monkeypatch.setattr(pricing, "MODEL_PRICING", dict(pricing.MODEL_PRICING))

    pricing.load_catalog({
        "claude-opus-5": {"cost": {"input": 7, "output": 35, "cache_read": 0.7, "cache_write": 8.75}},
        "claude-opus-6": {"cost": {"input": 9, "output": 45}},           # missing cache keys
        "gpt-6-astra": {"cost": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}},
    })

    assert pricing.MODEL_PRICING["claude-opus-5"] == pricing.ModelPricing(7, 35, 0.7, 8.75)
    assert "claude-opus-6" not in pricing.MODEL_PRICING
    assert "gpt-6-astra" not in pricing.MODEL_PRICING
    # Bundled-only ids survive the overlay.
    assert pricing.get_model_pricing("claude-sonnet-4-5") is pricing.MODEL_PRICING["claude-sonnet-4-5"]


def test_catalog_apply_reports_fallback_and_reads_fresh_cache_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(pricing, "MODEL_PRICING", dict(pricing.MODEL_PRICING))

    def no_network(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)

    assert model_catalog.apply() is False             # no cache, fetch failed
    assert pricing.MODEL_PRICING["claude-opus-5"].input_per_mtok == 5.0

    (tmp_path / "models_dev.json").write_text(json.dumps({
        "timestamp": "2099-01-01T00:00:00",
        "anthropic": {"claude-opus-5": {"cost": {"input": 7, "output": 35,
                                                 "cache_read": 0.7, "cache_write": 8.75}}},
        "openai": {},
    }), encoding="utf-8")

    assert model_catalog.apply() is True              # cache served, urlopen never called
    assert pricing.MODEL_PRICING["claude-opus-5"].input_per_mtok == 7.0
