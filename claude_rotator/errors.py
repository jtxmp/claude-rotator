"""Exceptions for claude-rotator."""

import re

_SENSITIVE_PATTERNS = [
    re.compile(r"(sk-ant-[a-zA-Z0-9-]+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+\S+)", re.IGNORECASE),
    re.compile(r"(token[=:]\s*\S+)", re.IGNORECASE),
]


def _sanitize_stderr(stderr: str) -> str:
    """Redact sensitive tokens and keys from stderr before including in error messages."""
    result = stderr
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result[:500]


class ClaudeError(Exception):
    """Raised when a Claude subprocess exits with a non-zero code or times out."""

    def __init__(self, stderr: str, returncode: int = 1):
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"Claude failed (exit {returncode}): {_sanitize_stderr(stderr)}")
