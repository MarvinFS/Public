"""DeepSeek usage parsing: envelope shapes, windows and auth failures."""

import json
from datetime import date, datetime

import pytest

import currency
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

    def test_previous_month_days_are_the_month_before(self):
        days = self.DAYS + [d.DayUsage(date="2026-08-31", cost=99.0),
                            d.DayUsage(date="2026-07-31", cost=55.0)]
        previous = d.previous_month_days(days, date(2026, 9, 15))
        assert [x.date for x in previous] == ["2026-08-31"]
        assert previous[0].cost == 99.0

    def test_previous_month_rolls_back_across_a_year(self):
        days = [d.DayUsage(date="2025-12-31", cost=1.0), d.DayUsage(date="2026-01-01", cost=2.0)]
        previous = d.previous_month_days(days, date(2026, 1, 1))
        assert [x.date for x in previous] == ["2025-12-31"]

    def test_month_boundary_window_reaches_back(self):
        days = [d.DayUsage(date="2026-08-31", cost=3.0), d.DayUsage(date="2026-09-01", cost=1.0)]
        window = d.last_n_days(days, date(2026, 9, 1), 7)
        assert window[0].date == "2026-08-26"
        assert sum(w.cost for w in window) == 4.0


class TestFetchUsage:
    def test_no_token_short_circuits(self):
        assert d.fetch_usage("").error == "No platform token"

    def test_both_months_are_always_fetched(self, monkeypatch):
        """The panel reports the previous month, so it is fetched every time,
        not only while the 7-day window can still reach into it."""
        calls = []
        monkeypatch.setattr(d, "_get", lambda url, headers: (calls.append(url) or
                                                            {"code": 0, "data": {"biz_data": {}}}, None))
        d.fetch_usage("token", today=date(2026, 9, 20))
        assert sorted(calls) == sorted([
            f"{d.USAGE_AMOUNT_URL}?month={m}&year=2026" for m in (9, 8)
        ] + [
            f"{d.USAGE_COST_URL}?month={m}&year=2026" for m in (9, 8)
        ])

    def test_january_reaches_back_into_december(self, monkeypatch):
        calls = []
        monkeypatch.setattr(d, "_get", lambda url, headers: (calls.append(url) or
                                                            {"code": 0, "data": {"biz_data": {}}}, None))
        d.fetch_usage("token", today=date(2026, 1, 3))
        assert any("month=1&year=2026" in url for url in calls)
        assert any("month=12&year=2025" in url for url in calls)

    def test_early_in_a_month_the_chart_reaches_two_months_back(self, monkeypatch):
        """1 March: the 31-day chart starts on 30 January, so January is
        fetched too instead of drawing those days as empty."""
        calls = []
        monkeypatch.setattr(d, "_get", lambda url, headers: (calls.append(url) or
                                                            {"code": 0, "data": {"biz_data": {}}}, None))
        d.fetch_usage("token", today=date(2026, 3, 1))
        months = {url.split("?")[1] for url in calls}
        assert months == {f"month={m}&year=2026" for m in (3, 2, 1)}

    def test_models_come_from_the_current_month_only(self, monkeypatch):
        """The top-model row is a share of this month's spend, so folding the
        previous month in would let a model report more than 100%."""
        amount = amount_payload([("20260831", DAY_ROWS)], total=[{"model": "old-model", "usage": DAY_ROWS}])
        cost = cost_payload([("20260831", DAY_COST)], total=[{"model": "old-model", "usage": DAY_COST}])
        current_amount = amount_payload([("20260902", DAY_ROWS)],
                                        total=[{"model": "deepseek-flash", "usage": DAY_ROWS}])
        current_cost = cost_payload([("20260902", DAY_COST)],
                                    total=[{"model": "deepseek-flash", "usage": DAY_COST}])

        def fake_get(url, headers):
            month = 8 if "month=8" in url else 9
            payload = (amount if month == 8 else current_amount) if "amount" in url \
                else (cost if month == 8 else current_cost)
            return payload, None

        monkeypatch.setattr(d, "_get", fake_get)
        merged = d.fetch_usage("token", today=date(2026, 9, 2))
        assert [m.model for m in merged.models] == ["deepseek-flash"]
        # ...while both months still contribute their days.
        assert any(day.date.startswith("2026-08") for day in merged.days)

    def test_an_expired_token_surfaces_as_unavailable(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: ({"code": 40003, "data": None}, None))
        assert not d.fetch_usage("token", today=date(2026, 9, 20)).available

    # How one month's two calls answer, for the tests below.
    EMPTY = {"code": 0, "msg": "", "data": {"biz_data": {}}}        # answered, nothing in it
    EXPIRED = {"code": 40003, "data": None}                          # answered: session rejected

    @staticmethod
    def months(monkeypatch, **answers):
        """Both calls of a month answer alike: a payload dict, or an error text for a call that
        did not complete. September is the current month (today is the 20th), August the previous."""
        rows = {"amount": lambda day: amount_payload([(day, DAY_ROWS)]),
                "cost": lambda day: cost_payload([(day, DAY_COST)])}

        def fake_get(url, headers):
            answer = answers["sep" if "month=9" in url else "aug"]
            if answer == "ok":
                kind = "amount" if "amount" in url else "cost"
                return rows[kind]("20260912" if "month=9" in url else "20260820"), None
            if isinstance(answer, str):
                return None, answer
            return answer, None

        monkeypatch.setattr(d, "_get", fake_get)

    @pytest.mark.parametrize("sep, aug", [("Rate limited", "ok"), ("ok", "HTTP 500"),
                                          ("ok", EXPIRED), (EXPIRED, "ok")])
    def test_a_month_that_failed_is_not_a_complete_reading(self, monkeypatch, sep, aug):
        """The figures of the months that did answer would read as the whole: with the current
        month missing, today and this month came out as a fresh zero."""
        self.months(monkeypatch, sep=sep, aug=aug)
        usage = d.fetch_usage("token", today=date(2026, 9, 20))
        assert not usage.available
        assert usage.error and usage.days == []

    def test_the_reason_given_is_the_failure_not_an_empty_months_answer(self, monkeypatch):
        """The current month answered with nothing and the previous one failed: what the panel
        is told is the failure."""
        self.months(monkeypatch, sep=self.EMPTY, aug="HTTP 500")
        usage = d.fetch_usage("token", today=date(2026, 9, 20))
        assert not usage.available and usage.error == "HTTP 500"

    @pytest.mark.parametrize("sep, aug, month", [("ok", EMPTY, "2026-09"), (EMPTY, "ok", "2026-08")])
    def test_a_month_that_answered_with_nothing_is_empty_not_failed(self, monkeypatch, sep, aug, month):
        """A new month has no spend yet, and an account may have none in the previous one: that is
        an answer, and the other month's days stand."""
        self.months(monkeypatch, sep=sep, aug=aug)
        usage = d.fetch_usage("token", today=date(2026, 9, 20))
        assert usage.available
        assert {day.date[:7] for day in usage.days} == {month}

    def test_months_that_all_answer_with_nothing_are_an_unexpected_response(self, monkeypatch):
        self.months(monkeypatch, sep=self.EMPTY, aug=self.EMPTY)
        usage = d.fetch_usage("token", today=date(2026, 9, 20))
        assert not usage.available and usage.error == "Unexpected usage response"


SUMMARY = {"code": 0, "msg": "", "data": {"biz_code": 0, "biz_data": {
    "normal_wallets": [{"currency": "USD", "balance": "14.5807473736000000", "token_estimation": "0"}],
    "bonus_wallets": [{"currency": "USD", "balance": "0", "token_estimation": "0"}],
    "total_costs": [{"currency": "USD", "amount": "15.4192526264000000"}],
}}}


class TestAccountSummary:
    """The session summary is the only source of the lifetime total, and the
    only way a platform token alone can show a balance."""

    def test_reads_balance_and_lifetime_spend(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: (SUMMARY, None))
        s = d.fetch_summary("token")
        assert s.available
        assert round(s.topped_up, 4) == 14.5807
        assert s.granted == 0.0
        assert round(s.total, 4) == 14.5807
        assert round(s.total_cost, 4) == 15.4193
        assert s.total_cost_available

    def test_no_token_never_calls_the_api(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: (_ for _ in ()).throw(AssertionError()))
        assert d.fetch_summary("").error == "No platform token"

    def test_expired_session_is_reported(self, monkeypatch):
        monkeypatch.setattr(d, "_get", lambda url, headers: ({"code": 40003, "data": None}, None))
        assert d.fetch_summary("token").error == "Session expired"

    def test_a_summary_without_costs_still_gives_the_balance(self, monkeypatch):
        payload = json.loads(json.dumps(SUMMARY))
        del payload["data"]["biz_data"]["total_costs"]
        monkeypatch.setattr(d, "_get", lambda url, headers: (payload, None))
        s = d.fetch_summary("token")
        assert s.available and not s.total_cost_available
        assert round(s.total, 4) == 14.5807

    def test_granted_balance_adds_to_the_total(self, monkeypatch):
        payload = json.loads(json.dumps(SUMMARY))
        payload["data"]["biz_data"]["bonus_wallets"] = [{"currency": "USD", "balance": "10.00"}]
        monkeypatch.setattr(d, "_get", lambda url, headers: (payload, None))
        s = d.fetch_summary("token")
        assert round(s.total, 4) == 24.5807

    def test_a_cny_summary_is_converted(self, monkeypatch):
        # Pin the rates: the live table would make the expected value drift.
        rates = currency.ExchangeRates(rates=dict(currency.FALLBACK_RATES),
                                       timestamp=datetime.now(), source="test")
        payload = json.loads(json.dumps(SUMMARY))
        payload["data"]["biz_data"]["normal_wallets"] = [{"currency": "CNY", "balance": "71.0"}]
        payload["data"]["biz_data"]["bonus_wallets"] = []
        payload["data"]["biz_data"]["total_costs"] = [{"currency": "CNY", "amount": "71.0"}]
        monkeypatch.setattr(d, "_get", lambda url, headers: (payload, None))
        s = d.fetch_summary("token", rates)
        assert round(s.topped_up, 3) == 10.0      # 71 / 7.10
        assert round(s.total_cost, 3) == 10.0
