use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Instant;

use crate::error::ClaudeError;
use crate::rate_limit::{is_usage_limited, RateLimitCache};

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

fn build_cmd(model: &str, tools: Option<&str>) -> Vec<String> {
    let mut cmd = vec![
        "claude".into(),
        "-p".into(),
        "--model".into(),
        model.into(),
        "--output-format".into(),
        "json".into(),
    ];
    if let Some(t) = tools {
        cmd.push("--allowedTools".into());
        cmd.push(t.into());
    }
    cmd
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

fn build_env(home_dir: &Option<String>) -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = std::env::vars().collect();
    if let Some(dir) = home_dir {
        env.retain(|(k, _)| k != "HOME" && k != "USERPROFILE");
        env.push(("HOME".into(), dir.clone()));
        #[cfg(windows)]
        env.push(("USERPROFILE".into(), dir.clone()));
    }
    env
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
    ) -> Result<ClaudeResult, ClaudeError> {
        let cmd_parts = build_cmd(model, tools);
        let program = &cmd_parts[0];
        let args = &cmd_parts[1..];

        for i in 0..self.accounts.len() {
            let home_dir = &self.accounts[i];
            if self.cache.is_limited(home_dir) {
                continue;
            }

            let start = Instant::now();
            let env = build_env(home_dir);

            let mut command = Command::new(program);
            command
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .envs(env);

            if let Some(dir) = cwd {
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
    ) -> Result<ClaudeResult, ClaudeError> {
        use tokio::io::AsyncWriteExt;
        use tokio::process::Command as TokioCommand;

        let cmd_parts = build_cmd(model, tools);
        let program = &cmd_parts[0];
        let args = &cmd_parts[1..];

        for i in 0..self.accounts.len() {
            let home_dir = &self.accounts[i];
            if self.cache.is_limited(home_dir) {
                continue;
            }

            let start = Instant::now();
            let env = build_env(home_dir);

            let mut command = TokioCommand::new(program);
            command
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .envs(env);

            if let Some(dir) = cwd {
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

            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(prompt.as_bytes()).await;
                drop(stdin);
            }

            // Read stdout/stderr concurrently, then wait for exit
            let mut stdout_handle = child.stdout.take();
            let mut stderr_handle = child.stderr.take();

            let timeout_dur = std::time::Duration::from_secs(timeout_secs);

            let read_and_wait = async {
                use tokio::io::AsyncReadExt;
                let mut stdout_buf = Vec::new();
                let mut stderr_buf = Vec::new();

                let (stdout_res, stderr_res) = tokio::join!(
                    async {
                        if let Some(ref mut r) = stdout_handle {
                            let _ = r.read_to_end(&mut stdout_buf).await;
                        }
                    },
                    async {
                        if let Some(ref mut r) = stderr_handle {
                            let _ = r.read_to_end(&mut stderr_buf).await;
                        }
                    },
                );
                let _ = (stdout_res, stderr_res);
                let status = child.wait().await;
                (stdout_buf, stderr_buf, status)
            };

            match tokio::time::timeout(timeout_dur, read_and_wait).await {
                Ok((stdout_buf, stderr_buf, status_result)) => {
                    let duration = start.elapsed().as_secs_f64();
                    let stdout = String::from_utf8_lossy(&stdout_buf).to_string();
                    let stderr = String::from_utf8_lossy(&stderr_buf).to_string();
                    let exit_code = match status_result {
                        Ok(s) => s.code().unwrap_or(1),
                        Err(e) => return Err(ClaudeError::new(e.to_string(), 1)),
                    };

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
                    // Timeout. child was moved into the async block, so we
                    // can't kill it here. The drop will clean up.
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
}

enum WaitResult {
    Completed(ProcessOutput),
    TimedOut,
}

fn wait_with_timeout(child: &mut std::process::Child, timeout: std::time::Duration) -> WaitResult {
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut stdout_buf = Vec::new();
                let mut stderr_buf = Vec::new();
                if let Some(mut stdout) = child.stdout.take() {
                    use std::io::Read;
                    let _ = stdout.read_to_end(&mut stdout_buf);
                }
                if let Some(mut stderr) = child.stderr.take() {
                    use std::io::Read;
                    let _ = stderr.read_to_end(&mut stderr_buf);
                }
                return WaitResult::Completed(ProcessOutput {
                    stdout: String::from_utf8_lossy(&stdout_buf).to_string(),
                    stderr: String::from_utf8_lossy(&stderr_buf).to_string(),
                    exit_code: status.code().unwrap_or(1),
                });
            }
            Ok(None) => {
                if start.elapsed() >= timeout {
                    return WaitResult::TimedOut;
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => {
                return WaitResult::TimedOut;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_cmd_with_tools() {
        let cmd = build_cmd("sonnet", Some("Read,Write"));
        assert_eq!(
            cmd,
            vec!["claude", "-p", "--model", "sonnet", "--output-format", "json", "--allowedTools", "Read,Write"]
        );
    }

    #[test]
    fn test_build_cmd_without_tools() {
        let cmd = build_cmd("opus", None);
        assert_eq!(
            cmd,
            vec!["claude", "-p", "--model", "opus", "--output-format", "json"]
        );
        assert!(!cmd.contains(&"--allowedTools".to_string()));
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
}
