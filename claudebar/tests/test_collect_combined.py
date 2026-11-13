"""One provider's failure must not take the refresh of the others with it.

collect_combined ran Claude, Codex and DeepSeek in sequence with nothing around them,
so an exception in the first (a cache file of the wrong shape, an odd reply) dropped
the whole refresh and left every provider showing the figures it had.
"""

import collections
import logging
from datetime import datetime, timedelta, timezone

import pytest

import data_collector as dc
import deepseek_usage
import snapshot_cache
from codex_log_parser import CodexTokenUsage
from config import Config
from currency import ExchangeRates
from deepseek_auth import Credential
from models import DeepSeekSnapshot, OpenAISnapshot, UsageSnapshot
from oauth_usage import OAuthUsageData, UsageWindow
from openai_usage import OpenAIUsageData, OpenAIUsageWindow

PROVIDERS = ["claude", "openai", "deepseek"]


def boom():
    raise RuntimeError("disk on fire")


def collector(monkeypatch, fail=(), apply=lambda: False):
    """A collector whose three providers return a figure of their own, except those in
    `fail`, which raise."""
    monkeypatch.setattr(dc.model_catalog, "apply", apply)
    c = dc.DataCollector()
    stubs = {
        "claude": lambda: UsageSnapshot(timestamp=datetime.now(), session_percent=11.0),
        "openai": lambda: OpenAISnapshot(timestamp=datetime.now(), session_percent=22.0, available=True),
        "deepseek": lambda: DeepSeekSnapshot(timestamp=datetime.now(), balance_available=True,
                                             balance_total=33.0),
    }
    c.collect = boom if "claude" in fail else stubs["claude"]
    c.collect_openai = boom if "openai" in fail else stubs["openai"]
    c.collect_deepseek = boom if "deepseek" in fail else stubs["deepseek"]
    return c


def errors(caplog):
    return [r for r in caplog.records if r.name == "claudebar" and r.levelno == logging.ERROR]


@pytest.mark.parametrize("failing", PROVIDERS)
def test_the_others_still_update_and_the_failure_is_logged_with_its_traceback(monkeypatch, caplog, failing):
    c = collector(monkeypatch, fail=(failing,))
    with caplog.at_level(logging.ERROR, logger="claudebar"):
        combined = c.collect_combined()

    if failing != "claude":
        assert combined.claude.session_percent == 11.0
    if failing != "openai":
        assert combined.openai.session_percent == 22.0
    if failing != "deepseek":
        assert combined.deepseek.balance_total == 33.0

    (record,) = errors(caplog)
    assert record.exc_info and isinstance(record.exc_info[1], RuntimeError)
    assert "disk on fire" in str(record.exc_info[1])


def test_with_no_cache_a_failed_provider_says_it_is_unavailable(monkeypatch):
    combined = collector(monkeypatch, fail=PROVIDERS).collect_combined()

    claude = combined.claude        # not the silent all-zeros panel the defaults would draw
    assert not claude.cli_available and not claude.logs_available
    assert claude.error_message and claude.get_status_level() == "error"

    assert not combined.openai.available and combined.openai.error_message

    deepseek = combined.deepseek
    assert not deepseek.balance_available and not deepseek.usage_available
    assert deepseek.error_message and deepseek.usage_error

    for snapshot in (claude, combined.openai, deepseek):
        assert "disk on fire" not in snapshot.error_message, "the panel never shows the exception's text"


def test_a_failed_provider_shows_its_cached_snapshot_marked_cached(monkeypatch):
    saved = datetime.now()
    snapshot_cache.save_cache(
        claude=UsageSnapshot(timestamp=saved, session_percent=33.0),
        openai=OpenAISnapshot(timestamp=saved, session_percent=44.0, available=True),
        deepseek=DeepSeekSnapshot(timestamp=saved, balance_available=True, balance_total=55.0,
                                  balance_fetched_at=saved))

    combined = collector(monkeypatch, fail=PROVIDERS).collect_combined()

    assert (combined.claude.session_percent, combined.claude.is_stale) == (33.0, True)
    assert combined.claude.stale_since is not None
    assert (combined.openai.session_percent, combined.openai.is_stale) == (44.0, True)
    assert (combined.deepseek.balance_total, combined.deepseek.is_stale) == (55.0, True)


def test_a_cache_file_of_the_wrong_shape_does_not_stop_the_refresh(monkeypatch):
    """The two defects together: the loader used to raise on this file, in the first provider."""
    path = snapshot_cache.get_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"claude": [1, 2]}', encoding="utf-8")

    combined = collector(monkeypatch, fail=("claude",)).collect_combined()

    assert combined.claude.error_message                 # no usable cache: unavailable
    assert combined.openai.session_percent == 22.0
    assert combined.deepseek.balance_total == 33.0


def test_a_failing_price_table_refresh_means_the_bundled_prices(monkeypatch, caplog):
    c = collector(monkeypatch, apply=boom)
    with caplog.at_level(logging.ERROR, logger="claudebar"):
        combined = c.collect_combined()
    assert c._pricing_source == "bundled"
    assert combined.claude.session_percent == 11.0
    assert len(errors(caplog)) == 1


ENGINES = ["claude", "codex", "deepseek"]
FIELD = {"claude": "claude", "codex": "openai", "deepseek": "deepseek"}


@pytest.fixture
def world(monkeypatch):
    """Every way an engine reaches outside the collector, counted as "<engine> <what>": the
    network, the logs, the cache file and the history. Each answers as a healthy account would,
    so a collect of an engine that is on leaves every one of its lines in the count."""
    calls = collections.Counter()
    soon = datetime.now(timezone.utc) + timedelta(hours=2)

    def counted(name, answer):
        def fake(*args, **kwargs):
            calls[name] += 1
            return answer
        return fake

    def saved(**snapshots):
        for field in snapshots:
            calls[f"{'codex' if field == 'openai' else field} cache write"] += 1

    def recorded(provider, *args, **kwargs):
        calls[f"{provider.split('_')[0]} history line"] += 1

    monkeypatch.setattr(dc.model_catalog, "apply", counted("price table", False))
    monkeypatch.setattr(dc, "fetch_oauth_usage", counted(
        "claude http", OAuthUsageData(session=UsageWindow(40.0, soon), weekly=UsageWindow(20.0, soon))))
    monkeypatch.setattr(dc, "collect_entries", counted("claude log parse", {}))
    monkeypatch.setattr(dc, "fetch_openai_usage", counted(
        "codex http", OpenAIUsageData(five_hour=OpenAIUsageWindow(30.0, soon, 18000))))
    monkeypatch.setattr(dc, "get_codex_daily_usage", counted("codex log parse", [CodexTokenUsage()] * 31))
    monkeypatch.setattr(dc.deepseek_auth, "discover_user_token", counted("deepseek token", Credential("t", "saved")))
    monkeypatch.setattr(dc, "get_exchange_rates", counted("deepseek rates", ExchangeRates({}, datetime.now())))
    monkeypatch.setattr(dc, "fetch_usage", counted("deepseek http", deepseek_usage.UsageData(available=False, error="x")))
    monkeypatch.setattr(dc, "fetch_summary", counted(
        "deepseek http", deepseek_usage.AccountSummary(available=True, topped_up=5.0)))
    monkeypatch.setattr(dc, "save_cache", saved)
    monkeypatch.setattr(dc.quota_history, "append", recorded)
    return calls


def lines_of(calls, engine):
    return {name for name in calls if name.startswith(engine + " ")}


def test_with_every_engine_on_every_engine_is_collected(world):
    combined = dc.DataCollector(config=Config()).collect_combined()

    assert all(getattr(combined, FIELD[engine]) is not None for engine in ENGINES)
    for engine in ENGINES:
        assert lines_of(world, engine) >= {f"{engine} http", f"{engine} cache write", f"{engine} history line"}
    assert world["price table"] == 1


def test_a_collector_with_no_config_collects_everything_as_before(world):
    combined = dc.DataCollector().collect_combined()
    assert all(getattr(combined, FIELD[engine]) is not None for engine in ENGINES)


@pytest.mark.parametrize("off", ENGINES)
def test_an_engine_switched_off_costs_the_refresh_nothing(world, off):
    """No request, no log parse, no cache write and no history line: the switch in Settings is
    for what ClaudeBar spends, not only for what the panel shows."""
    config = Config(**{f"{off}_enabled": False})

    combined = dc.DataCollector(config=config).collect_combined()

    assert lines_of(world, off) == set()
    assert getattr(combined, FIELD[off]) is None            # nothing collected, and it says so
    for engine in set(ENGINES) - {off}:
        assert getattr(combined, FIELD[engine]) is not None
        assert f"{engine} http" in world


def test_the_price_table_is_not_fetched_when_no_enabled_engine_prices_anything(world):
    dc.DataCollector(config=Config(claude_enabled=False, codex_enabled=False)).collect_combined()
    assert world["price table"] == 0
    dc.DataCollector(config=Config(claude_enabled=False)).collect_combined()
    assert world["price table"] == 1                        # Codex still prices its turns


def test_an_engine_switched_back_on_is_collected_at_the_next_refresh_without_a_restart(world):
    config = Config(codex_enabled=False)
    collector = dc.DataCollector(config=config)          # the one object Settings changes in place

    assert collector.collect_combined().openai is None
    assert lines_of(world, "codex") == set()

    config.codex_enabled = True
    combined = collector.collect_combined()

    assert combined.openai is not None and combined.openai.available
    assert {"codex http", "codex log parse"} <= lines_of(world, "codex")
