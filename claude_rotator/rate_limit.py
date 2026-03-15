"""Rate limit detection, reset time parsing, and per-account caching."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

USAGE_LIMIT_PHRASES = [
    "out of extra usage",
    "usage limit",
    "rate limit",
]


class RateLimitCache:
    """Tracks per-account rate limit cooldowns with auto-expiry."""

    def __init__(self) -> None:
        self._until: dict[str | None, datetime] = {}

    def is_limited(self, account: str | None) -> bool:
        """Return True if the account is known to be rate-limited."""
        reset_time = self._until.get(account)
        if reset_time is None:
            return False
        if datetime.now(timezone.utc) >= reset_time:
            self._until.pop(account, None)
            return False
        return True

    def record(self, account: str | None, stdout: str, stderr: str) -> None:
        """Cache the rate limit reset time for an account."""
        reset_time = parse_reset_time(stdout, stderr)
        if reset_time:
            self._until[account] = reset_time
            label = account or "default"
            logger.info(f"Account '{label}' rate-limited until {reset_time.isoformat()}")
        else:
            fallback = datetime.now(timezone.utc) + timedelta(minutes=5)
            self._until[account] = fallback

    def clear(self, account: str | None = None) -> None:
        """Clear cached rate limit for one account, or all if account is not given."""
        if account is None:
            self._until.clear()
        else:
            self._until.pop(account, None)


def is_usage_limited(stdout: str, stderr: str) -> bool:
    """Check if the output indicates a usage limit error."""
    combined = (stdout + stderr).lower()
    return any(phrase in combined for phrase in USAGE_LIMIT_PHRASES)


def parse_reset_time(stdout: str, stderr: str) -> datetime | None:
    """Extract the rate limit reset time from the error message.

    Looks for patterns like "resets 8pm (UTC)" or "resets 2am (UTC)".
    Returns a UTC datetime, or None if unparseable.
    """
    combined = stdout + stderr
    match = re.search(r"resets\s+(\d{1,2})(am|pm)\s*\(UTC\)", combined, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    ampm = match.group(2).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    now = datetime.now(timezone.utc)
    reset = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset
