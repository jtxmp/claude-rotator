"""Tests for ClaudeRunner sync and async execution."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_rotator import ClaudeError, ClaudeResult, ClaudeRunner
from claude_rotator.runner import (
    MAX_OUTPUT_BYTES,
    MIN_TIMEOUT,
    MAX_TIMEOUT,
    _build_cmd,
    _build_env,
    _kill_process_tree,
    _validate_inputs,
    _validate_timeout,
)


def _make_popen(stdout: str, stderr: str = "", returncode: int = 0):
    """Create a mock Popen with readable streams for threaded I/O."""
    proc = MagicMock()
    proc.stdout = StringIO(stdout)
    proc.stderr = StringIO(stderr)
    proc.stdin = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.wait.return_value = returncode
    return proc


def _popen_factory(stdout: str, stderr: str = "", returncode: int = 0):
    """Return a callable that creates a fresh mock Popen each time."""
    def factory(*args, **kwargs):
        return _make_popen(stdout, stderr, returncode)
    return factory


def _make_async_proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0, pid: int = 99999):
    """Create a mock async subprocess with readable streams."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode

    # stdin mock with async drain
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    proc.stdin = stdin

    # stdout mock: async read returns data once, then b""
    stdout_stream = MagicMock()
    stdout_chunks = iter([stdout, b""])
    stdout_stream.read = AsyncMock(side_effect=lambda n: next(stdout_chunks, b""))
    proc.stdout = stdout_stream

    # stderr mock: async read returns data once, then b""
    stderr_stream = MagicMock()
    stderr_chunks = iter([stderr, b""])
    stderr_stream.read = AsyncMock(side_effect=lambda n: next(stderr_chunks, b""))
    proc.stderr = stderr_stream

    proc.wait = AsyncMock(return_value=None)
    # Set returncode after wait
    type(proc).returncode = returncode
    return proc


class TestValidateInputs:
    def test_valid_model_short_alias(self):
        _validate_inputs("sonnet", None)

    def test_valid_model_full_id(self):
        _validate_inputs("claude-sonnet-4-5-20250514", None)

    def test_valid_model_with_dots(self):
        _validate_inputs("claude.sonnet.4.5", None)

    def test_invalid_model_with_spaces(self):
        with pytest.raises(ValueError, match="Invalid model"):
            _validate_inputs("sonnet --dangerous-flag", None)

    def test_invalid_model_with_double_dash(self):
        # Double dashes within an alphanumeric string are fine (e.g. claude-sonnet-4-5)
        # but standalone flags should be caught by the whitespace check
        with pytest.raises(ValueError, match="Invalid model"):
            _validate_inputs("sonnet --flag", None)

    def test_invalid_model_empty(self):
        with pytest.raises(ValueError, match="Invalid model"):
            _validate_inputs("", None)

    def test_invalid_model_starts_with_dash(self):
        with pytest.raises(ValueError, match="Invalid model"):
            _validate_inputs("-sonnet", None)

    def test_valid_tools(self):
        _validate_inputs("sonnet", "Read,Write")

    def test_valid_tools_single(self):
        _validate_inputs("sonnet", "Read")

    def test_valid_tools_with_underscores(self):
        _validate_inputs("sonnet", "Read_File,Write_File")

    def test_invalid_tools_with_spaces(self):
        with pytest.raises(ValueError, match="Invalid tools"):
            _validate_inputs("sonnet", "Read, Write")

    def test_invalid_tools_with_flags(self):
        with pytest.raises(ValueError, match="Invalid tools"):
            _validate_inputs("sonnet", "Read --inject")

    def test_invalid_tools_with_numbers(self):
        with pytest.raises(ValueError, match="Invalid tools"):
            _validate_inputs("sonnet", "Read123")

    def test_tools_none_is_valid(self):
        _validate_inputs("sonnet", None)


class TestBuildCmd:
    def test_rejects_invalid_model(self):
        with pytest.raises(ValueError):
            _build_cmd("sonnet --flag", None)

    def test_rejects_invalid_tools(self):
        with pytest.raises(ValueError):
            _build_cmd("sonnet", "Read --inject")



    def test_with_system_prompt(self):
        cmd = _build_cmd("sonnet", None, system_prompt="You are helpful")
        assert "--system-prompt" in cmd
        idx = cmd.index("--system-prompt")
        assert cmd[idx + 1] == "You are helpful"

    def test_without_system_prompt(self):
        cmd = _build_cmd("sonnet", None)
        assert "--system-prompt" not in cmd

    def test_system_prompt_none_omits_flag(self):
        cmd = _build_cmd("sonnet", "Read", None)
        assert "--system-prompt" not in cmd

    def test_rejects_system_prompt_starting_with_dash(self):
        with pytest.raises(ValueError, match="must not start with"):
            _build_cmd("sonnet", None, system_prompt="--inject")
        with pytest.raises(ValueError, match="must not start with"):
            _build_cmd("sonnet", None, system_prompt="-flag")

class TestValidateTimeout:
    def test_valid_timeout(self):
        _validate_timeout(600)

    def test_min_timeout(self):
        _validate_timeout(MIN_TIMEOUT)

    def test_max_timeout(self):
        _validate_timeout(MAX_TIMEOUT)

    def test_zero_timeout_raises(self):
        with pytest.raises(ValueError, match="Timeout must be between"):
            _validate_timeout(0)

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="Timeout must be between"):
            _validate_timeout(-1)

    def test_too_large_timeout_raises(self):
        with pytest.raises(ValueError, match="Timeout must be between"):
            _validate_timeout(MAX_TIMEOUT + 1)


class TestClaudeRunnerSync:
    def test_successful_run(self):
        output = json.dumps({"result": "Hello", "total_cost_usd": 0.01})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)):
            result = runner.run(prompt="test", model="sonnet")
        assert result.output == "Hello"
        assert result.cost_usd == 0.01
        assert result.model == "sonnet"
        assert result.duration_seconds > 0

    def test_non_json_output_returned_raw(self):
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen("raw text")):
            result = runner.run(prompt="test")
        assert result.output == "raw text"
        assert result.cost_usd == 0.0

    def test_is_error_raises(self):
        output = json.dumps({"is_error": True, "result": "Something broke"})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)):
            with pytest.raises(ClaudeError, match="Something broke"):
                runner.run(prompt="test")

    def test_nonzero_exit_raises(self):
        runner = ClaudeRunner(accounts=[None])
        proc = _make_popen("", stderr="fatal error", returncode=2)
        with patch("claude_rotator.runner.subprocess.Popen", return_value=proc):
            with pytest.raises(ClaudeError) as exc_info:
                runner.run(prompt="test")
        assert exc_info.value.returncode == 2

    def test_account_rotation_on_rate_limit(self, tmp_path):
        # Rate limit phrase must be in stderr now
        rate_limited = _make_popen("", stderr="You hit the usage limit", returncode=0)
        success_output = json.dumps({"result": "OK", "total_cost_usd": 0.02})
        success = _make_popen(success_output, returncode=0)

        fallback = str(tmp_path / "fallback")
        os.makedirs(fallback)
        runner = ClaudeRunner(accounts=[None, fallback])
        with patch("claude_rotator.runner.subprocess.Popen", side_effect=[rate_limited, success]):
            result = runner.run(prompt="test")
        assert result.output == "OK"

    def test_all_accounts_exhausted_raises(self, tmp_path):
        fallback = str(tmp_path / "fallback")
        os.makedirs(fallback)
        runner = ClaudeRunner(accounts=[None, fallback])
        # Use side_effect factory so each Popen call gets fresh StringIO streams
        with patch(
            "claude_rotator.runner.subprocess.Popen",
            side_effect=_popen_factory("", stderr="usage limit reached", returncode=0),
        ):
            with pytest.raises(ClaudeError, match="All Claude accounts"):
                runner.run(prompt="test")

    def test_skips_cached_rate_limited_accounts(self, tmp_path):
        """When an account is cached as rate-limited, it should be skipped."""
        success_output = json.dumps({"result": "OK", "total_cost_usd": 0.0})
        success = _make_popen(success_output)
        fallback = str(tmp_path / "fallback")
        os.makedirs(fallback)
        runner = ClaudeRunner(accounts=[None, fallback])

        # Pre-cache the first account as rate-limited
        from datetime import datetime, timedelta, timezone

        runner._cache._until[None] = datetime.now(timezone.utc) + timedelta(hours=1)

        with patch("claude_rotator.runner.subprocess.Popen", return_value=success) as mock_popen:
            result = runner.run(prompt="test")

        # Should have been called only once (skipping the first account)
        assert mock_popen.call_count == 1
        assert result.output == "OK"

    def test_timeout_raises(self):
        """Threads that block past the deadline trigger a timeout error."""
        proc = MagicMock()
        proc.pid = 12345
        proc.wait.return_value = None
        proc.stdin = MagicMock()

        # Simulate a stream that blocks longer than the timeout
        import time as _time

        def blocking_read(_n=None):
            _time.sleep(3)
            return ""

        stdout_stream = MagicMock()
        stdout_stream.read = blocking_read
        stderr_stream = MagicMock()
        stderr_stream.read = blocking_read
        proc.stdout = stdout_stream
        proc.stderr = stderr_stream

        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=proc):
            with patch("claude_rotator.runner._kill_process_tree"):
                with pytest.raises(ClaudeError, match="Timeout"):
                    runner.run(prompt="test", timeout=1)

    def test_cost_from_nested_format(self):
        output = json.dumps({"result": "Hi", "cost": {"total_usd": 0.05}})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)):
            result = runner.run(prompt="test")
        assert result.cost_usd == 0.05

    def test_tools_none_omits_flag(self):
        output = json.dumps({"result": "OK"})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)) as mock_popen:
            runner.run(prompt="test", tools=None)
        cmd = mock_popen.call_args[0][0]
        assert "--allowedTools" not in cmd

    def test_default_tools_is_none(self):
        """Default tools should be None (no tools restriction), not Read,Write."""
        output = json.dumps({"result": "OK"})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)) as mock_popen:
            runner.run(prompt="test")
        cmd = mock_popen.call_args[0][0]
        assert "--allowedTools" not in cmd

    def test_default_accounts_is_none(self):
        runner = ClaudeRunner()
        assert runner.accounts == [None]

    def test_invalid_cwd_raises(self):
        runner = ClaudeRunner(accounts=[None])
        with pytest.raises(ValueError, match="cwd is not an existing directory"):
            runner.run(prompt="test", cwd="/nonexistent/path/abc123")

    def test_output_size_limit(self):
        huge_stdout = "x" * (MAX_OUTPUT_BYTES + 1)
        runner = ClaudeRunner(accounts=[None])
        proc = _make_popen(huge_stdout)
        with patch("claude_rotator.runner.subprocess.Popen", return_value=proc):
            with patch("claude_rotator.runner._kill_process_tree"):
                with pytest.raises(ClaudeError, match="exceeded maximum"):
                    runner.run(prompt="test")

    def test_timeout_validation_rejects_zero(self):
        runner = ClaudeRunner(accounts=[None])
        with pytest.raises(ValueError, match="Timeout must be between"):
            runner.run(prompt="test", timeout=0)

    def test_timeout_validation_rejects_too_large(self):
        runner = ClaudeRunner(accounts=[None])
        with pytest.raises(ValueError, match="Timeout must be between"):
            runner.run(prompt="test", timeout=MAX_TIMEOUT + 1)



    def test_system_prompt_passed_to_subprocess(self):
        output = json.dumps({"result": "OK"})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)) as mock_popen:
            runner.run(prompt="test", system_prompt="Be concise")
        cmd = mock_popen.call_args[0][0]
        assert "--system-prompt" in cmd
        idx = cmd.index("--system-prompt")
        assert cmd[idx + 1] == "Be concise"

    def test_no_system_prompt_omits_flag(self):
        output = json.dumps({"result": "OK"})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)) as mock_popen:
            runner.run(prompt="test")
        cmd = mock_popen.call_args[0][0]
        assert "--system-prompt" not in cmd


class TestClaudeRunnerAsync:
    def test_successful_async_run(self):
        output = json.dumps({"result": "Async hello", "total_cost_usd": 0.03})

        async def _run():
            runner = ClaudeRunner(accounts=[None])
            proc = _make_async_proc(output.encode(), b"", 0)

            with patch("claude_rotator.runner.asyncio.create_subprocess_exec", return_value=proc):
                return await runner.run_async(prompt="test", model="opus")

        result = asyncio.run(_run())
        assert result.output == "Async hello"
        assert result.cost_usd == 0.03
        assert result.model == "opus"

    def test_async_rate_limit_rotation(self, tmp_path):
        success_output = json.dumps({"result": "OK", "total_cost_usd": 0.0})
        fallback = str(tmp_path / "fallback")
        os.makedirs(fallback)

        async def _run():
            runner = ClaudeRunner(accounts=[None, fallback])

            # Rate limit phrase in stderr
            proc1 = _make_async_proc(b"", b"You hit the usage limit", 0, pid=11111)
            proc2 = _make_async_proc(success_output.encode(), b"", 0, pid=22222)

            with patch(
                "claude_rotator.runner.asyncio.create_subprocess_exec",
                side_effect=[proc1, proc2],
            ):
                return await runner.run_async(prompt="test")

        result = asyncio.run(_run())
        assert result.output == "OK"

    def test_async_invalid_cwd_raises(self):
        async def _run():
            runner = ClaudeRunner(accounts=[None])
            return await runner.run_async(prompt="test", cwd="/nonexistent/path/abc123")

        with pytest.raises(ValueError, match="cwd is not an existing directory"):
            asyncio.run(_run())



    def test_async_system_prompt_passed(self):
        output = json.dumps({"result": "OK", "total_cost_usd": 0.0})
        async def _run():
            runner = ClaudeRunner(accounts=[None])
            proc = _make_async_proc(output.encode(), b"", 0)
            with patch("claude_rotator.runner.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
                await runner.run_async(prompt="test", system_prompt="Be concise")
            # The command parts are passed as positional args to create_subprocess_exec
            call_args = mock_exec.call_args[0]
            assert "--system-prompt" in call_args
            idx = list(call_args).index("--system-prompt")
            assert call_args[idx + 1] == "Be concise"
        asyncio.run(_run())


class TestBuildEnv:
    def test_no_home_dir_returns_current_env(self):
        env = _build_env(None)
        assert env == {**os.environ}

    def test_sets_home_on_unix(self, tmp_path):
        env = _build_env(str(tmp_path))
        assert env["HOME"] == str(tmp_path.resolve())

    @patch("claude_rotator.runner.sys")
    def test_sets_userprofile_on_windows(self, mock_sys, tmp_path):
        mock_sys.platform = "win32"
        env = _build_env(str(tmp_path))
        resolved = str(tmp_path.resolve())
        assert env["HOME"] == resolved
        assert env["USERPROFILE"] == resolved

    def test_nonexistent_home_dir_raises(self):
        with pytest.raises(ValueError, match="not an existing directory"):
            _build_env("/nonexistent/path/abc123")


class TestKillProcessTree:
    @patch("claude_rotator.runner.sys")
    @patch("claude_rotator.runner.os")
    def test_unix_sends_sigterm_then_sigkill(self, mock_os, mock_sys):
        mock_sys.platform = "linux"
        mock_os.getpgid.return_value = 100
        _kill_process_tree(12345)
        mock_os.killpg.assert_called_once_with(100, 15)  # SIGTERM
        mock_os.kill.assert_called_once_with(12345, 9)  # SIGKILL

    @patch("claude_rotator.runner.sys")
    @patch("claude_rotator.runner.subprocess")
    def test_windows_uses_taskkill(self, mock_subprocess, mock_sys):
        mock_sys.platform = "win32"
        _kill_process_tree(12345)
        mock_subprocess.run.assert_called_once()
        call_args = mock_subprocess.run.call_args[0][0]
        assert call_args == ["taskkill", "/F", "/T", "/PID", "12345"]

    def test_none_pid_is_noop(self):
        _kill_process_tree(None)

    @patch("claude_rotator.runner.sys")
    @patch("claude_rotator.runner.os")
    def test_unix_handles_process_already_dead(self, mock_os, mock_sys):
        mock_sys.platform = "linux"
        mock_os.getpgid.side_effect = ProcessLookupError
        mock_os.kill.side_effect = ProcessLookupError
        _kill_process_tree(12345)  # should not raise


class TestPlatformPopen:
    def test_sync_uses_start_new_session_on_unix(self):
        output = json.dumps({"result": "OK"})
        runner = ClaudeRunner(accounts=[None])
        with patch("claude_rotator.runner.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch("claude_rotator.runner.subprocess.Popen", return_value=_make_popen(output)) as mock_popen:
                runner.run(prompt="test")
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs

    def test_sync_uses_creationflags_on_windows(self):
        output = json.dumps({"result": "OK"})
        runner = ClaudeRunner(accounts=[None])
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        with patch("claude_rotator.runner.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("claude_rotator.runner.subprocess") as mock_sp:
                mock_sp.Popen.return_value = _make_popen(output)
                mock_sp.PIPE = subprocess.PIPE
                mock_sp.CREATE_NEW_PROCESS_GROUP = CREATE_NEW_PROCESS_GROUP
                runner.run(prompt="test")
        kwargs = mock_sp.Popen.call_args[1]
        assert kwargs.get("creationflags") == CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in kwargs
