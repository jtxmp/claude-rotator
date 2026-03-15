import { spawn, execFileSync } from "node:child_process";
import { platform } from "node:os";
import { ClaudeError } from "./errors.js";
import { RateLimitCache, isUsageLimited } from "./rate-limit.js";

export interface ClaudeResult {
  output: string;
  costUsd: number;
  durationSeconds: number;
  model: string;
}

export interface RunOptions {
  prompt: string;
  model?: string;
  tools?: string | null;
  cwd?: string;
  timeout?: number;
}

export interface ClaudeRunnerOptions {
  accounts?: Array<string | null>;
}

function buildCmd(model: string, tools: string | null | undefined): string[] {
  const cmd = ["claude", "-p", "--model", model, "--output-format", "json"];
  if (tools) {
    cmd.push("--allowedTools", tools);
  }
  return cmd;
}

function buildEnv(homeDir: string | null): NodeJS.ProcessEnv {
  const env = { ...process.env };
  if (homeDir) {
    env.HOME = homeDir;
    if (platform() === "win32") {
      env.USERPROFILE = homeDir;
    }
  }
  return env;
}

function parseOutput(stdout: string): { output: string; costUsd: number } {
  let costUsd = 0;
  let output = stdout;
  try {
    const data = JSON.parse(stdout);
    if (data.is_error) {
      throw new ClaudeError(data.result ?? "Unknown error");
    }
    output = data.result ?? stdout;
    costUsd = data.total_cost_usd ?? 0;
    if (!costUsd && typeof data.cost === "object" && data.cost !== null) {
      costUsd = data.cost.total_usd ?? 0;
    }
  } catch (e) {
    if (e instanceof ClaudeError) throw e;
    // JSON parse failed, return raw stdout
  }
  return { output, costUsd };
}

function killProcessTree(pid: number | undefined): void {
  if (pid === undefined) return;
  if (platform() === "win32") {
    try {
      execFileSync("taskkill", ["/F", "/T", "/PID", String(pid)], {
        stdio: "ignore",
      });
    } catch {
      // process may already be dead
    }
  } else {
    try {
      process.kill(-pid, "SIGTERM");
    } catch {
      // ignore
    }
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ignore
    }
  }
}

export class ClaudeRunner {
  public readonly accounts: Array<string | null>;
  private cache = new RateLimitCache();

  constructor(options?: ClaudeRunnerOptions) {
    this.accounts = options?.accounts ?? [null];
  }

  async run(options: RunOptions): Promise<ClaudeResult> {
    const {
      prompt,
      model = "sonnet",
      tools = "Read,Write",
      cwd,
      timeout = 600,
    } = options;

    const cmd = buildCmd(model, tools);
    const [command, ...args] = cmd;
    const isWin = platform() === "win32";

    for (let i = 0; i < this.accounts.length; i++) {
      const homeDir = this.accounts[i];
      if (this.cache.isLimited(homeDir)) continue;

      const start = performance.now();
      const env = buildEnv(homeDir);

      const result = await new Promise<{
        stdout: string;
        stderr: string;
        exitCode: number | null;
        timedOut: boolean;
      }>((resolve) => {
        const child = spawn(command, args, {
          stdio: ["pipe", "pipe", "pipe"],
          env,
          cwd,
          detached: !isWin,
        });

        let stdout = "";
        let stderr = "";
        let timedOut = false;
        let settled = false;

        const timer = setTimeout(() => {
          timedOut = true;
          killProcessTree(child.pid);
        }, timeout * 1000);

        child.stdout.on("data", (chunk: Buffer) => {
          stdout += chunk.toString();
        });
        child.stderr.on("data", (chunk: Buffer) => {
          stderr += chunk.toString();
        });

        child.on("close", (code) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve({ stdout, stderr, exitCode: code, timedOut });
        });

        child.on("error", (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve({
            stdout,
            stderr: err.message,
            exitCode: 1,
            timedOut: false,
          });
        });

        child.stdin.write(prompt);
        child.stdin.end();
      });

      if (result.timedOut) {
        throw new ClaudeError(`Timeout after ${timeout}s`, -1);
      }

      const durationSeconds = (performance.now() - start) / 1000;

      if (isUsageLimited(result.stdout, result.stderr)) {
        this.cache.record(homeDir, result.stdout, result.stderr);
        if (i < this.accounts.length - 1) continue;
        throw new ClaudeError("All Claude accounts hit usage limits", 1);
      }

      if (result.exitCode !== 0) {
        throw new ClaudeError(result.stderr, result.exitCode ?? 1);
      }

      const { output, costUsd } = parseOutput(result.stdout);
      return { output, costUsd, durationSeconds, model };
    }

    throw new ClaudeError("All Claude accounts hit usage limits", 1);
  }
}
