"""Banked resets: saved one-time limit resets that the user applies themselves.

Both Codex and Claude hand them out and both let them expire. This module is
pure: it turns the providers' responses into one shape, says which reset (if
any) may be spent, and words the panel's summary. It touches neither the
network nor Tk, so every rule here is a plain test. Spending one lives in
reset_redeem.py.

Times are aware UTC throughout and become local only when they are worded.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

CLAUDE = "claude"
CODEX = "codex"
PROVIDERS = (CLAUDE, CODEX)

# state: "ok" parsed (the count may be zero), "ineligible" the account has none
# to give, "failed" the data could not be loaded.
STATES = ("ok", "ineligible", "failed")

# Amber below this: a reset nobody used is lost when it expires.
URGENT_WITHIN = timedelta(days=7)

# The one reset type the Codex client spends; another kind is not ours to offer.
_CODEX_RESET_TYPE = "codex_rate_limits"

# Epoch values above this are milliseconds (1e11 s is the year 5138).
_EPOCH_MS_THRESHOLD = 1e11


@dataclass(frozen=True)
class ResetCredit:
    """One spendable thing: a Codex credit, or a Claude grant (which can hold
    several resets)."""
    id: str
    label: str = ""
    expires_at: Optional[datetime] = None
    count: int = 1                       # resets still unspent in it
    clears: tuple = ()                   # windows it refreshes (Claude)
    paused: bool = False
    usable_now: bool = True
    requires_limit: bool = False         # Claude: only usable while at a limit


@dataclass
class BankedResets:
    provider: str
    state: str = "ok"
    available: int = 0                   # authoritative count, never len(credits)
    credits: list = field(default_factory=list)   # earliest expiry first
    applicable: Optional[int] = None     # Codex: how many would reset something now
    next_credit_id: Optional[str] = None  # Claude: the only grant the server accepts
    cooldown_until: Optional[datetime] = None
    at_limit: bool = False               # Claude
    reason: Optional[str] = None         # why the account is not eligible


@dataclass(frozen=True)
class Claim:
    """What the panel may offer to spend, and what the confirmation names."""
    provider: str
    credit_id: Optional[str]
    label: str
    expires_at: Optional[datetime]
    clears: tuple = ()


def parse_expiry(value) -> Optional[datetime]:
    """An ISO 8601 string, epoch seconds or epoch milliseconds as aware UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            value = int(text)
        else:
            try:
                parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            except ValueError:
                return None
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None \
                else parsed.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        seconds = value / 1000 if abs(value) > _EPOCH_MS_THRESHOLD else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _whole(value) -> Optional[int]:
    """An integer count, or None. A JSON bool is an int subclass, so it is out."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _by_expiry(credits: list) -> list:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(credits, key=lambda c: c.expires_at or far)


def _valid_credit(credit: ResetCredit, now: datetime) -> bool:
    """Not paused, not spent and not past its expiry."""
    return (not credit.paused and credit.count >= 1
            and (credit.expires_at is None or credit.expires_at > now))


def _parse_grant(raw) -> Optional[ResetCredit]:
    """One Claude grant, or None when it is malformed or has nothing left."""
    if not isinstance(raw, dict):
        return None
    grant_id = raw.get("id")
    if not isinstance(grant_id, str) or not grant_id.strip():
        return None
    total, left = _whole(raw.get("resets_total")), _whole(raw.get("resets_left"))
    if total is None or total < 1 or left is None:
        return None
    ends_at = parse_expiry(raw.get("ends_at"))
    if ends_at is None:
        return None
    left = max(0, min(left, total))
    if left == 0:
        return None
    clears = raw.get("clears")
    return ResetCredit(
        id=grant_id.strip(),
        label=raw.get("label") if isinstance(raw.get("label"), str) else "",
        expires_at=ends_at,
        count=left,
        clears=tuple(c for c in clears if isinstance(c, str)) if isinstance(clears, list) else (),
        paused=raw.get("paused") is True,
        usable_now=raw.get("usable_now") is True,
        requires_limit=raw.get("use_requires_limit") is True,
    )


def parse_claude_banked(block, now: datetime) -> Optional[BankedResets]:
    """The `cedar_ember` block of Anthropic's usage response.

    None when there is no usable block at all (the panel then draws nothing).
    An account that is not eligible is remembered as such rather than dropped,
    so "none" and "not eligible" stay distinguishable.
    """
    if not isinstance(block, dict):
        return None
    if block.get("eligible") is not True:
        reason = block.get("ineligible_reason")
        return BankedResets(provider=CLAUDE, state="ineligible",
                            reason=reason if isinstance(reason, str) else None)
    grants = block.get("grants")
    credits = _by_expiry([c for c in map(_parse_grant, grants) if c]
                         if isinstance(grants, list) else [])
    next_id = block.get("next_grant_id")
    return BankedResets(
        provider=CLAUDE,
        available=sum(c.count for c in credits if _valid_credit(c, now)),
        credits=credits,
        next_credit_id=next_id if isinstance(next_id, str) and next_id else None,
        cooldown_until=parse_expiry(block.get("cooldown_until")),
        at_limit=block.get("at_limit") is True,
    )


def parse_codex_banked(usage_block, details, now: datetime) -> Optional[BankedResets]:
    """Codex credits from `rate_limit_reset_credits` (the count) and the
    details call (the list, which may have failed and so be None).

    The count in the usage response is authoritative. The list only adds
    titles and expiries; its length is never used as the count.
    """
    usage = usage_block if isinstance(usage_block, dict) else {}
    body = details if isinstance(details, dict) else {}

    available = _whole(usage.get("available_count"))
    if available is None:
        available = _whole(body.get("available_count"))
    if available is None or available < 0:
        return None

    credits = []
    rows = body.get("credits")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") != "available":
            continue
        credit_id = row.get("id")
        if not isinstance(credit_id, str) or not credit_id.strip():
            continue
        if row.get("reset_type") not in (None, _CODEX_RESET_TYPE):
            continue
        expires_at = parse_expiry(row.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        title = row.get("title")
        credits.append(ResetCredit(id=credit_id.strip(), label=title if isinstance(title, str) else "",
                                   expires_at=expires_at))

    applicable = _whole(usage.get("applicable_available_count"))
    return BankedResets(provider=CODEX, available=available, credits=_by_expiry(credits),
                        applicable=applicable if applicable is not None and applicable >= 0 else None)


def next_expiry(banked: Optional[BankedResets], now: datetime) -> Optional[datetime]:
    """When the soonest still-usable credit expires."""
    if banked is None:
        return None
    dates = [c.expires_at for c in banked.credits if c.expires_at and _valid_credit(c, now)]
    return min(dates) if dates else None


def claim_for(banked: Optional[BankedResets], live: bool, now: datetime) -> Optional[Claim]:
    """The reset that may be spent right now, or None.

    `live` is whether the snapshot was fetched this session. Cached data is
    shown but never spent: the reset it names may already be gone.
    """
    if banked is None or not live or banked.state != "ok" or banked.available < 1:
        return None
    if banked.provider == CLAUDE:
        credit = next((c for c in banked.credits if c.id == banked.next_credit_id), None)
        if credit is None or not _valid_credit(credit, now) or not credit.usable_now:
            return None
        if credit.requires_limit and not banked.at_limit:
            return None
        if banked.cooldown_until is not None and banked.cooldown_until > now:
            return None
        return Claim(CLAUDE, credit.id, credit.label, credit.expires_at, credit.clears)
    if banked.provider == CODEX:
        if not banked.applicable:
            return None
        credit = next((c for c in banked.credits if _valid_credit(c, now)), None)
        if credit is None:
            return Claim(CODEX, None, "Banked reset", None)
        return Claim(CODEX, credit.id, credit.label or "Banked reset", credit.expires_at)
    return None


def claim_hint(banked: Optional[BankedResets], now: datetime) -> str:
    """Why a live reset cannot be spent right now, in a few words ("" if it can
    or if there is nothing to say)."""
    if banked is None or banked.state != "ok" or banked.available < 1:
        return ""
    if banked.provider == CODEX:
        return "nothing to reset now" if banked.applicable == 0 else ""
    if banked.cooldown_until is not None and banked.cooldown_until > now:
        return "cooling down"
    credit = next((c for c in banked.credits if c.id == banked.next_credit_id), None)
    if credit is None:
        return ""
    if credit.paused:
        return "paused"
    if credit.requires_limit and not banked.at_limit:
        return "usable once limited"
    return ""


def restores_at(banked: Optional[BankedResets], when: datetime, now: datetime) -> bool:
    """Whether a banked reset would still be valid at `when` (a forecast run-out)."""
    if banked is None or banked.state != "ok" or banked.available < 1:
        return False
    if not banked.credits:
        return True        # a count with no expiry data: assume it is good
    return any(_valid_credit(c, now) and (c.expires_at is None or c.expires_at >= when)
               for c in banked.credits)


def _remaining_text(remaining: timedelta) -> str:
    if remaining >= timedelta(days=1):
        return f"{math.ceil(remaining / timedelta(days=1))} d"
    if remaining >= timedelta(hours=1):
        return f"{int(remaining.total_seconds() // 3600)} h"
    return f"{max(1, int(remaining.total_seconds() // 60))} min"


def summary_text(banked: BankedResets, now: datetime, tz=None) -> tuple:
    """("3 available · next expires Oct 22 (10 d)", urgent)."""
    text = f"{banked.available} available"
    soonest = next_expiry(banked, now)
    if soonest is None:
        return text, False
    local = soonest.astimezone(tz)
    remaining = soonest - now
    text += f" · next expires {local:%b} {local.day} ({_remaining_text(remaining)})"
    return text, remaining <= URGENT_WITHIN


def from_dict(data) -> Optional[BankedResets]:
    """Rebuild a BankedResets from the snapshot cache's JSON (dataclasses.asdict
    plus ISO datetimes). Anything unreadable is None, never an exception."""
    if not isinstance(data, dict) or data.get("provider") not in PROVIDERS:
        return None
    credits = []
    for raw in data.get("credits") if isinstance(data.get("credits"), list) else []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        credits.append(ResetCredit(
            id=raw["id"],
            label=raw.get("label") if isinstance(raw.get("label"), str) else "",
            expires_at=parse_expiry(raw.get("expires_at")),
            count=_whole(raw.get("count")) or 1,
            clears=tuple(c for c in raw.get("clears") or () if isinstance(c, str)),
            paused=raw.get("paused") is True,
            usable_now=raw.get("usable_now") is not False,
            requires_limit=raw.get("requires_limit") is True,
        ))
    state = data.get("state")
    available = _whole(data.get("available"))
    applicable = _whole(data.get("applicable"))
    next_id = data.get("next_credit_id")
    reason = data.get("reason")
    return BankedResets(
        provider=data["provider"],
        state=state if state in STATES else "failed",
        available=available if available is not None and available >= 0 else 0,
        credits=_by_expiry(credits),
        applicable=applicable,
        next_credit_id=next_id if isinstance(next_id, str) else None,
        cooldown_until=parse_expiry(data.get("cooldown_until")),
        at_limit=data.get("at_limit") is True,
        reason=reason if isinstance(reason, str) else None,
    )
