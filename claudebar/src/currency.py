"""Currency conversion support for ClaudeBar."""

from dataclasses import dataclass
from typing import Optional
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from config import get_config_dir
from retry import with_retry


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
}


@dataclass
class ExchangeRates:
    """Exchange rates from USD to other currencies."""
    rates: dict[str, float]
    timestamp: datetime
    source: str = "fallback"


def get_rates_cache_path() -> Path:
    """Get the path to cached exchange rates."""
    return get_config_dir() / "exchange_rates.json"


def load_cached_rates() -> Optional[ExchangeRates]:
    """Load exchange rates from cache."""
    cache_path = get_rates_cache_path()
    if not cache_path.exists():
        return None

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        timestamp = datetime.fromisoformat(data["timestamp"])

        # Cache valid for 24 hours
        if datetime.now() - timestamp > timedelta(hours=24):
            return None

        return ExchangeRates(
            rates=data["rates"],
            timestamp=timestamp,
            source=data.get("source", "cache"),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def save_rates_cache(rates: ExchangeRates) -> None:
    """Save exchange rates to cache."""
    cache_path = get_rates_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({
                "rates": rates.rates,
                "timestamp": rates.timestamp.isoformat(),
                "source": rates.source,
            }, f)
    except IOError:
        pass


@with_retry(max_attempts=3, base_delay=1.0)
def fetch_exchange_rates() -> Optional[ExchangeRates]:
    """Fetch current exchange rates from a free API."""
    try:
        # Use exchangerate-api.com free tier (no API key needed for USD base)
        url = "https://open.er-api.com/v6/latest/USD"
        req = urllib.request.Request(url, headers={"User-Agent": "ClaudeBar/1.0"})

        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("result") == "success":
            all_rates = data.get("rates", {})
            rates = {
                currency: all_rates.get(currency, FALLBACK_RATES[currency])
                for currency in CURRENCIES.keys()
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

    return None


def get_exchange_rates() -> ExchangeRates:
    """Get exchange rates (from cache, API, or fallback)."""
    # Try cache first
    cached = load_cached_rates()
    if cached:
        return cached

    # Try fetching from API
    fetched = fetch_exchange_rates()
    if fetched:
        return fetched

    # Use fallback rates
    return ExchangeRates(
        rates=FALLBACK_RATES.copy(),
        timestamp=datetime.now(),
        source="fallback",
    )


def convert_usd(amount_usd: float, currency: str, rates: Optional[ExchangeRates] = None) -> float:
    """Convert USD amount to target currency."""
    if currency == "USD":
        return amount_usd

    if rates is None:
        rates = get_exchange_rates()

    rate = rates.rates.get(currency, 1.0)
    return amount_usd * rate


def format_currency(amount_usd: float, currency: str, rates: Optional[ExchangeRates] = None) -> str:
    """Format an amount in the specified currency."""
    if currency not in CURRENCIES:
        currency = "USD"

    converted = convert_usd(amount_usd, currency, rates)
    symbol = CURRENCIES[currency]["symbol"]

    # Format based on magnitude
    if converted < 0.01:
        return f"{symbol}{converted:.4f}"
    elif converted < 100:
        return f"{symbol}{converted:.2f}"
    else:
        return f"{symbol}{converted:,.0f}"


def get_currency_symbol(currency: str) -> str:
    """Get the symbol for a currency."""
    return CURRENCIES.get(currency, CURRENCIES["USD"])["symbol"]


def get_supported_currencies() -> list[str]:
    """Get list of supported currency codes."""
    return list(CURRENCIES.keys())
