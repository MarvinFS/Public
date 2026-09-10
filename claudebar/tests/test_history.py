"""Pace projection and the daily cost series behind the panel."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from codex_log_parser import get_codex_daily_usage
from log_parser import collect_entries, daily_costs, aggregate_usage
from models import project_window
from pricing import format_tokens
from tests.test_claude_cost import assistant


def test_pace_marks_even_spend_and_projects_runout():
    """58% used with 2h12m of a 5h window left: even spend would be at 56%,
    and the remaining 42% lasts ~2h at the pace so far - just past the reset."""
    now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=2, minutes=12)

    expected, remaining, runout = project_window(58, resets_at, 5, now=now)

    assert expected == pytest.approx(56.0, abs=0.1)
    assert remaining == pytest.approx(2.2)
    assert runout == pytest.approx(2.03, abs=0.01)
    assert runout < remaining                       # "Runs out in 2h 1m"


def test_pace_edge_cases():
    now = datetime(2026, 9, 10, 9, 0)
    assert project_window(50, None, 5) is None                       # no reset known
    _, _, runout = project_window(0, now + timedelta(hours=1), 5, now=now)
    assert runout == float("inf")                                    # nothing spent yet
    expected, remaining, _ = project_window(10, now - timedelta(hours=1), 5, now=now)
    assert (expected, remaining) == (100.0, 0.0)                     # reset already passed


def test_daily_costs_buckets_by_local_day_and_pads_empty_days(tmp_path):
    def at(day, msg):
        e = assistant(msg, f"req-{msg}", "text")
        e["timestamp"] = f"2026-09-{day:02d}T12:00:00.000Z"
        return e

    project = tmp_path / "p"
    project.mkdir()
    (project / "s.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [at(1, "a"), at(1, "b"), at(9, "c"), at(2, "old")]) + "\n",
        encoding="utf-8")

    entries = collect_entries(tmp_path)
    series = daily_costs(entries, days=7, end=date(2026, 9, 9))

    _, _, one_call = aggregate_usage(entries=entries, start_date=date(2026, 9, 9), end_date=date(2026, 9, 9))
    assert len(series) == 7
    assert series[-1] == pytest.approx(one_call)             # 9th
    assert series[:-1] == [0.0] * 6                           # 3rd..8th empty
    # A 7-day window ending on the 9th starts on the 3rd: the 1st and 2nd are out.
    assert sum(series) == pytest.approx(one_call)


def test_codex_daily_series_ends_today_and_pads_missing_days(tmp_path):
    today = datetime.now()
    day_dir = tmp_path / str(today.year) / f"{today.month:02d}" / f"{today.day:02d}"
    day_dir.mkdir(parents=True)
    (day_dir / "rollout.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "session_meta", "payload": {"model_provider": "openai"}},
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 1_000, "cached_input_tokens": 0,
                                 "cache_write_input_tokens": 0, "output_tokens": 10,
                                 "reasoning_output_tokens": 0}}}},
    ]) + "\n", encoding="utf-8")

    series = get_codex_daily_usage(tmp_path, days=5)

    assert len(series) == 5
    assert series[-1].input_tokens == 1_000 and series[-1].cost_usd > 0
    assert all(u.total_tokens == 0 for u in series[:-1])


def test_format_tokens_has_a_billions_step():
    assert format_tokens(1_234_000_000) == "1.23B"
    assert format_tokens(999_999_999) == "1000.0M"
    assert format_tokens(46_200_000) == "46.2M"
