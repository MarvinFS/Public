"""Coordinate data collection from all sources."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import logging

from models import (UsageSnapshot, OpenAISnapshot, DeepSeekSnapshot, CombinedSnapshot,
                    Engine, ModelUsage, DAILY_HISTORY_DAYS)
from log_parser import collect_entries, aggregate_usage, daily_costs
from oauth_usage import fetch_oauth_usage, OAuthUsageData
from openai_usage import fetch_openai_usage, is_codex_configured
from codex_log_parser import CodexTokenUsage, get_codex_daily_usage
from config import get_claude_projects_dir
import model_catalog
import deepseek_auth
from deepseek_usage import fetch_usage, fetch_summary, last_n_days, month_days, previous_month_days
from currency import to_usd
from snapshot_cache import save_cache, load_cache, load_deepseek_cache

logger = logging.getLogger("claudebar")

#: While the OAuth fetch stays broken, repeat the warning no more often than
#: this. A five-minute refresh would otherwise write 288 identical lines a day.
OAUTH_REMINDER_INTERVAL = timedelta(hours=1)


def format_duration(since: Optional[datetime]) -> str:
    """Elapsed time as a short human string for a log line."""
    if since is None:
        return "unknown"
    seconds = int((datetime.now() - since).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


class DataCollector:
    """Collects and aggregates usage data from all sources."""

    def __init__(self, projects_dir: Optional[Path] = None,
                 on_oauth_failure: Optional[callable] = None,
                 on_oauth_success: Optional[callable] = None):
        self.projects_dir = projects_dir or get_claude_projects_dir()
        self._last_snapshot: Optional[UsageSnapshot] = None
        self._last_openai_snapshot: Optional[OpenAISnapshot] = None
        self._last_deepseek_snapshot: Optional[DeepSeekSnapshot] = None
        self._last_combined: Optional[CombinedSnapshot] = None
        self._active_engine: Engine = Engine.CLAUDE
        self._on_oauth_failure = on_oauth_failure
        self._on_oauth_success = on_oauth_success
        self._pricing_source = "bundled"

        # Bookkeeping for edge-triggered OAuth logging: the last outcome (None
        # until the first attempt), the failure run, and when it was last
        # mentioned. Success repeats every refresh, so only changes get a line.
        self._oauth_ok: Optional[bool] = None
        self._oauth_failures = 0
        self._oauth_failed_since: Optional[datetime] = None
        self._oauth_last_reminder: Optional[datetime] = None

    def set_oauth_callbacks(self, on_failure: Optional[callable] = None,
                            on_success: Optional[callable] = None) -> None:
        """Set OAuth callbacks after initialization."""
        self._on_oauth_failure = on_failure
        self._on_oauth_success = on_success

    def collect(self) -> UsageSnapshot:
        """Collect fresh usage data from all sources."""
        snapshot = UsageSnapshot(timestamp=datetime.now())
        errors = []
        oauth_failed = False

        # Fetch usage limits via OAuth API; on failure fall back to cached data below.
        oauth_data = self._fetch_usage_limits(errors)
        if oauth_data.is_valid:
            if oauth_data.session:
                snapshot.session_percent = oauth_data.session.percent
                snapshot.session_reset = oauth_data.session.reset_str
                snapshot.session_resets_at = oauth_data.session.resets_at
            if oauth_data.weekly:
                snapshot.weekly_percent = oauth_data.weekly.percent
                snapshot.weekly_reset = oauth_data.weekly.reset_str
                snapshot.weekly_resets_at = oauth_data.weekly.resets_at
            if oauth_data.extra:
                snapshot.extra_enabled = oauth_data.extra.enabled
                snapshot.extra_percent = oauth_data.extra.percent
                snapshot.extra_used = oauth_data.extra.used_credits
                snapshot.extra_limit = oauth_data.extra.monthly_limit
                snapshot.extra_currency = oauth_data.extra.currency
            snapshot.cli_available = True
        else:
            snapshot.cli_available = False
            oauth_failed = True

        # If OAuth failed, try to load cached OAuth data and notify callback
        if oauth_failed:
            # Notify callback about OAuth failure
            error_msg = errors[-1] if errors else "OAuth authentication failed"
            if self._on_oauth_failure:
                self._on_oauth_failure(error_msg)

            cached_claude, _, cached_at = load_cache()
            if cached_claude:
                # Use cached OAuth data (session/weekly/extra percentages)
                snapshot.session_percent = cached_claude.session_percent
                snapshot.session_reset = cached_claude.session_reset
                snapshot.session_resets_at = cached_claude.session_resets_at
                snapshot.weekly_percent = cached_claude.weekly_percent
                snapshot.weekly_reset = cached_claude.weekly_reset
                snapshot.weekly_resets_at = cached_claude.weekly_resets_at
                snapshot.extra_enabled = cached_claude.extra_enabled
                snapshot.extra_percent = cached_claude.extra_percent
                snapshot.extra_used = cached_claude.extra_used
                snapshot.extra_limit = cached_claude.extra_limit
                snapshot.extra_currency = cached_claude.extra_currency
                # Mark as stale
                snapshot.is_stale = True
                snapshot.stale_since = cached_at
            else:
                # No cache and OAuth unavailable - surface it rather than render
                # a silent all-zeros usage panel (the line-121 guard would suppress it).
                snapshot.error_message = error_msg
        else:
            # OAuth succeeded, notify callback to clear any previous error
            if self._on_oauth_success:
                self._on_oauth_success()

        # Collect log data for tokens and models: one parse, several windows.
        try:
            today = snapshot.timestamp.date()
            entries = collect_entries(self.projects_dir)
            _, snapshot.today_tokens, snapshot.today_cost_usd = aggregate_usage(
                entries=entries, start_date=today, end_date=today)
            month_models, snapshot.month_tokens, snapshot.month_cost_usd = aggregate_usage(
                entries=entries, start_date=today.replace(day=1), end_date=today)
            snapshot.models_used = list(month_models.values())
            _, last31_tokens, snapshot.last31_cost_usd = aggregate_usage(
                entries=entries, start_date=today - timedelta(days=DAILY_HISTORY_DAYS - 1), end_date=today)
            snapshot.last31_tokens = last31_tokens.total_tokens
            snapshot.daily_costs = daily_costs(entries, DAILY_HISTORY_DAYS, today)
            snapshot.pricing_source = self._pricing_source
            snapshot.logs_available = True
        except Exception as e:
            snapshot.logs_available = False
            errors.append(f"Logs: {str(e)}")

        if errors and not snapshot.cli_available and not snapshot.logs_available:
            snapshot.error_message = "; ".join(errors)

        self._last_snapshot = snapshot

        # Save to cache on successful OAuth fetch
        if not oauth_failed:
            save_cache(claude=snapshot)

        return snapshot

    def _fetch_usage_limits(self, errors: list) -> OAuthUsageData:
        """Fetch usage limits from the OAuth API (caller falls back to cache)."""
        try:
            data = fetch_oauth_usage()
            if data.is_valid:
                self._note_oauth_success()
                return data
            self._note_oauth_failure(data.error or "unknown error")
            errors.append(f"OAuth: {data.error}")
        except Exception as e:
            self._note_oauth_failure(str(e))
            errors.append(f"OAuth: {e}")

        return OAuthUsageData(error=errors[-1] if errors else "OAuth usage unavailable")

    def _note_oauth_success(self) -> None:
        """Log the end of a failure run, then say nothing until it breaks again."""
        if self._oauth_ok is False:
            logger.info("OAuth usage recovered after %d failed attempt(s) over %s",
                        self._oauth_failures, format_duration(self._oauth_failed_since))
        else:
            logger.debug("Usage from OAuth API")
        self._oauth_ok = True
        self._oauth_failures = 0
        self._oauth_failed_since = None
        self._oauth_last_reminder = None

    def _note_oauth_failure(self, reason: str) -> None:
        """Warn when the fetch breaks, then at most hourly while it stays broken."""
        now = datetime.now()
        if self._oauth_ok is not False:
            self._oauth_ok = False
            self._oauth_failures = 1
            self._oauth_failed_since = now
            self._oauth_last_reminder = now
            logger.warning("OAuth usage unavailable, showing cached data: %s", reason)
            return

        self._oauth_failures += 1
        if now - self._oauth_last_reminder >= OAUTH_REMINDER_INTERVAL:
            self._oauth_last_reminder = now
            logger.warning("OAuth usage still unavailable after %d attempt(s) over %s: %s",
                           self._oauth_failures,
                           format_duration(self._oauth_failed_since),
                           reason)

    @property
    def last_snapshot(self) -> Optional[UsageSnapshot]:
        """Get the most recent snapshot without refreshing."""
        return self._last_snapshot

    def collect_openai(self) -> OpenAISnapshot:
        """Collect fresh usage data from OpenAI/Codex."""
        snapshot = OpenAISnapshot(timestamp=datetime.now())
        api_failed = False

        # Fetch API usage data (rate limits)
        try:
            openai_data = fetch_openai_usage()
            if openai_data.is_valid:
                snapshot.session_percent = openai_data.session_percent
                snapshot.session_reset = openai_data.session_reset
                snapshot.session_available = openai_data.has_session_window
                snapshot.session_resets_at = openai_data.five_hour.resets_at if openai_data.five_hour else None
                snapshot.weekly_percent = openai_data.weekly_percent
                snapshot.weekly_reset = openai_data.weekly_reset
                snapshot.weekly_resets_at = openai_data.weekly.resets_at if openai_data.weekly else None
                snapshot.credits_remaining = openai_data.credits_remaining
                snapshot.plan_type = openai_data.plan_type
                snapshot.available = True
            else:
                snapshot.available = False
                api_failed = True
                if openai_data.error:
                    snapshot.error_message = openai_data.error
        except Exception as e:
            snapshot.available = False
            api_failed = True
            snapshot.error_message = str(e)

        # If API failed, try to load cached data
        if api_failed:
            _, cached_openai, cached_at = load_cache()
            if cached_openai:
                snapshot.session_percent = cached_openai.session_percent
                snapshot.session_reset = cached_openai.session_reset
                snapshot.session_available = cached_openai.session_available
                snapshot.session_resets_at = cached_openai.session_resets_at
                snapshot.weekly_percent = cached_openai.weekly_percent
                snapshot.weekly_reset = cached_openai.weekly_reset
                snapshot.weekly_resets_at = cached_openai.weekly_resets_at
                snapshot.credits_remaining = cached_openai.credits_remaining
                snapshot.plan_type = cached_openai.plan_type
                snapshot.is_stale = True
                snapshot.stale_since = cached_at

        # Fetch token usage and costs from local JSONL logs: the daily series
        # ends today, so today, the month and the last 31 days are slices of it.
        try:
            series = get_codex_daily_usage(days=DAILY_HISTORY_DAYS, end=snapshot.timestamp)
            today_usage = series[-1]
            snapshot.today_input_tokens = today_usage.input_tokens
            snapshot.today_output_tokens = today_usage.output_tokens
            snapshot.today_cached_tokens = today_usage.cached_input_tokens
            snapshot.today_reasoning_tokens = today_usage.reasoning_tokens
            snapshot.today_cost_usd = today_usage.cost_usd

            month_usage = sum(series[-snapshot.timestamp.day:], CodexTokenUsage())
            snapshot.month_input_tokens = month_usage.input_tokens
            snapshot.month_output_tokens = month_usage.output_tokens
            snapshot.month_cost_usd = month_usage.cost_usd

            last31 = sum(series, CodexTokenUsage())
            snapshot.last31_cost_usd = last31.cost_usd
            snapshot.last31_tokens = last31.total_tokens
            snapshot.daily_costs = [u.cost_usd for u in series]
            snapshot.pricing_source = self._pricing_source
        except Exception:
            pass  # Token counts are optional, don't fail if logs unavailable

        self._last_openai_snapshot = snapshot

        # Save to cache on successful API fetch
        if not api_failed:
            save_cache(openai=snapshot)

        return snapshot

    @property
    def last_openai_snapshot(self) -> Optional[OpenAISnapshot]:
        """Get the most recent OpenAI snapshot without refreshing."""
        return self._last_openai_snapshot

    def collect_deepseek(self) -> DeepSeekSnapshot:
        """Collect DeepSeek balance and spend from the signed-in session.

        The two calls fail independently, so a summary that times out still
        leaves the usage figures standing and vice versa.
        """
        snapshot = DeepSeekSnapshot(timestamp=datetime.now())
        today = snapshot.timestamp.date()
        balance_fresh = False
        usage_fresh = False

        token = deepseek_auth.discover_user_token()

        if not token:
            # Nothing signed in. Return an empty snapshot rather than replaying
            # the cache: stale values here would look like live data, which is
            # exactly what someone sees after signing out.
            snapshot.error_message = "Not signed in"
            snapshot.usage_error = "Not signed in"
            self._last_deepseek_snapshot = snapshot
            return snapshot

        usage = fetch_usage(token.value, today)
        if usage.available:
            self._apply_usage(snapshot, usage, today)
            snapshot.usage_fetched_at = snapshot.timestamp
            usage_fresh = True
        else:
            snapshot.usage_error = usage.error or "Usage unavailable"

        # The session summary is the only source of the balance and the
        # lifetime total, so a signed-in session supplies everything.
        summary = fetch_summary(token.value)
        if summary.available:
            snapshot.balance_available = True
            snapshot.balance_usable = summary.total > 0
            snapshot.balance_total = summary.total
            snapshot.balance_topped_up = summary.topped_up
            snapshot.balance_granted = summary.granted
            snapshot.balance_currency = summary.currency
            snapshot.error_message = None
            snapshot.balance_fetched_at = snapshot.timestamp
            balance_fresh = True
            if summary.total_cost_available:
                snapshot.total_cost_usd = summary.total_cost
                snapshot.total_cost_available = True

        # Carry over whatever the last good fetch had, so a transient failure
        # does not blank the panel or evict a good cache entry.
        if not (balance_fresh and usage_fresh):
            cached, _ = load_deepseek_cache()
            carried = []
            if cached:
                if not balance_fresh and cached.balance_available:
                    snapshot.balance_available = True
                    snapshot.balance_usable = cached.balance_usable
                    snapshot.balance_total = cached.balance_total
                    snapshot.balance_topped_up = cached.balance_topped_up
                    snapshot.balance_granted = cached.balance_granted
                    snapshot.balance_currency = cached.balance_currency
                    snapshot.balance_fetched_at = cached.balance_fetched_at
                    snapshot.error_message = None
                    carried.append(cached.balance_fetched_at)
                if not usage_fresh and cached.usage_available:
                    for name in ("usage_available", "usage_currency", "today_cost_usd",
                                 "today_tokens", "month_cost_usd", "month_tokens",
                                 "last7_cost_usd", "last7_tokens", "daily_costs",
                                 "prev_month_cost_usd", "prev_month_tokens",
                                 "total_cost_usd", "total_cost_available",
                                 "daily_tokens", "daily_requests", "daily_dates",
                                 "models_used", "usage_fetched_at"):
                        setattr(snapshot, name, getattr(cached, name))
                    snapshot.usage_error = None
                    carried.append(cached.usage_fetched_at)
            # Anything shown from the cache is stale, whether or not the other
            # half refreshed in this pass, and as old as the oldest half carried.
            if carried:
                snapshot.is_stale = True
                snapshot.stale_since = min((at for at in carried if at), default=None)

        self._last_deepseek_snapshot = snapshot

        # Only a snapshot carrying something fresh is worth persisting
        if balance_fresh or usage_fresh:
            save_cache(deepseek=snapshot)

        return snapshot

    def _apply_usage(self, snapshot: DeepSeekSnapshot, usage, today) -> None:
        """Fold parsed platform usage into the snapshot.

        Costs stay in USD: when the platform reports another currency (CNY for
        some accounts) the whole set is scaled once at this boundary.
        """
        currency = (usage.currency or "USD").upper()
        factor = to_usd(1.0, currency) if currency != "USD" else 1.0

        days = last_n_days(usage.days, today, DAILY_HISTORY_DAYS)
        week = days[-7:]
        month = month_days(usage.days, today)
        previous = previous_month_days(usage.days, today)

        snapshot.usage_available = True
        snapshot.usage_currency = currency

        # The chart runs the same 31 days as the other engines' charts.
        snapshot.daily_costs = [d.cost * factor for d in days]
        snapshot.daily_tokens = [d.tokens for d in days]
        snapshot.daily_requests = [d.requests for d in days]
        snapshot.daily_dates = [d.date for d in days]

        snapshot.last7_cost_usd = sum(d.cost for d in week) * factor
        snapshot.last7_tokens = sum(d.tokens for d in week)

        snapshot.today_cost_usd = week[-1].cost * factor
        snapshot.today_tokens = week[-1].tokens

        snapshot.month_cost_usd = sum(d.cost for d in month) * factor
        snapshot.month_tokens = sum(d.tokens for d in month)

        snapshot.prev_month_cost_usd = sum(d.cost for d in previous) * factor
        snapshot.prev_month_tokens = sum(d.tokens for d in previous)

        snapshot.models_used = [
            ModelUsage(model=m.model, cost_usd=m.cost * factor, message_count=0)
            for m in usage.models
        ]

    @property
    def last_deepseek_snapshot(self) -> Optional[DeepSeekSnapshot]:
        """Get the most recent DeepSeek snapshot without refreshing."""
        return self._last_deepseek_snapshot

    def collect_combined(self) -> CombinedSnapshot:
        """Collect data from Claude, OpenAI/Codex and DeepSeek."""
        # Refresh price tables first so both parsers below use the same rates.
        self._pricing_source = "models.dev" if model_catalog.apply() else "bundled"
        claude_snapshot = self.collect()
        openai_snapshot = self.collect_openai()
        deepseek_snapshot = self.collect_deepseek()

        combined = CombinedSnapshot(
            claude=claude_snapshot,
            openai=openai_snapshot,
            deepseek=deepseek_snapshot,
            active_engine=self._active_engine,
            timestamp=datetime.now(),
        )
        self._last_combined = combined
        return combined

    @property
    def last_combined(self) -> Optional[CombinedSnapshot]:
        """Get the most recent combined snapshot without refreshing."""
        return self._last_combined

    def set_active_engine(self, engine: Engine) -> None:
        """Set the active engine for display."""
        self._active_engine = engine
        if self._last_combined:
            self._last_combined.active_engine = engine

    @property
    def active_engine(self) -> Engine:
        """Get the currently active engine."""
        return self._active_engine

    @staticmethod
    def is_openai_available() -> bool:
        """Check if OpenAI/Codex is configured."""
        return is_codex_configured()
