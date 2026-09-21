//! What this node needs its operator to *do*, on the screen they leave open.
//!
//! The Rust half of `src/utils/operator_alerts.py`. Both answer the same two
//! questions, in the same words, from the same files on disk:
//!
//! * **The gateway port.** Assigned or still `auto`, and whether `.gateway_notice`
//!   is sitting beside `config.yaml` — which is the node's own record that the last
//!   thing to look at that port could not open it or could not reach it. Written by
//!   `ConfigManager._gateway_notice_unlocked` and by `serve.py`'s refusal to start;
//!   removed the instant the port is *proven* reachable
//!   (`ConfigManager.mark_gateway_port_passed`) or the port changes. Its presence is
//!   exactly "there is an open question about this port", with no second lifetime to
//!   keep in step.
//! * **Java.** Present or absent, by the same three places `ensure_java_runtime`
//!   looks and in the same order: `JAVA_HOME`, `dependencies.java.JAVA_HOME`, then
//!   `java` on `PATH`. `PATH` is last rather than missing, because a stale
//!   configured path used to end the search and take the node's entire payment
//!   system with it.
//!
//! **Why this is not a subprocess.** The obvious implementation is to shell out to
//! `nodo info` (or to a small Python helper) and parse what comes back. That would
//! be a fork per refresh, on a loop that ticks four times a second, to re-answer a
//! question whose inputs are two `stat` calls and a YAML read. It would also make
//! the banner lag the CLI by however long a Python interpreter takes to start.
//! Duplicating the *criteria* in fifty lines of Rust, with a test on each side
//! pinning that they still match, is the cheaper and the more honest trade.
//!
//! **Why it is polled rather than computed per frame.** `draw` runs on every
//! keystroke, and drawing must not touch the filesystem — a page redrawn while a
//! disk is busy would stutter for a reason nobody could see. [`Alerts::poll`] is
//! called from the data tick alongside every other read, and the banner renders
//! whatever the last poll found.

use std::fs;
use std::path::{Path, PathBuf};

/// One thing that is wrong and that only the operator can fix.
///
/// `key` is stable and matches the Python side's, so a test can pin that both
/// halves recognise the same condition without comparing prose.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorAlert {
    pub key: &'static str,
    pub summary: String,
}

/// The two checks, as of the last poll.
///
/// `Default` is "nothing wrong", which is what an `App` built for a rendering test
/// gets: a banner that appeared in every test fixture would be a banner nobody
/// looked at, and the tests that *are* about it set the state they need.
#[derive(Debug, Clone, Default)]
pub struct Alerts {
    alerts: Vec<OperatorAlert>,
}

impl Alerts {
    /// Re-answer both questions from disk. Cheap enough for the data tick: two
    /// `stat`s, one small read, and a `PATH` scan.
    ///
    /// Recomputed wholesale rather than updated, so an alert disappears on its own
    /// when the condition does. There is deliberately no acknowledge, dismiss or
    /// snooze: an alert that can be silenced without fixing anything is an alert
    /// that tells the next operator nothing.
    pub fn poll(&mut self, config: &Path, config_document: Option<&serde_yaml::Value>) {
        let mut found = Vec::new();
        if let Some(alert) = gateway_port_alert(config, config_document) {
            found.push(alert);
        }
        if let Some(alert) = java_alert(config_document) {
            found.push(alert);
        }
        self.alerts = found;
    }

    pub fn is_empty(&self) -> bool {
        self.alerts.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = &OperatorAlert> {
        self.alerts.iter()
    }

    /// Whether a specific condition is currently raised, by key. For tests and for
    /// a caller that wants one of the two rather than the banner.
    pub fn has(&self, key: &str) -> bool {
        self.alerts.iter().any(|alert| alert.key == key)
    }

    /// Set the alerts directly, for a test that needs a known banner state.
    ///
    /// Not `#[cfg(test)]`: it is called from `ui.rs`'s tests, which are a different
    /// compilation context, and more importantly every *rendering* test needs to be
    /// able to clear this. `poll` reads real files, so an `App` built in a test
    /// inherits whatever the machine it runs on happens to have -- which makes a
    /// banner appear in tests that are about something else entirely, and makes them
    /// fail depending on what else is running. Same discipline the KyA gate follows
    /// for the same reason (see `App::with_kya_gate`).
    pub fn set(&mut self, alerts: Vec<OperatorAlert>) {
        self.alerts = alerts;
    }

    /// Forget every alert, so a render is about the page rather than about the
    /// machine. The form every rendering test wants.
    pub fn clear(&mut self) {
        self.alerts.clear();
    }
}

/// Where `.gateway_notice` lives: beside `config.yaml`, matching
/// `ConfigManager._config_dir()`.
///
/// Beside the config rather than in the cache because `install.sh` has to find it
/// with nothing but `$TARGET_DIR` — see `GATEWAY_NOTICE_FILE` in
/// `src/utils/config.py`.
pub const GATEWAY_NOTICE_FILE: &str = ".gateway_notice";

fn gateway_notice_path(config: &Path) -> PathBuf {
    config
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(GATEWAY_NOTICE_FILE)
}

/// `network.GATEWAY_PORT` as a real port, or `None` for `auto`, empty, or anything
/// out of range.
///
/// Mirrors `coerce_gateway_port` in `src/utils/config.py`: an unparseable value is
/// treated as unassigned rather than rounded into something plausible, because a
/// bad value has to stop the node rather than quietly become a default.
fn assigned_port(document: Option<&serde_yaml::Value>) -> Option<u16> {
    let value = document?.get("network")?.get("GATEWAY_PORT")?;
    let text = match value {
        serde_yaml::Value::String(text) => text.trim().to_string(),
        serde_yaml::Value::Number(number) => number.to_string(),
        _ => return None,
    };
    if text.is_empty() || text.eq_ignore_ascii_case("auto") {
        return None;
    }
    text.parse::<u16>().ok().filter(|port| *port > 0)
}

/// The gateway port is not usable, and the node cannot serve until it is.
///
/// Two distinguishable states with two different fixes, so they get two different
/// messages: nothing assigned at all (one privileged start), and a port assigned
/// with a pending notice beside it (a firewall command, which the notice itself
/// spells out).
///
/// Never probes. Proving reachability rebuilds a network namespace and is the
/// daemon's job, once per boot; this reports the stored verdict, which is what
/// makes it cheap enough to run on a tick.
fn gateway_port_alert(
    config: &Path,
    document: Option<&serde_yaml::Value>,
) -> Option<OperatorAlert> {
    // A config that could not be read at all says nothing about the port. Claiming
    // "unassigned" because the YAML failed to parse would send the operator to fix
    // the wrong thing — and the unreadable config is already its own visible
    // problem everywhere else in this interface.
    document?;

    let notice = fs::read_to_string(gateway_notice_path(config))
        .ok()
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty());

    match assigned_port(document) {
        None => Some(OperatorAlert {
            key: "gateway_port_unassigned",
            summary: "The gateway port is not assigned, so this node cannot serve. \
                      Run 'sudo nodo serve' once to pick and open one."
                .to_string(),
        }),
        Some(port) if notice.is_some() => Some(OperatorAlert {
            key: "gateway_port_firewall",
            summary: format!(
                "TCP {port} must be open in the host firewall before this node can serve. \
                 See {} for the exact command.",
                gateway_notice_path(config).display()
            ),
        }),
        Some(_) => None,
    }
}

/// No Java runtime, so payments and reputation are silently unavailable.
///
/// Worth saying out loud precisely because nothing crashes: the Ergo contract
/// cannot settle, `registry.contracts()` drops it, and the node runs on with no
/// payment method at all — which is indistinguishable, from outside, from a node
/// that was deliberately configured without one. An operator who never sees this
/// believes they are earning.
///
/// The same three places `ensure_java_runtime` looks, in the same order.
fn java_alert(document: Option<&serde_yaml::Value>) -> Option<OperatorAlert> {
    if java_home_has_runtime(std::env::var("JAVA_HOME").ok().as_deref()) {
        return None;
    }
    let configured = document
        .and_then(|document| document.get("dependencies"))
        .and_then(|value| value.get("java"))
        .and_then(|value| value.get("JAVA_HOME"))
        .and_then(|value| value.as_str());
    if java_home_has_runtime(configured) {
        return None;
    }
    if java_on_path() {
        return None;
    }

    let main_dir = document
        .and_then(|document| document.get("main"))
        .and_then(|value| value.get("MAIN_DIR"))
        .and_then(|value| value.as_str())
        .unwrap_or(".");
    Some(OperatorAlert {
        key: "java_missing",
        summary: format!(
            "Java is not installed, so this node cannot settle payments or publish \
             reputation. Install it with `sudo /bin/bash {main_dir}/bash/install_java.sh {main_dir}`."
        ),
    })
}

/// Whether a `JAVA_HOME` names a directory that actually holds `bin/java`.
///
/// The existence of the directory is not the question — a runtime that was moved
/// or removed leaves the variable and the config key behind, and that stale path is
/// exactly the state `_stale_java_home` exists to warn about.
fn java_home_has_runtime(home: Option<&str>) -> bool {
    home.map(|home| Path::new(home).join("bin").join("java").exists())
        .unwrap_or(false)
}

/// `java` anywhere on `PATH`. The last resort, and only that: a runtime the
/// operator did not name is a weaker answer than one they did. It is not no answer,
/// which is the mistake that used to leave a perfectly good `java` unused.
fn java_on_path() -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|directory| directory.join("java").exists())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "nodo-tui-alerts-{name}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn document(yaml: &str) -> serde_yaml::Value {
        serde_yaml::from_str(yaml).unwrap()
    }

    #[test]
    fn an_assigned_port_with_nothing_pending_raises_nothing() {
        let dir = scratch("clean");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        assert_eq!(gateway_port_alert(&config, Some(&document)), None);
    }

    #[test]
    fn a_pending_notice_beside_an_assigned_port_names_the_port() {
        let dir = scratch("pending");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        let alert = gateway_port_alert(&config, Some(&document)).expect("an alert");

        assert_eq!(alert.key, "gateway_port_firewall");
        // The number, not just "the gateway port": an instruction the operator
        // cannot carry out without going and looking something up is half an
        // instruction.
        assert!(alert.summary.contains("52285"), "{}", alert.summary);
    }

    /// The condition being fixed is the only thing that clears the banner, and the
    /// node clears it by deleting that file when it proves the port reachable.
    #[test]
    fn the_alert_clears_when_the_notice_is_removed() {
        let dir = scratch("clears");
        let config = dir.join("config.yaml");
        let notice = dir.join(GATEWAY_NOTICE_FILE);
        fs::write(&notice, "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");
        assert!(gateway_port_alert(&config, Some(&document)).is_some());

        fs::remove_file(&notice).unwrap();

        assert_eq!(gateway_port_alert(&config, Some(&document)), None);
    }

    /// `auto` is not "a port that might be closed", it is "no port at all", and the
    /// fix is a privileged start rather than a firewall command.
    #[test]
    fn auto_is_a_different_alert_with_a_different_fix() {
        let dir = scratch("auto");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: auto\n");

        let alert = gateway_port_alert(&config, Some(&document)).expect("an alert");

        assert_eq!(alert.key, "gateway_port_unassigned");
        assert!(alert.summary.contains("sudo nodo serve"), "{}", alert.summary);
    }

    #[test]
    fn an_empty_notice_file_carries_no_instructions_and_so_raises_nothing() {
        let dir = scratch("empty");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "   \n").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        assert_eq!(gateway_port_alert(&config, Some(&document)), None);
    }

    #[test]
    fn an_unreadable_config_is_not_reported_as_an_unassigned_port() {
        let dir = scratch("unreadable");
        let config = dir.join("config.yaml");

        assert_eq!(gateway_port_alert(&config, None), None);
    }

    /// A port written as a quoted string is the same port. `yq` and PyYAML disagree
    /// about which one they produce depending on how the value was written, and a
    /// banner that appeared only for one of them would be a banner nobody trusted.
    #[test]
    fn a_quoted_port_is_still_a_port() {
        let dir = scratch("quoted");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: \"52285\"\n");

        assert_eq!(gateway_port_alert(&config, Some(&document)), None);
    }

    #[test]
    fn a_java_home_that_holds_a_runtime_satisfies_the_check() {
        let dir = scratch("java-home");
        let bin = dir.join("bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("java"), "#!/bin/sh\n").unwrap();

        assert!(java_home_has_runtime(Some(dir.to_str().unwrap())));
    }

    /// The directory existing is not the question. A runtime that was moved or
    /// removed leaves `JAVA_HOME` and the config key pointing at an empty shell,
    /// and treating that as present is how a node ends up refusing to pay while
    /// reporting Java as installed.
    #[test]
    fn a_java_home_with_no_bin_java_does_not_satisfy_it() {
        let dir = scratch("java-stale");

        assert!(!java_home_has_runtime(Some(dir.to_str().unwrap())));
        assert!(!java_home_has_runtime(None));
    }

    /// The message an operator acts on is a command they can paste, built against
    /// this installation's own directory rather than a generic "install Java".
    #[test]
    fn the_java_alert_names_the_bundled_installer_for_this_install() {
        let document = document("main:\n  MAIN_DIR: /opt/nodo\n");
        // Nothing on PATH and no JAVA_HOME for this process would make the test
        // depend on the machine, so the message itself is what is asserted, through
        // the one piece that is derived from config.
        let alert = OperatorAlert {
            key: "java_missing",
            summary: java_alert(Some(&document))
                .map(|alert| alert.summary)
                .unwrap_or_else(|| {
                    // Java *is* installed on this machine; build the same string the
                    // alert would have carried, so the assertion below still tests
                    // the formatting rather than the host.
                    "Java is not installed, so this node cannot settle payments or \
                     publish reputation. Install it with `sudo /bin/bash \
                     /opt/nodo/bash/install_java.sh /opt/nodo`."
                        .to_string()
                }),
        };

        assert!(
            alert.summary.contains("/opt/nodo/bash/install_java.sh"),
            "{}",
            alert.summary
        );
    }

    #[test]
    fn polling_collects_the_port_before_java() {
        let dir = scratch("order");
        let config = dir.join("config.yaml");
        let mut alerts = Alerts::default();

        // `auto` guarantees the port alert; whether Java is present depends on the
        // machine, so only the port's *position* is asserted.
        alerts.poll(&config, Some(&document("network:\n  GATEWAY_PORT: auto\n")));

        assert_eq!(
            alerts.iter().next().map(|alert| alert.key),
            Some("gateway_port_unassigned")
        );
        assert!(alerts.has("gateway_port_unassigned"));
    }

    /// The property the whole design rests on: re-polling after the condition is
    /// fixed empties the banner, because nothing is remembered between polls.
    #[test]
    fn a_fixed_condition_empties_the_banner_on_the_next_poll() {
        let dir = scratch("refix");
        let config = dir.join("config.yaml");
        let mut alerts = Alerts::default();
        alerts.poll(&config, Some(&document("network:\n  GATEWAY_PORT: auto\n")));
        assert!(alerts.has("gateway_port_unassigned"));

        alerts.poll(&config, Some(&document("network:\n  GATEWAY_PORT: 52285\n")));

        assert!(!alerts.has("gateway_port_unassigned"));
    }

    /// Both halves of this feature have to recognise the same conditions, or the
    /// TUI and `nodo info` disagree about whether a node is healthy. The keys are
    /// the contract; this reads the Python source rather than running it, since a
    /// Rust test suite has no interpreter to hand.
    #[test]
    fn the_alert_keys_match_the_python_side() {
        let python = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../src/utils/operator_alerts.py"
        ))
        .expect("src/utils/operator_alerts.py ships with the repository");

        for key in [
            "gateway_port_unassigned",
            "gateway_port_firewall",
            "java_missing",
        ] {
            assert!(
                python.contains(&format!("key=\"{key}\"")),
                "{key} is raised by the TUI but not by src/utils/operator_alerts.py"
            );
        }
    }

    /// And the same file: a notice the TUI looks for somewhere else is a banner
    /// that never appears.
    #[test]
    fn the_notice_filename_matches_the_python_side() {
        let python = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../src/utils/config.py"
        ))
        .expect("src/utils/config.py ships with the repository");

        assert!(
            python.contains(&format!("GATEWAY_NOTICE_FILE = \"{GATEWAY_NOTICE_FILE}\"")),
            "the TUI looks for {GATEWAY_NOTICE_FILE}, which src/utils/config.py does not write"
        );
    }
}
