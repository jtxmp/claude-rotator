"""Tests for rate limit detection, parsing, and caching."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from claude_rotator.rate_limit import (
    RateLimitCache,
    is_usage_limited,
    parse_reset_time,
)


class TestIsUsageLimited:
    def test_detects_out_of_extra_usage(self):
        assert is_usage_limited("You are out of extra usage for today", "")

    def test_detects_usage_limit(self):
        assert is_usage_limited("", "usage limit reached")

    def test_detects_rate_limit(self):
        assert is_usage_limited("rate limit exceeded", "")

    def test_case_insensitive(self):
        assert is_usage_limited("USAGE LIMIT", "")

    def test_no_limit_phrases(self):
        assert not is_usage_limited("Hello world", "some error")

    def test_empty_strings(self):
        assert not is_usage_limited("", "")


class TestParseResetTime:
    def test_parses_pm_time(self):
        result = parse_reset_time("Your usage resets 8pm (UTC)", "")
        assert result is not None
        assert result.hour == 20
        assert result.minute == 0
        assert result.tzinfo == timezone.utc

    def test_parses_am_time(self):
        result = parse_reset_time("resets 2am (UTC)", "")
        assert result is not None
        assert result.hour == 2

    def test_parses_12pm(self):
        result = parse_reset_time("resets 12pm (UTC)", "")
        assert result is not None
        assert result.hour == 12

    def test_parses_12am(self):
        result = parse_reset_time("resets 12am (UTC)", "")
        assert result is not None
        assert result.hour == 0

    def test_returns_none_for_no_match(self):
        assert parse_reset_time("some error", "") is None

    def test_searches_stderr_too(self):
        result = parse_reset_time("", "resets 3pm (UTC)")
        assert result is not None
        assert result.hour == 15

    def test_future_time_is_today(self):
        # Mock "now" to 10:00 UTC and parse "resets 8pm (UTC)"
        mock_now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        with patch("claude_rotator.rate_limit.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = parse_reset_time("resets 8pm (UTC)", "")
        assert result is not None
        assert result.day == 15

    def test_past_time_is_tomorrow(self):
        # Mock "now" to 22:00 UTC and parse "resets 8pm (UTC)"
        mock_now = datetime(2026, 3, 15, 22, 0, 0, tzinfo=timezone.utc)
        with patch("claude_rotator.rate_limit.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = parse_reset_time("resets 8pm (UTC)", "")
        assert result is not None
        assert result.day == 16


class TestRateLimitCache:
    def test_not_limited_by_default(self):
        cache = RateLimitCache()
        assert not cache.is_limited(None)
        assert not cache.is_limited("/some/path")

    def test_record_and_check(self):
        cache = RateLimitCache()
        # Record a rate limit that resets in the future
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        cache._until[None] = future
        assert cache.is_limited(None)

    def test_expired_limit_is_cleared(self):
        cache = RateLimitCache()
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        cache._until[None] = past
        assert not cache.is_limited(None)
        assert None not in cache._until

    def test_clear_specific_account(self):
        cache = RateLimitCache()
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        cache._until["/acct1"] = future
        cache._until["/acct2"] = future
        cache.clear("/acct1")
        assert not cache.is_limited("/acct1")
        assert cache.is_limited("/acct2")

    def test_clear_all(self):
        cache = RateLimitCache()
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        cache._until[None] = future
        cache._until["/acct1"] = future
        cache.clear()
        assert not cache.is_limited(None)
        assert not cache.is_limited("/acct1")

    def test_record_with_parseable_time(self):
        cache = RateLimitCache()
        cache.record(None, "resets 8pm (UTC)", "")
        assert cache.is_limited(None)

    def test_record_without_parseable_time_uses_fallback(self):
        cache = RateLimitCache()
        cache.record(None, "some error", "")
        assert cache.is_limited(None)
        # Fallback should be ~5 minutes from now
        reset = cache._until[None]
        diff = reset - datetime.now(timezone.utc)
        assert timedelta(minutes=4) < diff < timedelta(minutes=6)
