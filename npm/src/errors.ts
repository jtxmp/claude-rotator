const SENSITIVE_PATTERNS = [
  /sk-ant-[a-zA-Z0-9-]+/gi,
  /Bearer\s+\S+/gi,
  /token[=:]\s*\S+/gi,
];

function sanitizeStderr(stderr: string): string {
  let result = stderr;
  for (const pattern of SENSITIVE_PATTERNS) {
    result = result.replace(pattern, "[REDACTED]");
  }
  return result.slice(0, 500);
}

export class ClaudeError extends Error {
  public readonly stderr: string;
  public readonly returncode: number;

  constructor(stderr: string, returncode: number = 1) {
    super(
      `Claude failed (exit ${returncode}): ${sanitizeStderr(stderr)}`,
    );
    this.name = "ClaudeError";
    this.stderr = stderr;
    this.returncode = returncode;
  }
}
