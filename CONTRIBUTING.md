# Contributing to claude-rotator

Contributions are welcome. This document covers the basics for getting started.

## Development Setup

Clone the repo and install dependencies for the language(s) you plan to work on:

```bash
# Python
pip install -e .
pip install pytest

# TypeScript
cd npm
pnpm install

# Rust
cd rust
cargo build
```

## Running Tests

```bash
# Python
pytest

# TypeScript
cd npm && pnpm test

# Rust
cd rust && cargo test
```

All three test suites must pass before submitting a PR.

## Project Structure

The library is implemented in three languages with identical behavior:

```
claude_rotator/     # Python implementation
npm/src/            # TypeScript implementation
rust/src/           # Rust implementation
tests/              # Python tests
npm/tests/          # TypeScript tests
rust/src/ (inline)  # Rust tests (in-file #[cfg(test)] modules)
```

Changes to shared behavior (validation rules, error handling, rate limit detection) should be applied to all three implementations.

## Security Contributions

Security improvements are a priority. Audit reports live in `audits/`. If you find a vulnerability:

1. Open an issue describing the problem, affected files, and severity.
2. If you have a fix, submit a PR referencing the issue.
3. For sensitive disclosures, contact the maintainer directly before opening a public issue.

When submitting security fixes:

- Include test cases that demonstrate the vulnerability and verify the fix.
- Apply the fix to all three language implementations where applicable.
- Note the severity (High, Medium, Low) in the PR description.

## Code Style

- Python: standard library only, no external dependencies. Type hints on all public APIs.
- TypeScript: ESM, strict mode, no runtime dependencies. `pnpm` for package management.
- Rust: edition 2021, MSRV 1.80. `serde_json` and `regex` are the only required dependencies.

## Pull Requests

- Keep PRs focused on a single change.
- Add or update tests for any behavioral change.
- Run the full test suite for the language(s) you changed.
- Use clear commit messages that describe what changed and why.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
