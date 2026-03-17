import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  isUsageLimited,
  parseResetTime,
  RateLimitCache,
} from "../src/rate-limit.js";

describe("isUsageLimited", () => {
  it("detects 'out of extra usage' in stderr", () => {
    expect(
      isUsageLimited("", "You are out of extra usage for today"),
    ).toBe(true);
  });

  it("detects 'usage limit' in stderr", () => {
    expect(isUsageLimited("", "usage limit reached")).toBe(true);
  });

  it("detects 'rate limit' in stderr", () => {
    expect(isUsageLimited("", "rate limit exceeded")).toBe(true);
  });

  it("is case insensitive", () => {
    expect(isUsageLimited("", "USAGE LIMIT")).toBe(true);
  });

  it("returns false when no phrases match", () => {
    expect(isUsageLimited("Hello world", "some error")).toBe(false);
  });

  it("returns false for empty strings", () => {
    expect(isUsageLimited("", "")).toBe(false);
  });

  it("ignores stdout content (false positive prevention)", () => {
    expect(isUsageLimited("You hit the usage limit", "")).toBe(false);
  });

  it("ignores rate limit in stdout only", () => {
    expect(isUsageLimited("rate limit exceeded", "")).toBe(false);
  });

  it("detects when phrase in stderr with noisy stdout", () => {
    expect(isUsageLimited("normal output", "usage limit reached")).toBe(
      true,
    );
  });
});

describe("parseResetTime", () => {
  it("parses pm time", () => {
    const result = parseResetTime("Your usage resets 8pm (UTC)", "");
    expect(result).not.toBeNull();
    expect(result!.getUTCHours()).toBe(20);
    expect(result!.getUTCMinutes()).toBe(0);
  });

  it("parses am time", () => {
    const result = parseResetTime("resets 2am (UTC)", "");
    expect(result).not.toBeNull();
    expect(result!.getUTCHours()).toBe(2);
  });

  it("parses 12pm as noon", () => {
    const result = parseResetTime("resets 12pm (UTC)", "");
    expect(result).not.toBeNull();
    expect(result!.getUTCHours()).toBe(12);
  });

  it("parses 12am as midnight", () => {
    const result = parseResetTime("resets 12am (UTC)", "");
    expect(result).not.toBeNull();
    expect(result!.getUTCHours()).toBe(0);
  });

  it("returns null for no match", () => {
    expect(parseResetTime("some error", "")).toBeNull();
  });

  it("searches stderr too", () => {
    const result = parseResetTime("", "resets 3pm (UTC)");
    expect(result).not.toBeNull();
    expect(result!.getUTCHours()).toBe(15);
  });

  it("returns a future date", () => {
    const result = parseResetTime("resets 8pm (UTC)", "");
    expect(result).not.toBeNull();
    expect(result!.getTime()).toBeGreaterThan(Date.now());
  });
});

describe("RateLimitCache", () => {
  let cache: RateLimitCache;

  beforeEach(() => {
    cache = new RateLimitCache();
  });

  it("is not limited by default", () => {
    expect(cache.isLimited(null)).toBe(false);
    expect(cache.isLimited("/some/path")).toBe(false);
  });

  it("records and checks limits", () => {
    // Manually set a future limit via record with a parseable message
    cache.record(null, "resets 8pm (UTC)", "");
    expect(cache.isLimited(null)).toBe(true);
  });

  it("expires old limits", () => {
    // Access internal state to set an expired time
    const past = new Date(Date.now() - 60_000);
    (cache as any).until.set("__default__", past);
    expect(cache.isLimited(null)).toBe(false);
  });

  it("clears specific account", () => {
    cache.record("/acct1", "resets 8pm (UTC)", "");
    cache.record("/acct2", "resets 8pm (UTC)", "");
    cache.clear("/acct1");
    expect(cache.isLimited("/acct1")).toBe(false);
    expect(cache.isLimited("/acct2")).toBe(true);
  });

  it("clears all accounts", () => {
    cache.record(null, "resets 8pm (UTC)", "");
    cache.record("/acct1", "resets 8pm (UTC)", "");
    cache.clear();
    expect(cache.isLimited(null)).toBe(false);
    expect(cache.isLimited("/acct1")).toBe(false);
  });

  it("uses 5-minute fallback when reset time is unparseable", () => {
    cache.record(null, "some error", "");
    expect(cache.isLimited(null)).toBe(true);
  });
});
