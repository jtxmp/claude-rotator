use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::Instant;

use regex::Regex;

use crate::error::ClaudeError;
use crate::rate_limit::{is_usage_limited, RateLimitCache};

const MAX_OUTPUT_BYTES: usize = 50 * 1024 * 1024; // 50 MB
const MIN_TIMEOUT_SECS: u64 = 1;
const MAX_TIMEOUT_SECS: u64 = 3600;

static VALID_MODEL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*$").unwrap());
static VALID_TOOLS_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z_]+(,[A-Za-z_]+)*$").unwrap());

#[derive(Debug, Clone)]
pub struct ClaudeResult {
    pub output: String,
    pub cost_usd: f64,
    pub duration_seconds: f64,
    pub model: String,
}

pub struct ClaudeRunner {
    pub accounts: Vec<Option<String>>,
    cache: RateLimitCache,
}

fn validate_inputs(model: &str, tools: Option<&str>) -> Result<(), ClaudeError> {
    if !VALID_MODEL_RE.is_match(model) {
        return Err(ClaudeError::new(
            format!("Invalid model identifier: {}", model),
            1,
        ));
    }
    if let Some(t) = tools {
        if !VALID_TOOLS_RE.is_match(t) {
            return Err(ClaudeError::new(
                format!("Invalid tools specification: {}", t),
                1,
            ));
        }
    }
    Ok(())
}

fn validate_timeout(timeout_secs: u64) -> Result<(), ClaudeError> {
    if timeout_secs < MIN_TIMEOUT_SECS || timeout_secs > MAX_TIMEOUT_SECS {
        return Err(ClaudeError::new(
            format!(
                "Timeout must be between {} and {} seconds",
                MIN_TIMEOUT_SECS, MAX_TIMEOUT_SECS
            ),
            1,
        ));
    }
    Ok(())
}

fn validate_directory(path: &Path, label: &str) -> Result<std::path::PathBuf, ClaudeError> {
    let canonical = std::fs::canonicalize(path).map_err(|_| {
        ClaudeError::new(
            format!(
                "{} is not an existing directory: {}",
                label,
                path.display()
            ),
            1,
        )
    })?;
    if !canonical.is_dir() {
        return Err(ClaudeError::new(
            format!("{} is not a directory: {}", label, canonical.display()),
            1,
        ));
    }
    Ok(canonical)
}

fn build_cmd(model: &str, tools: Option<&str>, system_prompt: Option<&str>) -> Result<Vec<String>, ClaudeError> {
    validate_inputs(model, tools)?;
    let mut cmd = vec![
        "claude".into(),
        "-p".into(),
        "--model".into(),
        model.into(),
        "--output-format".into(),
        "json".into(),
    ];
    if let Some(sp) = system_prompt {
        if sp.starts_with('-') {
            return Err(ClaudeError::new(
                format!("system_prompt must not start with '-': {}", sp),
                1,
            ));
        }
        cmd.push("--system-prompt".into());
        cmd.push(sp.into());
    }
    if let Some(t) = tools {
        cmd.push("--allowedTools".into());
        cmd.push(t.into());
    }
    Ok(cmd)
}

fn parse_output(stdout: &str) -> Result<(String, f64), ClaudeError> {
    let mut cost = 0.0_f64;
    let mut output = stdout.to_string();

    match serde_json::from_str::<serde_json::Value>(stdout) {
        Ok(data) => {
            if data.get("is_error").and_then(|v| v.as_bool()).unwrap_or(false) {
                let msg = data
                    .get("result")
                    .and_then(|v| v.as_str())
                    .unwrap_or("Unknown error");
                return Err(ClaudeError::new(msg, 1));
            }
            if let Some(result) = data.get("result").and_then(|v| v.as_str()) {
                output = result.to_string();
            }
            cost = data
                .get("total_cost_usd")
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0);
            if cost == 0.0 {
                if let Some(cost_obj) = data.get("cost") {
                    cost = cost_obj
                        .get("total_usd")
                        .and_then(|v| v.as_f64())
                        .unwrap_or(0.0);
                }
            }
        }
        Err(_) => {
            // Not valid JSON, return raw stdout
        }
    }

    Ok((output, cost))
}

#[cfg(unix)]
fn kill_process_tree(pid: u32) {
    // Send SIGTERM to the process group (negative PID)
    let _ = Command::new("kill")
        .args(["-TERM", &format!("-{}", pid)])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    // Then SIGKILL the individual process
    let _ = Command::new("kill")
        .args(["-KILL", &pid.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(windows)]
fn kill_process_tree(pid: u32) {
    let _ = Command::new("taskkill")
        .args(["/F", "/T", "/PID", &pid.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(not(any(unix, windows)))]
fn kill_process_tree(_pid: u32) {
    // Unsupported platform, do nothing
}

fn build_env(home_dir: &Option<String>) -> Result<Vec<(String, String)>, ClaudeError> {
    let mut env: Vec<(String, String)> = std::env::vars().collect();
    if let Some(dir) = home_dir {
        let canonical = validate_directory(Path::new(dir), "Account home_dir")?;
        let dir_str = canonical.to_string_lossy().to_string();
        env.retain(|(k, _)| k != "HOME" && k != "USERPROFILE");
        env.push(("HOME".into(), dir_str.clone()));
        #[cfg(windows)]
        env.push(("USERPROFILE".into(), dir_str));
    }
    Ok(env)
}

/// Read from a stream with a size limit. Returns (data, exceeded).
fn read_limited<R: std::io::Read>(
    stream: R,
    max_bytes: usize,
) -> (Vec<u8>, bool) {
    use std::io::Read;
    let mut buf = Vec::new();
    let limit = (max_bytes + 1) as u64;
    let bytes_read = stream.take(limit).read_to_end(&mut buf);
    let exceeded = match bytes_read {
        Ok(_) => buf.len() > max_bytes,
        Err(_) => false,
    };
    if exceeded {
        buf.truncate(max_bytes);
    }
    (buf, exceeded)
}

impl ClaudeRunner {
    pub fn new(accounts: Vec<Option<String>>) -> Self {
        Self {
            accounts,
            cache: RateLimitCache::new(),
        }
    }

    pub fn with_default() -> Self {
        Self::new(vec![None])
    }

    pub fn run(
        &mut self,
        prompt: &str,
        model: &str,
        tools: Option<&str>,
        cwd: Option<&Path>,
        timeout_secs: u64,
        system_prompt: Option<&str>,
    ) -> Result<ClaudeResult, ClaudeError> {
        validate_timeout(timeout_secs)?;
        let cmd_parts = build_cmd(model, tools, system_prompt)?;
        let program = &cmd_parts[0];
        let args = &cmd_parts[1..];

        let resolved_cwd = match cwd {
            Some(dir) => Some(validate_directory(dir, "cwd")?),
            None => None,
        };

        for i in 0..self.accounts.len() {
            let home_dir = &self.accounts[i];
            if self.cache.is_limited(home_dir) {
                continue;
            }

            let start = Instant::now();
            let env = build_env(home_dir)?;

            let mut command = Command::new(program);
            command
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .envs(env);

            if let Some(ref dir) = resolved_cwd {
                command.current_dir(dir);
            }

            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                command.process_group(0);
            }

            let mut child = command
                .spawn()
                .map_err(|e| ClaudeError::new(e.to_string(), 1))?;

            // Write prompt to stdin
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(prompt.as_bytes());
            }

            // Wait with timeout
            let timeout = std::time::Duration::from_secs(timeout_secs);
            let result = wait_with_timeout(&mut child, timeout);

            match result {
                WaitResult::Completed(output) => {
                    let duration = start.elapsed().as_secs_f64();

                    if output.output_exceeded {
                        return Err(ClaudeError::new(
                            "Subprocess output exceeded maximum allowed size",
                            -1,
                        ));
                    }

                    if is_usage_limited(&output.stdout, &output.stderr) {
                        let home_clone = self.accounts[i].clone();
                        self.cache.record(&home_clone, &output.stdout, &output.stderr);
                        if i < self.accounts.len() - 1 {
                            continue;
                        }
                        return Err(ClaudeError::new(
                            "All Claude accounts hit usage limits",
                            1,
                        ));
                    }

                    if output.exit_code != 0 {
                        return Err(ClaudeError::new(output.stderr, output.exit_code));
                    }

                    let (output_text, cost) = parse_output(&output.stdout)?;
                    return Ok(ClaudeResult {
                        output: output_text,
                        cost_usd: cost,
                        duration_seconds: duration,
                        model: model.to_string(),
                    });
                }
                WaitResult::TimedOut => {
                    kill_process_tree(child.id());
                    let _ = child.wait();
                    return Err(ClaudeError::new(
                        format!("Timeout after {}s", timeout_secs),
                        -1,
                    ));
                }
            }
        }

        Err(ClaudeError::new(
            "All Claude accounts hit usage limits",
            1,
        ))
    }

    #[cfg(feature = "async")]
    pub async fn run_async(
        &mut self,
        prompt: &str,
        model: &str,
        tools: Option<&str>,
        cwd: Option<&Path>,
        timeout_secs: u64,
        system_prompt: Option<&str>,
    ) -> Result<ClaudeResult, ClaudeError> {
        use tokio::io::AsyncWriteExt;
        use tokio::process::Command as TokioCommand;

        validate_timeout(timeout_secs)?;
        let cmd_parts = build_cmd(model, tools, system_prompt)?;
        let program = &cmd_parts[0];
        let args = &cmd_parts[1..];

        let resolved_cwd = match cwd {
            Some(dir) => Some(validate_directory(dir, "cwd")?),
            None => None,
        };

        for i in 0..self.accounts.len() {
            let home_dir = &self.accounts[i];
            if self.cache.is_limited(home_dir) {
                continue;
            }

            let start = Instant::now();
            let env = build_env(home_dir)?;

            let mut command = TokioCommand::new(program);
            command
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .envs(env);

            if let Some(ref dir) = resolved_cwd {
                command.current_dir(dir);
            }

            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                command.process_group(0);
            }

            let mut child = command
                .spawn()
                .map_err(|e| ClaudeError::new(e.to_string(), 1))?;

            // Capture PID before moving child into the async block
            let child_pid = child.id().unwrap_or(0);

            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(prompt.as_bytes()).await;
                drop(stdin);
            }

            // Read stdout/stderr concurrently with size limits, then wait for exit
            let mut stdout_handle = child.stdout.take();
            let mut stderr_handle = child.stderr.take();
            let max_bytes = MAX_OUTPUT_BYTES;

            let timeout_dur = std::time::Duration::from_secs(timeout_secs);

            let read_and_wait = async {
                use tokio::io::AsyncReadExt;

                async fn drain_limited(
                    handle: &mut Option<tokio::process::ChildStdout>,
                    max_bytes: usize,
                ) -> (Vec<u8>, bool) {
                    let mut buf = Vec::new();
                    if let Some(ref mut r) = handle {
                        let mut total = 0usize;
                        let mut chunk = [0u8; 8192];
                        loop {
                            match r.read(&mut chunk).await {
                                Ok(0) => break,
                                Ok(n) => {
                                    total += n;
                                    if total > max_bytes {
                                        return (buf, true);
                                    }
                                    buf.extend_from_slice(&chunk[..n]);
                                }
                                Err(_) => break,
                            }
                        }
                    }
                    (buf, false)
                }

                async fn drain_limited_stderr(
                    handle: &mut Option<tokio::process::ChildStderr>,
                    max_bytes: usize,
                ) -> (Vec<u8>, bool) {
                    let mut buf = Vec::new();
                    if let Some(ref mut r) = handle {
                        let mut total = 0usize;
                        let mut chunk = [0u8; 8192];
                        loop {
                            match r.read(&mut chunk).await {
                                Ok(0) => break,
                                Ok(n) => {
                                    total += n;
                                    if total > max_bytes {
                                        return (buf, true);
                                    }
                                    buf.extend_from_slice(&chunk[..n]);
                                }
                                Err(_) => break,
                            }
                        }
                    }
                    (buf, false)
                }

                let ((stdout_buf, stdout_exceeded), (stderr_buf, stderr_exceeded)) = tokio::join!(
                    drain_limited(&mut stdout_handle, max_bytes),
                    drain_limited_stderr(&mut stderr_handle, max_bytes),
                );

                let status = child.wait().await;
                (stdout_buf, stderr_buf, stdout_exceeded || stderr_exceeded, status)
            };

            match tokio::time::timeout(timeout_dur, read_and_wait).await {
                Ok((stdout_buf, stderr_buf, output_exceeded, status_result)) => {
                    let duration = start.elapsed().as_secs_f64();
                    let stdout = String::from_utf8_lossy(&stdout_buf).to_string();
                    let stderr = String::from_utf8_lossy(&stderr_buf).to_string();
                    let exit_code = match status_result {
                        Ok(s) => s.code().unwrap_or(1),
                        Err(e) => return Err(ClaudeError::new(e.to_string(), 1)),
                    };

                    if output_exceeded {
                        return Err(ClaudeError::new(
                            "Subprocess output exceeded maximum allowed size",
                            -1,
                        ));
                    }

                    if is_usage_limited(&stdout, &stderr) {
                        let home_clone = self.accounts[i].clone();
                        self.cache.record(&home_clone, &stdout, &stderr);
                        if i < self.accounts.len() - 1 {
                            continue;
                        }
                        return Err(ClaudeError::new(
                            "All Claude accounts hit usage limits",
                            1,
                        ));
                    }

                    if exit_code != 0 {
                        return Err(ClaudeError::new(stderr, exit_code));
                    }

                    let (output_text, cost) = parse_output(&stdout)?;
                    return Ok(ClaudeResult {
                        output: output_text,
                        cost_usd: cost,
                        duration_seconds: duration,
                        model: model.to_string(),
                    });
                }
                Err(_) => {
                    // Timeout: kill using captured PID
                    if child_pid != 0 {
                        kill_process_tree(child_pid);
                    }
                    return Err(ClaudeError::new("Timeout exceeded", -1));
                }
            }
        }

        Err(ClaudeError::new(
            "All Claude accounts hit usage limits",
            1,
        ))
    }
}

struct ProcessOutput {
    stdout: String,
    stderr: String,
    exit_code: i32,
    output_exceeded: bool,
}

enum WaitResult {
    Completed(ProcessOutput),
    TimedOut,
}

fn wait_with_timeout(child: &mut std::process::Child, timeout: std::time::Duration) -> WaitResult {
    // Take pipe handles and drain them in background threads to prevent
    // deadlocks when the subprocess output exceeds the OS pipe buffer.
    let stdout_handle = child.stdout.take();
    let stderr_handle = child.stderr.take();
    let max_bytes = MAX_OUTPUT_BYTES;

    let stdout_thread = std::thread::spawn(move || match stdout_handle {
        Some(stream) => read_limited(stream, max_bytes),
        None => (Vec::new(), false),
    });

    let stderr_thread = std::thread::spawn(move || match stderr_handle {
        Some(stream) => read_limited(stream, max_bytes),
        None => (Vec::new(), false),
    });

    // Poll for process exit with timeout
    let start = Instant::now();
    let exit_code = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status.code().unwrap_or(1),
            Ok(None) => {
                if start.elapsed() >= timeout {
                    // Reader threads will finish once the caller kills the process
                    return WaitResult::TimedOut;
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => {
                return WaitResult::TimedOut;
            }
        }
    };

    // Process exited; join reader threads (pipes are closed, so they return quickly)
    let (stdout_buf, stdout_exceeded) = stdout_thread.join().unwrap_or_default();
    let (stderr_buf, stderr_exceeded) = stderr_thread.join().unwrap_or_default();

    WaitResult::Completed(ProcessOutput {
        stdout: String::from_utf8_lossy(&stdout_buf).to_string(),
        stderr: String::from_utf8_lossy(&stderr_buf).to_string(),
        exit_code,
        output_exceeded: stdout_exceeded || stderr_exceeded,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_cmd_with_tools() {
        let cmd = build_cmd("sonnet", Some("Read,Write"), None).unwrap();
        assert_eq!(
            cmd,
            vec![
                "claude",
                "-p",
                "--model",
                "sonnet",
                "--output-format",
                "json",
                "--allowedTools",
                "Read,Write"
            ]
        );
    }

    #[test]
    fn test_build_cmd_without_tools() {
        let cmd = build_cmd("opus", None, None).unwrap();
        assert_eq!(
            cmd,
            vec!["claude", "-p", "--model", "opus", "--output-format", "json"]
        );
        assert!(!cmd.contains(&"--allowedTools".to_string()));
    }

    #[test]
    fn test_build_cmd_rejects_invalid_model() {
        let result = build_cmd("sonnet --flag", None, None);
        assert!(result.is_err());
    }

    #[test]
    fn test_build_cmd_rejects_invalid_tools() {
        let result = build_cmd("sonnet", Some("Read --inject"), None);
        assert!(result.is_err());
    }

    #[test]
    fn test_build_cmd_accepts_full_model_id() {
        let result = build_cmd("claude-sonnet-4-5-20250514", None, None);
        assert!(result.is_ok());
    }

    #[test]
    fn test_validate_timeout_rejects_zero() {
        assert!(validate_timeout(0).is_err());
    }

    #[test]
    fn test_validate_timeout_rejects_too_large() {
        assert!(validate_timeout(MAX_TIMEOUT_SECS + 1).is_err());
    }

    #[test]
    fn test_validate_timeout_accepts_valid() {
        assert!(validate_timeout(600).is_ok());
    }

    #[test]
    fn test_parse_output_valid_json() {
        let json = r#"{"result": "Hello", "total_cost_usd": 0.01}"#;
        let (output, cost) = parse_output(json).unwrap();
        assert_eq!(output, "Hello");
        assert!((cost - 0.01).abs() < f64::EPSILON);
    }

    #[test]
    fn test_parse_output_raw_text() {
        let (output, cost) = parse_output("raw text").unwrap();
        assert_eq!(output, "raw text");
        assert_eq!(cost, 0.0);
    }

    #[test]
    fn test_parse_output_is_error() {
        let json = r#"{"is_error": true, "result": "Something broke"}"#;
        let err = parse_output(json).unwrap_err();
        assert!(err.stderr.contains("Something broke"));
    }

    #[test]
    fn test_parse_output_nested_cost() {
        let json = r#"{"result": "Hi", "cost": {"total_usd": 0.05}}"#;
        let (output, cost) = parse_output(json).unwrap();
        assert_eq!(output, "Hi");
        assert!((cost - 0.05).abs() < f64::EPSILON);
    }

    #[test]
    fn test_default_accounts() {
        let runner = ClaudeRunner::with_default();
        assert_eq!(runner.accounts, vec![None]);
    }

    #[test]
    fn test_custom_accounts() {
        let runner = ClaudeRunner::new(vec![None, Some("/fallback".into())]);
        assert_eq!(runner.accounts.len(), 2);
    }

    #[test]
    fn test_read_limited_within_bounds() {
        let data = b"hello world";
        let mut cursor = std::io::Cursor::new(data);
        let (buf, exceeded) = read_limited(&mut cursor, 1024);
        assert_eq!(buf, data);
        assert!(!exceeded);
    }

    #[test]
    fn test_read_limited_exceeds_bounds() {
        let data = vec![b'x'; 100];
        let mut cursor = std::io::Cursor::new(data);
        let (buf, exceeded) = read_limited(&mut cursor, 50);
        assert!(exceeded);
        assert_eq!(buf.len(), 50);
    }

    #[test]
    fn test_build_cmd_with_system_prompt() {
        let cmd = build_cmd("sonnet", None, Some("You are helpful")).unwrap();
        assert!(cmd.contains(&"--system-prompt".to_string()));
        let idx = cmd.iter().position(|x| x == "--system-prompt").unwrap();
        assert_eq!(cmd[idx + 1], "You are helpful");
    }

    #[test]
    fn test_build_cmd_without_system_prompt() {
        let cmd = build_cmd("sonnet", None, None).unwrap();
        assert!(!cmd.contains(&"--system-prompt".to_string()));
    }

    #[test]
    fn test_build_cmd_with_system_prompt_and_tools() {
        let cmd = build_cmd("sonnet", Some("Read,Write"), Some("Be concise")).unwrap();
        let sp_idx = cmd.iter().position(|x| x == "--system-prompt").unwrap();
        let tools_idx = cmd.iter().position(|x| x == "--allowedTools").unwrap();
        assert_eq!(cmd[sp_idx + 1], "Be concise");
        assert_eq!(cmd[tools_idx + 1], "Read,Write");
        // system-prompt should come before tools
        assert!(sp_idx < tools_idx);
    }

    #[test]
    fn test_build_cmd_rejects_system_prompt_starting_with_dash() {
        let result = build_cmd("sonnet", None, Some("--inject"));
        assert!(result.is_err());
        let result2 = build_cmd("sonnet", None, Some("-flag"));
        assert!(result2.is_err());
    }
}
