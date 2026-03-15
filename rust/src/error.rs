use std::fmt;

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
        let truncated: String = self.stderr.chars().take(500).collect();
        write!(
            f,
            "Claude failed (exit {}): {}",
            self.returncode, truncated
        )
    }
}

impl std::error::Error for ClaudeError {}
