import { describe, it, expect, vi, beforeEach } from "vitest";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

vi.mock("node:child_process", () => ({
  spawn: vi.fn(),
  execFileSync: vi.fn(),
}));

import { spawn } from "node:child_process";
import { ClaudeRunner } from "../src/runner.js";
import { ClaudeError } from "../src/errors.js";

const mockSpawn = vi.mocked(spawn);

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

describe("ClaudeRunner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    const successOutput = JSON.stringify({ result: "OK", total_cost_usd: 0.02 });
    let callCount = 0;
    mockSpawn.mockImplementation(() => {
      callCount++;
      if (callCount === 1) {
        return createMockChild("You hit the usage limit") as any;
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
    mockSpawn.mockImplementation(mockSpawnFactory("usage limit reached"));

    const runner = new ClaudeRunner({
      accounts: [null, "/fallback"],
    });
    await expect(runner.run({ prompt: "test" })).rejects.toThrow(
      "All Claude accounts",
    );
  });

  it("skips cached rate-limited accounts", async () => {
    const successOutput = JSON.stringify({ result: "OK", total_cost_usd: 0 });
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

  it("defaults accounts to [null]", () => {
    const runner = new ClaudeRunner();
    expect(runner.accounts).toEqual([null]);
  });

  it("defaults model to sonnet", async () => {
    const output = JSON.stringify({ result: "OK" });
    mockSpawn.mockImplementation(mockSpawnFactory(output));

    const runner = new ClaudeRunner();
    const result = await runner.run({ prompt: "test" });
    expect(result.model).toBe("sonnet");
  });
});
