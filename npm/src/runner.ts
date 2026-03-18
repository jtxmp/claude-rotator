import { spawn, execFileSync } from "node:child_process";
import { statSync } from "node:fs";
import { platform } from "node:os";
import { resolve } from "node:path";
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
  systemPrompt?: string;
  cwd?: string;
  timeout?: number;
}

export interface ClaudeRunnerOptions {
  accounts?: Array<string | null>;
}

const MAX_OUTPUT_BYTES = 50 * 1024 * 1024; // 50 MB
const MIN_TIMEOUT = 1;
const MAX_TIMEOUT = 3600;

const VALID_MODEL_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;
const VALID_TOOLS_PATTERN = /^[A-Za-z_]+(,[A-Za-z_]+)*$/;

function validateInputs(
  model: string,
  tools: string | null | undefined,
): void {
  if (!VALID_MODEL_PATTERN.test(model)) {
    throw new Error(`Invalid model identifier: ${model}`);
  }
  if (tools && !VALID_TOOLS_PATTERN.test(tools)) {
    throw new Error(`Invalid tools specification: ${tools}`);
  }
}

function buildCmd(model: string, tools: string | null | undefined, systemPrompt?: string): string[] {
  validateInputs(model, tools);
  const cmd = ["claude", "-p", "--model", model, "--output-format", "json"];
  if (systemPrompt !== undefined) {
    cmd.push("--system-prompt", systemPrompt);
  }
  if (tools) {
    cmd.push("--allowedTools", tools);
  }
  return cmd;
}

function buildEnv(homeDir: string | null): NodeJS.ProcessEnv {
  const env = { ...process.env };
  if (homeDir) {
    const resolved = resolve(homeDir);
    try {
      if (!statSync(resolved).isDirectory()) {
        throw new Error(`Account homeDir is not a directory: ${resolved}`);
      }
    } catch (e: any) {
      if (e.code === "ENOENT")
        throw new Error(`Account homeDir does not exist: ${resolved}`);
      throw e;
    }
    env.HOME = resolved;
    if (platform() === "win32") {
      env.USERPROFILE = resolved;
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
      tools = null,
      systemPrompt,
      cwd,
      timeout = 600,
    } = options;

    if (timeout < MIN_TIMEOUT || timeout > MAX_TIMEOUT) {
      throw new Error(
        `Timeout must be between ${MIN_TIMEOUT} and ${MAX_TIMEOUT} seconds`,
      );
    }

    const cmd = buildCmd(model, tools, systemPrompt);
    const [command, ...args] = cmd;
    const isWin = platform() === "win32";

    let resolvedCwd: string | undefined;
    if (cwd) {
      resolvedCwd = resolve(cwd);
      try {
        if (!statSync(resolvedCwd).isDirectory()) {
          throw new Error(`cwd is not a directory: ${resolvedCwd}`);
        }
      } catch (e: any) {
        if (e.code === "ENOENT")
          throw new Error(`cwd does not exist: ${resolvedCwd}`);
        throw e;
      }
    }

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
        outputExceeded: boolean;
      }>((resolvePromise) => {
        const child = spawn(command, args, {
          stdio: ["pipe", "pipe", "pipe"],
          env,
          cwd: resolvedCwd,
          detached: !isWin,
        });

        let stdout = "";
        let stderr = "";
        let stdoutLen = 0;
        let stderrLen = 0;
        let timedOut = false;
        let outputExceeded = false;
        let settled = false;

        const timer = setTimeout(() => {
          timedOut = true;
          killProcessTree(child.pid);
        }, timeout * 1000);

        child.stdout.on("data", (chunk: Buffer) => {
          stdoutLen += chunk.length;
          if (stdoutLen > MAX_OUTPUT_BYTES) {
            outputExceeded = true;
            killProcessTree(child.pid);
            return;
          }
          stdout += chunk.toString();
        });
        child.stderr.on("data", (chunk: Buffer) => {
          stderrLen += chunk.length;
          if (stderrLen > MAX_OUTPUT_BYTES) {
            outputExceeded = true;
            killProcessTree(child.pid);
            return;
          }
          stderr += chunk.toString();
        });

        child.on("close", (code) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolvePromise({
            stdout,
            stderr,
            exitCode: code,
            timedOut,
            outputExceeded,
          });
        });

        child.on("error", (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolvePromise({
            stdout,
            stderr: err.message,
            exitCode: 1,
            timedOut: false,
            outputExceeded: false,
          });
        });

        child.stdin.write(prompt);
        child.stdin.end();
      });

      if (result.timedOut) {
        throw new ClaudeError(`Timeout after ${timeout}s`, -1);
      }

      if (result.outputExceeded) {
        throw new ClaudeError(
          "Subprocess output exceeded maximum allowed size",
          -1,
        );
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
