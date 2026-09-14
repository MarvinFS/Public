"""DeepSeek usage parsing: envelope shapes, windows and auth failures."""

from datetime import date

import deepseek_usage as d


def amount_payload(days, total=None, biz_key="object"):
    """Build an amount envelope. `biz_key` selects the object or list shape."""
    biz = {
        "total": total if total is not None else [],
        "days": [{"date": day, "data": [{"model": "deepseek-flash", "usage": rows}]}
                 for day, rows in days],
    }
    data = {"biz_data": biz if biz_key == "object" else [biz]}
    return {"code": 0, "msg": "", "data": data}


def cost_payload(days, total=None, currency="USD", biz_key="list"):
    biz = {
        "currency": currency,
        "total": total if total is not None else [],
        "days": [{"date": day, "data": [{"model": "deepseek-flash", "usage": rows}]}
                 for day, rows in days],
    }
    data = {"biz_data": biz if biz_key == "object" else [biz]}
    return {"code": 0, "msg": "", "data": data}


DAY_ROWS = [
    {"type": "REQUEST", "amount": "10"},
    {"type": "PROMPT_CACHE_HIT_TOKEN", "amount": "1000"},
    {"type": "PROMPT_CACHE_MISS_TOKEN", "amount": "200"},
    {"type": "RESPONSE_TOKEN", "amount": "50"},
]
DAY_COST = [{"type": "PROMPT_CACHE_HIT_TOKEN", "amount": "0.30"},
            {"type": "RESPONSE_TOKEN", "amount": "0.20"}]


class TestParsing:
    def test_reads_the_object_shape(self):
        parsed = d.parse_usage(amount_payload([("20260912", DAY_ROWS)]),
                               cost_payload([("20260912", DAY_COST)]))
        assert parsed.available
        assert parsed.currency == "USD"
        assert len(parsed.days) == 1

    def test_reads_the_list_shape(self):
        """The cost endpoint wraps biz_data in a one-element list."""
        parsed = d.parse_usage(amount_payload([("20260912", DAY_ROWS)], biz_key="list"),
                               cost_payload([("20260912", DAY_COST)], biz_key="list"))
        assert parsed.available
        assert parsed.days[0].cost == 0.5

    def test_splits_tokens_by_type(self):
        parsed = d.parse_usage(amount_payload([("20260912", DAY_ROWS)]),
                               cost_payload([("20260912", DAY_COST)]))
        day = parsed.days[0]
        assert day.tokens == 1250
        assert day.cache_hit == 1000
        assert day.cache_miss == 200
        assert day.output == 50
        assert day.requests == 10

    def test_cost_ignores_request_rows(self):
        """REQUEST rows carry a count, not money; counting them corrupts cost."""
        rows = DAY_COST + [{"type": "REQUEST", "amount": "9999"}]
        parsed = d.parse_usage(amount_payload([("20260912", DAY_ROWS)]),
                               cost_payload([("20260912", rows)]))
        assert parsed.days[0].cost == 0.5

    def test_compact_dates_are_normalised(self):
        parsed = d.parse_usage(amount_payload([("20260912", DAY_ROWS)]),
                               cost_payload([("20260912", DAY_COST)]))
        assert parsed.days[0].date == "2026-09-12"

    def test_model_totals_come_from_the_month_block(self):
        total = [{"model": "deepseek-flash", "usage": DAY_ROWS},
                 {"model": "deepseek-v4-pro", "usage": [{"type": "RESPONSE_TOKEN", "amount": "10"}]}]
        parsed = d.parse_usage(
            amount_payload([("20260912", DAY_ROWS)], total=total),
            cost_payload([("20260912", DAY_COST)],
                         total=[{"model": "deepseek-flash", "usage": DAY_COST}]))
        assert parsed.models[0].model == "deepseek-flash"
        assert parsed.models[0].cost == 0.5

    def test_unexpected_payload_is_unavailable_not_an_exception(self):
        parsed = d.parse_usage({"code": 0, "data": {"biz_data": {}}}, {})
        assert not parsed.available
        assert parsed.error

    def test_garbage_amounts_do_not_raise(self):
        rows = [{"type": "RESPONSE_TOKEN", "amount": "not-a-number"}]
        parsed = d.parse_usage(amount_payload([("20260912", rows)]),
                               cost_payload([("20260912", [])]))
        assert parsed.available
        assert parsed.days[0].tokens == 0


class TestAuthErrors:
    """A rejected token answers HTTP 200 with an error envelope."""

    def test_top_level_code(self):
        assert d.envelope_auth_error({"code": 40003, "msg": "Authorization Failed", "data": None})

    def test_expired_code(self):
        assert d.envelope_auth_error({"code": 40002})

    def test_nested_biz_code(self):
        assert d.envelope_auth_error({"code": 0, "data": {"biz_code": 40003}})

    def test_healthy_envelope_is_not_an_error(self):
        assert not d.envelope_auth_error({"code": 0, "data": {"biz_data": {}}})

    def test_parse_reports_session_expired(self):
        parsed = d.parse_usage({"code": 40003, "data": None}, {"code": 40003, "data": None})
        assert not parsed.available
        assert parsed.error == "Session expired"


class TestWindows:
    DAYS = [d.DayUsage(date=f"2026-09-{n:02d}", cost=float(n), tokens=n * 100)
            for n in range(1, 16)]

    def test_last_seven_days_are_zero_filled_and_ordered(self):
        window = d.last_n_days(self.DAYS, date(2026, 9, 15), 7)
        assert [w.date for w in window] == [f"2026-09-{n:02d}" for n in range(9, 16)]
        assert window[-1].cost == 15.0

    def test_missing_days_read_as_zero(self):
        window = d.last_n_days([d.DayUsage(date="2026-09-15", cost=5.0)], date(2026, 9, 15), 7)
        assert len(window) == 7
        assert sum(w.cost for w in window) == 5.0

    def test_month_days_exclude_other_months(self):
        days = self.DAYS + [d.DayUsage(date="2026-08-31", cost=99.0)]
        assert all(x.date.startswith("2026-09") for x in d.month_days(days, date(2026, 9, 15)))

    def test_month_boundary_window_reaches_back(self):
        days = [d.DayUsage(date="2026-08-31", cost=3.0), d.DayUsage(date="2026-09-01", cost=1.0)]
        window = d.last_n_days(days, date(2026, 9, 1), 7)
        assert window[0].date == "2026-08-26"
        assert sum(w.cost for w in window) == 4.0


class TestBalance:
    def test_usd_is_preferred_over_other_currencies(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: ({
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "100.00", "granted_balance": "0", "topped_up_balance": "100"},
                {"currency": "USD", "total_balance": "15.21", "granted_balance": "0", "topped_up_balance": "15.21"},
            ],
        }, None))
        info = d.fetch_balance("sk-test")
        assert info.currency == "USD"
        assert info.total == 15.21
        assert info.available is True

    def test_missing_key_never_calls_the_api(self, monkeypatch):
        def explode(url, headers):
            raise AssertionError("should not fetch without a key")
        monkeypatch.setattr(d, "_get", explode)
        assert d.fetch_balance("").error == "No API key"

    def test_http_401_reads_as_an_expired_session(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: (None, "Session expired"))
        assert d.fetch_balance("sk-test").error == "Session expired"

    def test_empty_balance_list_is_an_error(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: ({"is_available": True, "balance_infos": []}, None))
        assert d.fetch_balance("sk-test").error


class TestFetchUsage:
    def test_no_token_short_circuits(self):
        assert d.fetch_usage("").error == "No platform token"

    def test_two_months_are_fetched_near_a_month_boundary(self, monkeypatch):
        calls = []

        def fake_get(url, headers):
            calls.append(url)
            return {"code": 0, "data": {"biz_data": {}}}, None

        monkeypatch.setattr(d, "_get", fake_get)
        d.fetch_usage("token", today=date(2026, 9, 3))
        assert any("month=9" in url for url in calls)
        assert any("month=8" in url for url in calls)

    def test_one_month_is_enough_mid_month(self, monkeypatch):
        calls = []
        monkeypatch.setattr(d, "_get", lambda url, headers: (calls.append(url) or
                                                             {"code": 0, "data": {"biz_data": {}}}, None))
        d.fetch_usage("token", today=date(2026, 9, 20))
        assert len(calls) == 2  # amount + cost for the current month only

    def test_an_expired_token_surfaces_as_unavailable(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: ({"code": 40003, "data": None}, None))
        assert not d.fetch_usage("token", today=date(2026, 9, 20)).available
