"""How the collector assembles a DeepSeek snapshot from the platform session."""

from datetime import date, datetime, timedelta

import currency
import data_collector as dc
import deepseek_usage as du


def fixed_rates(monkeypatch):
    """Pin exchange rates so conversion assertions are deterministic."""
    monkeypatch.setattr(currency, "get_exchange_rates",
                        lambda: currency.ExchangeRates(rates=dict(currency.FALLBACK_RATES),
                                                       timestamp=datetime.now(), source="test"))


def isolate(monkeypatch, usage=None, summary=None, cached=None, signed_in=True):
    """Replace the network, the credential and the cache."""
    token = dc.deepseek_auth.Credential("tok", "saved") if signed_in else None
    monkeypatch.setattr(dc.deepseek_auth, "discover_user_token", lambda: token)
    monkeypatch.setattr(dc, "fetch_usage", lambda token, today: usage or du.UsageData(error="no usage"))
    monkeypatch.setattr(dc, "fetch_summary",
                        lambda token: summary or du.AccountSummary(error="no summary"))
    monkeypatch.setattr(dc, "save_cache", lambda **kwargs: None)
    monkeypatch.setattr(dc, "load_deepseek_cache", lambda: (cached, None))


def day(iso, cost=0.0, tokens=0, requests=0, hit=0, out=0):
    return du.DayUsage(date=iso, cost=cost, tokens=tokens, requests=requests,
                       cache_hit=hit, output=out)


def summary(**kw):
    base = dict(available=True, currency="USD", topped_up=14.58, granted=0.0,
                total_cost=15.42, total_cost_available=True)
    base.update(kw)
    return du.AccountSummary(**base)


class TestSignedOut:
    """With nothing signed in the panel shows nothing. Replaying the cache
    would look like live data, which is what someone sees right after signing
    out, so the credential is checked before any fallback runs."""

    def test_no_token_reports_not_signed_in(self, monkeypatch):
        fixed_rates(monkeypatch)
        isolate(monkeypatch, signed_in=False)
        snap = dc.DataCollector().collect_deepseek()
        assert not snap.balance_available
        assert not snap.usage_available
        assert snap.error_message == "Not signed in"
        assert snap.status_level == "error"

    def test_no_token_ignores_a_populated_cache(self, monkeypatch):
        from models import DeepSeekSnapshot
        fixed_rates(monkeypatch)
        cached = DeepSeekSnapshot(balance_available=True, balance_usable=True,
                                  balance_total=99.99, usage_available=True,
                                  today_cost_usd=5.0, month_cost_usd=50.0,
                                  daily_costs=[1.0, 2.0, 3.0])
        isolate(monkeypatch, cached=cached, signed_in=False)

        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_total == 0.0
        assert snap.month_cost_usd == 0.0
        assert snap.daily_costs == []
        assert not snap.is_stale


class TestCurrencyNormalisation:
    """Every cost field is USD, so a CNY account must not render a CNY figure
    behind a dollar sign."""

    def test_cny_usage_costs_are_converted(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        isolate(monkeypatch, usage=du.UsageData(available=True, currency="CNY",
                                                days=[day(today, cost=7.10)]))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available
        assert round(snap.today_cost_usd, 4) == 1.0

    def test_a_summary_balance_is_already_usd(self, monkeypatch):
        """fetch_summary normalises to USD itself, so the collector copies the
        figure straight through and keeps the reported currency as a label."""
        fixed_rates(monkeypatch)
        isolate(monkeypatch, summary=summary(currency="CNY", topped_up=10.0, granted=0.0))
        snap = dc.DataCollector().collect_deepseek()
        assert round(snap.balance_total, 3) == 10.0
        assert snap.balance_currency == "CNY"


class TestAssembly:
    def test_windows_are_filled_from_the_day_buckets(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today()
        days = [day(today.isoformat(), cost=1.0, tokens=100, requests=5),
                day(today.replace(day=1).isoformat(), cost=2.0, tokens=200, requests=6)]
        isolate(monkeypatch, usage=du.UsageData(available=True, days=days))
        snap = dc.DataCollector().collect_deepseek()

        assert snap.today_cost_usd == 1.0
        assert snap.today_tokens == 100
        assert len(snap.daily_costs) == 7          # a full week, zero-filled
        assert snap.daily_costs[-1] == 1.0
        assert snap.last7_cost_usd == 1.0 + (2.0 if today.day == 1 else 0.0)

    def test_week_tokens_track_the_week_field(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        isolate(monkeypatch, usage=du.UsageData(available=True, days=[day(today, tokens=500)]))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.week_tokens == snap.last7_tokens == 500

    def test_top_model_comes_from_the_month_totals(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        usage = du.UsageData(available=True, days=[day(today, cost=1.0)],
                             models=[du.ModelTotal("deepseek-flash", 9.0, 100),
                                     du.ModelTotal("deepseek-v4-pro", 1.0, 10)])
        isolate(monkeypatch, usage=usage)
        snap = dc.DataCollector().collect_deepseek()
        assert snap.models_used[0].model == "deepseek-flash"

    def test_previous_month_is_totalled_separately(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today()
        previous = today.replace(day=1) - timedelta(days=1)
        days = [day(today.isoformat(), cost=1.0, tokens=100),
                day(previous.isoformat(), cost=4.0, tokens=400)]
        isolate(monkeypatch, usage=du.UsageData(available=True, days=days))
        snap = dc.DataCollector().collect_deepseek()

        assert snap.month_cost_usd == 1.0
        assert snap.month_tokens == 100
        assert snap.prev_month_cost_usd == 4.0
        assert snap.prev_month_tokens == 400

    def test_previous_month_costs_are_converted_too(self, monkeypatch):
        fixed_rates(monkeypatch)
        previous = date.today().replace(day=1) - timedelta(days=1)
        isolate(monkeypatch, usage=du.UsageData(available=True, currency="CNY",
                                                days=[day(previous.isoformat(), cost=7.10,
                                                          tokens=70)]))
        snap = dc.DataCollector().collect_deepseek()
        assert round(snap.prev_month_cost_usd, 4) == 1.0
        assert snap.prev_month_tokens == 70


class TestSessionSummary:
    """The account summary is the only source of the balance and the lifetime
    total, so it is what makes a signed-in session self-sufficient."""

    def test_token_supplies_balance_and_lifetime_spend(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        isolate(monkeypatch, usage=du.UsageData(available=True, days=[day(today, cost=1.0)]),
                summary=summary())
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available
        assert round(snap.balance_total, 2) == 14.58
        assert snap.balance_usable                     # a positive balance is usable
        assert snap.usage_available
        assert snap.total_cost_available
        assert round(snap.total_cost_usd, 2) == 15.42
        assert snap.status_level == "normal"

    def test_an_empty_balance_is_not_usable(self, monkeypatch):
        fixed_rates(monkeypatch)
        isolate(monkeypatch, summary=summary(topped_up=0.0, granted=0.0))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available
        assert not snap.balance_usable
        assert snap.balance_message == "Add credits"

    def test_lifetime_total_is_optional(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        isolate(monkeypatch, usage=du.UsageData(available=True, days=[day(today, cost=1.0)]),
                summary=summary(total_cost_available=False))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available
        assert not snap.total_cost_available

    def test_usage_survives_a_failed_summary(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        isolate(monkeypatch, usage=du.UsageData(available=True, days=[day(today, cost=1.0)]))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available
        assert not snap.balance_available
        # Usage is the substance, so its presence is the healthy state even
        # when the summary call failed.
        assert snap.status_level == "normal"


class TestCacheFallback:
    """The cache covers a transient failure, never a missing credential, and
    never overwrites a good entry with an empty fetch."""

    def test_a_failed_fetch_keeps_the_cached_balance(self, monkeypatch):
        from models import DeepSeekSnapshot
        fixed_rates(monkeypatch)
        cached = DeepSeekSnapshot(balance_available=True, balance_usable=True, balance_total=12.0)
        isolate(monkeypatch, cached=cached)
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available
        assert snap.balance_total == 12.0
        assert snap.is_stale

    def test_cached_usage_is_kept_when_the_fetch_fails(self, monkeypatch):
        from models import DeepSeekSnapshot
        fixed_rates(monkeypatch)
        cached = DeepSeekSnapshot(usage_available=True, month_cost_usd=50.0,
                                  daily_costs=[1.0, 2.0, 3.0])
        isolate(monkeypatch, cached=cached)
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available
        assert snap.month_cost_usd == 50.0
        assert snap.is_stale

    def test_the_cached_previous_month_comes_back_too(self, monkeypatch):
        from models import DeepSeekSnapshot
        fixed_rates(monkeypatch)
        cached = DeepSeekSnapshot(usage_available=True, prev_month_cost_usd=12.5,
                                  prev_month_tokens=1250)
        isolate(monkeypatch, cached=cached)
        snap = dc.DataCollector().collect_deepseek()
        assert snap.prev_month_cost_usd == 12.5
        assert snap.prev_month_tokens == 1250
        assert snap.is_stale


class TestCacheRoundTrip:
    """The previous month has to survive a save and a load, and a cache written
    by an older build has no such key at all."""

    def test_previous_month_survives_a_round_trip(self, monkeypatch, tmp_path):
        import snapshot_cache
        from models import DeepSeekSnapshot
        monkeypatch.setattr(snapshot_cache, "get_cache_path",
                            lambda: tmp_path / "snapshot_cache.json")
        snapshot_cache.save_cache(deepseek=DeepSeekSnapshot(
            usage_available=True, prev_month_cost_usd=12.5, prev_month_tokens=1250))

        loaded, _ = snapshot_cache.load_deepseek_cache()
        assert loaded.prev_month_cost_usd == 12.5
        assert loaded.prev_month_tokens == 1250

    def test_a_cache_without_the_key_still_loads(self, monkeypatch, tmp_path):
        import json
        import snapshot_cache
        path = tmp_path / "snapshot_cache.json"
        monkeypatch.setattr(snapshot_cache, "get_cache_path", lambda: path)
        path.write_text(json.dumps({"deepseek": {"usage_available": True, "month_cost_usd": 3.0},
                                    "deepseek_cached_at": "2026-09-15T10:00:00"}),
                        encoding="utf-8")

        loaded, _ = snapshot_cache.load_deepseek_cache()
        assert loaded.month_cost_usd == 3.0
        assert loaded.prev_month_cost_usd == 0.0
        assert loaded.prev_month_tokens == 0
