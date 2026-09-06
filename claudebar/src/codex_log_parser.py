"""Parse Codex CLI session JSONL files for token usage and cost calculation."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from codex_pricing import calculate_cost
from validation import safe_get_int


@dataclass
class CodexTokenUsage:
    """Token usage from Codex sessions."""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "CodexTokenUsage") -> "CodexTokenUsage":
        return CodexTokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


def get_codex_sessions_dir() -> Path:
    """Get the Codex sessions directory."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "sessions"
    return Path.home() / ".codex" / "sessions"


def parse_session_file(file_path: Path) -> CodexTokenUsage:
    """Parse a single Codex session JSONL file for token usage.

    Returns the whole file's token usage, summed across resume epochs, with
    cost calculation.
    """
    # `codex resume` appends to the same file and restarts total_token_usage at
    # zero, so a rollout holds one cumulative counter per run. Bank the epoch
    # whenever the counter goes backwards and carry the sum.
    banked = CodexTokenUsage()
    epoch = CodexTokenUsage()
    # ponytail: last model wins; a session that switches models mid-way prices
    # its whole total at the final one. Attribute per turn_id if that matters.
    model = "default"

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                # Session files reach hundreds of MB on base64 payloads; only
                # parse the two line kinds we need.
                if '"turn_context"' not in line and '"token_count"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue

                # The model in force for the turn.
                if entry.get("type") == "turn_context":
                    if payload.get("model"):
                        model = payload["model"]
                    continue

                if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue

                info = payload.get("info")
                if not isinstance(info, dict):
                    continue

                total_usage = info.get("total_token_usage")
                if not isinstance(total_usage, dict):
                    continue

                # Cumulative within the run - keep the latest, never sum.
                latest = CodexTokenUsage(
                    input_tokens=safe_get_int(total_usage, "input_tokens"),
                    cached_input_tokens=safe_get_int(total_usage, "cached_input_tokens"),
                    output_tokens=safe_get_int(total_usage, "output_tokens"),
                    reasoning_tokens=safe_get_int(total_usage, "reasoning_output_tokens"),
                )
                # A restart's first record has spent nothing but the turn it
                # just logged, so total <= last. A cumulative counter that dips
                # while a run continues (seen once, a 1% correction after a
                # resume re-derived its total) fails that and must not bank.
                last_usage = info.get("last_token_usage")
                fresh_run = True
                if isinstance(last_usage, dict):
                    fresh_run = latest.total_tokens <= (
                        safe_get_int(last_usage, "input_tokens")
                        + safe_get_int(last_usage, "output_tokens")
                    )
                if latest.total_tokens < epoch.total_tokens and fresh_run:
                    banked = banked + epoch
                epoch = latest
    except (IOError, OSError):
        return CodexTokenUsage()

    usage = banked + epoch
    usage.cost_usd = calculate_cost(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
    )
    return usage


def get_sessions_for_date(sessions_dir: Path, date: datetime) -> list[Path]:
    """Get all session files for a specific date."""
    date_dir = sessions_dir / str(date.year) / f"{date.month:02d}" / f"{date.day:02d}"

    if not date_dir.exists():
        return []

    return list(date_dir.glob("*.jsonl"))


def get_today_codex_usage(sessions_dir: Optional[Path] = None) -> CodexTokenUsage:
    """Get token usage from today's Codex sessions."""
    if sessions_dir is None:
        sessions_dir = get_codex_sessions_dir()

    today = datetime.now()
    session_files = get_sessions_for_date(sessions_dir, today)

    total = CodexTokenUsage()
    for file_path in session_files:
        session_usage = parse_session_file(file_path)
        total = total + session_usage

    return total


def get_month_codex_usage(sessions_dir: Optional[Path] = None) -> CodexTokenUsage:
    """Get token usage for the current calendar month, so the figure resets on
    the 1st - matching `log_parser.get_month_usage` on the Claude side."""
    if sessions_dir is None:
        sessions_dir = get_codex_sessions_dir()

    total = CodexTokenUsage()
    today = datetime.now()

    for day in range(1, today.day + 1):
        date = today.replace(day=day)
        session_files = get_sessions_for_date(sessions_dir, date)

        for file_path in session_files:
            session_usage = parse_session_file(file_path)
            total = total + session_usage

    return total
