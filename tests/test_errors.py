"""Tests for error sanitization."""

from claude_rotator.errors import ClaudeError, _sanitize_stderr


class TestSanitizeStderr:
    def test_redacts_anthropic_api_key(self):
        stderr = "Error: invalid key sk-ant-abc123-xyz"
        result = _sanitize_stderr(stderr)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result

    def test_redacts_bearer_token(self):
        stderr = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret"
        result = _sanitize_stderr(stderr)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "[REDACTED]" in result

    def test_redacts_token_equals(self):
        stderr = "token=abc123secret"
        result = _sanitize_stderr(stderr)
        assert "abc123secret" not in result
        assert "[REDACTED]" in result

    def test_truncates_to_500_chars(self):
        stderr = "x" * 1000
        result = _sanitize_stderr(stderr)
        assert len(result) == 500

    def test_preserves_non_sensitive_content(self):
        stderr = "Process exited with code 1"
        result = _sanitize_stderr(stderr)
        assert result == stderr

    def test_redacts_multiple_patterns(self):
        stderr = "key=sk-ant-abc123 Bearer my-token"
        result = _sanitize_stderr(stderr)
        assert "sk-ant-" not in result
        assert "my-token" not in result


class TestClaudeError:
    def test_message_is_sanitized(self):
        error = ClaudeError("sk-ant-api-key-12345 failed", 1)
        assert "sk-ant-" not in str(error)
        assert "[REDACTED]" in str(error)

    def test_raw_stderr_preserved(self):
        error = ClaudeError("sk-ant-api-key-12345 failed", 1)
        assert error.stderr == "sk-ant-api-key-12345 failed"
