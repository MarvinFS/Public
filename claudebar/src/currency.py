"""Currency conversion support for ClaudeBar."""

from dataclasses import dataclass
from typing import Optional
import json
from datetime import datetime, timedelta
from pathlib import Path

import http_json
from config import atomic_write_text, get_config_dir
from version import USER_AGENT


# Supported currencies
CURRENCIES = {
    "USD": {"symbol": "$", "name": "US Dollar"},
    "EUR": {"symbol": "€", "name": "Euro"},
    "GBP": {"symbol": "£", "name": "British Pound"},
    "RUB": {"symbol": "₽", "name": "Russian Ruble"},
    "RON": {"symbol": "lei", "name": "Romanian Leu"},
}

# Fallback exchange rates (USD base) - updated periodically
FALLBACK_RATES = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "RUB": 97.5,
    "RON": 4.58,
    "CNY": 7.10,
}

# Rates to keep in the table. CNY is not a display currency in the dropdown,
# but DeepSeek's platform reports its cost in CNY for some accounts, so the
# rate is needed to normalise those figures to USD.
RATE_CURRENCIES = list(CURRENCIES.keys()) + ["CNY"]


@dataclass
class ExchangeRates:
    """Exchange rates from USD to other currencies."""
    rates: dict[str, float]
    timestamp: datetime
    source: str = "fallback"


def get_rates_cache_path() -> Path:
    """Get the path to cached exchange rates."""
    return get_config_dir() / "exchange_rates.json"


# Rates this old are fetched again when they are needed.
RATES_MAX_AGE = timedelta(hours=24)


def _read_rates_cache() -> Optional[ExchangeRates]:
    """The rates in the cache file, however old; None when there are none.

    The cache is a convenience behind a refresh, so no file may stop one: a sharing violation
    or a missing file (OSError), bad JSON or UTF-8 (ValueError), valid JSON of the wrong shape
    (a list where an object belongs, a number where the time goes: TypeError, AttributeError,
    KeyError) all count as no cache."""
    try:
        with open(get_rates_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)

        rates = data["rates"]
        timestamp = datetime.fromisoformat(data["timestamp"])
        if (not isinstance(rates, dict)
                or not all(isinstance(r, (int, float)) and not isinstance(r, bool) for r in rates.values())
                or timestamp.tzinfo is not None):       # save_rates_cache writes a naive local time
            return None
        return ExchangeRates(rates=rates, timestamp=timestamp, source=data.get("source", "cache"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def load_cached_rates() -> Optional[ExchangeRates]:
    """Load exchange rates from cache, if they are no older than RATES_MAX_AGE."""
    cached = _read_rates_cache()
    if cached is None or datetime.now() - cached.timestamp > RATES_MAX_AGE:
        return None
    return cached


def save_rates_cache(rates: ExchangeRates) -> None:
    """Save exchange rates to cache."""
    try:
        atomic_write_text(get_rates_cache_path(), json.dumps({
            "rates": rates.rates,
            "timestamp": rates.timestamp.isoformat(),
            "source": rates.source,
        }))
    except OSError:
        pass


# A failed download is remembered, so a host that is down costs one timeout per ten
# minutes and not one per refresh.
_fetch_failures = http_json.FailureMemory()


def fetch_exchange_rates() -> Optional[ExchangeRates]:
    """Fetch current exchange rates from a free API.

    Blocks for up to its timeout, so only a background thread may call it (the panel's
    loader and the refresh thread); everything the Tk thread calls uses local_exchange_rates.
    """
    if _fetch_failures.blocked():
        return None
    try:
        # Use exchangerate-api.com free tier (no API key needed for USD base)
        url = "https://open.er-api.com/v6/latest/USD"
        # Public and credential-free, so it may follow a redirect.
        data = http_json.request_json(url, {"User-Agent": USER_AGENT}, timeout=5,
                                      follow_redirects=True)

        if data.get("result") == "success":
            all_rates = data.get("rates", {})
            rates = {
                currency: all_rates.get(currency, FALLBACK_RATES[currency])
                for currency in RATE_CURRENCIES
            }

            exchange_rates = ExchangeRates(
                rates=rates,
                timestamp=datetime.now(),
                source="api",
            )

            # Cache the rates
            save_rates_cache(exchange_rates)

            return exchange_rates
    except Exception:
        pass

    _fetch_failures.failed()
    return None


def _fallback_rates() -> ExchangeRates:
    return ExchangeRates(
        rates=FALLBACK_RATES.copy(),
        timestamp=datetime.now(),
        source="fallback",
    )


def local_exchange_rates() -> ExchangeRates:
    """Rates from the cache file, else the bundled table. Never touches the network, so
    it is what the Tk thread and every conversion without rates of its own use. A cache past
    its age still counts: real rates a day old are closer than the bundled ones."""
    return _read_rates_cache() or _fallback_rates()


def get_exchange_rates() -> ExchangeRates:
    """Get exchange rates (from cache, API, or fallback). May fetch, so background
    threads only. When the cache has expired and the download fails, the expired cache
    is used, not the bundled table."""
    return load_cached_rates() or fetch_exchange_rates() or _read_rates_cache() or _fallback_rates()


def convert_usd(amount_usd: float, currency: str, rates: Optional[ExchangeRates] = None) -> float:
    """Convert USD amount to target currency."""
    if currency == "USD":
        return amount_usd

    if rates is None:
        rates = local_exchange_rates()

    rate = rates.rates.get(currency, 1.0)
    return amount_usd * rate


def to_usd(amount: float, currency: str, rates: Optional[ExchangeRates] = None) -> float:
    """Convert an amount in `currency` to USD.

    Every cost field in the snapshots is USD, so a source that reports in
    another currency (DeepSeek's platform can report CNY) is normalised at the
    point of ingest rather than downstream.
    """
    code = (currency or "USD").upper()
    if code == "USD":
        return amount

    if rates is None:
        rates = local_exchange_rates()

    rate = rates.rates.get(code)
    if not rate:  # unknown currency: leave the figure unconverted rather than zero it
        return amount
    return amount / rate


def format_currency(amount_usd: float, currency: str, rates: Optional[ExchangeRates] = None) -> str:
    """Format an amount in the specified currency."""
    if currency not in CURRENCIES:
        currency = "USD"

    converted = convert_usd(amount_usd, currency, rates)
    symbol = CURRENCIES[currency]["symbol"]

    # Format based on magnitude. An exact zero reads as money ("$0.00"), not as
    # the four-decimal form reserved for genuinely sub-cent spending.
    if converted == 0:
        return f"{symbol}0.00"
    elif abs(converted) < 0.01:
        return f"{symbol}{converted:.4f}"
    elif abs(converted) < 100:
        return f"{symbol}{converted:.2f}"
    else:
        return f"{symbol}{converted:,.0f}"


def get_currency_symbol(currency: str) -> str:
    """Get the symbol for a currency."""
    return CURRENCIES.get(currency, CURRENCIES["USD"])["symbol"]


def get_supported_currencies() -> list[str]:
    """Get list of supported currency codes."""
    return list(CURRENCIES.keys())
