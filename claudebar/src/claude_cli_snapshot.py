"""The Claude CLI's saved copy of the usage reply: a second source that costs no request.

The usage endpoint refuses (429, Retry-After 0) a caller that comes soon after
another caller, and Claude Code asks at every start, so ClaudeBar is often refused
in the minutes after a Claude Code session began. Each time the CLI does get an
answer it keeps the whole reply in its own config, `~/.claude.json`:

    "cachedUsageUtilization": {"fetchedAtMs": 1791..., "accountUuid": "...",
                               "utilization": {<the endpoint's reply>}}

Reading that file asks nothing of Anthropic, so it can neither be refused nor take
a share of the budget the CLI and other tools draw on. It is an internal file the
CLI may change, so this is best-effort: any surprise is "no reading", noted once in
the log, and the caller falls back to what it had before.

The same limits as the CLI's own use of it: the reply must be for the signed-in
account and at most an hour old. A window whose reset time has passed describes a
window that is over and is dropped. Banked resets and extra usage are not carried:
a reset is only ever offered from a live reading of ClaudeBar's own.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from oauth_usage import OAuthUsageData, parse_usage_response

logger = logging.getLogger("claudebar")

#: The CLI itself trusts its saved reply for this long.
MAX_AGE = timedelta(hours=1)

_WINDOWS = ("five_hour", "seven_day")
_warned = False


class CliReading(NamedTuple):
    t: datetime               # aware UTC: when the CLI got the reply
    usage: OAuthUsageData     # the running windows only


def config_path() -> Path:
    """Where the CLI keeps its global config, beside the credentials ClaudeBar reads."""
    return Path.home() / ".claude.json"


def _not_understood() -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("Claude CLI usage snapshot not understood; the CLI's format may have changed")


def read(now: Optional[datetime] = None) -> Optional[CliReading]:
    """The CLI's latest usage reading for the signed-in account, or None. Never raises."""
    now = now or datetime.now(timezone.utc)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return None  # no CLI config, or it is being rewritten as we read: next refresh
    try:
        return _reading(config, now)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
        _not_understood()
        return None


def _reading(config, now: datetime) -> Optional[CliReading]:
    if not isinstance(config, dict) or "cachedUsageUtilization" not in config:
        return None
    cached = config["cachedUsageUtilization"]
    fetched_ms, reply = cached["fetchedAtMs"], cached["utilization"]
    if isinstance(fetched_ms, bool) or not isinstance(fetched_ms, (int, float)) \
            or not math.isfinite(fetched_ms) or not isinstance(reply, dict):
        raise ValueError("snapshot shape")
    for key in _WINDOWS:
        if reply.get(key) is not None and not isinstance(reply[key], dict):
            raise ValueError("window shape")

    account = config.get("oauthAccount")
    signed_in = account.get("accountUuid") if isinstance(account, dict) else None
    if cached.get("accountUuid") != signed_in:
        return None

    taken = datetime.fromtimestamp(fetched_ms / 1000, timezone.utc)
    if not timedelta(0) <= now - taken <= MAX_AGE:
        return None

    parsed = parse_usage_response({key: reply[key] for key in _WINDOWS if isinstance(reply.get(key), dict)})
    running = [w if w is not None and (w.resets_at is None or w.resets_at > now) else None
               for w in (parsed.session, parsed.weekly)]
    if not any(running):
        return None
    return CliReading(taken, OAuthUsageData(session=running[0], weekly=running[1]))
