"""Exceptions for claude-rotator."""


class ClaudeError(Exception):
    """Raised when a Claude subprocess exits with a non-zero code or times out."""

    def __init__(self, stderr: str, returncode: int = 1):
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"Claude failed (exit {returncode}): {stderr[:500]}")
