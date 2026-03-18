# claude-rotator

Run Claude CLI (`claude -p`) subprocesses with automatic multi-account rotation and rate limit handling. Zero dependencies.

Available in [Python](https://github.com/jtxmp/claude-rotator), [TypeScript](https://github.com/jtxmp/claude-rotator/tree/master/npm), and [Rust](https://github.com/jtxmp/claude-rotator/tree/master/rust).

## Install

```bash
pip install claude-rotator
```

Requires `claude` CLI installed and authenticated on at least one account.

## Quick Start

```python
from claude_rotator import ClaudeRunner

runner = ClaudeRunner(
    accounts=[
        None,                      # default (~/.claude/)
        "/home/user/.claude_alt",  # fallback account
    ],
)

result = runner.run(prompt="Explain this code", model="sonnet", system_prompt="You are a code reviewer")
print(result.output)
print(f"Cost: ${result.cost_usd:.4f}")
print(f"Duration: {result.duration_seconds:.1f}s")
```

When the first account hits a rate limit, claude-rotator detects it, caches the cooldown, and retries with the next account.

## Async

```python
result = await runner.run_async(prompt="Summarize", model="opus", system_prompt="Be concise")
```

## API

### ClaudeRunner

```python
ClaudeRunner(accounts=[None])
```

- `accounts`: List of HOME directory paths. `None` means the default HOME. Each directory should contain a `.claude/` folder with separate credentials.

### runner.run() / runner.run_async()

```python
runner.run(
    prompt="...",
    model="sonnet",           # "sonnet", "opus", or full model ID
    tools="Read,Write",       # allowed tools (None to omit)
    system_prompt="...",     # optional system prompt
    cwd=Path("."),            # working directory
    timeout=600,              # seconds
)
```

Returns a `ClaudeResult`:

- `output: str` - the response text
- `cost_usd: float` - total cost
- `duration_seconds: float` - wall clock time
- `model: str` - model used

### ClaudeError

Raised on subprocess failure, timeout, or when all accounts are rate-limited.

- `stderr: str` - error output
- `returncode: int` - exit code (-1 for timeout)

## How Account Rotation Works

1. claude-rotator tries the first account in the list.
2. If the output contains rate limit phrases ("usage limit", "rate limit", "out of extra usage"), it caches the cooldown time.
3. It moves to the next account and retries.
4. Cached accounts are skipped on subsequent calls until their cooldown expires.
5. If all accounts are exhausted, it raises `ClaudeError`.

## How Accounts Work

Each account is a directory containing a `.claude/` folder with its own OAuth credentials. Set up multiple accounts by logging in with different HOME directories:

```bash
# Account 1 (default)
claude login

# Account 2
HOME=/home/user/.claude_alt claude login
```

Then pass the directories to `ClaudeRunner`:

```python
runner = ClaudeRunner(accounts=[None, "/home/user/.claude_alt"])
```

## Security

Audit reports are in [`audits/`](audits/). If you find a vulnerability, please open an issue or submit a PR. Independent audits and security contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License

MIT

---

Made with <3 at Bitcoin.com
