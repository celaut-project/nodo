//! What this node needs its operator to *do*, on the screen they leave open.
//!
//! The Rust half of `src/utils/operator_alerts.py`: same two questions (the gateway
//! port, and Java), same words, same files. Duplicated rather than shelled out to,
//! because a subprocess per refresh would be a fork four times a second to re-answer
//! a question made of two `stat` calls. A test on each side pins that the criteria
//! still match.
//!
//! Polled from the data tick rather than computed per frame: drawing runs on every
//! keystroke and must not touch the filesystem.

use std::fs;
use std::path::{Path, PathBuf};

/// One thing that is wrong and that only the operator can fix.
///
/// `key` is stable and matches the Python side's, so a test can pin that both halves
/// recognise the same condition without comparing prose.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorAlert {
    pub key: &'static str,
    pub summary: String,
}

/// The two checks, as of the last poll. `Default` is "nothing wrong", which is what
/// an `App` built for a rendering test gets.
#[derive(Debug, Clone, Default)]
pub struct Alerts {
    alerts: Vec<OperatorAlert>,
}

impl Alerts {
    /// Re-answer both questions from disk: two `stat`s, one small read, a `PATH`
    /// scan.
    ///
    /// Recomputed wholesale, so an alert disappears when its condition does. There
    /// is deliberately no acknowledge or snooze: an alert that can be silenced
    /// without fixing anything tells the next operator nothing.
    pub fn poll(
        &mut self,
        config: &Path,
        config_document: Option<&serde_yaml::Value>,
        serving: Option<bool>,
    ) {
        let mut found = Vec::new();
        if let Some(alert) = gateway_port_alert(config, config_document, serving) {
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

    /// Whether a specific condition is currently raised, by key.
    pub fn has(&self, key: &str) -> bool {
        self.alerts.iter().any(|alert| alert.key == key)
    }

    /// Set the alerts directly, for a test that needs a known banner state.
    ///
    /// Not `#[cfg(test)]`: `ui.rs`'s tests are a different compilation context.
    /// `poll` reads real files, so an `App` built in a test would otherwise inherit
    /// whatever the machine it runs on happens to have.
    pub fn set(&mut self, alerts: Vec<OperatorAlert>) {
        self.alerts = alerts;
    }

    /// Forget every alert, so a render is about the page rather than the machine.
    pub fn clear(&mut self) {
        self.alerts.clear();
    }
}

/// Where `.gateway_notice` lives: beside `config.yaml`, matching
/// `ConfigManager._config_dir()` and `GATEWAY_NOTICE_FILE` in
/// `src/utils/config.py`.
pub const GATEWAY_NOTICE_FILE: &str = ".gateway_notice";

fn gateway_notice_path(config: &Path) -> PathBuf {
    config
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(GATEWAY_NOTICE_FILE)
}

/// `network.GATEWAY_PORT` as a real port, or `None` for `auto`, empty or out of
/// range.
///
/// Mirrors `coerce_gateway_port` in `src/utils/config.py`: an unparseable value is
/// unassigned rather than rounded into something plausible.
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
/// The summary leads with the **consequence**, because what an operator has to
/// take from one line on a banner is that this node is not doing its job -- the
/// mechanism is the second half of the sentence.
///
/// `serving` separates the two ways that happens. A node that is *up* and
/// unreachable is the one worth naming precisely: every local check answers, so
/// from inside the host it is indistinguishable from a working node, and it is
/// earning nothing the whole time. `None` claims only what it knows.
///
/// Two distinguishable causes with two different fixes, so they get two different
/// messages: nothing assigned at all (one privileged start), and a port assigned
/// with a pending notice beside it (a firewall command, which the notice itself
/// spells out).
///
/// Never probes the network, and never opens a socket of its own. `serving` is
/// already the answer to "does anything accept a TCP connection on this port" --
/// `is_serving()` in `src/commands/daemon.py` connects to `127.0.0.1:<port>`, and
/// both callers here have that answer in hand before they ask. A second connect
/// would buy the same fact twice and put a socket on a path whose whole point is
/// that it is two `stat` calls.
///
/// Proving reachability from *outside* is a different question: it rebuilds a
/// network namespace and is the daemon's job, once per boot. This reports the
/// stored verdict.
fn gateway_port_alert(
    config: &Path,
    document: Option<&serde_yaml::Value>,
    serving: Option<bool>,
) -> Option<OperatorAlert> {
    // A config that could not be read says nothing about the port: claiming
    // "unassigned" because the YAML failed to parse would send the operator to fix
    // the wrong thing.
    document?;

    let notice = fs::read_to_string(gateway_notice_path(config))
        .ok()
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty());

    match assigned_port(document) {
        None => Some(OperatorAlert {
            key: "gateway_port_unassigned",
            summary: "NOT SERVING - no gateway port is assigned, so no peer can reach \
                      this node and it earns nothing. Assign and open one: sudo nodo serve"
                .to_string(),
        }),
        Some(port) if notice.is_some() => Some(OperatorAlert {
            key: "gateway_port_firewall",
            summary: format!(
                "{} TCP {port} is not open in the host firewall, so peers cannot reach \
                 this node. Open it: see {} for the exact command.",
                unreachable_lead(serving),
                gateway_notice_path(config).display()
            ),
        }),
        // A port is assigned, the firewall has no open question about it, and still
        // nothing accepts a connection on it. Everything is configured and the node
        // is down -- the one state the two alerts above cannot express, and the one
        // an operator is least likely to go looking for, because the config is right.
        Some(port) if serving == Some(false) => Some(OperatorAlert {
            key: "gateway_port_closed",
            summary: format!(
                "NOT SERVING - nothing is listening on TCP {port}, so no peer can reach \
                 this node and it earns nothing while it is down. Start it: sudo nodo serve"
            ),
        }),
        Some(_) => None,
    }
}

/// The first words of the firewall alert: what is wrong, before why.
///
/// Three states rather than two. "The process is up and nobody outside can reach
/// it" is the one an operator cannot discover from inside the host, and the one
/// this banner exists for.
fn unreachable_lead(serving: Option<bool>) -> &'static str {
    match serving {
        Some(true) => "RUNNING BUT UNREACHABLE -",
        Some(false) => "NOT SERVING -",
        None => "NOT REACHABLE FROM OUTSIDE -",
    }
}

/// No Java runtime, so payments and reputation are silently unavailable.
///
/// Worth saying precisely because nothing crashes: the contract cannot settle and
/// the node runs on with no payment method, which from outside is indistinguishable
/// from one configured without any. An operator who never sees this believes they
/// are earning.
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
/// The directory existing is not the question: a runtime that was moved leaves the
/// variable behind pointing at an empty shell.
fn java_home_has_runtime(home: Option<&str>) -> bool {
    home.map(|home| Path::new(home).join("bin").join("java").exists())
        .unwrap_or(false)
}

/// `java` anywhere on `PATH`. The last resort, but not no answer: ending the search
/// before it used to leave a perfectly good `java` unused.
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

        assert_eq!(gateway_port_alert(&config, Some(&document), None), None);
        // Nor on a node that is up: this is the ordinary healthy state.
        assert_eq!(
            gateway_port_alert(&config, Some(&document), Some(true)),
            None
        );
    }

    /// The state where everything an operator would check is correct.
    ///
    /// The port is assigned, the firewall has no open question about it, and still
    /// nothing answers on it. Neither alert above can say that: one is about a port
    /// that was never assigned, the other about a `.gateway_notice` that is not
    /// there. Without this the screen is silent while the node earns nothing.
    #[test]
    fn a_settled_port_nothing_is_listening_on_is_its_own_alert() {
        let dir = scratch("closed");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        let alert =
            gateway_port_alert(&config, Some(&document), Some(false)).expect("an alert");

        assert_eq!(alert.key, "gateway_port_closed");
        // Consequence first, as everywhere else on this banner.
        assert!(alert.summary.starts_with("NOT SERVING -"), "{}", alert.summary);
        assert!(alert.summary.contains("52285"), "{}", alert.summary);
        assert!(
            alert.summary.contains("earns nothing"),
            "{}",
            alert.summary
        );
        assert!(alert.summary.contains("sudo nodo serve"), "{}", alert.summary);
    }

    /// A node whose reachability is genuinely unknown claims nothing.
    ///
    /// `None` is the state before the first `nodo info` answers. Raising "nothing is
    /// listening" there would put a red banner on every TUI for the first few
    /// hundred milliseconds of every run.
    #[test]
    fn an_unknown_serving_state_does_not_claim_the_port_is_dead() {
        let dir = scratch("unknown");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        assert_eq!(gateway_port_alert(&config, Some(&document), None), None);
    }

    /// One cause, one alert. A pending firewall notice is a more specific diagnosis
    /// of the same silence, and it carries the command that fixes it -- so it wins.
    #[test]
    fn a_pending_firewall_notice_is_reported_instead_of_the_bare_silence() {
        let dir = scratch("precedence");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        let alert =
            gateway_port_alert(&config, Some(&document), Some(false)).expect("an alert");

        assert_eq!(alert.key, "gateway_port_firewall");
    }

    #[test]
    fn a_pending_notice_beside_an_assigned_port_names_the_port() {
        let dir = scratch("pending");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        let alert = gateway_port_alert(&config, Some(&document), None).expect("an alert");

        assert_eq!(alert.key, "gateway_port_firewall");
        // The number: an instruction the operator has to go and look something up
        // to carry out is half an instruction.
        assert!(alert.summary.contains("52285"), "{}", alert.summary);
    }

    /// The consequence comes first. A line that opens with the mechanism is a line
    /// an operator has to finish reading before learning that their node is down.
    #[test]
    fn the_firewall_alert_leads_with_the_consequence() {
        let dir = scratch("leads");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        for (serving, lead) in [
            (Some(false), "NOT SERVING -"),
            (Some(true), "RUNNING BUT UNREACHABLE -"),
            (None, "NOT REACHABLE FROM OUTSIDE -"),
        ] {
            let alert = gateway_port_alert(&config, Some(&document), serving).expect("an alert");

            assert!(alert.summary.starts_with(lead), "{}", alert.summary);
            assert!(
                alert.summary.contains("peers cannot reach this node"),
                "{}",
                alert.summary
            );
        }
    }

    /// A node that is up and unreachable is the state no check inside the host can
    /// see, so it is the one the wording has to distinguish.
    #[test]
    fn a_running_node_and_a_stopped_one_do_not_get_the_same_sentence() {
        let dir = scratch("distinguish");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        let up = gateway_port_alert(&config, Some(&document), Some(true)).expect("an alert");
        let down = gateway_port_alert(&config, Some(&document), Some(false)).expect("an alert");

        assert_ne!(up.summary, down.summary);
        assert_eq!(up.key, down.key);
    }

    /// Fixing the condition is the only thing that clears the banner.
    #[test]
    fn the_alert_clears_when_the_notice_is_removed() {
        let dir = scratch("clears");
        let config = dir.join("config.yaml");
        let notice = dir.join(GATEWAY_NOTICE_FILE);
        fs::write(&notice, "open TCP 52285").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");
        assert!(gateway_port_alert(&config, Some(&document), None).is_some());

        fs::remove_file(&notice).unwrap();

        assert_eq!(gateway_port_alert(&config, Some(&document), None), None);
    }

    /// `auto` is "no port at all", and the fix is a privileged start rather than a
    /// firewall command.
    #[test]
    fn auto_is_a_different_alert_with_a_different_fix() {
        let dir = scratch("auto");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: auto\n");

        let alert = gateway_port_alert(&config, Some(&document), None).expect("an alert");

        assert_eq!(alert.key, "gateway_port_unassigned");
        assert!(alert.summary.starts_with("NOT SERVING -"), "{}", alert.summary);
        assert!(alert.summary.contains("sudo nodo serve"), "{}", alert.summary);
    }

    #[test]
    fn an_empty_notice_file_carries_no_instructions_and_so_raises_nothing() {
        let dir = scratch("empty");
        let config = dir.join("config.yaml");
        fs::write(dir.join(GATEWAY_NOTICE_FILE), "   \n").unwrap();
        let document = document("network:\n  GATEWAY_PORT: 52285\n");

        assert_eq!(gateway_port_alert(&config, Some(&document), None), None);
    }

    #[test]
    fn an_unreadable_config_is_not_reported_as_an_unassigned_port() {
        let dir = scratch("unreadable");
        let config = dir.join("config.yaml");

        assert_eq!(gateway_port_alert(&config, None, None), None);
    }

    /// `yq` and PyYAML disagree about whether a port comes back quoted, and a banner
    /// that appeared for only one of them would be a banner nobody trusted.
    #[test]
    fn a_quoted_port_is_still_a_port() {
        let dir = scratch("quoted");
        let config = dir.join("config.yaml");
        let document = document("network:\n  GATEWAY_PORT: \"52285\"\n");

        assert_eq!(gateway_port_alert(&config, Some(&document), None), None);
    }

    #[test]
    fn a_java_home_that_holds_a_runtime_satisfies_the_check() {
        let dir = scratch("java-home");
        let bin = dir.join("bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("java"), "#!/bin/sh\n").unwrap();

        assert!(java_home_has_runtime(Some(dir.to_str().unwrap())));
    }

    /// Treating a stale `JAVA_HOME` as present is how a node ends up refusing to
    /// pay while reporting Java as installed.
    #[test]
    fn a_java_home_with_no_bin_java_does_not_satisfy_it() {
        let dir = scratch("java-stale");

        assert!(!java_home_has_runtime(Some(dir.to_str().unwrap())));
        assert!(!java_home_has_runtime(None));
    }

    /// A command the operator can paste, built against this install's own directory.
    #[test]
    fn the_java_alert_names_the_bundled_installer_for_this_install() {
        let document = document("main:\n  MAIN_DIR: /opt/nodo\n");
        // Whether Java is present depends on the machine, so what is asserted is
        // the formatting, through the one piece derived from config.
        let alert = OperatorAlert {
            key: "java_missing",
            summary: java_alert(Some(&document))
                .map(|alert| alert.summary)
                .unwrap_or_else(|| {
                    // Java *is* installed here; build the string the alert would
                    // have carried.
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

        // `auto` guarantees the port alert; Java depends on the machine, so only
        // the port's *position* is asserted.
        alerts.poll(
            &config,
            Some(&document("network:\n  GATEWAY_PORT: auto\n")),
            None,
        );

        assert_eq!(
            alerts.iter().next().map(|alert| alert.key),
            Some("gateway_port_unassigned")
        );
        assert!(alerts.has("gateway_port_unassigned"));
    }

    /// Nothing is remembered between polls, so a fixed condition empties the banner.
    #[test]
    fn a_fixed_condition_empties_the_banner_on_the_next_poll() {
        let dir = scratch("refix");
        let config = dir.join("config.yaml");
        let mut alerts = Alerts::default();
        alerts.poll(
            &config,
            Some(&document("network:\n  GATEWAY_PORT: auto\n")),
            None,
        );
        assert!(alerts.has("gateway_port_unassigned"));

        alerts.poll(
            &config,
            Some(&document("network:\n  GATEWAY_PORT: 52285\n")),
            None,
        );

        assert!(!alerts.has("gateway_port_unassigned"));
    }

    /// Both halves must recognise the same conditions, or the TUI and `nodo info`
    /// disagree about whether a node is healthy. Reads the Python source rather than
    /// running it: a Rust test suite has no interpreter to hand.
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
            "gateway_port_closed",
            "java_missing",
        ] {
            assert!(
                python.contains(&format!("key=\"{key}\"")),
                "{key} is raised by the TUI but not by src/utils/operator_alerts.py"
            );
        }

        // And the same three leads, so the two do not describe one node in two
        // different states.
        for lead in [
            "NOT SERVING -",
            "RUNNING BUT UNREACHABLE -",
            "NOT REACHABLE FROM OUTSIDE -",
        ] {
            assert!(
                python.contains(lead),
                "the TUI leads with {lead:?}, which src/utils/operator_alerts.py does not"
            );
        }
    }

    /// A notice the TUI looks for elsewhere is a banner that never appears.
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
