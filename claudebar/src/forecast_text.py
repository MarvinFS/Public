"""The words on the quota and balance views, built from the forecasts.

Pure: every time and zone is passed in. Rows are (label, value, tone) with the
tone one of "normal", "ok", "warn" or "muted", which the panel maps to its
colours; an empty label is a continuation line.
"""

from datetime import datetime, timedelta
from typing import Callable, Optional

import banked_resets
from forecast import BalanceForecast, QuotaForecast, YEAR_DAYS

MID_DOT = "·"

# A weekday name is unambiguous only within a week of now.
WEEKDAY_WITHIN = timedelta(days=6)


def window_title(hours: float) -> str:
    """"Weekly" for a seven-day window, else its length ("30-day")."""
    if abs(hours - 168) < 1:
        return "Weekly"
    days = hours / 24
    if days >= 2 and abs(days - round(days)) < 0.05:
        return f"{round(days)}-day"
    return f"{round(hours)}-hour"


def rate_text(rate: float) -> str:
    """"~0.9%/h": more decimals the smaller the rate, so it never reads 0."""
    if rate <= 0:
        return "no recent use"
    if rate >= 10:
        return f"~{rate:.0f}%/h"
    if rate >= 0.1:
        return f"~{rate:.1f}%/h"
    return f"~{rate:.2f}%/h"


def stamp(when: datetime, now: datetime, tz=None) -> str:
    """"Tue 22:00" within a week of now, "Nov 4 12:00" beyond that."""
    local = when.astimezone(tz)
    if abs(when - now) < WEEKDAY_WITHIN:
        return f"{local:%a %H:%M}"
    return f"{local:%b} {local.day} {local:%H:%M}"


def _date(when: datetime, tz=None) -> str:
    local = when.astimezone(tz)
    return f"{local:%b} {local.day}"


def percent_left(value: float) -> str:
    return f"{value:.0f}% left"


def gain_text(f: QuotaForecast) -> str:
    """What the next reset adds back, e.g. "+58%"; empty when there is none."""
    return f"+{f.gain:.0f}%" if f.gain is not None else ""


def quota_header(f: QuotaForecast) -> tuple:
    return percent_left(f.remaining), rate_text(f.rate)


def quota_rows(f: QuotaForecast, banked: Optional[banked_resets.BankedResets],
               now: datetime, tz=None) -> list:
    rows = []
    if f.next_reset is not None:
        rows.append(("Next reset", f"{gain_text(f)} {MID_DOT} {stamp(f.next_reset, now, tz)}", "normal"))

    if f.runout is not None:
        rows.append(("At this rate", f"Runs out {stamp(f.runout, now, tz)}", "warn"))
        if banked_resets.restores_at(banked, f.runout, now):
            rows.append(("", "A banked reset would restore it", "muted"))
    elif f.unused is not None:
        rows.append(("At this rate", f"Lasts until reset {MID_DOT} ~{f.unused:.0f}% unused", "ok"))

    if banked is not None and banked.state == "ok" and banked.available > 0:
        soonest = banked_resets.next_expiry(banked, now)
        text = f"{banked.available} available"
        if soonest is not None:
            text += f", expires {_date(soonest, tz)}"
        urgent = soonest is not None and soonest - now <= banked_resets.URGENT_WITHIN
        rows.append(("Banked", text, "warn" if urgent else "normal"))

    if f.recording and f.since is not None:
        rows.append(("Recording since", stamp(f.since, now, tz), "muted"))
    return rows


def balance_header(f: BalanceForecast, money_fixed: Callable, money: Callable) -> tuple:
    return f"{money_fixed(f.balance)} left", f"~{money(f.rate_per_day)}/day"


def balance_rows(f: BalanceForecast, now: datetime, tz, money: Callable) -> list:
    if f.rate_per_day <= 0:
        return [("Spend rate", "none in the last 7 days", "muted")]
    rows = []
    if f.days_left > YEAR_DAYS:
        rows.append(("At this rate", "Lasts over a year", "ok"))
    else:
        days = "<1 day" if f.days_left < 1 else (
            "1 day" if round(f.days_left) == 1 else f"{round(f.days_left)} days")
        rows.append(("Runs out", f"~{_date(f.runout, tz)} ({days})", "normal"))
    rows.append(("Spend rate", f"{money(f.rate_per_day)}/day, last 7 days", "normal"))
    return rows


def tip_text(when: datetime, value: float, projected: bool, now: datetime, tz,
             fmt: Callable) -> str:
    """The hover flyout: "Tue 14:00 · 54% left", plus "(projected)" past now."""
    text = f"{stamp(when, now, tz)} {MID_DOT} {fmt(value)}"
    return text + " (projected)" if projected else text
