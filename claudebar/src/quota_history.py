"""The quota history: what the forecast's solid line is drawn from.

One JSON object per line in quota_history.jsonl beside the config:

    {"t": iso-utc, "p": "claude_session", "remaining": 61.0, "resets_at": iso-utc}
    {"t": iso-utc, "p": "deepseek", "balance": 12.4}

A percentage window is keyed by window_key: "claude_session" and
"codex_session" for the 5-hour sessions, "codex" for the one long window of a
Codex plan without a session. Lines under "claude", written before the chart
moved to the session, are no longer read and age out with RETENTION.

The refresh thread is the only writer and appends after a live fetch, never
for a value replayed from the snapshot cache. The first append in each process,
and then the first one a day after each prune, drops records older than
RETENTION, so a panel left running for weeks keeps the file near 9,000 lines at
the default five-minute refresh. A reader on the UI thread skips any line it
cannot parse, so a line caught half written costs nothing. Bytes that are not
UTF-8 are read as replacement characters and break only the line they are in.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from config import atomic_write_text, get_config_dir
from forecast import as_utc

logger = logging.getLogger("claudebar")

RETENTION = timedelta(days=10)
PRUNE_EVERY = timedelta(days=1)

_last_prune: Optional[datetime] = None   # when this process last pruned the file


class Record(NamedTuple):
    t: datetime                      # aware UTC
    value: float                     # remaining percent, or a balance in USD
    resets_at: Optional[datetime]    # aware UTC


def history_path() -> Path:
    return get_config_dir() / "quota_history.jsonl"


def window_key(provider: str, session: bool) -> str:
    return f"{provider}_session" if session else provider


def _value_key(provider: str) -> str:
    return "balance" if provider == "deepseek" else "remaining"


def _parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _parse_line(line: str) -> Optional[tuple]:
    """(provider, Record) for a well-formed line, else None."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("p"), str):
        return None
    t = _parse_time(raw.get("t"))
    value = raw.get(_value_key(raw["p"]))
    if t is None or isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        return None
    return raw["p"], Record(t, float(value), _parse_time(raw.get("resets_at")))


def _prune_if_due(path: Path, now: datetime) -> None:
    """Rewrite the file without old or malformed lines, at most once a day."""
    global _last_prune
    if _last_prune is not None and now - _last_prune < PRUNE_EVERY:
        return
    if path.exists():
        cutoff = now - RETENTION
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = [line for line in f.read().splitlines()]
        kept = [line for line in all_lines
                if (parsed := _parse_line(line)) is not None and parsed[1].t >= cutoff]
        if len(kept) != len(all_lines):
            atomic_write_text(path, "".join(line + "\n" for line in kept))
    _last_prune = now


def append(provider: str, value: float, resets_at: Optional[datetime] = None,
           now: Optional[datetime] = None) -> None:
    """Record one live reading. Never raises: a full disk must not break a refresh."""
    now = now or datetime.now(timezone.utc)
    record = {"t": as_utc(now).isoformat(), "p": provider, _value_key(provider): round(float(value), 3)}
    if resets_at is not None:
        record["resets_at"] = as_utc(resets_at).isoformat()
    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _prune_if_due(path, as_utc(now))
        except OSError as e:
            logger.debug("Quota history not pruned: %s", type(e).__name__)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        logger.warning("Quota history not written: %s", type(e).__name__)


def load(provider: str, since: datetime) -> list:
    """The provider's records from `since` on, oldest first."""
    since = as_utc(since)
    try:
        with open(history_path(), "r", encoding="utf-8", errors="replace") as f:
            parsed = (_parse_line(line) for line in f.read().splitlines())
            records = [r for p, r in (x for x in parsed if x) if p == provider and r.t >= since]
    except OSError:
        return []
    return sorted(records, key=lambda r: r.t)
