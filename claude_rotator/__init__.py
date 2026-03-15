"""claude-rotator: Claude CLI subprocess runner with automatic account rotation."""

from .errors import ClaudeError
from .runner import ClaudeResult, ClaudeRunner

__all__ = ["ClaudeRunner", "ClaudeResult", "ClaudeError"]
__version__ = "1.0.0"
