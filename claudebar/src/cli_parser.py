"""Parse output from claude /usage CLI command.

NOTE: The `claude /usage` command opens an interactive TUI and cannot be
parsed programmatically. This module is kept for potential future use if
Claude Code adds a non-interactive usage query option.

Currently, ClaudeBar uses local stats files (daily_stats.json, stats-cache.json)
and JSONL logs for usage data instead.
"""

import re
import subprocess
from typing import Optional

from models import CLIUsageData
from config import find_claude_cli, load_config


def run_claude_usage(cli_path: Optional[str] = None, timeout: Optional[int] = None) -> tuple[str, Optional[str]]:
    """
    Run claude /usage command and return output.

    Returns:
        Tuple of (stdout, error_message)
    """
    if cli_path is None:
        cli_path = find_claude_cli()

    if cli_path is None:
        return "", "Claude CLI not found"

    if timeout is None:
        config = load_config()
        timeout = config.cli_timeout

    try:
        result = subprocess.run(
            [cli_path, "/usage"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return result.stdout + result.stderr, None
    except subprocess.TimeoutExpired:
        return "", f"CLI timeout after {timeout}s"
    except FileNotFoundError:
        return "", f"Claude CLI not found at {cli_path}"
    except Exception as e:
        return "", f"CLI error: {str(e)}"


def parse_percentage(text: str, pattern: str) -> Optional[float]:
    """Extract percentage value using regex pattern."""
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, IndexError):
            pass
    return None


def parse_reset_time(text: str, pattern: str) -> Optional[str]:
    """Extract reset time using regex pattern."""
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def parse_cli_output(output: str) -> CLIUsageData:
    """
    Parse claude /usage output into structured data.

    Expected format variations:
    - "Session: 45% used (resets in 2h 30m)"
    - "Weekly: 12.5% used (resets Monday)"
    - "session usage: 45%"
    - "weekly limit: 12.5%"
    """
    data = CLIUsageData(raw_output=output)

    if not output.strip():
        data.parse_error = "Empty output"
        return data

    # Try various patterns for session percentage
    session_patterns = [
        r"session[:\s]+(\d+(?:\.\d+)?)\s*%",
        r"session usage[:\s]+(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?session",
    ]
    for pattern in session_patterns:
        pct = parse_percentage(output, pattern)
        if pct is not None:
            data.session_percent = pct
            break

    # Try various patterns for weekly percentage
    weekly_patterns = [
        r"weekly[:\s]+(\d+(?:\.\d+)?)\s*%",
        r"weekly usage[:\s]+(\d+(?:\.\d+)?)\s*%",
        r"weekly limit[:\s]+(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?weekly",
    ]
    for pattern in weekly_patterns:
        pct = parse_percentage(output, pattern)
        if pct is not None:
            data.weekly_percent = pct
            break

    # Parse reset times
    reset_patterns = [
        (r"session.*?reset[s]?\s+(?:in\s+)?([^,\n\)]+)", "session_reset"),
        (r"weekly.*?reset[s]?\s+(?:in\s+)?([^,\n\)]+)", "weekly_reset"),
        (r"reset[s]?\s+(?:in\s+)?(\d+[hm]\s*\d*[hm]?)", "session_reset"),
    ]
    for pattern, attr in reset_patterns:
        reset = parse_reset_time(output, pattern)
        if reset:
            setattr(data, attr, reset)

    # Check if we got any useful data
    if data.session_percent == 0 and data.weekly_percent == 0:
        # Try to find any percentage as fallback
        any_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", output)
        if any_pct:
            data.session_percent = float(any_pct.group(1))
        else:
            data.parse_error = "Could not parse usage percentages"

    return data


def get_cli_usage(cli_path: Optional[str] = None, timeout: Optional[int] = None) -> CLIUsageData:
    """
    Run claude /usage and parse the output.

    Returns:
        CLIUsageData with parsed values or error information
    """
    output, error = run_claude_usage(cli_path, timeout)

    if error:
        return CLIUsageData(raw_output=output, parse_error=error)

    return parse_cli_output(output)
