const USAGE_LIMIT_PHRASES = [
  "out of extra usage",
  "usage limit",
  "rate limit",
];

export function isUsageLimited(stdout: string, stderr: string): boolean {
  const text = stderr.toLowerCase();
  return USAGE_LIMIT_PHRASES.some((phrase) => text.includes(phrase));
}

export function parseResetTime(stdout: string, stderr: string): Date | null {
  const combined = stdout + stderr;
  const match = combined.match(/resets\s+(\d{1,2})(am|pm)\s*\(UTC\)/i);
  if (!match) return null;

  let hour = parseInt(match[1], 10);
  if (hour < 1 || hour > 12) return null;
  const ampm = match[2].toLowerCase();
  if (ampm === "pm" && hour !== 12) hour += 12;
  else if (ampm === "am" && hour === 12) hour = 0;

  const now = new Date();
  const reset = new Date(
    Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate(),
      hour,
      0,
      0,
      0,
    ),
  );

  if (reset.getTime() <= now.getTime()) {
    reset.setUTCDate(reset.getUTCDate() + 1);
  }

  return reset;
}

export class RateLimitCache {
  private until = new Map<string, Date>();

  private keyFor(account: string | null): string {
    return account ?? "__default__";
  }

  isLimited(account: string | null): boolean {
    const key = this.keyFor(account);
    const resetTime = this.until.get(key);
    if (!resetTime) return false;
    if (Date.now() >= resetTime.getTime()) {
      this.until.delete(key);
      return false;
    }
    return true;
  }

  record(account: string | null, stdout: string, stderr: string): void {
    const key = this.keyFor(account);
    const resetTime = parseResetTime(stdout, stderr);
    if (resetTime) {
      this.until.set(key, resetTime);
    } else {
      const fallback = new Date(Date.now() + 5 * 60 * 1000);
      this.until.set(key, fallback);
    }
  }

  clear(account?: string | null): void {
    if (account === undefined) {
      this.until.clear();
    } else {
      this.until.delete(this.keyFor(account));
    }
  }
}
