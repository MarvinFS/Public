"""Quota forecast: the drain rate, the projection and the chart's time axis.

Pure functions. Nothing here reads the clock or touches Tk or the disk: every
time is passed in as an aware datetime, so each case is a plain example. A
Codex reset time arrives naive and local, so callers pass it through as_utc
first.

Remaining quota is a percentage (0-100); a balance is the same shape with
dollars and no reset.
"""

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

FULL = 100.0

# The rules below are written for a week. A shorter window (the 5-hour
# session) scales the rate lookback, the minimum rate span and the young
# history to its length; a longer one keeps them.
WEEK_HOURS = 168.0

# The rate is read from this much recent history: it measures how the quota is
# being used now, not over the whole window.
RATE_LOOKBACK = timedelta(hours=72)
# Less history than this and the rate is too noisy; use the window average.
MIN_RATE_SPAN_HOURS = 6.0

# Chart view: a window under a day shows one window back and one ahead, so the
# reset is always in view. Up to eight days it shows three days back and four
# ahead. A longer one stretches the forward range to its reset (back is three
# quarters of that, so "Now" always sits three sevenths across).
DAY_HOURS = 24
SHORT_WINDOW_HOURS = 8 * 24
VIEW_BACK = timedelta(hours=72)
VIEW_AHEAD = timedelta(hours=96)
VIEW_BACK_RATIO = VIEW_BACK / VIEW_AHEAD

# Recording is called young while the history covers less than this.
YOUNG_HISTORY = timedelta(hours=72)

# At most this many day labels on the axis.
MAX_TICKS = 7

# A balance that lasts longer than this is not given a run-out date.
YEAR_DAYS = 365

# A recording that began less than this after its window did needs no estimate.
ESTIMATE_MIN_GAP = timedelta(minutes=30)


def as_utc(when: Optional[datetime]) -> Optional[datetime]:
    """Aware UTC. A naive time is read as local, which is what Codex sends."""
    if when is None:
        return None
    return when.astimezone(timezone.utc)


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _scale(window_hours: float) -> float:
    """How much of a week the window is, never more than one."""
    return min(window_hours / WEEK_HOURS, 1.0)


def window_rate(percent_used: float, resets_at: Optional[datetime], window_hours: float,
                now: datetime) -> float:
    """Percent per hour spent so far in this window: the fallback rate, on the
    same basis as models.project_window. A reset time already past (stale data)
    counts the whole window, because the percent belongs to the window that
    ended; next_reset() rolls the schedule forward separately."""
    if resets_at is None:
        return 0.0
    remaining = max(_hours(as_utc(resets_at) - now), 0.0)
    elapsed = max(window_hours - remaining, 0.0)
    return percent_used / elapsed if elapsed > 0 else 0.0


def drain_rate(samples: list, now: datetime, fallback: float, scale: float = 1.0) -> float:
    """Percent per hour lost over the last 72 hours of (time, remaining) samples.

    Only drops count: a rise is a reset and says nothing about use. With less
    than six hours of history the fallback is returned instead; with more, a
    flat history is a real zero. A short window passes its `scale`, which
    shrinks both spans.
    """
    recent = sorted((s for s in samples if now - RATE_LOOKBACK * scale <= s[0] <= now),
                    key=lambda s: s[0])
    if len(recent) < 2:
        return fallback
    span = _hours(recent[-1][0] - recent[0][0])
    if span < MIN_RATE_SPAN_HOURS * scale:
        return fallback
    drops = sum(max(a[1] - b[1], 0.0) for a, b in zip(recent, recent[1:]))
    return drops / span


def projected_remaining(now: datetime, remaining: float, rate: float, at: datetime) -> float:
    """What is left at `at` if the drain continues, floored at zero."""
    return max(remaining - rate * max(_hours(at - now), 0.0), 0.0)


def next_reset(now: datetime, resets_at: Optional[datetime], window_hours: float) -> Optional[datetime]:
    """The first reset after now. A reset time that has already passed (stale
    data) is rolled forward by whole windows."""
    if resets_at is None:
        return None
    reset = as_utc(resets_at)
    if reset > now:
        return reset
    window = timedelta(hours=window_hours)
    return reset + window * (int((now - reset) / window) + 1)


def with_resets(records: list, now: datetime) -> list:
    """(time, remaining) points from (time, remaining, resets_at) records, with
    a step wherever a reading's window ended before the next reading (or before
    now): flat to the reset, then full. Without it a gap between two sessions
    would be drawn as a slow climb."""
    points = []
    for i, (t, value, resets_at) in enumerate(records):
        points.append((t, value))
        after = records[i + 1][0] if i + 1 < len(records) else now
        reset = as_utc(resets_at)
        if reset is not None and t < reset <= after:
            points += [(reset, value), (reset, FULL)]
    return points


def estimate_past(samples: list, remaining: float, resets_at: Optional[datetime],
                  window_hours: float, now: datetime) -> list:
    """The stretch of the current window before the first reading, as a straight
    line from a full quota at the window's start to that reading ([] when there
    is nothing to fill in). It is an estimate and the chart draws it as one.

    A window starts full, so the line is the window's own average until the
    recording takes over. Stale data (a reset time already past) has none: its
    percent belongs to a window that ended. Neither does a recording that began
    in an earlier window or within half an hour of this one's start.
    """
    if resets_at is None:
        return []
    reset = as_utc(resets_at)
    window_start = reset - timedelta(hours=window_hours)
    if reset <= now or window_start >= now:
        return []
    first = min(samples, key=lambda s: s[0], default=(now, remaining))
    if first[0] < window_start or first[0] - window_start < ESTIMATE_MIN_GAP:
        return []
    return [(window_start, FULL), first]


def project(now: datetime, remaining: float, rate: float, resets_at: Optional[datetime],
            window_hours: float, end: datetime) -> list:
    """The dashed line as [(time, value)]: drain at `rate`, floor at zero, jump
    to full at each reset (one window apart), stop at `end`.

    A jump is two points at the same time. A reset exactly at `end` draws no
    jump: nothing lies to its right.
    """
    points = [(now, remaining)]
    t, value = now, remaining
    reset = next_reset(now, resets_at, window_hours)
    window = timedelta(hours=window_hours)
    while t < end:
        boundary = end if reset is None or reset > end else reset
        hours = _hours(boundary - t)
        if rate > 0 and 0 < value and value / rate < hours:
            points.append((t + timedelta(hours=value / rate), 0.0))
            value = 0.0
        else:
            value = max(value - rate * hours, 0.0)
        points.append((boundary, value))
        t = boundary
        if reset is not None and boundary == reset and reset < end:
            points.append((reset, FULL))
            value = FULL
            reset += window
        else:
            break
    return points


def runout_time(now: datetime, remaining: float, rate: float, resets_at: Optional[datetime],
                window_hours: float) -> Optional[datetime]:
    """When the quota hits zero, or None if it lasts until the next reset (or
    is not being drained). Running dry exactly at the reset counts as lasting."""
    if remaining <= 0:
        return now
    if rate <= 0:
        return None
    dry = now + timedelta(hours=remaining / rate)
    reset = next_reset(now, resets_at, window_hours)
    return None if reset is not None and dry >= reset else dry


def next_reset_gain(now: datetime, remaining: float, rate: float, resets_at: Optional[datetime],
                    window_hours: float) -> Optional[float]:
    """Percentage points the next reset adds back, or None when it is unknown."""
    reset = next_reset(now, resets_at, window_hours)
    if reset is None:
        return None
    return FULL - projected_remaining(now, remaining, rate, reset)


def value_at(points: list, when: datetime) -> Optional[float]:
    """Linear interpolation on a polyline of (time, value), None outside it.
    At a vertical jump (two points at one time) the later value wins."""
    if not points or when < points[0][0] or when > points[-1][0]:
        return None
    times = [p[0] for p in points]
    i = bisect_right(times, when)             # first point after `when`
    if i == len(points):
        return points[-1][1]                  # exactly the last point
    t0, v0 = points[i - 1]
    t1, v1 = points[i]
    span = (t1 - t0).total_seconds()
    return v0 if span <= 0 else v0 + (v1 - v0) * (when - t0).total_seconds() / span


def view_span(window_hours: float, resets_at: Optional[datetime], now: datetime) -> tuple:
    """(back, ahead) of the chart around now, as timedeltas. A session is
    charted from its start to its reset; one without a reset, a window each way."""
    if window_hours < DAY_HOURS:
        window = timedelta(hours=window_hours)
        reset = next_reset(now, resets_at, window_hours)
        return (window, window) if reset is None else (window - (reset - now), reset - now)
    reset = next_reset(now, resets_at, window_hours) if window_hours > SHORT_WINDOW_HOURS else None
    if reset is None:
        return VIEW_BACK, VIEW_AHEAD
    ahead = min(max(reset - now, VIEW_AHEAD), timedelta(hours=window_hours))
    return ahead * VIEW_BACK_RATIO, ahead


def time_ticks(start: datetime, end: datetime, tz=None) -> list:
    """The axis labels: hour_ticks for a span under a day, else day_ticks."""
    if end - start < timedelta(hours=DAY_HOURS):
        return hour_ticks(start, end, tz)
    return day_ticks(start, end, tz)


def hour_ticks(start: datetime, end: datetime, tz=None) -> list:
    """Local whole hours in [start, end) as [(aware UTC time, "14:00")], every
    hour or every few, so there are at most MAX_TICKS of them."""
    step = math.ceil(_hours(end - start) / MAX_TICKS)
    first = start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    when = first.astimezone(timezone.utc)
    if when < start:
        when += timedelta(hours=1)
    ticks = []
    while when < end:
        local = when.astimezone(tz)
        if local.hour % step == 0:
            ticks.append((when, f"{local:%H}:00"))
        when += timedelta(hours=1)
    return ticks


def day_ticks(start: datetime, end: datetime, tz=None) -> list:
    """Local midnights in [start, end) as [(aware UTC time, label)].

    Up to eight days the label is the weekday at every midnight; beyond that it
    is a date, spaced so there are at most MAX_TICKS of them.
    """
    local = start.astimezone(tz)
    span_days = (end - start) / timedelta(days=1)
    short = span_days <= 8
    step = 1 if short else math.ceil(span_days / MAX_TICKS)

    day = local.date()
    if local.timetz().replace(tzinfo=None) != time.min:
        day += timedelta(days=1)
    ticks = []
    while True:
        midnight = datetime.combine(day, time.min)
        midnight = midnight.replace(tzinfo=tz) if tz else midnight.astimezone()
        when = midnight.astimezone(timezone.utc)
        if when >= end:
            return ticks
        ticks.append((when, f"{midnight:%a}" if short else f"{midnight:%b} {midnight.day}"))
        day += timedelta(days=step)


@dataclass
class QuotaForecast:
    remaining: float
    rate: float                           # percent per hour
    start: datetime
    end: datetime
    recorded: list = field(default_factory=list)   # [(time, remaining)], ends at now
    estimated: list = field(default_factory=list)  # the window before the first reading
    projected: list = field(default_factory=list)
    next_reset: Optional[datetime] = None
    gain: Optional[float] = None          # what that reset adds back
    runout: Optional[datetime] = None     # None: lasts until the reset
    unused: Optional[float] = None        # what is left at the reset, when it lasts
    since: Optional[datetime] = None      # first recorded sample
    recording: bool = True                # the history is still under three days


def forecast_quota(samples: list, used_percent: float, resets_at: Optional[datetime],
                   window_hours: float, now: datetime) -> QuotaForecast:
    """Everything the quota view shows for a percentage window."""
    remaining = min(max(FULL - used_percent, 0.0), FULL)
    reset = next_reset(now, resets_at, window_hours)
    fallback = window_rate(used_percent, resets_at, window_hours, now)
    scale = _scale(window_hours)
    rate = drain_rate(samples, now, fallback, scale)
    back, ahead = view_span(window_hours, resets_at, now)
    start, end = now - back, now + ahead

    runout = runout_time(now, remaining, rate, resets_at, window_hours)
    since = min((t for t, _ in samples), default=None)
    recorded = [(t, v) for t, v in sorted(samples, key=lambda s: s[0]) if start <= t < now]
    if len(recorded) > 1 and recorded[0][0] == recorded[1][0]:
        del recorded[0]                   # a reset on the left edge: start from its full side
    return QuotaForecast(
        remaining=remaining, rate=rate, start=start, end=end,
        recorded=recorded + [(now, remaining)],
        estimated=estimate_past(samples, remaining, resets_at, window_hours, now),
        projected=project(now, remaining, rate, resets_at, window_hours, end),
        next_reset=reset,
        gain=next_reset_gain(now, remaining, rate, resets_at, window_hours),
        runout=runout,
        unused=(projected_remaining(now, remaining, rate, reset)
                if reset is not None and runout is None else None),
        since=since,
        recording=since is None or now - since < YOUNG_HISTORY * scale,
    )


@dataclass
class BalanceForecast:
    balance: float
    rate_per_day: float
    start: datetime
    end: datetime
    y_max: float
    recorded: list = field(default_factory=list)
    projected: list = field(default_factory=list)
    runout: Optional[datetime] = None
    days_left: Optional[float] = None


def forecast_balance(samples: list, balance: float, daily_costs: list,
                     now: datetime) -> BalanceForecast:
    """The DeepSeek balance view: recorded balance, and a projection at the mean
    of the last seven days' spend (which exists from the first day)."""
    week = [c for c in daily_costs[-7:]]
    per_day = sum(week) / len(week) if week else 0.0
    per_hour = per_day / 24
    start, end = now - VIEW_BACK, now + VIEW_AHEAD

    recorded = [(t, v) for t, v in sorted(samples, key=lambda s: s[0]) if start <= t < now]
    recorded.append((now, balance))
    peak = max(v for _, v in recorded)
    days_left = balance / per_day if per_day > 0 else None
    return BalanceForecast(
        balance=balance, rate_per_day=per_day, start=start, end=end,
        y_max=(peak * 1.1) or 1.0,
        recorded=recorded,
        projected=project(now, balance, per_hour, None, 168, end),
        runout=(now + timedelta(days=days_left)
                if days_left is not None and days_left <= YEAR_DAYS else None),
        days_left=days_left,
    )
