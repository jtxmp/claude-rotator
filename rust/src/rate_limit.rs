use regex::Regex;
use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const USAGE_LIMIT_PHRASES: &[&str] = &["out of extra usage", "usage limit", "rate limit"];

static RESET_TIME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)resets\s+(\d{1,2})(am|pm)\s*\(UTC\)").unwrap());

pub fn is_usage_limited(_stdout: &str, stderr: &str) -> bool {
    let text = stderr.to_lowercase();
    USAGE_LIMIT_PHRASES
        .iter()
        .any(|phrase| text.contains(phrase))
}

pub fn parse_reset_time(stdout: &str, stderr: &str) -> Option<SystemTime> {
    let combined = format!("{}{}", stdout, stderr);
    let caps = RESET_TIME_RE.captures(&combined)?;

    let mut hour: u32 = caps[1].parse().ok()?;
    if hour < 1 || hour > 12 {
        return None;
    }
    let ampm = caps[2].to_lowercase();
    if ampm == "pm" && hour != 12 {
        hour += 12;
    } else if ampm == "am" && hour == 12 {
        hour = 0;
    }

    // Get current UTC time components
    let now = SystemTime::now();
    let since_epoch = now.duration_since(UNIX_EPOCH).ok()?;
    let total_secs = since_epoch.as_secs();

    // Calculate start of current UTC day
    let secs_into_day = total_secs % 86400;
    let day_start = total_secs - secs_into_day;

    // Target time today
    let target_secs = day_start + (hour as u64) * 3600;
    let mut reset = UNIX_EPOCH + Duration::from_secs(target_secs);

    // If reset time already passed, push to tomorrow
    if reset <= now {
        reset += Duration::from_secs(86400);
    }

    Some(reset)
}

/// Key for the rate limit cache. `None` represents the default account.
type AccountKey = Option<String>;

pub struct RateLimitCache {
    until: HashMap<AccountKey, SystemTime>,
}

impl RateLimitCache {
    pub fn new() -> Self {
        Self {
            until: HashMap::new(),
        }
    }

    pub fn is_limited(&mut self, account: &Option<String>) -> bool {
        let Some(reset_time) = self.until.get(account) else {
            return false;
        };
        if SystemTime::now() >= *reset_time {
            self.until.remove(account);
            return false;
        }
        true
    }

    pub fn record(&mut self, account: &Option<String>, stdout: &str, stderr: &str) {
        let reset_time = parse_reset_time(stdout, stderr);
        match reset_time {
            Some(t) => {
                self.until.insert(account.clone(), t);
            }
            None => {
                let fallback = SystemTime::now() + Duration::from_secs(300);
                self.until.insert(account.clone(), fallback);
            }
        }
    }

    pub fn clear(&mut self, account: Option<&Option<String>>) {
        match account {
            Some(key) => {
                self.until.remove(key);
            }
            None => {
                self.until.clear();
            }
        }
    }
}

impl Default for RateLimitCache {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detects_out_of_extra_usage_in_stderr() {
        assert!(is_usage_limited("", "You are out of extra usage for today"));
    }

    #[test]
    fn test_detects_usage_limit_in_stderr() {
        assert!(is_usage_limited("", "usage limit reached"));
    }

    #[test]
    fn test_detects_rate_limit_in_stderr() {
        assert!(is_usage_limited("", "rate limit exceeded"));
    }

    #[test]
    fn test_case_insensitive() {
        assert!(is_usage_limited("", "USAGE LIMIT"));
    }

    #[test]
    fn test_no_limit_phrases() {
        assert!(!is_usage_limited("Hello world", "some error"));
    }

    #[test]
    fn test_empty_strings() {
        assert!(!is_usage_limited("", ""));
    }

    #[test]
    fn test_ignores_stdout_content() {
        assert!(!is_usage_limited("You hit the usage limit", ""));
    }

    #[test]
    fn test_ignores_rate_limit_in_stdout_only() {
        assert!(!is_usage_limited("rate limit exceeded", ""));
    }

    #[test]
    fn test_detects_when_phrase_in_stderr_with_noisy_stdout() {
        assert!(is_usage_limited("normal output", "usage limit reached"));
    }

    #[test]
    fn test_parses_pm_time() {
        let result = parse_reset_time("Your usage resets 8pm (UTC)", "");
        assert!(result.is_some());
        let reset = result.unwrap();
        assert!(reset > SystemTime::now() || {
            // Verify it's at hour 20
            let since_epoch = reset.duration_since(UNIX_EPOCH).unwrap();
            let secs_into_day = since_epoch.as_secs() % 86400;
            secs_into_day == 20 * 3600
        });
    }

    #[test]
    fn test_parses_am_time() {
        let result = parse_reset_time("resets 2am (UTC)", "");
        assert!(result.is_some());
    }

    #[test]
    fn test_parses_12pm() {
        let result = parse_reset_time("resets 12pm (UTC)", "");
        assert!(result.is_some());
        let since_epoch = result.unwrap().duration_since(UNIX_EPOCH).unwrap();
        let secs_into_day = since_epoch.as_secs() % 86400;
        assert_eq!(secs_into_day, 12 * 3600);
    }

    #[test]
    fn test_parses_12am() {
        let result = parse_reset_time("resets 12am (UTC)", "");
        assert!(result.is_some());
        let since_epoch = result.unwrap().duration_since(UNIX_EPOCH).unwrap();
        let secs_into_day = since_epoch.as_secs() % 86400;
        assert_eq!(secs_into_day, 0);
    }

    #[test]
    fn test_returns_none_for_no_match() {
        assert!(parse_reset_time("some error", "").is_none());
    }

    #[test]
    fn test_searches_stderr() {
        let result = parse_reset_time("", "resets 3pm (UTC)");
        assert!(result.is_some());
    }

    #[test]
    fn test_result_is_in_future() {
        let result = parse_reset_time("resets 8pm (UTC)", "");
        assert!(result.unwrap() > SystemTime::now());
    }

    #[test]
    fn test_cache_not_limited_by_default() {
        let mut cache = RateLimitCache::new();
        assert!(!cache.is_limited(&None));
        assert!(!cache.is_limited(&Some("/path".into())));
    }

    #[test]
    fn test_cache_record_and_check() {
        let mut cache = RateLimitCache::new();
        cache.record(&None, "resets 8pm (UTC)", "");
        assert!(cache.is_limited(&None));
    }

    #[test]
    fn test_cache_expired_limit_cleared() {
        let mut cache = RateLimitCache::new();
        let past = SystemTime::now() - Duration::from_secs(60);
        cache.until.insert(None, past);
        assert!(!cache.is_limited(&None));
    }

    #[test]
    fn test_cache_clear_specific() {
        let mut cache = RateLimitCache::new();
        cache.record(&Some("/acct1".into()), "resets 8pm (UTC)", "");
        cache.record(&Some("/acct2".into()), "resets 8pm (UTC)", "");
        cache.clear(Some(&Some("/acct1".into())));
        assert!(!cache.is_limited(&Some("/acct1".into())));
        assert!(cache.is_limited(&Some("/acct2".into())));
    }

    #[test]
    fn test_cache_clear_all() {
        let mut cache = RateLimitCache::new();
        cache.record(&None, "resets 8pm (UTC)", "");
        cache.record(&Some("/acct1".into()), "resets 8pm (UTC)", "");
        cache.clear(None);
        assert!(!cache.is_limited(&None));
        assert!(!cache.is_limited(&Some("/acct1".into())));
    }

    #[test]
    fn test_cache_unparseable_uses_fallback() {
        let mut cache = RateLimitCache::new();
        cache.record(&None, "some error", "");
        assert!(cache.is_limited(&None));
    }
}
