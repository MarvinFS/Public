"""How the collector assembles a DeepSeek snapshot from the two sources."""

from datetime import date, datetime

import currency
import data_collector as dc
import deepseek_usage as du


def fixed_rates(monkeypatch):
    """Pin exchange rates so conversion assertions are deterministic."""
    monkeypatch.setattr(currency, "get_exchange_rates",
                        lambda: currency.ExchangeRates(rates=dict(currency.FALLBACK_RATES),
                                                       timestamp=datetime.now(), source="test"))


def isolate(monkeypatch, balance=None, usage=None, cached=None):
    """Replace the network, the key discovery and the cache."""
    monkeypatch.setattr(dc.deepseek_auth, "discover_api_key",
                        lambda: dc.deepseek_auth.Credential("sk-test", "saved"))
    monkeypatch.setattr(dc.deepseek_auth, "discover_user_token",
                        lambda: dc.deepseek_auth.Credential("tok", "saved") if usage else None)
    monkeypatch.setattr(dc, "fetch_balance", lambda key: balance or du.BalanceInfo(error="no balance"))
    monkeypatch.setattr(dc, "fetch_usage", lambda token, today: usage or du.UsageData(error="no usage"))
    monkeypatch.setattr(dc, "save_cache", lambda **kwargs: None)
    monkeypatch.setattr(dc, "load_deepseek_cache", lambda: (cached, None))


def day(iso, cost=0.0, tokens=0, requests=0, hit=0, out=0):
    return du.DayUsage(date=iso, cost=cost, tokens=tokens, requests=requests,
                       cache_hit=hit, output=out)


class TestCurrencyNormalisation:
    """Every cost field in every snapshot is USD, so a CNY account must not
    render a CNY figure behind a dollar sign."""

    def test_a_cny_balance_is_converted(self, monkeypatch):
        fixed_rates(monkeypatch)
        isolate(monkeypatch, balance=du.BalanceInfo(available=True, currency="CNY",
                                                    total=71.0, topped_up=71.0, granted=0.0))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available
        assert round(snap.balance_total, 4) == 10.0      # 71 CNY / 7.10
        assert snap.balance_currency == "CNY"            # the source is still known

    def test_a_usd_balance_is_untouched(self, monkeypatch):
        fixed_rates(monkeypatch)
        isolate(monkeypatch, balance=du.BalanceInfo(available=True, currency="USD", total=15.18))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_total == 15.18

    def test_cny_usage_costs_are_converted(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today().isoformat()
        usage = du.UsageData(available=True, currency="CNY", days=[day(today, cost=7.10)])
        isolate(monkeypatch, usage=usage)
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available
        assert round(snap.today_cost_usd, 4) == 1.0


class TestAssembly:
    def test_windows_are_filled_from_the_day_buckets(self, monkeypatch):
        fixed_rates(monkeypatch)
        today = date.today()
        days = [day((today).isoformat(), cost=1.0, tokens=100, requests=5),
                day((today.replace(day=1)).isoformat(), cost=2.0, tokens=200, requests=6)]
        isolate(monkeypatch, usage=du.UsageData(available=True, days=days))
        snap = dc.DataCollector().collect_deepseek()

        assert snap.today_cost_usd == 1.0
        assert snap.today_tokens == 100
        assert snap.today_requests == 5
        # A month that began today still includes today
        assert snap.month_cost_usd >= 1.0
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


class TestDegradedStates:
    def test_balance_without_a_platform_token(self, monkeypatch):
        fixed_rates(monkeypatch)
        isolate(monkeypatch, balance=du.BalanceInfo(available=True, currency="USD", total=15.18))
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available and not snap.usage_available
        assert snap.usage_error == "No platform token"
        assert snap.status_level == "warning"

    def test_no_api_key_reports_it_plainly(self, monkeypatch):
        fixed_rates(monkeypatch)
        isolate(monkeypatch)
        monkeypatch.setattr(dc.deepseek_auth, "discover_api_key", lambda: None)
        snap = dc.DataCollector().collect_deepseek()
        assert not snap.balance_available
        assert snap.error_message == "No API key"
        assert snap.status_level == "error"

    def test_a_failed_balance_keeps_the_cached_one(self, monkeypatch):
        fixed_rates(monkeypatch)
        from models import DeepSeekSnapshot
        cached = DeepSeekSnapshot(balance_available=True, balance_usable=True, balance_total=12.0)
        isolate(monkeypatch, cached=cached)
        snap = dc.DataCollector().collect_deepseek()
        assert snap.balance_available
        assert snap.balance_total == 12.0
        assert snap.is_stale
