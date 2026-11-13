"""Nothing the Tk thread calls goes to the network.

Formatting and conversion use the rates they are given or the cache file; the fetch
belongs to the panel's loader thread and the refresh thread, and a download that failed
is not tried again for ten minutes.
"""

import json
import urllib.request
from datetime import datetime

import pytest

import currency
import data_collector as dc
import deepseek_usage as du
import http_json
import model_catalog
import ui_window
from currency import ExchangeRates, FALLBACK_RATES
from models import Engine


@pytest.fixture
def downloads(monkeypatch):
    """Every download any module tries, as a list; each one fails like a dead host."""
    attempts = []

    def urlopen(request, timeout=None):
        attempts.append(request.full_url)
        raise OSError("host is down")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return attempts


def write_cache(rates, hours_old=1):
    from datetime import timedelta
    stamp = (datetime.now() - timedelta(hours=hours_old)).isoformat()
    currency.get_rates_cache_path().parent.mkdir(parents=True, exist_ok=True)
    currency.get_rates_cache_path().write_text(
        json.dumps({"rates": rates, "timestamp": stamp, "source": "api"}), encoding="utf-8")


class TestConversionNeverFetches:
    def test_no_cache_means_the_bundled_table(self, monkeypatch):
        fetched = []
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: fetched.append(1))
        assert currency.convert_usd(2.0, "EUR") == pytest.approx(2.0 * FALLBACK_RATES["EUR"])
        assert currency.to_usd(7.10, "CNY") == pytest.approx(1.0)
        assert currency.format_currency(2.0, "RON") == "lei9.16"
        assert fetched == []

    def test_an_expired_cache_is_not_a_reason_to_fetch_either(self, monkeypatch):
        """It still beats the bundled table: real rates a day old are closer than rates that
        were typed into the source months ago."""
        write_cache({"EUR": 0.5}, hours_old=30)
        fetched = []
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: fetched.append(1))
        assert currency.convert_usd(2.0, "EUR") == pytest.approx(1.0)
        assert fetched == []

    def test_a_fresh_cache_is_used(self, downloads):
        write_cache({"EUR": 0.5})
        assert currency.convert_usd(2.0, "EUR") == pytest.approx(1.0)
        assert downloads == []

    def test_given_rates_win(self, downloads):
        rates = ExchangeRates(rates={"EUR": 3.0, "CNY": 2.0}, timestamp=datetime.now())
        assert currency.convert_usd(2.0, "EUR", rates) == 6.0
        assert currency.to_usd(6.0, "CNY", rates) == 3.0
        assert downloads == []

    def test_the_loader_path_still_fetches(self, monkeypatch):
        """get_exchange_rates is the one that may go to the network."""
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: ExchangeRates({"EUR": 9.0}, datetime.now()))
        assert currency.get_exchange_rates().rates == {"EUR": 9.0}


class TestTheLoaderWhenTheCacheHasExpired:
    def test_a_day_old_cache_beats_the_bundled_table_when_the_download_fails(self, monkeypatch):
        write_cache({"EUR": 0.5, "CNY": 7.0}, hours_old=30)
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: None)
        rates = currency.get_exchange_rates()
        assert rates.rates == {"EUR": 0.5, "CNY": 7.0}
        assert rates.source == "api"                      # where those rates came from, as saved

    def test_a_download_that_works_beats_an_expired_cache(self, monkeypatch):
        write_cache({"EUR": 0.5}, hours_old=30)
        fresh = ExchangeRates({"EUR": 0.7}, datetime.now(), "api")
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: fresh)
        assert currency.get_exchange_rates() is fresh

    def test_a_fresh_cache_is_used_without_a_download(self, monkeypatch):
        write_cache({"EUR": 0.5}, hours_old=1)
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: pytest.fail("downloaded"))
        assert currency.get_exchange_rates().rates == {"EUR": 0.5}

    def test_no_usable_cache_and_no_download_is_the_bundled_table(self, monkeypatch):
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: None)
        rates = currency.get_exchange_rates()
        assert rates.rates == FALLBACK_RATES and rates.source == "fallback"
        currency.get_rates_cache_path().parent.mkdir(parents=True, exist_ok=True)
        currency.get_rates_cache_path().write_text("{not json", encoding="utf-8")
        assert currency.get_exchange_rates().source == "fallback"


class TestAFailedDownloadIsRemembered:
    def test_the_rates_are_not_asked_for_again_within_ten_minutes(self, downloads):
        assert currency.fetch_exchange_rates() is None
        assert currency.fetch_exchange_rates() is None
        assert currency.get_exchange_rates().source == "fallback"
        assert len(downloads) == 1

    def test_they_are_asked_for_again_after_ten_minutes(self, downloads, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(currency, "_fetch_failures",
                            http_json.FailureMemory(seconds=600, clock=lambda: now[0]))
        currency.fetch_exchange_rates()
        now[0] += 601
        currency.fetch_exchange_rates()
        assert len(downloads) == 2

    def test_a_refresh_with_the_catalog_down_asks_once(self, downloads, monkeypatch, tmp_path):
        monkeypatch.setattr(model_catalog, "get_config_dir", lambda: tmp_path)
        for _ in range(3):                                   # three refreshes
            assert model_catalog.apply() is False
        assert len(downloads) == 1

    def test_the_catalog_is_asked_again_after_ten_minutes(self, downloads, monkeypatch, tmp_path):
        monkeypatch.setattr(model_catalog, "get_config_dir", lambda: tmp_path)
        now = [1000.0]
        monkeypatch.setattr(model_catalog, "_fetch_failures",
                            http_json.FailureMemory(seconds=600, clock=lambda: now[0]))
        model_catalog.apply()
        now[0] += 601
        model_catalog.apply()
        assert len(downloads) == 2


class TestOneRefreshResolvesTheRatesOnce:
    CNY_SUMMARY = {"code": 0, "data": {"biz_code": 0, "biz_data": {
        "normal_wallets": [{"currency": "CNY", "balance": "71.0"}],
        "bonus_wallets": [{"currency": "CNY", "balance": "7.1"}],
        "total_costs": [{"currency": "CNY", "amount": "142.0"}]}}}

    def test_a_cny_refresh_with_the_rates_host_down_tries_it_once(self, downloads, monkeypatch):
        today = datetime.now().date().isoformat()
        monkeypatch.setattr(dc.deepseek_auth, "discover_user_token",
                            lambda: dc.deepseek_auth.Credential("tok", "saved"))
        monkeypatch.setattr(du, "_get", lambda url, headers: (self.CNY_SUMMARY, None))
        monkeypatch.setattr(dc, "fetch_usage", lambda token, day: du.UsageData(
            available=True, currency="CNY", days=[du.DayUsage(date=today, cost=7.10)]))
        monkeypatch.setattr(dc, "save_cache", lambda **kwargs: None)
        monkeypatch.setattr(dc, "load_deepseek_cache", lambda: (None, None))
        # The failure memory is the second line of defence; this proves the first: the
        # table is resolved once per refresh, so four conversions are not four tries.
        monkeypatch.setattr(currency, "_fetch_failures", http_json.FailureMemory(seconds=0),
                            raising=False)

        snap = dc.DataCollector().collect_deepseek()

        assert len(downloads) == 1
        assert snap.balance_total == pytest.approx(10.0 + 1.0)       # 78.1 CNY at 7.10
        assert snap.today_cost_usd == pytest.approx(1.0)


class TestARatesFileThatCannotBeUsed:
    """The rates cache is a convenience behind a refresh, so a file that cannot be used is no
    cache: it must not be an exception out of every refresh that wants a rate."""

    NOW = datetime.now().isoformat()

    @pytest.mark.parametrize("content", [
        pytest.param([1, 2], id="a list"),
        pytest.param("text", id="a string"),
        pytest.param({"rates": {"EUR": 0.5}, "timestamp": 1.7e9}, id="a number for the time"),
        pytest.param({"rates": {"EUR": 0.5}, "timestamp": None}, id="no time"),
        pytest.param({"rates": {"EUR": 0.5}}, id="no time at all"),
        pytest.param({"rates": {"EUR": 0.5}, "timestamp": "2026-11-12T10:00:00+00:00"}, id="a time with a zone"),
        pytest.param({"rates": [0.5], "timestamp": NOW}, id="a list of rates"),
        pytest.param({"rates": {"EUR": "high"}, "timestamp": NOW}, id="a rate that is not a number"),
        pytest.param({"rates": None, "timestamp": NOW}, id="no rates"),
    ])
    def test_a_file_of_the_wrong_shape_is_no_cache(self, content, monkeypatch):
        currency.get_rates_cache_path().parent.mkdir(parents=True, exist_ok=True)
        currency.get_rates_cache_path().write_text(json.dumps(content), encoding="utf-8")
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: None)
        assert currency.load_cached_rates() is None
        assert currency.local_exchange_rates().source == "fallback"
        assert currency.convert_usd(2.0, "EUR") == pytest.approx(2.0 * FALLBACK_RATES["EUR"])
        assert currency.get_exchange_rates().source == "fallback"

    def test_a_file_another_process_holds_is_no_cache(self, monkeypatch):
        write_cache({"EUR": 0.5})
        real_open = open

        def locked(path, *args, **kwargs):
            if str(path) == str(currency.get_rates_cache_path()):
                raise PermissionError(13, "The process cannot access the file")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(currency, "open", locked, raising=False)
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: None)
        assert currency.load_cached_rates() is None
        assert currency.local_exchange_rates().source == "fallback"
        assert currency.get_exchange_rates().source == "fallback"

    def test_a_directory_in_its_place_is_no_cache(self, monkeypatch):
        currency.get_rates_cache_path().mkdir(parents=True)
        monkeypatch.setattr(currency, "fetch_exchange_rates", lambda: None)
        assert currency.load_cached_rates() is None
        assert currency.get_exchange_rates().source == "fallback"

    def test_a_good_file_is_still_a_cache(self):
        write_cache({"EUR": 0.5, "CNY": 7})
        cached = currency.load_cached_rates()
        assert cached is not None and cached.rates == {"EUR": 0.5, "CNY": 7}


class TestARefreshAsksForRatesOnlyWhenAnAmountNeedsThem:
    """A USD account never converts anything, so it must not read the rates file or call the
    rates host; one that is not in USD asks once, for the usage and the summary together."""

    USD_SUMMARY = {"code": 0, "data": {"biz_code": 0, "biz_data": {
        "normal_wallets": [{"currency": "USD", "balance": "14.58"}],
        "total_costs": [{"currency": "USD", "amount": "15.42"}]}}}

    @pytest.fixture
    def account(self, monkeypatch):
        """A signed-in account whose platform answers in the currency the test names."""
        today = datetime.now().date().isoformat()

        def sign_in(currency_code, summary):
            monkeypatch.setattr(dc.deepseek_auth, "discover_user_token",
                                lambda: dc.deepseek_auth.Credential("tok", "saved"))
            monkeypatch.setattr(du, "_get", lambda url, headers: (summary, None))
            monkeypatch.setattr(dc, "fetch_usage", lambda token, day: du.UsageData(
                available=True, currency=currency_code, days=[du.DayUsage(date=today, cost=7.10)]))
            monkeypatch.setattr(dc, "save_cache", lambda **kwargs: None)
            monkeypatch.setattr(dc, "load_deepseek_cache", lambda: (None, None))

        return sign_in

    def test_a_usd_account_makes_no_rate_request_and_reads_no_rates_file(self, account, downloads, monkeypatch):
        account("USD", self.USD_SUMMARY)
        read = []
        monkeypatch.setattr(currency, "_read_rates_cache", lambda: read.append(1))

        snap = dc.DataCollector().collect_deepseek()

        assert snap.usage_available and snap.balance_available
        assert downloads == [] and read == []

    def test_a_usd_account_is_not_stopped_by_a_rates_file_of_the_wrong_shape(self, account, downloads):
        account("USD", self.USD_SUMMARY)
        currency.get_rates_cache_path().parent.mkdir(parents=True, exist_ok=True)
        currency.get_rates_cache_path().write_text("[1, 2]", encoding="utf-8")
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available and snap.balance_available

    def test_a_cny_account_is_not_stopped_by_a_rates_file_of_the_wrong_shape_either(
            self, account, downloads):
        account("CNY", TestOneRefreshResolvesTheRatesOnce.CNY_SUMMARY)
        currency.get_rates_cache_path().parent.mkdir(parents=True, exist_ok=True)
        currency.get_rates_cache_path().write_text("[1, 2]", encoding="utf-8")
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available and snap.balance_available
        assert snap.today_cost_usd == pytest.approx(1.0)            # at the bundled 7.10

    def test_a_cny_account_resolves_the_rates_once_for_usage_and_summary(self, account, downloads, monkeypatch):
        account("CNY", TestOneRefreshResolvesTheRatesOnce.CNY_SUMMARY)
        resolved = []
        real = dc.get_exchange_rates
        monkeypatch.setattr(dc, "get_exchange_rates", lambda: resolved.append(1) or real())
        snap = dc.DataCollector().collect_deepseek()
        assert snap.usage_available and snap.balance_available
        assert len(resolved) == 1


class TestThePanel:
    @pytest.fixture
    def panel(self, monkeypatch):
        monkeypatch.setattr(ui_window.ClaudeBarWindow, "_load_initial_data", lambda self: None)
        return ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                         on_exit=lambda: None)

    def test_saving_the_settings_does_not_fetch_rates(self, panel, monkeypatch):
        asked = []
        monkeypatch.setattr(ui_window, "get_exchange_rates", lambda: asked.append(1))
        monkeypatch.setattr(ui_window.ClaudeBarWindow, "_rebuild_window", lambda self: None)
        panel._window = None
        panel._on_settings_saved()
        assert asked == []

    def test_a_missing_icon_is_not_downloaded(self, panel, monkeypatch, tmp_path, downloads):
        missing = tmp_path / "icons" / "claude_icon.png"
        meta = ui_window._ENGINE_META[Engine.CLAUDE]
        monkeypatch.setitem(ui_window._ENGINE_META, Engine.CLAUDE,
                            tuple(missing if hasattr(part, "exists") else part for part in meta))
        assert panel._engine_icon_images(Engine.CLAUDE) is None     # the text button takes over
        assert downloads == []
        assert not missing.exists()

    def test_the_three_marks_ship_with_the_app(self):
        for engine, meta in ui_window._ENGINE_META.items():
            icon = next(part for part in meta if hasattr(part, "exists"))
            assert icon.exists(), f"{engine} has no bundled icon"
