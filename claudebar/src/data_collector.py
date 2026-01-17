"""Coordinate data collection from all sources."""

from datetime import datetime
from pathlib import Path
from typing import Optional

from models import UsageSnapshot, TokenUsage, OpenAISnapshot, CombinedSnapshot, Engine
from log_parser import get_today_usage, get_month_usage
from stats_parser import get_local_stats, get_today_api_cost
from oauth_usage import fetch_oauth_usage
from openai_usage import fetch_openai_usage, is_codex_configured
from codex_log_parser import get_today_codex_usage, get_month_codex_usage
from config import get_claude_projects_dir


class DataCollector:
    """Collects and aggregates usage data from all sources."""

    def __init__(self, projects_dir: Optional[Path] = None):
        self.projects_dir = projects_dir or get_claude_projects_dir()
        self._last_snapshot: Optional[UsageSnapshot] = None
        self._last_openai_snapshot: Optional[OpenAISnapshot] = None
        self._last_combined: Optional[CombinedSnapshot] = None
        self._active_engine: Engine = Engine.CLAUDE

    def collect(self) -> UsageSnapshot:
        """Collect fresh usage data from all sources."""
        snapshot = UsageSnapshot(timestamp=datetime.now())
        errors = []

        # Fetch OAuth usage data (session/weekly limits and extra usage)
        try:
            oauth_data = fetch_oauth_usage()
            if oauth_data.is_valid:
                if oauth_data.session:
                    snapshot.session_percent = oauth_data.session.percent
                    snapshot.session_reset = oauth_data.session.reset_str
                if oauth_data.weekly:
                    snapshot.weekly_percent = oauth_data.weekly.percent
                    snapshot.weekly_reset = oauth_data.weekly.reset_str
                # Pass extra usage data to snapshot
                if oauth_data.extra:
                    snapshot.extra_enabled = oauth_data.extra.enabled
                    snapshot.extra_percent = oauth_data.extra.percent
                    snapshot.extra_used = oauth_data.extra.used_credits
                    snapshot.extra_limit = oauth_data.extra.monthly_limit
                    snapshot.extra_currency = oauth_data.extra.currency
                snapshot.cli_available = True
            else:
                snapshot.cli_available = False
                if oauth_data.error:
                    errors.append(f"OAuth: {oauth_data.error}")
        except Exception as e:
            snapshot.cli_available = False
            errors.append(f"OAuth: {str(e)}")

        # Collect log data for tokens and models
        try:
            today_tokens, today_cost, today_models = get_today_usage(self.projects_dir)
            snapshot.today_tokens = today_tokens

            month_tokens, month_cost, month_models = get_month_usage(self.projects_dir)
            snapshot.month_tokens = month_tokens
            snapshot.month_cost_usd = month_cost
            snapshot.models_used = month_models
            snapshot.logs_available = True

            # Use JSONL-calculated cost as fallback
            snapshot.today_cost_usd = today_cost
        except Exception as e:
            snapshot.logs_available = False
            errors.append(f"Logs: {str(e)}")

        # Get API cost from daily_stats.json (preferred, more accurate for billing)
        try:
            api_cost = get_today_api_cost()
            if api_cost > 0:
                snapshot.today_cost_usd = api_cost
        except Exception as e:
            errors.append(f"Stats: {str(e)}")

        if errors and not snapshot.cli_available and not snapshot.logs_available:
            snapshot.error_message = "; ".join(errors)

        self._last_snapshot = snapshot
        return snapshot

    @property
    def last_snapshot(self) -> Optional[UsageSnapshot]:
        """Get the most recent snapshot without refreshing."""
        return self._last_snapshot

    def collect_openai(self) -> OpenAISnapshot:
        """Collect fresh usage data from OpenAI/Codex."""
        snapshot = OpenAISnapshot(timestamp=datetime.now())

        # Fetch API usage data (rate limits)
        try:
            openai_data = fetch_openai_usage()
            if openai_data.is_valid:
                snapshot.session_percent = openai_data.session_percent
                snapshot.session_reset = openai_data.session_reset
                snapshot.weekly_percent = openai_data.weekly_percent
                snapshot.weekly_reset = openai_data.weekly_reset
                snapshot.credits_remaining = openai_data.credits_remaining
                snapshot.plan_type = openai_data.plan_type
                snapshot.available = True
            else:
                snapshot.available = False
                if openai_data.error:
                    snapshot.error_message = openai_data.error
        except Exception as e:
            snapshot.available = False
            snapshot.error_message = str(e)

        # Fetch token usage and costs from local JSONL logs
        try:
            today_usage = get_today_codex_usage()
            snapshot.today_input_tokens = today_usage.input_tokens
            snapshot.today_output_tokens = today_usage.output_tokens
            snapshot.today_cached_tokens = today_usage.cached_input_tokens
            snapshot.today_reasoning_tokens = today_usage.reasoning_tokens
            snapshot.today_cost_usd = today_usage.cost_usd

            month_usage = get_month_codex_usage()
            snapshot.month_input_tokens = month_usage.input_tokens
            snapshot.month_output_tokens = month_usage.output_tokens
            snapshot.month_cost_usd = month_usage.cost_usd
        except Exception:
            pass  # Token counts are optional, don't fail if logs unavailable

        self._last_openai_snapshot = snapshot
        return snapshot

    @property
    def last_openai_snapshot(self) -> Optional[OpenAISnapshot]:
        """Get the most recent OpenAI snapshot without refreshing."""
        return self._last_openai_snapshot

    def collect_combined(self) -> CombinedSnapshot:
        """Collect data from Claude and OpenAI/Codex."""
        claude_snapshot = self.collect()
        openai_snapshot = self.collect_openai()

        combined = CombinedSnapshot(
            claude=claude_snapshot,
            openai=openai_snapshot,
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


def format_snapshot_tooltip(snapshot: UsageSnapshot) -> str:
    """Format a snapshot for display in system tray tooltip."""
    lines = ["ClaudeBar"]

    if snapshot.logs_available:
        lines.append(f"Today: ${snapshot.today_cost_usd:.2f}")
        lines.append(f"Month: ${snapshot.month_cost_usd:.2f}")
        total_tokens = snapshot.today_tokens.total_tokens
        if total_tokens > 0:
            if total_tokens >= 1_000_000:
                lines.append(f"Tokens: {total_tokens / 1_000_000:.1f}M")
            elif total_tokens >= 1_000:
                lines.append(f"Tokens: {total_tokens / 1_000:.1f}K")
    else:
        lines.append("No data available")

    lines.append(f"Updated: {snapshot.timestamp.strftime('%H:%M')}")

    return "\n".join(lines)


def format_snapshot_detail(snapshot: UsageSnapshot) -> str:
    """Format a snapshot for detailed menu display."""
    lines = [f"Last updated: {snapshot.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"]
    lines.append("")

    if snapshot.logs_available:
        lines.append("=== Costs ===")
        lines.append(f"Today: ${snapshot.today_cost_usd:.2f}")
        lines.append(f"This month: ${snapshot.month_cost_usd:.2f}")
        lines.append("")

        lines.append("=== Today's Tokens ===")
        lines.append(f"Input: {snapshot.today_tokens.input_tokens:,}")
        lines.append(f"Output: {snapshot.today_tokens.output_tokens:,}")
        lines.append(f"Cache read: {snapshot.today_tokens.cache_read_input_tokens:,}")
        lines.append(f"Cache create: {snapshot.today_tokens.cache_creation_input_tokens:,}")

        if snapshot.models_used:
            lines.append("")
            lines.append("=== Models Used ===")
            for model in sorted(snapshot.models_used, key=lambda m: m.cost_usd, reverse=True):
                short_name = model.model.split("-")[1] if "-" in model.model else model.model
                lines.append(f"{short_name}: ${model.cost_usd:.2f} ({model.message_count} msgs)")
    else:
        lines.append("No usage data available")

    if snapshot.error_message:
        lines.append("")
        lines.append(f"Errors: {snapshot.error_message}")

    return "\n".join(lines)
