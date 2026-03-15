# claude-rotator

Run Claude CLI (`claude -p`) subprocesses with automatic multi-account rotation and rate limit handling.

## Install

```toml
[dependencies]
claude-rotator = "1.0"

# For async support:
claude-rotator = { version = "1.0", features = ["async"] }
```

Requires `claude` CLI installed and authenticated on at least one account.

## Quick Start

```rust
use claude_rotator::ClaudeRunner;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut runner = ClaudeRunner::new(vec![
        None,                                    // default (~/.claude/)
        Some("/home/user/.claude_alt".into()),   // fallback
    ]);

    let result = runner.run("Explain this code", "sonnet", None, None, 600)?;
    println!("{}", result.output);
    println!("Cost: ${:.4}", result.cost_usd);
    println!("Duration: {:.1}s", result.duration_seconds);
    Ok(())
}
```

## Async (requires `async` feature)

```rust
use claude_rotator::ClaudeRunner;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut runner = ClaudeRunner::new(vec![None]);
    let result = runner.run_async("Summarize", "opus", None, None, 600).await?;
    println!("{}", result.output);
    Ok(())
}
```

## API

### `ClaudeRunner::new(accounts)`

- `accounts`: `Vec<Option<String>>` of HOME directory paths. `None` means the default HOME.

### `runner.run(prompt, model, tools, cwd, timeout)`

- `prompt`: `&str`
- `model`: `&str` ("sonnet", "opus", or full model ID)
- `tools`: `Option<&str>` (e.g., `Some("Read,Write")`)
- `cwd`: `Option<&Path>`
- `timeout`: `u64` (seconds)

Returns `Result<ClaudeResult, ClaudeError>`.

### `ClaudeResult`

```rust
pub struct ClaudeResult {
    pub output: String,
    pub cost_usd: f64,
    pub duration_seconds: f64,
    pub model: String,
}
```

### `ClaudeError`

```rust
pub struct ClaudeError {
    pub stderr: String,
    pub returncode: i32,
}
```

## License

MIT
