use regex::Regex;
use std::fmt;
use std::sync::LazyLock;

static SENSITIVE_PATTERNS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    vec![
        Regex::new(r"(?i)sk-ant-[a-zA-Z0-9-]+").unwrap(),
        Regex::new(r"(?i)Bearer\s+\S+").unwrap(),
        Regex::new(r"(?i)token[=:]\s*\S+").unwrap(),
    ]
});

fn sanitize_stderr(stderr: &str) -> String {
    let mut result = stderr.to_string();
    for pattern in SENSITIVE_PATTERNS.iter() {
        result = pattern.replace_all(&result, "[REDACTED]").to_string();
    }
    result.chars().take(500).collect()
}

#[derive(Debug)]
pub struct ClaudeError {
    pub stderr: String,
    pub returncode: i32,
}

impl ClaudeError {
    pub fn new(stderr: impl Into<String>, returncode: i32) -> Self {
        Self {
            stderr: stderr.into(),
            returncode,
        }
    }
}

impl fmt::Display for ClaudeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Claude failed (exit {}): {}",
            self.returncode,
            sanitize_stderr(&self.stderr)
        )
    }
}

impl std::error::Error for ClaudeError {}
