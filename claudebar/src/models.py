"""Data models for ClaudeBar usage tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Engine(Enum):
    """AI engine/provider selection."""
    CLAUDE = "claude"
    CODEX = "codex"


@dataclass
class TokenUsage:
    """Token usage for a single interaction."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens including cache operations."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @property
    def total_tokens(self) -> int:
        """Total tokens across all categories."""
        return self.total_input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


@dataclass
class ModelUsage:
    """Usage statistics for a specific model."""
    model: str
    tokens: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    message_count: int = 0


class _UsageStatus:
    """Shared session/weekly status-level helpers for usage snapshots."""

    @property
    def max_percent(self) -> float:
        """Return the higher of session or weekly percentage."""
        return max(self.session_percent, self.weekly_percent)

    def get_status_level(self, warning_threshold: int = 80, critical_threshold: int = 95) -> str:
        """Return status level: normal, warning, critical, or error."""
        if self.error_message:
            return "error"
        pct = self.max_percent
        if pct >= critical_threshold:
            return "critical"
        if pct >= warning_threshold:
            return "warning"
        return "normal"


@dataclass
class UsageSnapshot(_UsageStatus):
    """Complete usage snapshot combining all data sources."""
    timestamp: datetime = field(default_factory=datetime.now)

    # CLI data
    session_percent: float = 0.0
    session_reset: Optional[str] = None
    weekly_percent: float = 0.0
    weekly_reset: Optional[str] = None

    # Extra usage (paid overage)
    extra_enabled: bool = False
    extra_percent: float = 0.0           # Utilization percentage (0-100)
    extra_used: float = 0.0              # Used credits in actual currency
    extra_limit: float = 0.0             # Monthly limit in actual currency
    extra_currency: str = "usd"          # Currency code from API

    # Log-derived data
    today_cost_usd: float = 0.0
    month_cost_usd: float = 0.0
    today_tokens: TokenUsage = field(default_factory=TokenUsage)
    month_tokens: TokenUsage = field(default_factory=TokenUsage)
    models_used: list[ModelUsage] = field(default_factory=list)

    # Status
    cli_available: bool = True
    logs_available: bool = True
    error_message: Optional[str] = None

    # Staleness tracking (for cached data)
    is_stale: bool = False
    stale_since: Optional[datetime] = None

    @property
    def status_level(self) -> str:
        """Return status level using default thresholds."""
        return self.get_status_level()


@dataclass
class OpenAISnapshot(_UsageStatus):
    """OpenAI/Codex usage snapshot."""
    timestamp: datetime = field(default_factory=datetime.now)

    # Usage limits
    session_percent: float = 0.0
    session_reset: Optional[str] = None
    session_available: bool = True  # False when the plan has no 5-hour window
    weekly_percent: float = 0.0
    weekly_reset: Optional[str] = None

    # Token usage (from local JSONL logs)
    today_input_tokens: int = 0
    today_output_tokens: int = 0
    today_cached_tokens: int = 0
    today_reasoning_tokens: int = 0
    month_input_tokens: int = 0
    month_output_tokens: int = 0

    # Cost calculations (from log parsing + pricing)
    today_cost_usd: float = 0.0
    month_cost_usd: float = 0.0

    # Plan type and credits
    plan_type: Optional[str] = None  # "plus", "pro", "enterprise", "free"
    credits_remaining: Optional[float] = None

    # Status
    available: bool = False
    error_message: Optional[str] = None

    # Staleness tracking (for cached data)
    is_stale: bool = False
    stale_since: Optional[datetime] = None

    @property
    def today_total_tokens(self) -> int:
        return self.today_input_tokens + self.today_output_tokens

    @property
    def month_total_tokens(self) -> int:
        return self.month_input_tokens + self.month_output_tokens


@dataclass
class CombinedSnapshot:
    """Combined snapshot for both Claude and OpenAI/Codex."""
    claude: Optional[UsageSnapshot] = None
    openai: Optional[OpenAISnapshot] = None
    active_engine: Engine = Engine.CLAUDE
    timestamp: datetime = field(default_factory=datetime.now)

    def get_session_percent(self) -> float:
        """Get session percent for active engine."""
        if self.active_engine == Engine.CLAUDE and self.claude:
            return self.claude.session_percent
        if self.active_engine == Engine.CODEX and self.openai:
            return self.openai.session_percent
        return 0.0

    def get_weekly_percent(self) -> float:
        """Get weekly percent for active engine."""
        if self.active_engine == Engine.CLAUDE and self.claude:
            return self.claude.weekly_percent
        if self.active_engine == Engine.CODEX and self.openai:
            return self.openai.weekly_percent
        return 0.0
