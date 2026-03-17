"""claude-rotator: Claude CLI subprocess runner with automatic account rotation."""

from importlib.metadata import PackageNotFoundError, version

from .errors import ClaudeError
from .runner import ClaudeResult, ClaudeRunner

__all__ = ["ClaudeRunner", "ClaudeResult", "ClaudeError"]

try:
    __version__ = version("claude-rotator")
except PackageNotFoundError:
    __version__ = "0.0.0"
