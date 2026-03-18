# claude-rotator

Run Claude CLI (`claude -p`) subprocesses with automatic multi-account rotation and rate limit handling. Zero dependencies.

## Install

```bash
pnpm add claude-rotator
```

Requires `claude` CLI installed and authenticated on at least one account.

## Quick Start

```typescript
import { ClaudeRunner } from "claude-rotator";

const runner = new ClaudeRunner({
  accounts: [
    null, // default (~/.claude/)
    "/home/user/.claude_alt", // fallback account
  ],
});

const result = await runner.run({
  prompt: "Explain this code",
  model: "sonnet",
  systemPrompt: "You are a code reviewer",
});

console.log(result.output);
console.log(`Cost: $${result.costUsd.toFixed(4)}`);
console.log(`Duration: ${result.durationSeconds.toFixed(1)}s`);
```

## API

### `new ClaudeRunner(options?)`

- `accounts`: Array of HOME directory paths. `null` means the default HOME.

### `runner.run(options): Promise<ClaudeResult>`

- `prompt`: The prompt text (piped via stdin)
- `model`: `"sonnet"`, `"opus"`, or a full model ID (default: `"sonnet"`)
- `systemPrompt`: Optional system prompt for Claude
- `tools`: Allowed tools string, or `null` to omit (default: `"Read,Write"`)
- `cwd`: Working directory (default: current)
- `timeout`: Seconds before killing the process (default: `600`)

### `ClaudeResult`

- `output: string`
- `costUsd: number`
- `durationSeconds: number`
- `model: string`

### `ClaudeError`

Thrown on subprocess failure, timeout, or when all accounts are rate-limited.

- `stderr: string`
- `returncode: number` (-1 for timeout)

## License

MIT
