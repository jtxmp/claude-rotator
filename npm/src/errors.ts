export class ClaudeError extends Error {
  public readonly stderr: string;
  public readonly returncode: number;

  constructor(stderr: string, returncode: number = 1) {
    super(`Claude failed (exit ${returncode}): ${stderr.slice(0, 500)}`);
    this.name = "ClaudeError";
    this.stderr = stderr;
    this.returncode = returncode;
  }
}
