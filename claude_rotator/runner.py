"""ClaudeRunner: sync and async Claude CLI subprocess management with account rotation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ClaudeError
from .rate_limit import RateLimitCache, is_usage_limited

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    """Result from a Claude CLI invocation."""

    output: str
    cost_usd: float
    duration_seconds: float
    model: str


def _build_cmd(model: str, allowed_tools: str | None) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    return cmd


def _build_env(home_dir: str | None) -> dict[str, str]:
    env = {**os.environ}
    if home_dir:
        env["HOME"] = home_dir
        # Windows uses USERPROFILE instead of HOME for the user directory.
        if sys.platform == "win32":
            env["USERPROFILE"] = home_dir
    return env


def _parse_output(stdout: str) -> tuple[str, float]:
    """Extract result text and cost from Claude JSON output.

    Returns (output_text, cost_usd).
    Raises ClaudeError if the JSON response indicates an error.
    """
    cost = 0.0
    output_text = stdout
    try:
        data = json.loads(stdout)
        if data.get("is_error"):
            raise ClaudeError(data.get("result", "Unknown error"))
        output_text = data.get("result", stdout)
        cost = data.get("total_cost_usd", 0.0)
        if not cost and isinstance(data.get("cost"), dict):
            cost = data["cost"].get("total_usd", 0.0)
    except ClaudeError:
        raise
    except (json.JSONDecodeError, TypeError):
        pass
    return output_text, cost


def _kill_process_tree(pid: int) -> None:
    """Kill the process and its children. Platform-aware."""
    if pid is None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        # On Linux/macOS, kill the entire process group created by
        # start_new_session=True (equivalent to setsid).
        sigterm = getattr(signal, "SIGTERM", 15)
        sigkill = getattr(signal, "SIGKILL", 9)
        try:
            os.killpg(os.getpgid(pid), sigterm)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(pid, sigkill)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class ClaudeRunner:
    """Runs Claude CLI subprocesses with automatic account rotation on rate limits.

    Args:
        accounts: List of HOME directory paths for Claude CLI accounts.
            Use None for the default HOME (~/.claude/).
            Each directory should contain a .claude/ folder with separate credentials.
    """

    def __init__(self, accounts: list[str | None] | None = None) -> None:
        self.accounts: list[str | None] = accounts if accounts is not None else [None]
        self._cache = RateLimitCache()

    def run(
        self,
        prompt: str,
        model: str = "sonnet",
        tools: str | None = "Read,Write",
        cwd: Path | str | None = None,
        timeout: int = 600,
    ) -> ClaudeResult:
        """Run ``claude -p`` synchronously.

        Pipes the prompt via stdin. On usage limit errors, retries with
        the next account in the list.
        """
        cmd = _build_cmd(model, tools)
        cwd_str = str(cwd) if cwd else None
        is_win = sys.platform == "win32"

        popen_kwargs: dict = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=None,
            cwd=cwd_str,
        )
        if is_win:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        for i, home_dir in enumerate(self.accounts):
            if self._cache.is_limited(home_dir):
                logger.debug(f"Skipping rate-limited account '{home_dir or 'default'}'")
                continue

            start = time.time()
            popen_kwargs["env"] = _build_env(home_dir)

            proc = subprocess.Popen(cmd, **popen_kwargs)

            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc.pid)
                proc.wait()
                raise ClaudeError(f"Timeout after {timeout}s", -1)

            duration = time.time() - start

            if is_usage_limited(stdout, stderr):
                self._cache.record(home_dir, stdout, stderr)
                if i < len(self.accounts) - 1:
                    next_acct = self.accounts[i + 1] or "default"
                    logger.warning(
                        f"Account '{home_dir or 'default'}' hit usage limit, trying '{next_acct}'"
                    )
                    continue
                raise ClaudeError("All Claude accounts hit usage limits", 1)

            if proc.returncode != 0:
                raise ClaudeError(stderr, proc.returncode)

            output_text, cost = _parse_output(stdout)
            return ClaudeResult(
                output=output_text,
                cost_usd=cost,
                duration_seconds=duration,
                model=model,
            )

        raise ClaudeError("All Claude accounts hit usage limits", 1)

    async def run_async(
        self,
        prompt: str,
        model: str = "sonnet",
        tools: str | None = "Read,Write",
        cwd: Path | str | None = None,
        timeout: int = 600,
    ) -> ClaudeResult:
        """Run ``claude -p`` asynchronously.

        Pipes the prompt via stdin. On usage limit errors, retries with
        the next account in the list.
        """
        cmd = _build_cmd(model, tools)
        cwd_str = str(cwd) if cwd else None
        is_win = sys.platform == "win32"

        for i, home_dir in enumerate(self.accounts):
            if self._cache.is_limited(home_dir):
                logger.debug(f"Skipping rate-limited account '{home_dir or 'default'}'")
                continue

            start = time.time()
            env = _build_env(home_dir)

            # asyncio.create_subprocess_exec does not support creationflags,
            # but on Windows start_new_session is ignored (harmless to pass False).
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd_str,
                env=env,
                start_new_session=not is_win,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                _kill_process_tree(proc.pid)
                await proc.wait()
                raise ClaudeError("Timeout exceeded", -1)

            duration = time.time() - start
            stdout = stdout_bytes.decode()
            stderr = stderr_bytes.decode()

            if is_usage_limited(stdout, stderr):
                self._cache.record(home_dir, stdout, stderr)
                if i < len(self.accounts) - 1:
                    next_acct = self.accounts[i + 1] or "default"
                    logger.warning(
                        f"Account '{home_dir or 'default'}' hit usage limit, trying '{next_acct}'"
                    )
                    continue
                raise ClaudeError("All Claude accounts hit usage limits", 1)

            if proc.returncode != 0:
                raise ClaudeError(stderr, proc.returncode or 1)

            output_text, cost = _parse_output(stdout)
            return ClaudeResult(
                output=output_text,
                cost_usd=cost,
                duration_seconds=duration,
                model=model,
            )

        raise ClaudeError("All Claude accounts hit usage limits", 1)
