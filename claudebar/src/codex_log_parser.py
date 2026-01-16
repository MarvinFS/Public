"""Parse Codex CLI session JSONL files for token usage."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class CodexTokenUsage:
    """Token usage from Codex sessions."""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "CodexTokenUsage") -> "CodexTokenUsage":
        return CodexTokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


def get_codex_sessions_dir() -> Path:
    """Get the Codex sessions directory."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "sessions"
    return Path.home() / ".codex" / "sessions"


def parse_session_file(file_path: Path) -> CodexTokenUsage:
    """Parse a single Codex session JSONL file for token usage.

    Returns the final token count from the session (cumulative).
    """
    usage = CodexTokenUsage()

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)

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
                        # Update with latest cumulative values
                        usage = CodexTokenUsage(
                            input_tokens=total_usage.get("input_tokens", 0),
                            cached_input_tokens=total_usage.get("cached_input_tokens", 0),
                            output_tokens=total_usage.get("output_tokens", 0),
                            reasoning_tokens=total_usage.get("reasoning_output_tokens", 0),
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


def get_month_codex_usage(sessions_dir: Optional[Path] = None, days: int = 30) -> CodexTokenUsage:
    """Get token usage from the last N days of Codex sessions."""
    if sessions_dir is None:
        sessions_dir = get_codex_sessions_dir()

    total = CodexTokenUsage()
    today = datetime.now()

    for i in range(days):
        date = today - timedelta(days=i)
        session_files = get_sessions_for_date(sessions_dir, date)

        for file_path in session_files:
            session_usage = parse_session_file(file_path)
            total = total + session_usage

    return total
