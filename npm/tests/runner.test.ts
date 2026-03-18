import { describe, it, expect, vi, beforeEach } from "vitest";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

vi.mock("node:child_process", () => ({
  spawn: vi.fn(),
  execFileSync: vi.fn(),
}));

vi.mock("node:fs", async (importOriginal) => {
  const actual = (await importOriginal()) as any;
  return {
    ...actual,
    statSync: vi.fn((path: string, opts?: any) => {
      // Allow null account (no homeDir validation) and mock paths
      // For real paths, delegate to the actual implementation
      try {
        return actual.statSync(path, opts);
      } catch {
        if (opts?.throwIfNoEntry === false) return undefined;
        const err: any = new Error(`ENOENT: no such file or directory`);
        err.code = "ENOENT";
        throw err;
      }
    }),
  };
});

import { statSync } from "node:fs";
import { spawn } from "node:child_process";
import { ClaudeRunner } from "../src/runner.js";
import { ClaudeError } from "../src/errors.js";

const mockSpawn = vi.mocked(spawn);
const mockStatSync = vi.mocked(statSync);

function createMockChild(
  stdoutData: string,
  stderrData: string = "",
  exitCode: number = 0,
) {
  const child = new EventEmitter() as any;
  child.pid = 12345;

  const stdinStream = new PassThrough();
  const stdoutStream = new PassThrough();
  const stderrStream = new PassThrough();

  child.stdin = stdinStream;
  child.stdout = stdoutStream;
  child.stderr = stderrStream;

  // Write data and end streams, then emit close on next tick
  process.nextTick(() => {
    stdoutStream.end(stdoutData);
    stderrStream.end(stderrData);
    // Emit close after streams have flushed
    setTimeout(() => {
      child.emit("close", exitCode);
    }, 5);
  });

  return child;
}

/** Returns a factory fn so each spawn call gets a fresh mock child */
function mockSpawnFactory(
  stdoutData: string,
  stderrData: string = "",
  exitCode: number = 0,
) {
  return () => createMockChild(stdoutData, stderrData, exitCode) as any;
}

/** Make statSync treat a path as a valid directory */
function allowPath(path: string) {
  mockStatSync.mockImplementation((p: any, opts?: any) => {
    if (typeof p === "string" && p.includes("fallback")) {
      return { isDirectory: () => true } as any;
    }
    // For non-mocked paths, throw ENOENT
    if (opts?.throwIfNoEntry === false) return undefined;
    const err: any = new Error("ENOENT");
    err.code = "ENOENT";
    throw err;
  });
}

describe("ClaudeRunner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: allow all statSync calls (null accounts don't trigger it)
    mockStatSync.mockImplementation((p: any, opts?: any) => {
      if (typeof p === "string" && p.includes("fallback")) {
        return { isDirectory: () => true } as any;
      }
      if (opts?.throwIfNoEntry === false) return undefined;
      const err: any = new Error("ENOENT");
      err.code = "ENOENT";
      throw err;
    });
  });

  it("returns successful result", async () => {
    const output = JSON.stringify({
      result: "Hello",
      total_cost_usd: 0.01,
    });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    const result = await runner.run({ prompt: "test", model: "sonnet" });

    expect(result.output).toBe("Hello");
    expect(result.costUsd).toBe(0.01);
    expect(result.model).toBe("sonnet");
    expect(result.durationSeconds).toBeGreaterThan(0);
  });

  it("returns raw output when JSON parsing fails", async () => {
    mockSpawn.mockImplementation(mockSpawnFactory("raw text"));

    const runner = new ClaudeRunner({ accounts: [null] });
    const result = await runner.run({ prompt: "test" });

    expect(result.output).toBe("raw text");
    expect(result.costUsd).toBe(0);
  });

  it("throws on is_error response", async () => {
    const output = JSON.stringify({
      is_error: true,
      result: "Something broke",
    });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(runner.run({ prompt: "test" })).rejects.toThrow(
      "Something broke",
    );
  });

  it("throws on nonzero exit code", async () => {
    mockSpawn.mockImplementation(mockSpawnFactory("", "fatal error", 2));

    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(runner.run({ prompt: "test" })).rejects.toThrow(ClaudeError);
  });

  it("rotates accounts on rate limit", async () => {
    const successOutput = JSON.stringify({
      result: "OK",
      total_cost_usd: 0.02,
    });
    let callCount = 0;
    mockSpawn.mockImplementation(() => {
      callCount++;
      if (callCount === 1) {
        // Rate limit phrase in stderr
        return createMockChild("", "You hit the usage limit") as any;
      }
      return createMockChild(successOutput) as any;
    });

    const runner = new ClaudeRunner({
      accounts: [null, "/fallback"],
    });
    const result = await runner.run({ prompt: "test" });
    expect(result.output).toBe("OK");
  });

  it("throws when all accounts exhausted", async () => {
    // Rate limit phrase in stderr
    mockSpawn.mockImplementation(
      mockSpawnFactory("", "usage limit reached"),
    );

    const runner = new ClaudeRunner({
      accounts: [null, "/fallback"],
    });
    await expect(runner.run({ prompt: "test" })).rejects.toThrow(
      "All Claude accounts",
    );
  });

  it("skips cached rate-limited accounts", async () => {
    const successOutput = JSON.stringify({
      result: "OK",
      total_cost_usd: 0,
    });
    mockSpawn.mockImplementation(mockSpawnFactory(successOutput));

    const runner = new ClaudeRunner({
      accounts: [null, "/fallback"],
    });

    // Pre-cache first account as rate-limited
    (runner as any).cache.record(null, "resets 8pm (UTC)", "");

    const result = await runner.run({ prompt: "test" });
    expect(mockSpawn).toHaveBeenCalledTimes(1);
    expect(result.output).toBe("OK");
  });

  it("reads cost from nested format", async () => {
    const output = JSON.stringify({
      result: "Hi",
      cost: { total_usd: 0.05 },
    });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    const result = await runner.run({ prompt: "test" });
    expect(result.costUsd).toBe(0.05);
  });

  it("omits --allowedTools when tools is null", async () => {
    const output = JSON.stringify({ result: "OK" });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    await runner.run({ prompt: "test", tools: null });

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).not.toContain("--allowedTools");
  });

  it("defaults tools to null (no tools restriction)", async () => {
    const output = JSON.stringify({ result: "OK" });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    await runner.run({ prompt: "test" });

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).not.toContain("--allowedTools");
  });

  it("defaults accounts to [null]", () => {
    const runner = new ClaudeRunner();
    expect(runner.accounts).toEqual([null]);
  });

  it("includes --system-prompt when systemPrompt is provided", async () => {
    const output = JSON.stringify({ result: "OK" });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    await runner.run({ prompt: "test", systemPrompt: "Be concise" });

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).toContain("--system-prompt");
    const idx = args.indexOf("--system-prompt");
    expect(args[idx + 1]).toBe("Be concise");
  });

  it("omits --system-prompt when systemPrompt is undefined", async () => {
    const output = JSON.stringify({ result: "OK" });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner({ accounts: [null] });
    await runner.run({ prompt: "test" });

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).not.toContain("--system-prompt");
  });

  it("defaults model to sonnet", async () => {
    const output = JSON.stringify({ result: "OK" });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner();
    const result = await runner.run({ prompt: "test" });
    expect(result.model).toBe("sonnet");
  });

  it("rejects invalid model with spaces", async () => {
    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(
      runner.run({ prompt: "test", model: "sonnet --flag" }),
    ).rejects.toThrow("Invalid model");
  });

  it("rejects invalid tools with flags", async () => {
    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(
      runner.run({ prompt: "test", tools: "Read --inject" }),
    ).rejects.toThrow("Invalid tools");
  });

  it("rejects nonexistent cwd", async () => {
    mockStatSync.mockImplementation((p: any, opts?: any) => {
      const err: any = new Error("ENOENT");
      err.code = "ENOENT";
      throw err;
    });

    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(
      runner.run({ prompt: "test", cwd: "/nonexistent/path/abc123" }),
    ).rejects.toThrow(/does not exist/);
  });

  it("rejects zero timeout", async () => {
    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(
      runner.run({ prompt: "test", timeout: 0 }),
    ).rejects.toThrow("Timeout must be between");
  });

  it("rejects timeout over 3600", async () => {
    const runner = new ClaudeRunner({ accounts: [null] });
    await expect(
      runner.run({ prompt: "test", timeout: 3601 }),
    ).rejects.toThrow("Timeout must be between");
  });
});

describe("ClaudeError sanitization", () => {
  it("redacts API keys in error messages", () => {
    const error = new ClaudeError("sk-ant-api-key-12345 failed", 1);
    expect(error.message).not.toContain("sk-ant-");
    expect(error.message).toContain("[REDACTED]");
  });

  it("preserves raw stderr on the error object", () => {
    const error = new ClaudeError("sk-ant-api-key-12345 failed", 1);
    expect(error.stderr).toBe("sk-ant-api-key-12345 failed");
  });

  it("redacts Bearer tokens", () => {
    const error = new ClaudeError("Bearer eyJhbGciOiJIUz", 1);
    expect(error.message).not.toContain("eyJhbGciOiJIUz");
    expect(error.message).toContain("[REDACTED]");
  });

  it("redacts tokens that straddle the 500-char boundary", () => {
    // Token starts near position 500. If we truncated first, the partial
    // token would leak. Redacting first ensures the secret is removed.
    const padding = "x".repeat(490);
    const stderr = `${padding}sk-ant-secret-token-12345 more text`;
    const error = new ClaudeError(stderr, 1);
    expect(error.message).not.toContain("sk-ant-");
    expect(error.message).not.toContain("secret-token");
  });
});
