"""Fetch Claude usage data by parsing CLI /usage command output via ConPTY.

Last-resort fallback when OAuth API is unavailable.
Spawns Claude CLI in a Windows pseudo-terminal, sends /usage command,
parses the rendered TUI output for usage percentages.
"""

import logging
import os
import re
import time
from typing import Optional

from oauth_usage import OAuthUsageData, UsageWindow
from config import find_claude_cli

logger = logging.getLogger("claudebar")

try:
    import winpty
    HAS_WINPTY = True
except ImportError:
    HAS_WINPTY = False

# ANSI escape patterns for stripping terminal formatting
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")

# Pre-compiled pattern for matching blank lines (newline + optional whitespace + newline)
_BLANK_LINE = re.compile(r"\n\s*\n")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and carriage returns from text."""
    return ANSI_ESCAPE.sub("", text)


def _clean_reset_text(raw):
    """Clean up garbled reset text from PTY rendering."""
    if not raw:
        return raw
    # Insert space between capitalized word and digit: "Mar13" -> "Mar 13"
    raw = re.sub(r"([A-Z][a-z]+)(\d)", r"\1 \2", raw)
    # Space after comma if missing: ",7am" -> ", 7am"
    raw = re.sub(r",(\S)", r", \1", raw)
    # Space before opening paren if missing: "am(Europe" -> "am (Europe"
    raw = re.sub(r"(\S)\(", r"\1 (", raw)
    # Normalize whitespace
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _extract_reset(rest_text):
    """Extract reset time from text after a percentage marker."""
    # Find earliest section boundary (blank line or "Current")
    bl_match = _BLANK_LINE.search(rest_text)
    cur_match = re.search(r"[Cc]urrent", rest_text)

    end_pos = len(rest_text)
    if bl_match:
        end_pos = min(end_pos, bl_match.start())
    if cur_match:
        end_pos = min(end_pos, cur_match.start())

    segment = rest_text[:end_pos]

    # Find text after "used" keyword
    um = re.search(r"used\s*(.*)", segment, re.DOTALL)
    if not um:
        return None

    raw = um.group(1).strip()
    if not raw:
        return None

    # Strip Res/Reset/Resets/Reses prefix (PTY garbles "Resets" to "Reses" etc)
    raw = re.sub(r"^[Rr]es[a-z]*\s*", "", raw).strip()
    if raw:
        return _clean_reset_text(raw)
    return None


def _parse_usage_output(output: str) -> OAuthUsageData:
    """Parse percentage values and reset times from CLI /usage TUI output."""
    clean = _strip_ansi(output)

    session = None
    weekly = None
    session_reset = None
    weekly_reset = None

    # Session: "Current session" followed by "XX% used"
    m = re.search(r"[Cc]urrent\s*session.*?(\d+(?:\.\d+)?)\s*%", clean, re.DOTALL)
    if m:
        session = UsageWindow(utilization=float(m.group(1)))
        session_reset = _extract_reset(clean[m.end():])

    # Weekly: "Current week (all models)" followed by "XX% used"
    m = re.search(
        r"[Cc]urrent\s*week\s*\(?all\s*models?\)?.*?(\d+(?:\.\d+)?)\s*%",
        clean, re.DOTALL,
    )
    if m:
        weekly = UsageWindow(utilization=float(m.group(1)))
        weekly_reset = _extract_reset(clean[m.end():])

    # Fallback patterns
    if session is None:
        m = re.search(r"session.*?(\d+(?:\.\d+)?)\s*%", clean, re.IGNORECASE | re.DOTALL)
        if m:
            session = UsageWindow(utilization=float(m.group(1)))

    if weekly is None:
        m = re.search(r"week\s*\(.*?\).*?(\d+(?:\.\d+)?)\s*%", clean, re.IGNORECASE | re.DOTALL)
        if m:
            weekly = UsageWindow(utilization=float(m.group(1)))

    # Attach reset text to UsageWindow objects
    if session and session_reset:
        session.raw_reset_text = session_reset
    if weekly and weekly_reset:
        weekly.raw_reset_text = weekly_reset

    if session is None and weekly is None:
        return OAuthUsageData(error="Could not parse usage from CLI output")

    return OAuthUsageData(session=session, weekly=weekly)


def _read_available(pty) -> str:
    """Read whatever is currently available from PTY (non-blocking)."""
    buf = ""
    for _ in range(20):
        try:
            chunk = pty.read()
            if chunk:
                buf += chunk
            else:
                break
        except Exception:
            break
    return buf


def _read_all(pty, timeout: float = 5.0, interval: float = 0.3) -> str:
    """Read all available output from PTY within timeout."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = pty.read()
            if chunk:
                buf += chunk
                continue
        except Exception:
            pass
        time.sleep(interval)
    return buf


def _wait_for_ready(pty, max_wait: float = 20.0) -> str:
    """Wait for TUI to be ready by polling for known indicators."""
    buf = ""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            chunk = pty.read()
            if chunk:
                buf += chunk
                lower = buf.lower()
                if "plan mode" in lower or "shift+tab" in lower or "what" in lower:
                    time.sleep(0.5)
                    buf += _read_available(pty)
                    return buf
        except Exception:
            pass
        time.sleep(0.3)
    return buf


def _wait_for_usage_panel(pty, max_wait: float = 12.0) -> str:
    """Wait for /usage panel to render by polling for percentage text."""
    buf = ""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            chunk = pty.read()
            if chunk:
                buf += chunk
                clean = _strip_ansi(buf)
                if "% used" in clean.lower() or "urrent session" in clean:
                    time.sleep(1.5)
                    buf += _read_available(pty)
                    return buf
        except Exception:
            pass
        time.sleep(0.3)
    return buf


def _send_slash_command(pty, command: str) -> None:
    """Send a TUI slash command using Escape to dismiss autocomplete.

    Type the command, wait for autocomplete dropdown to fully render,
    press Escape to dismiss it (so the typed text stays as-is),
    then Enter to submit the slash command.
    """
    pty.write(command)
    time.sleep(1.0)
    _read_all(pty, timeout=2.0)  # Let autocomplete fully render
    pty.write(chr(27))  # Escape: dismiss autocomplete dropdown
    time.sleep(0.5)
    _read_all(pty, timeout=2.0)  # Let TUI process Escape
    pty.write(chr(13))  # Enter: submit slash command


def fetch_cli_usage(cli_path: Optional[str] = None) -> OAuthUsageData:
    """Fetch usage by spawning Claude CLI in a ConPTY and sending /usage."""
    if not HAS_WINPTY:
        return OAuthUsageData(error="pywinpty not installed")

    if cli_path is None:
        cli_path = find_claude_cli()
    if not cli_path:
        return OAuthUsageData(error="Claude CLI not found")

    try:
        pty = winpty.PTY(160, 50)

        env_parts = []
        for k, v in os.environ.items():
            if k.upper() != "CLAUDECODE":
                env_parts.append(f"{k}={v}")
        env_str = chr(0).join(env_parts) + chr(0)

        cmdline = f'"{cli_path}" --allowed-tools ""'
        pty.spawn(cli_path, cmdline=cmdline, env=env_str)

        logger.info("CLI PTY spawned PID=%d", pty.pid)

        initial = _wait_for_ready(pty, max_wait=20.0)
        if not initial:
            return OAuthUsageData(error="CLI PTY: TUI did not start")

        logger.info("CLI PTY: TUI ready (%d chars)", len(initial))

        lower = initial.lower()
        if "trust" in lower or "safety" in lower:
            logger.info("CLI PTY: trust dialog detected")
            pty.write("y" + chr(13))
            time.sleep(3)
            _read_all(pty, timeout=2)

        _read_all(pty, timeout=1)

        logger.info("CLI PTY: sending /usage")
        _send_slash_command(pty, "/usage")

        output = _wait_for_usage_panel(pty, max_wait=12.0)

        logger.info("CLI PTY: /usage output (%d chars)", len(output))

        try:
            import signal
            os.kill(pty.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        # Debug: dump raw CLI output for diagnosis
        try:
            import tempfile
            debug_dir = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "ClaudeBar")
            with open(os.path.join(debug_dir, "cli_raw.txt"), "w", encoding="utf-8") as dbg:
                dbg.write(output)
            with open(os.path.join(debug_dir, "cli_clean.txt"), "w", encoding="utf-8") as dbg:
                dbg.write(_strip_ansi(output))
        except Exception:
            pass

        if not output:
            return OAuthUsageData(error="Empty CLI /usage output")

        result = _parse_usage_output(output)
        if result.is_valid:
            logger.info(
                "CLI PTY: session=%.1f%%, weekly=%.1f%%, session_reset=%s, weekly_reset=%s",
                result.session.utilization if result.session else 0,
                result.weekly.utilization if result.weekly else 0,
                result.session.raw_reset_text if result.session else None,
                result.weekly.raw_reset_text if result.weekly else None,
            )
        return result

    except Exception as e:
        logger.warning("CLI PTY failed: %s", e)
        return OAuthUsageData(error=f"CLI PTY error: {e}")
