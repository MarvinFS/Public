"""DeepSeek usage data.

Two surfaces, two credentials:

* ``fetch_balance`` hits the documented public API with an API key.
* ``fetch_usage`` hits the platform's private dashboard endpoints, which need
  the signed-in platform ``userToken``. An API key is rejected there with
  HTTP 200 and a body-level error code, so auth failures are detected from the
  envelope, never from the status line alone.

The private endpoints are undocumented and may change, so every parser here is
tolerant: unknown shapes degrade to "usage unavailable" rather than raising.
"""

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

BALANCE_URL = "https://api.deepseek.com/user/balance"
USAGE_AMOUNT_URL = "https://platform.deepseek.com/api/v0/usage/amount"
USAGE_COST_URL = "https://platform.deepseek.com/api/v0/usage/cost"

# The platform endpoints answer a browser session, not a bare client
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
REFERER = "https://platform.deepseek.com/usage"

# Envelope codes that mean "this token is not valid", returned with HTTP 200
AUTH_ERROR_CODES = (40002, 40003)

TIMEOUT = 15.0

# Usage row types
REQUEST = "REQUEST"
TOKEN_TYPES = ("PROMPT_CACHE_HIT_TOKEN", "PROMPT_CACHE_MISS_TOKEN", "RESPONSE_TOKEN")

DAILY_HISTORY_DAYS = 31


@dataclass
class BalanceInfo:
    """Balance from the public API. `available` is the API's is_available flag."""
    available: bool = False
    currency: str = "USD"
    total: float = 0.0
    granted: float = 0.0
    topped_up: float = 0.0
    error: Optional[str] = None


@dataclass
class DayUsage:
    """One calendar day of platform usage."""
    date: str  # ISO "YYYY-MM-DD"
    cost: float = 0.0
    tokens: int = 0
    requests: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    output: int = 0


@dataclass
class ModelTotal:
    """Month-to-date total for one model."""
    model: str
    cost: float = 0.0
    tokens: int = 0


@dataclass
class UsageData:
    """Parsed platform usage for the fetched window(s)."""
    available: bool = False
    error: Optional[str] = None
    currency: str = "USD"
    days: list[DayUsage] = field(default_factory=list)
    models: list[ModelTotal] = field(default_factory=list)


def _to_float(value) -> float:
    """Platform numbers arrive as strings, floats, or not at all."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value) -> int:
    return int(_to_float(value))


def _normalize_date(value) -> str:
    """'20260912' and '2026-09-12' both become '2026-09-12'."""
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _biz_data(payload: dict) -> dict:
    """`data.biz_data` is an object on one endpoint and a 1-element list on the
    other; accept either."""
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        return {}
    biz = data.get("biz_data")
    if isinstance(biz, list):
        return biz[0] if biz and isinstance(biz[0], dict) else {}
    return biz if isinstance(biz, dict) else {}


def envelope_auth_error(payload) -> bool:
    """True when the body reports an expired or rejected token, even on HTTP 200."""
    if not isinstance(payload, dict):
        return False
    if payload.get("code") in AUTH_ERROR_CODES:
        return True
    data = payload.get("data")
    return isinstance(data, dict) and data.get("biz_code") in AUTH_ERROR_CODES


def _get(url: str, headers: dict) -> tuple[Optional[dict], Optional[str]]:
    """GET JSON. Returns (payload, error)."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "Session expired"
        if e.code == 429:
            return None, "Rate limited"
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "Invalid JSON response"
    except Exception as e:  # never let a fetcher take the refresh loop down
        return None, str(e)


def fetch_balance(api_key: str) -> BalanceInfo:
    """Balance for an API key. USD is preferred when several are reported."""
    if not api_key:
        return BalanceInfo(error="No API key")

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    payload, error = _get(BALANCE_URL, headers)
    if error:
        return BalanceInfo(error=error)
    if not isinstance(payload, dict):
        return BalanceInfo(error="Unexpected balance response")

    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return BalanceInfo(error="No balance reported")

    chosen = next((i for i in infos if isinstance(i, dict) and
                   str(i.get("currency", "")).upper() == "USD"), None)
    if chosen is None:
        chosen = next((i for i in infos if isinstance(i, dict)), None)
    if chosen is None:
        return BalanceInfo(error="No balance reported")

    return BalanceInfo(
        available=bool(payload.get("is_available")),
        currency=str(chosen.get("currency") or "USD").upper(),
        total=_to_float(chosen.get("total_balance")),
        granted=_to_float(chosen.get("granted_balance")),
        topped_up=_to_float(chosen.get("topped_up_balance")),
    )


def _usage_headers(user_token: str) -> dict:
    return {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/json",
        "User-Agent": BROWSER_UA,
        "Referer": REFERER,
    }


def parse_usage(amount_payload: dict, cost_payload: dict) -> UsageData:
    """Merge the amount and cost envelopes into per-day and per-model totals.

    Token counts come from `amount`, money from `cost`. Days are keyed by
    ISO date; a date present in only one of the two endpoints keeps the values
    it does have.
    """
    if envelope_auth_error(amount_payload) or envelope_auth_error(cost_payload):
        return UsageData(error="Session expired")

    amount_biz = _biz_data(amount_payload)
    cost_biz = _biz_data(cost_payload)
    if not amount_biz and not cost_biz:
        return UsageData(error="Unexpected usage response")

    currency = str(cost_biz.get("currency") or "USD").upper()
    days: dict[str, DayUsage] = {}

    def day(key: str) -> DayUsage:
        if key not in days:
            days[key] = DayUsage(date=key)
        return days[key]

    # Per-day buckets
    for biz, is_cost in ((amount_biz, False), (cost_biz, True)):
        for entry in biz.get("days") or []:
            if not isinstance(entry, dict):
                continue
            key = _normalize_date(entry.get("date"))
            if not key:
                continue
            target = day(key)
            for model_block in entry.get("data") or []:
                if not isinstance(model_block, dict):
                    continue
                for row in model_block.get("usage") or []:
                    if not isinstance(row, dict):
                        continue
                    _accumulate(target, row, is_cost)

    # Month-to-date per model, for the top-model row
    model_cost: dict[str, float] = {}
    model_tokens: dict[str, int] = {}
    if isinstance(cost_biz.get("total"), list):
        for model_block in cost_biz["total"]:
            if not isinstance(model_block, dict):
                continue
            name = str(model_block.get("model") or "(unknown)")
            for row in model_block.get("usage") or []:
                if isinstance(row, dict) and str(row.get("type") or "") != REQUEST:
                    model_cost[name] = model_cost.get(name, 0.0) + _to_float(row.get("amount"))
    if isinstance(amount_biz.get("total"), list):
        for model_block in amount_biz["total"]:
            if not isinstance(model_block, dict):
                continue
            name = str(model_block.get("model") or "(unknown)")
            for row in model_block.get("usage") or []:
                if isinstance(row, dict) and str(row.get("type") or "") != REQUEST:
                    model_tokens[name] = model_tokens.get(name, 0) + _to_int(row.get("amount"))

    models = [
        ModelTotal(model=name, cost=model_cost.get(name, 0.0), tokens=model_tokens.get(name, 0))
        for name in set(model_cost) | set(model_tokens)
    ]
    models.sort(key=lambda m: m.cost, reverse=True)

    return UsageData(
        available=True,
        currency=currency,
        days=sorted(days.values(), key=lambda d: d.date),
        models=models,
    )


def _accumulate(target: DayUsage, row: dict, is_cost: bool) -> None:
    """Fold one usage row into a day bucket.

    The cost endpoint reports money in `amount` for every non-REQUEST row; the
    amount endpoint reports token counts. REQUEST rows are counts on both.
    """
    kind = str(row.get("type") or "")
    value = _to_float(row.get("amount"))

    if kind == REQUEST:
        target.requests += int(value)
        return

    if is_cost:
        target.cost += value
        return

    target.tokens += int(value)
    if kind == "PROMPT_CACHE_HIT_TOKEN":
        target.cache_hit += int(value)
    elif kind == "PROMPT_CACHE_MISS_TOKEN":
        target.cache_miss += int(value)
    elif kind == "RESPONSE_TOKEN":
        target.output += int(value)


def fetch_month(user_token: str, year: int, month: int) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    """Fetch the amount and cost envelopes for one month, concurrently."""
    if not user_token:
        return None, None, "No platform token"

    headers = _usage_headers(user_token)
    amount_url = f"{USAGE_AMOUNT_URL}?month={month}&year={year}"
    cost_url = f"{USAGE_COST_URL}?month={month}&year={year}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        amount_future = pool.submit(_get, amount_url, headers)
        cost_future = pool.submit(_get, cost_url, headers)
        amount_payload, amount_error = amount_future.result()
        cost_payload, cost_error = cost_future.result()

    return amount_payload, cost_payload, (amount_error or cost_error)


def fetch_usage(user_token: str, today: Optional[date] = None) -> UsageData:
    """Usage covering today, the current month and the trailing 7 days.

    The previous month is fetched as well while the 7-day window can still
    reach into it, so the chart does not lose its oldest bars at a month
    boundary.
    """
    if not user_token:
        return UsageData(error="No platform token")

    today = today or date.today()
    months = [(today.year, today.month)]
    if today.day <= 7:
        previous = today.replace(day=1) - timedelta(days=1)
        months.append((previous.year, previous.month))

    merged = UsageData(available=True)
    errors: list[str] = []
    seen_models: dict[str, ModelTotal] = {}

    for year, month in months:
        amount_payload, cost_payload, error = fetch_month(user_token, year, month)
        if error:
            errors.append(error)
            continue
        parsed = parse_usage(amount_payload or {}, cost_payload or {})
        if not parsed.available:
            if parsed.error:
                errors.append(parsed.error)
            continue
        merged.days.extend(parsed.days)
        merged.currency = parsed.currency or merged.currency
        for model in parsed.models:
            existing = seen_models.get(model.model)
            if existing:
                existing.cost += model.cost
                existing.tokens += model.tokens
            else:
                seen_models[model.model] = ModelTotal(model.model, model.cost, model.tokens)

    if not merged.days:
        return UsageData(error=errors[0] if errors else "No usage reported")

    merged.days.sort(key=lambda d: d.date)
    merged.models = sorted(seen_models.values(), key=lambda m: m.cost, reverse=True)
    return merged


def last_n_days(days: list[DayUsage], today: date, count: int) -> list[DayUsage]:
    """`count` consecutive day buckets ending today, zero-filled for gaps."""
    by_date = {d.date: d for d in days}
    window = []
    for offset in range(count - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        window.append(by_date.get(key) or DayUsage(date=key))
    return window


def month_days(days: list[DayUsage], today: date) -> list[DayUsage]:
    """Day buckets falling in today's calendar month."""
    prefix = today.strftime("%Y-%m")
    return [d for d in days if d.date.startswith(prefix)]
