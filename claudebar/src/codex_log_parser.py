"""Parse Codex CLI session JSONL files for token usage and cost calculation."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from codex_pricing import calculate_cost
from validation import safe_get_int

# Parser safety limits
MAX_JSONL_FILE_SIZE_MB = 100
MAX_LINES_PER_FILE = 100_000


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

    Returns the final token count from the session (cumulative) with cost calculation.
    """
    # Check file size first
    try:
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        if file_size_mb > MAX_JSONL_FILE_SIZE_MB:
            return CodexTokenUsage()
    except OSError:
        return CodexTokenUsage()

    usage = CodexTokenUsage()
    model = "gpt-4o"  # Default model if not found
    line_count = 0

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if line_count >= MAX_LINES_PER_FILE:
                    break
                line_count += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)

                    # Extract model from conversation_item entries
                    if entry.get("type") == "conversation_item":
                        payload = entry.get("payload", {})
                        if payload.get("model"):
                            model = payload["model"]

                    # Look for event_msg with token_count type
                    if entry.get("type") != "event_msg":
                        continue

                    payload = entry.get("payload", {})
                    if payload.get("type") != "token_count":
                        continue

                    # Get total_token_usage (cumulative for the session)
                    info = payload.get("info")
                    if not info:
                        continue

                    total_usage = info.get("total_token_usage", {})
                    if total_usage:
                        input_tokens = safe_get_int(total_usage, "input_tokens")
                        cached_tokens = safe_get_int(total_usage, "cached_input_tokens")
                        output_tokens = safe_get_int(total_usage, "output_tokens")
                        reasoning_tokens = safe_get_int(total_usage, "reasoning_output_tokens")

                        # Calculate cost for this session
                        cost = calculate_cost(
                            model=model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_input_tokens=cached_tokens,
                            reasoning_tokens=reasoning_tokens,
                        )

                        # Update with latest cumulative values
                        usage = CodexTokenUsage(
                            input_tokens=input_tokens,
                            cached_input_tokens=cached_tokens,
                            output_tokens=output_tokens,
                            reasoning_tokens=reasoning_tokens,
                            cost_usd=cost,
                        )
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        pass

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
