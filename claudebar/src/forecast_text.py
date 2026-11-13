"""The words on the quota and balance views, built from the forecasts.

Pure: every time and zone is passed in. Rows are (label, value, tone) with the
tone one of "normal", "ok", "warn" or "muted", which the panel maps to its
colours; an empty label is a continuation line.

The panel says no fact twice: the limit bars, the banked-resets block and the
DeepSeek balance above the tabs keep what they show, and these views carry only
what those do not (the burn rate, the clock times, the gain of a reset).
"""

from datetime import datetime, timedelta
from typing import Callable, Optional

import banked_resets
from forecast import BalanceForecast, QuotaForecast, YEAR_DAYS

MID_DOT = "·"

# A weekday name is unambiguous only within a week of now.
WEEKDAY_WITHIN = timedelta(days=6)

# What the balance forecast's spend rate averages (forecast.forecast_balance takes the last seven days).
BALANCE_BASIS = "last 7 days"


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
        return "No recent use"
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


def hours_text(hours: float) -> str:
    """"3d 4h" from a day up, else "0h 24m"."""
    if hours == float("inf"):
        return "never"
    d, r = divmod(hours, 24)
    if d >= 1:
        return f"{int(d)}d {int(r)}h"
    return f"{int(r)}h {int((r % 1) * 60)}m"


def runout_text(hours: float) -> str:
    """The run-out as the limit bar words it, "Runs out in 0h 24m". The quota view's row
    gives the clock time only, so one prediction is not said twice."""
    return f"Runs out in {hours_text(hours)}"


def percent_left(value: float) -> str:
    return f"{value:.0f}% left"


def gain_text(f: QuotaForecast) -> str:
    """What the next reset adds back, e.g. "+58%"; empty when there is none."""
    return f"+{f.gain:.0f}%" if f.gain is not None else ""


def quota_header(f: QuotaForecast, window_hours: float, session: bool) -> tuple:
    """The burn rate, the one figure only this view has, and the window it was measured on
    ("5-hour session", "30-day window"). The percent left is the limit bar's."""
    return rate_text(f.rate), f"{window_title(window_hours)} {'session' if session else 'window'}"


def quota_rows(f: QuotaForecast, banked: Optional[banked_resets.BankedResets],
               now: datetime, tz=None) -> list:
    """What the limit bars and the banked block do not say: the gain and the time of the next
    reset, the run-out as a clock time (the bar has the countdown), what is left unused when it
    lasts (the bar says it lasts), and whether a banked reset would restore a run-out."""
    rows = []
    if f.next_reset is not None:
        rows.append(("Next reset", f"{gain_text(f)} {MID_DOT} {stamp(f.next_reset, now, tz)}", "normal"))

    if f.runout is not None:
        rows.append(("At this rate", f"Runs out {stamp(f.runout, now, tz)}", "warn"))
        if banked_resets.restores_at(banked, f.runout, now):
            rows.append(("", "A banked reset would restore it", "muted"))
    elif f.unused is not None:
        rows.append(("At this rate", f"~{f.unused:.0f}% unused at reset", "ok"))

    if f.recording and f.since is not None:
        rows.append(("Recording since", stamp(f.since, now, tz), "muted"))
    return rows


def balance_header(f: BalanceForecast, money: Callable) -> tuple:
    """The spend rate, the one figure the balance block above does not give, and what it averages."""
    if f.rate_per_day <= 0:
        return "No spend", BALANCE_BASIS
    return f"~{money(f.rate_per_day)}/day", BALANCE_BASIS


def balance_rows(f: BalanceForecast, now: datetime, tz) -> list:
    if f.rate_per_day <= 0:
        return []
    if f.days_left > YEAR_DAYS:
        return [("At this rate", "Lasts over a year", "ok")]
    days = "<1 day" if f.days_left < 1 else (
        "1 day" if round(f.days_left) == 1 else f"{round(f.days_left)} days")
    return [("Runs out", f"~{_date(f.runout, tz)} ({days})", "normal")]


def tip_text(when: datetime, value: float, projected: bool, now: datetime, tz,
             fmt: Callable) -> str:
    """The hover flyout: "Tue 14:00 · 54% left", plus "(projected)" past now."""
    text = f"{stamp(when, now, tz)} {MID_DOT} {fmt(value)}"
    return text + " (projected)" if projected else text
