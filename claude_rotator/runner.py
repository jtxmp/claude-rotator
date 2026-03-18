"""ClaudeRunner: sync and async Claude CLI subprocess management with account rotation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ClaudeError
from .rate_limit import RateLimitCache, is_usage_limited

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 50 * 1024 * 1024  # 50 MB
MIN_TIMEOUT = 1
MAX_TIMEOUT = 3600

VALID_MODEL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
VALID_TOOLS_PATTERN = re.compile(r"^[A-Za-z_]+(,[A-Za-z_]+)*$")


def _validate_inputs(model: str, tools: str | None) -> None:
    """Validate model and tools arguments against injection attacks."""
    if not VALID_MODEL_PATTERN.match(model):
        raise ValueError(f"Invalid model identifier: {model!r}")
    if tools is not None and not VALID_TOOLS_PATTERN.match(tools):
        raise ValueError(f"Invalid tools specification: {tools!r}")


def _validate_timeout(timeout: int) -> None:
    """Validate timeout is within acceptable bounds."""
    if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise ValueError(
            f"Timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds"
        )


@dataclass
class ClaudeResult:
    """Result from a Claude CLI invocation."""

    output: str
    cost_usd: float
    duration_seconds: float
    model: str


def _build_cmd(model: str, allowed_tools: str | None, system_prompt: str | None = None) -> list[str]:
    _validate_inputs(model, allowed_tools)
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
    ]
    if system_prompt is not None and system_prompt.startswith("-"):
        raise ValueError(f"system_prompt must not start with '-': {system_prompt!r}")
    if system_prompt is not None:
        cmd.extend(["--system-prompt", system_prompt])
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    return cmd


def _build_env(home_dir: str | None) -> dict[str, str]:
    env = {**os.environ}
    if home_dir:
        resolved = Path(home_dir).resolve()
        if not resolved.is_dir():
            raise ValueError(f"Account home_dir is not an existing directory: {resolved}")
        home_str = str(resolved)
        env["HOME"] = home_str
        # Windows uses USERPROFILE instead of HOME for the user directory.
        if sys.platform == "win32":
            env["USERPROFILE"] = home_str
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


def _drain_text_stream(
    stream, max_size: int, pid: int, exceeded_flag: list[bool]
) -> str:
    """Read a text stream with a size limit.

    If the stream exceeds max_size characters, sets exceeded_flag[0] = True
    and kills the process tree. Returns the data read so far.
    """
    chunks: list[str] = []
    total = 0
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            exceeded_flag[0] = True
            _kill_process_tree(pid)
            return "".join(chunks)
        chunks.append(chunk)
    return "".join(chunks)


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
        tools: str | None = None,
        system_prompt: str | None = None,
        cwd: Path | str | None = None,
        timeout: int = 600,
    ) -> ClaudeResult:
        """Run ``claude -p`` synchronously.

        Pipes the prompt via stdin. On usage limit errors, retries with
        the next account in the list.
        """
        _validate_timeout(timeout)
        cmd = _build_cmd(model, tools, system_prompt)
        if cwd is not None:
            resolved_cwd = Path(cwd).resolve()
            if not resolved_cwd.is_dir():
                raise ValueError(f"cwd is not an existing directory: {resolved_cwd}")
            cwd_str = str(resolved_cwd)
        else:
            cwd_str = None
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
                logger.debug("Skipping rate-limited account %r", home_dir or "default")
                continue

            start = time.time()
            popen_kwargs["env"] = _build_env(home_dir)

            proc = subprocess.Popen(cmd, **popen_kwargs)

            # Write prompt and close stdin
            try:
                if proc.stdin:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
            except OSError:
                pass

            # Read stdout/stderr concurrently with size limits
            exceeded = [False]
            stdout_result: list[str | None] = [None]
            stderr_result: list[str | None] = [None]

            def _read_stdout():
                stdout_result[0] = _drain_text_stream(
                    proc.stdout, MAX_OUTPUT_BYTES, proc.pid, exceeded
                )

            def _read_stderr():
                stderr_result[0] = _drain_text_stream(
                    proc.stderr, MAX_OUTPUT_BYTES, proc.pid, exceeded
                )

            t_out = threading.Thread(target=_read_stdout, daemon=True)
            t_err = threading.Thread(target=_read_stderr, daemon=True)
            t_out.start()
            t_err.start()

            remaining = timeout - (time.time() - start)
            t_out.join(timeout=max(0, remaining))
            remaining = timeout - (time.time() - start)
            t_err.join(timeout=max(0, remaining))

            if t_out.is_alive() or t_err.is_alive():
                _kill_process_tree(proc.pid)
                proc.wait()
                raise ClaudeError(f"Timeout after {timeout}s", -1)

            proc.wait()

            if exceeded[0]:
                raise ClaudeError("Subprocess output exceeded maximum allowed size", -1)

            stdout = stdout_result[0] or ""
            stderr = stderr_result[0] or ""
            duration = time.time() - start

            if is_usage_limited(stdout, stderr):
                self._cache.record(home_dir, stdout, stderr)
                if i < len(self.accounts) - 1:
                    next_acct = self.accounts[i + 1] or "default"
                    logger.warning(
                        "Account %r hit usage limit, trying %r",
                        home_dir or "default",
                        next_acct,
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
        tools: str | None = None,
        system_prompt: str | None = None,
        cwd: Path | str | None = None,
        timeout: int = 600,
    ) -> ClaudeResult:
        """Run ``claude -p`` asynchronously.

        Pipes the prompt via stdin. On usage limit errors, retries with
        the next account in the list.
        """
        _validate_timeout(timeout)
        cmd = _build_cmd(model, tools, system_prompt)
        if cwd is not None:
            resolved_cwd = Path(cwd).resolve()
            if not resolved_cwd.is_dir():
                raise ValueError(f"cwd is not an existing directory: {resolved_cwd}")
            cwd_str = str(resolved_cwd)
        else:
            cwd_str = None
        is_win = sys.platform == "win32"

        for i, home_dir in enumerate(self.accounts):
            if self._cache.is_limited(home_dir):
                logger.debug("Skipping rate-limited account %r", home_dir or "default")
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

            pid = proc.pid

            async def _drain_async(stream, max_bytes: int):
                """Read an async stream with a size limit."""
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        _kill_process_tree(pid)
                        raise ClaudeError(
                            "Subprocess output exceeded maximum allowed size", -1
                        )
                    chunks.append(chunk)
                return b"".join(chunks)

            async def _communicate():
                if proc.stdin:
                    proc.stdin.write(prompt.encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                try:
                    stdout_bytes, stderr_bytes = await asyncio.gather(
                        _drain_async(proc.stdout, MAX_OUTPUT_BYTES),
                        _drain_async(proc.stderr, MAX_OUTPUT_BYTES),
                    )
                except ClaudeError:
                    await proc.wait()
                    raise
                await proc.wait()
                return stdout_bytes.decode(), stderr_bytes.decode()

            try:
                stdout, stderr = await asyncio.wait_for(
                    _communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                _kill_process_tree(pid)
                await proc.wait()
                raise ClaudeError("Timeout exceeded", -1)

            duration = time.time() - start

            if is_usage_limited(stdout, stderr):
                self._cache.record(home_dir, stdout, stderr)
                if i < len(self.accounts) - 1:
                    next_acct = self.accounts[i + 1] or "default"
                    logger.warning(
                        "Account %r hit usage limit, trying %r",
                        home_dir or "default",
                        next_acct,
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
