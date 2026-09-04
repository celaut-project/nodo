use crate::cell::{self, Lever, LeverKind, LeverStatus, Organelle};
use prost::Message;
use ratatui::layout::{Position, Rect};
use ratatui::widgets::TableState;
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, Result as SqlResult};
use serde_yaml::Value;
use sha1::{Digest, Sha1};
use std::collections::{HashMap, VecDeque};
use std::error;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use sysinfo::{Disks, System};
use tokio::process::Command;
use tokio::task::JoinHandle;
use tui_tree_widget::TreeState;

/// Application result type.
pub type AppResult<T> = std::result::Result<T, Box<dyn error::Error>>;

pub const HISTORY_POINTS: usize = 120;
const DATA_REFRESH_INTERVAL: Duration = Duration::from_secs(2);
const WALLET_REFRESH_INTERVAL: Duration = Duration::from_secs(60);
/// Shortest gap between two counter samples that yields a meaningful rate. The
/// ordinary sweep is `DATA_REFRESH_INTERVAL` apart, but a forced refresh (after a
/// kill, or an `r` keypress) can land immediately after one; dividing a counter
/// delta by a few milliseconds of wall time produces noise, not a measurement.
const MIN_RATE_INTERVAL: Duration = Duration::from_millis(500);

pub mod protos {
    include!(concat!("protos", "/celaut.rs"));
}

pub trait Identifiable {
    fn id(&self) -> &str;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Page {
    Overview,
    Instances,
    Services,
    /// Peers we talk to, and what we have paid them.
    Peers,
    /// Clients that talk to us, and what they have paid.
    Clients,
    /// The policy panel: what this node lets in, sells, trusts and says, as a set
    /// of named decisions rather than as a YAML tree.
    Cell,
    Pricing,
    Config,
    Logs,
}

impl Page {
    pub const ALL: [Page; 9] = [
        Page::Overview,
        Page::Instances,
        Page::Services,
        Page::Peers,
        Page::Clients,
        // The three editors sit together and run from the most general to the most
        // specific: postures, then prices, then single keys.
        Page::Cell,
        Page::Pricing,
        Page::Config,
        Page::Logs,
    ];

    pub fn title(self) -> &'static str {
        match self {
            Page::Overview => "OVERVIEW",
            Page::Instances => "INSTANCES",
            Page::Services => "SERVICES",
            Page::Peers => "PEERS",
            Page::Clients => "CLIENTS",
            Page::Cell => "CELL",
            Page::Pricing => "PRICING",
            Page::Config => "CONFIG",
            Page::Logs => "LOGS",
        }
    }
}

/// Which tab covers column `x`, given the bordered block the tab bar was drawn in.
///
/// Retraces what `Tabs` lays out rather than asking it: the widget keeps no hit map.
/// Each title sits in `padding_left + title + padding_right` (one space each side, the
/// default this TUI keeps) and tabs are joined by a 3-cell `" │ "` divider.
fn tab_at(x: u16, area: Rect) -> Option<usize> {
    const DIVIDER_WIDTH: u16 = 3;
    let mut cursor = area.x + 1; // the block's left border
    for (index, page) in Page::ALL.iter().enumerate() {
        let width = page.title().chars().count() as u16 + 2; // one space of padding each side
        if x >= cursor && x < cursor + width {
            return Some(index);
        }
        cursor += width + DIVIDER_WIDTH;
    }
    None
}

/// How many rows below the first visible one a click at terminal row `y` lands, for a
/// table drawn in `area`. `None` when the click was on the border, the header, or
/// outside the table entirely.
///
/// The three-row lead-in is the block's top border, the header, and the blank line
/// `header_row` puts under it (`bottom_margin(1)`) — pinned by
/// `mouse_clicks::a_click_lands_on_the_row_under_the_pointer`, which reads it off a
/// real render rather than trusting this arithmetic.
fn visible_row_at(y: u16, area: Rect) -> Option<usize> {
    const HEADER_ROWS: u16 = 3;
    let first_row = area.y + HEADER_ROWS;
    let last_row = area.y + area.height.checked_sub(1)?; // bottom border
    (area.height > HEADER_ROWS && y >= first_row && y < last_row).then(|| (y - first_row) as usize)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputMode {
    Normal,
    Connect,
    EditConfig,
    /// A new element for the list the Config page's selection points at.
    AddConfigItem,
    FilterConfig,
    /// Amount entry for crediting/debiting the selected client's balance.
    CreditClient,
    /// Yes/no confirmation before a destructive action (delete service, kill instance).
    Confirm,
    /// Read-only, scrollable overlay (e.g. `nodo inspect` output).
    Details,
    /// Profile picker on the CELL page: choose a posture, then confirm its diff.
    PickProfile,
    /// Confirmation showing every key a lever or profile would change, before any
    /// of them is written. A posture is a dozen keys, and writing them without
    /// showing them is the failure this page exists to prevent.
    ConfirmWrites,
}

/// How the `EditConfig` popup should let the user set a value, chosen from the
/// entry's inferred YAML type (and, for a handful of known keys, a fixed set of
/// accepted values) rather than always falling back to freeform text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EditKind {
    /// Freeform YAML literal (the original behaviour) — strings, secrets, lists,
    /// objects, and anything else with no more specific editor.
    Text,
    /// A checkbox: Space/←/→ toggles, no character input.
    Bool,
    /// A number: ↑/↓ steps by 1 on top of ordinary typing.
    Number,
    /// A closed set of accepted values (e.g. `network.DELEGATION_TUNNEL_POLICY`):
    /// ↑/↓ cycles through them on top of ordinary typing.
    Enum(Vec<String>),
}

/// Fixed value sets for config keys whose comment in `config.example.yaml`
/// documents a closed set of options. Deliberately small and explicit --
/// listing a value here is a claim that it is a real, working setting.
///
/// This does not have to be the *complete* set a key accepts: `Enum` only adds
/// an ↑/↓ cycle-through-these on top of ordinary typing (see `EditKind::Enum`
/// and `adjust_edit_value`), and `save_config_edit` validates the typed value
/// as YAML, never against this list. So `hashing.HASH` belongs here even
/// though it also accepts an arbitrary hex hash-id: the picker offers the
/// four canonical algorithm names (`src/utils/hashing.py`'s `HASH_SPECS`) as a
/// fast path, and typing a hex id past it still works exactly as before.
fn known_enum_values(path: &str) -> Option<&'static [&'static str]> {
    match path {
        "network.DELEGATION_TUNNEL_POLICY" => Some(&["auto", "always", "never"]),
        "hashing.HASH" => Some(&["sha2_256", "sha3_256", "shake_256", "blake2b_256"]),
        _ => None,
    }
}

/// A destructive action awaiting user confirmation.
#[derive(Debug, Clone)]
pub enum PendingAction {
    DeleteService { id: String, label: String },
    KillInstance { id: String, label: String },
    DisconnectPeer { id: String, label: String },
    /// Remove one element from a list in config.yaml. Confirmed like the others
    /// because dropping an entry from, say, a network policy loosens it silently.
    DeleteConfigItem {
        path: Vec<ConfigPathSegment>,
        label: String,
    },
    /// Apply a set of config keys — one cell lever, or a whole profile — after the
    /// operator has seen every key it changes.
    ApplyWrites {
        label: String,
        writes: Vec<(String, String)>,
    },
}

/// The `nodo` invocation a confirmed [`PendingAction`] turns into, plus the label its
/// outcome is reported under. Every destructive action that has a CLI equivalent goes
/// through the same CLI the operator would type, so the TUI can never do something
/// `nodo` cannot.
///
/// `None` for the one action that has no such equivalent: no `nodo` subcommand edits a
/// single config key, which is why the config editor writes through `yq` at all, so
/// removing a list element takes that same path (`delete_config_list_item`).
fn pending_command(action: PendingAction) -> Option<(String, Vec<String>)> {
    match action {
        PendingAction::DeleteService { id, label } => Some((
            format!("Delete service {label}"),
            vec!["remove".to_string(), id],
        )),
        PendingAction::KillInstance { id, label } => {
            Some((format!("Kill instance {label}"), vec!["kill".to_string(), id]))
        }
        PendingAction::DisconnectPeer { id, label } => Some((
            format!("Forget peer {label}"),
            vec!["disconnect".to_string(), id],
        )),
        PendingAction::DeleteConfigItem { .. } => None,
        PendingAction::ApplyWrites { .. } => None,
    }
}

/// One configuration change, as the transaction that applies it.
///
/// A change to config.yaml and the restart that makes the node read it are a single
/// step here, and the file is put back if that restart does not happen. Before this,
/// a write landed on disk and the operator was told to restart -- which left the
/// node running settings that were no longer the settings on disk, for as long as it
/// took someone to act on a status line. Every editor in this TUI now goes through
/// the same transaction: raw config, prices, and cell levers alike.
#[derive(Debug, Clone)]
struct ConfigWrite {
    /// What the status line calls this change.
    label: String,
    /// The `yq` expression to apply in place. Several key assignments may be
    /// chained with `|`, which is what makes a lever or a profile one write.
    expression: String,
    /// Values handed to `yq` through the environment, never interpolated into the
    /// expression, so nothing an operator types can be read as yq syntax. `env()`
    /// (not `strenv()`) parses each one, which is what keeps a number a number.
    values: Vec<(String, String)>,
    follow_up: ConfigFollowUp,
}

/// What the interface has to do once a transaction lands, beyond the reload every
/// one of them does.
#[derive(Debug, Clone)]
enum ConfigFollowUp {
    None,
    /// Open a config-tree branch: a list that was empty was a leaf and became a
    /// section, so an element appended to it would be written but invisible.
    OpenBranch(Vec<String>),
    /// Move the config-tree selection: every index after a removed element shifts
    /// down, so the selection cannot stay on a position that now holds something
    /// else.
    SelectNode(Vec<String>),
}

/// How far a configuration change got.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Applied {
    /// Written, and the node restarted and came back serving with it.
    Restarted,
    /// Written while nothing was serving on the gateway port. There is no running
    /// node to disagree with the file, so the change stands and the next start
    /// reads it.
    NotRunning,
}

/// Result of a configuration transaction.
#[derive(Debug)]
struct ConfigTransaction {
    label: String,
    result: Result<Applied, String>,
}

/// One `yq` expression that assigns every key in `writes`, and the environment
/// each value travels in.
///
/// Chained with `|` so a whole posture is one in-place edit: one backup, one
/// restart, and no window in which the file holds half a profile. Each value gets
/// its own variable because they are passed through the environment rather than
/// interpolated into the expression -- nothing an operator types can be read as yq
/// syntax, and `env()` (not `strenv()`) parses it, so a number stays a number and a
/// list stays a list.
fn chained_write(writes: &[(String, String)]) -> (String, Vec<(String, String)>) {
    let mut expressions: Vec<String> = Vec::with_capacity(writes.len());
    let mut values: Vec<(String, String)> = Vec::with_capacity(writes.len());
    for (index, (path, value)) in writes.iter().enumerate() {
        let variable = format!("NODO_TUI_V{index}");
        let segments = crate::cell::path_segments(path);
        expressions.push(format!(
            "{} = env({variable})",
            yq_path_expression(&segments)
        ));
        values.push((variable, value.clone()));
    }
    (expressions.join(" | "), values)
}

/// Longest a `nodo daemon restart` may take before it is called failed.
const RESTART_TIMEOUT: Duration = Duration::from_secs(180);
/// Longest to wait for the node to accept connections again after a restart.
/// Cloud Hypervisor assets, the database migration and the gateway reachability
/// probe all happen before the port opens, so this is generous on purpose.
const NODE_READY_TIMEOUT: Duration = Duration::from_secs(120);
/// Gap between two checks of the gateway port while waiting for the node.
const NODE_READY_POLL: Duration = Duration::from_millis(500);

/// Back up config.yaml, apply the change, restart the node, and restore the backup
/// if the node does not come back.
///
/// The invariant: **what the file says is what the running node loaded.** A change
/// that cannot be restarted into is not a change, so it is undone rather than left
/// on disk. The operator is never handed a node whose behaviour and configuration
/// disagree, and never has to remember that they still owe it a restart.
///
/// Also the single place that invalidates the gateway-port verdict. The node records
/// "this port was proven reachable" in `<CACHE>/gateway_port_passed`
/// (`src/utils/config.py`) and skips its startup probe while that holds; a port
/// edited here has never been proven, so the file has to go or the next start would
/// serve on an unchecked port. Compared before and after rather than matched against
/// the expression, because an expression that touches the key can be shaped many
/// ways and only the value actually matters.
async fn apply_config_change(
    yq: PathBuf,
    config: PathBuf,
    cache: PathBuf,
    write: ConfigWrite,
) -> ConfigTransaction {
    let label = write.label.clone();
    let fail = |error: String| ConfigTransaction {
        label: label.clone(),
        result: Err(error),
    };

    // Asked before the write, because the change may be to the port itself: what
    // decides whether a restart is owed is whether a node is serving now.
    let port_before = read_gateway_port(&config);
    let was_serving = serving_on(port_before.as_deref()).await;

    let backup = match backup_config(&config) {
        Ok(backup) => backup,
        Err(error) => return fail(format!("Could not create config backup: {error}")),
    };

    let mut command = Command::new(&yq);
    command
        .arg("e")
        .arg("-i")
        .arg(&write.expression)
        .arg(&config);
    for (variable, value) in &write.values {
        command.env(variable, value);
    }
    match command.output().await {
        Ok(output) if output.status.success() => {}
        Ok(output) => {
            return fail(format!(
                "yq could not update configuration: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ))
        }
        Err(error) => return fail(format!("Could not run {}: {error}", yq.display())),
    }

    let port_after = read_gateway_port(&config);
    if port_after != port_before {
        let _ = fs::remove_file(cache.join("gateway_port_passed"));
    }

    if !was_serving {
        return ConfigTransaction {
            label,
            result: Ok(Applied::NotRunning),
        };
    }

    match restart_node().await {
        Ok(()) => {}
        Err(error) => {
            return fail(revert(&backup, &config, &format!("{label} NOT applied: {error}")));
        }
    }

    // A restart command that returned successfully is not yet a node that serves:
    // systemd reports the unit started, and the process still has its assets,
    // migrations and reachability probe ahead of it. The port answering is the only
    // evidence that the new configuration was actually loadable.
    if !wait_until_serving(port_after.as_deref()).await {
        let message = revert(
            &backup,
            &config,
            &format!(
                "{label} NOT applied: nodo did not come back within {}s",
                NODE_READY_TIMEOUT.as_secs()
            ),
        );
        // Best effort: bring the node back up on the configuration that was working
        // before, so a failed change costs the operator a change and not their node.
        let _ = restart_node().await;
        return fail(message);
    }

    ConfigTransaction {
        label,
        result: Ok(Applied::Restarted),
    }
}

/// Put `backup` back over `config`, and say what state that leaves things in.
///
/// The backup is removed once it has been restored: it is byte-identical to the
/// file beside it and records no change that was ever kept. A backup that could NOT
/// be restored is kept and named, because it is then the only copy of the working
/// configuration.
fn revert(backup: &Path, config: &Path, reason: &str) -> String {
    match fs::copy(backup, config) {
        Ok(_) => {
            let _ = fs::remove_file(backup);
            format!("{reason} • config.yaml restored")
        }
        Err(error) => format!(
            "{reason} • COULD NOT RESTORE config.yaml ({error}) — the previous file is {}",
            backup.display()
        ),
    }
}

/// Restart the node through the same command an operator would type.
///
/// `nodo daemon restart` drives systemd and needs root, and it exits non-zero when
/// it could not do that -- which is what makes "the node was restarted" a fact here
/// rather than a hope (`src/commands/daemon.py`).
async fn restart_node() -> Result<(), String> {
    let output = tokio::time::timeout(
        RESTART_TIMEOUT,
        Command::new("nodo")
            .arg("daemon")
            .arg("restart")
            .output(),
    )
    .await
    .map_err(|_| {
        format!(
            "nodo daemon restart timed out after {}s",
            RESTART_TIMEOUT.as_secs()
        )
    })?
    .map_err(|error| format!("could not run nodo daemon restart: {error}"))?;
    if output.status.success() {
        return Ok(());
    }
    let reason = failure_reason(&output);
    Err(if reason.is_empty() {
        "nodo daemon restart failed".to_string()
    } else {
        reason
    })
}

/// Whether anything is serving on the gateway port.
///
/// The same question `nodo info` asks (`is_nodo_service_running` in nodo.py): a
/// connection to the port, rather than a systemd unit state, so a node someone
/// started by hand counts as running too. `auto` and `0` are not ports and read as
/// nothing serving.
async fn serving_on(port: Option<&str>) -> bool {
    let Some(port) = port.and_then(|port| port.trim().parse::<u16>().ok()) else {
        return false;
    };
    if port == 0 {
        return false;
    }
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    tokio::time::timeout(
        Duration::from_millis(500),
        tokio::net::TcpStream::connect(address),
    )
    .await
    .map(|attempt| attempt.is_ok())
    .unwrap_or(false)
}

/// Wait for the node to accept connections on `port` again, up to
/// `NODE_READY_TIMEOUT`. False means it never did.
async fn wait_until_serving(port: Option<&str>) -> bool {
    let deadline = Instant::now() + NODE_READY_TIMEOUT;
    loop {
        if serving_on(port).await {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(NODE_READY_POLL).await;
    }
}

/// What to report a failed command by: its stderr, or its stdout when the command
/// printed the reason there. `nodo daemon restart` prints its permission refusal on
/// stdout, and reporting an empty stderr would drop the one line that explains it.
fn failure_reason(output: &std::process::Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    if stderr.trim().is_empty() {
        first_line(&String::from_utf8_lossy(&output.stdout))
    } else {
        first_line(&stderr)
    }
}

/// What to do with a background command's output once it finishes.
#[derive(Debug, Clone)]
enum CommandKind {
    /// Append output to the action log and report status.
    Generic,
    /// Render stdout in the Details overlay (carries the service id for the title).
    Inspect(String),
}

/// Result of a background `nodo` invocation.
#[derive(Debug)]
struct CommandOutcome {
    kind: CommandKind,
    label: String,
    stdout: String,
    stderr: String,
    success: bool,
}

/// Scrollable read-only overlay contents.
#[derive(Debug, Clone)]
pub struct DetailsView {
    pub title: String,
    pub lines: Vec<String>,
    pub scroll: usize,
}

#[derive(Debug, Clone)]
pub struct Peer {
    pub id: String,
    pub uris: String,
    /// Our balance on this peer, in raw MU as stored. Rendered in the operator's
    /// display unit at draw time (see `Money`), never at read time, so changing the
    /// unit does not need a data refresh. Source of truth is the `balance_mu` column on the
    /// `peer` table itself — NOT the local `clients` table. `peer.remote_client_id`
    /// identifies our client *inside the remote peer*, so it can never be joined
    /// against our local `clients` table (see issue #178).
    pub balance: String,
    /// Our client id *inside this peer*, as it assigned it to us — what `nodo peers`
    /// prints as "Remote Client ID". Empty when we have never registered there.
    /// Never a key into our own `clients` table (see the balance note above).
    pub remote_client_id: String,
    /// Every reputation proof this peer announced. These are the peer's *own*
    /// opinions about other nodes, published on-chain — not a credential we hold on
    /// it, and not one value: a single identity key can hold several proofs, so the
    /// list comes from its signed advertisement rather than a column (issue #281).
    pub proof_ids: Vec<String>,
    /// Local reputation score (nodo-managed, independent of the on-chain proof).
    pub reputation_score: String,
    /// Every payment contract this peer has registered. Rendered in the peer
    /// detail card rather than the table: a peer can hold several instances,
    /// and each carries more than a row can show (see issue #231).
    pub contracts: Vec<PeerContract>,
}

/// One `contract_instance` row: the ledger a peer settles on, the contract it
/// charges through, the address it gets paid at, and what one of its units is worth.
#[derive(Debug, Clone)]
pub struct PeerContract {
    /// Ledger tag (e.g. "ergo"), falling back to the raw stored hash when the
    /// ledger row can't be resolved or carries no tag.
    pub ledger: String,
    pub contract_hash: String,
    pub address: String,
    pub mu_per_unit: String,
}

impl Identifiable for Peer {
    fn id(&self) -> &str {
        &self.id
    }
}

#[derive(Debug, Clone)]
pub struct Client {
    pub id: String,
    pub balance: String,
    pub last_usage: String,
    /// A client this node never charges — its own dev clients. Stored on the row
    /// since before this page existed, and shown nowhere until it did: an operator
    /// wondering why a balance never moves is owed this word.
    pub unmetered: bool,
}

impl Identifiable for Client {
    fn id(&self) -> &str {
        &self.id
    }
}

/// One `payments` row, as the detail cards show it. The amount stays in raw MU and is
/// rendered in the operator's display unit at draw time, like every other balance here.
#[derive(Debug, Clone)]
pub struct PaymentRow {
    pub created_at: String,
    pub amount: String,
    /// `communicated` / `unacknowledged` / `accepted` / `rejected` — see the
    /// `payments` table. `unacknowledged` is the one worth reading: money left and
    /// no balance arrived.
    pub status: String,
    pub tx_id: String,
    pub deposit_token: String,
}

/// One `reputation_events` row: what moved a score, by how much, and why.
#[derive(Debug, Clone)]
pub struct ReputationEvent {
    pub created_at: String,
    pub amount: i64,
    pub reason: String,
    pub score_after: Option<i64>,
}

/// A deposit token issued to a client, with what became of it.
#[derive(Debug, Clone)]
pub struct DepositToken {
    pub id: String,
    pub status: String,
    pub created_at: String,
}

/// An instance a client started on this node (`local_instances.father_id`).
#[derive(Debug, Clone)]
pub struct ClientInstance {
    pub id: String,
    pub name: String,
}

/// Everything the Peers page shows about the selected peer beyond its table row.
///
/// Loaded for the selection rather than for every peer: this is three queries, and
/// the list refreshes every couple of seconds whether or not anyone is reading it.
#[derive(Debug, Clone, Default)]
pub struct PeerDetail {
    pub peer_id: String,
    pub payments: Vec<PaymentRow>,
    pub events: Vec<ReputationEvent>,
}

/// A service's reputation: the score every instance of it contributed to, and the
/// events that got it there.
#[derive(Debug, Clone, Default)]
pub struct ServiceDetail {
    pub service_id: String,
    /// None when the service has never been scored — different from a score of 0,
    /// which is a service that earned and lost in equal measure.
    pub score: Option<i64>,
    pub events: Vec<ReputationEvent>,
}

/// Everything the Clients page shows about the selected client beyond its table row.
///
/// A client is not a peer and cannot be resolved to one: `peer.remote_client_id` is
/// our client id *inside* a remote peer, not a key into our `clients` table (#178).
/// So this shows what the client itself did here — nothing is inferred about who it is.
#[derive(Debug, Clone, Default)]
pub struct ClientDetail {
    pub client_id: String,
    pub deposits: Vec<DepositToken>,
    pub instances: Vec<ClientInstance>,
    pub payments: Vec<PaymentRow>,
}

#[derive(Debug, Clone)]
pub struct Service {
    pub id: String,
    pub tag: String,
    pub size_bytes: u64,
}

impl Identifiable for Service {
    fn id(&self) -> &str {
        &self.id
    }
}

/// What an instance is *using* right now, as opposed to what it was allocated.
///
/// Every field is read from cgroupfs/sysfs on each refresh, exactly where
/// `nodo observe` reads it (`src/commands/observe.py`,
/// `src/virtualizers/ch/observability.py`). All of them stay `None` — never `0` —
/// when the source file is absent or unreadable: a delegated instance has no local
/// cgroup, a non-`ch` virtualizer has no tap, and a dying instance loses both
/// mid-sweep. `0` would read as "idle", which is a different claim than "unknown".
#[derive(Debug, Clone, Default)]
pub struct InstanceUsage {
    pub memory_current: Option<u64>,
    /// Cumulative `usage_usec` from the instance's cgroup `cpu.stat`. Kept on the
    /// row so the next sweep can delta against it (see `derive_instance_rates`).
    pub cpu_usage_usec: Option<u64>,
    /// CPU use over the interval between the last two sweeps, in the convention
    /// `observe` uses (`compute_cpu_percent`, `observe.py:192`): cumulative core
    /// time, so a 2-vCPU guest pinning both cores reads 200%, not 100%. The
    /// allowance it should be judged against is `Instance::vcpus`.
    pub cpu_percent: Option<f64>,
    pub disk_read_bytes: Option<u64>,
    pub disk_write_bytes: Option<u64>,
    pub net_rx_bytes: Option<u64>,
    pub net_tx_bytes: Option<u64>,
    /// Bytes per second over the same interval, from the tap byte counters.
    /// Orientation is the host tap's, as in `_tap_net_snapshot`: `rx` is what the
    /// host received from the VM (VM egress), `tx` what it sent to the VM.
    pub net_rx_rate: Option<f64>,
    pub net_tx_rate: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct Instance {
    pub id: String,
    pub name: String,
    pub ip: String,
    pub service: String,
    pub balance: String,
    pub virtualizer: String,
    pub memory_limit: u64,
    pub disk_limit: u64,
    /// vCPU allowance from the CFS pair (`cpu_quota / cpu_period`), or `None` when
    /// the row stores no quota (unbounded). This is what `usage.cpu_percent` is
    /// saturating when it reaches `vcpus * 100`.
    pub vcpus: Option<f64>,
    /// Live readings, sampled per refresh; see `InstanceUsage`.
    pub usage: InstanceUsage,
    /// "local" for locally-run instances, otherwise the owning peer id for
    /// delegated/remote instances.
    pub location: String,
    /// Parent instance id (from `father_id`); empty when this is a root.
    pub father_id: String,
    /// Burn rate: what this instance costs to keep running, in MU per minute / per
    /// hour, derived from the `instance_consumption` running average
    /// (`mu_per_second`). `None` — rendered `—`, never `0` — when no maintenance tick
    /// has charged it yet, and always `None` for delegated instances (their charge
    /// happens on the owning peer). It prices *reserved* resources at current
    /// scarcity, so it is "cost at present prices", not measured resource usage (#245).
    pub mu_per_minute: Option<f64>,
    pub mu_per_hour: Option<f64>,
    /// How many samples the average is built from, and how long ago it was last
    /// updated (seconds), so a rate from two stale samples reads differently from a
    /// fresh one. Both `None` when there is no consumption row.
    pub consumption_samples: Option<u64>,
    pub consumption_age_secs: Option<f64>,
}

impl Instance {
    pub fn is_local(&self) -> bool {
        self.location == "local"
    }

    /// The CPU percentage that means "saturating its whole allowance", against which
    /// `usage.cpu_percent` is read. `None` when the instance has no quota.
    pub fn cpu_allowance_percent(&self) -> Option<f64> {
        self.vcpus.map(|vcpus| vcpus * 100.0)
    }
}

/// One instance's raw counters plus the rates last derived from them.
///
/// `refresh()` rebuilds the instance list wholesale, so the previous sweep's
/// counters have to be kept here, keyed by instance id, and matched up on the next
/// one. The derived rates are stored alongside so a *forced* refresh landing a few
/// milliseconds after the previous one can carry them forward instead of blanking
/// the columns (see `MIN_RATE_INTERVAL`).
#[derive(Debug, Clone)]
struct InstanceCounters {
    sampled_at: Instant,
    cpu_usage_usec: Option<u64>,
    net_rx_bytes: Option<u64>,
    net_tx_bytes: Option<u64>,
    cpu_percent: Option<f64>,
    net_rx_rate: Option<f64>,
    net_tx_rate: Option<f64>,
}

impl Identifiable for Instance {
    fn id(&self) -> &str {
        &self.id
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigPathSegment {
    Key(String),
    Index(usize),
}

#[derive(Debug, Clone)]
pub struct ConfigEntry {
    pub path: String,
    pub path_segments: Vec<ConfigPathSegment>,
    pub value: String,
    pub edit_value: String,
    pub value_type: String,
    pub secret: bool,
}

impl Identifiable for ConfigEntry {
    fn id(&self) -> &str {
        &self.path
    }
}

impl ConfigEntry {
    pub fn display_value(&self) -> String {
        if self.secret && !self.value.is_empty() && self.value != "null" {
            "•••••••• (set)".to_string()
        } else {
            self.value.clone()
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct NodeInfo {
    pub service_status: String,
    pub version: String,
    pub address: String,
    pub reputation_proof: String,
    pub wallet_address: String,
    pub wallet_balance: Option<f64>,
    pub cold_wallet_address: String,
    pub error: String,
}

#[derive(Debug, Clone, Default)]
pub struct DashboardStats {
    pub cpu_percent: u64,
    pub memory_used: u64,
    pub memory_total: u64,
    pub disk_used: u64,
    pub disk_total: u64,
    pub storage_bytes: u64,
    pub instance_memory_current: u64,
    pub instance_memory_reserved: u64,
    pub instance_disk_reserved: u64,
}

#[derive(Debug, Clone)]
pub struct Paths {
    pub root: PathBuf,
    pub config: PathBuf,
    pub database: PathBuf,
    pub storage: PathBuf,
    pub registry: PathBuf,
    pub metadata: PathBuf,
    pub log: PathBuf,
    pub cgroups: PathBuf,
    pub cache: PathBuf,
    pub yq: PathBuf,
}

impl Paths {
    pub fn discover() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."));
        let config = root.join("config.yaml");
        let document = read_yaml(&config).ok();

        let main_dir = yaml_string(document.as_ref(), &["main", "MAIN_DIR"])
            .map(PathBuf::from)
            .unwrap_or_else(|| root.clone());
        let storage = yaml_string(document.as_ref(), &["main", "STORAGE"])
            .map(|value| resolve_config_path(&value, &main_dir, None))
            .unwrap_or_else(|| root.join("storage"));

        let resolve = |keys: &[&str], fallback: PathBuf| {
            yaml_string(document.as_ref(), keys)
                .map(|value| resolve_config_path(&value, &main_dir, Some(&storage)))
                .unwrap_or(fallback)
        };

        Self {
            root: root.clone(),
            config,
            database: resolve(&["main", "DATABASE_FILE"], storage.join("database.sqlite")),
            registry: resolve(&["main", "REGISTRY"], storage.join("__registry__")),
            metadata: resolve(&["main", "METADATA_REGISTRY"], storage.join("__metadata__")),
            log: storage.join("app.log"),
            cgroups: resolve(
                &["virtualizers", "ch", "CGROUPS_BASE_DIR"],
                PathBuf::from("/sys/fs/cgroup"),
            ),
            cache: resolve(&["main", "CACHE"], storage.join("__cache__")),
            yq: resolve(&["dependencies", "yq", "BIN"], PathBuf::from("yq")),
            storage,
        }
    }
}

/// How the operator's money is denominated.
///
/// Three separate things, and the TUI has to keep them apart the same way the node
/// does: amounts are stored in **MU** (the node's unit of account, see
/// `src/utils/monetary.py`), what an MU is worth is the *payment contract's* rate — for
/// Ergo, `ledgers.ergo.payments.MU_PER_NANOERG`, whose Python side lives in
/// `src/payment_system/contracts/ergo/rate.py` — and what the operator reads is
/// `ui.DISPLAY_UNIT`. The TUI reads the catalogue database directly, so it resolves all
/// three from `config.yaml` itself rather than asking the node; a second payment system
/// would need its rate read here too.
#[derive(Debug, Clone)]
pub struct Money {
    pub unit_name: String,
    pub symbol: String,
    /// MU in one display unit.
    pub mu_per_unit: f64,
    /// Set when `mu_per_unit` is an exact power of ten, which lets formatting be a
    /// digit shift on the decimal string instead of an f64 division — exact for any
    /// balance, however large. The built-in units and any whole-numbered custom rate
    /// land here; only an awkward custom rate falls back to floating point, where the
    /// configured decimals round it anyway.
    pub mu_per_unit_pow10: Option<u32>,
    pub decimals: usize,
    /// MU bought by one nanoERG. Only meaningful against the Ergo ledger.
    pub mu_per_nanoerg: f64,
}

impl Default for Money {
    fn default() -> Self {
        Self {
            unit_name: "erg".to_string(),
            symbol: "ERG".to_string(),
            mu_per_unit: 1e9,
            mu_per_unit_pow10: Some(9),
            decimals: 9,
            mu_per_nanoerg: 1.0,
        }
    }
}

fn exact_pow10(value: f64) -> Option<u32> {
    if !(value.is_finite() && value >= 1.0) {
        return None;
    }
    let exponent = value.log10().round();
    if !(0.0..=30.0).contains(&exponent) {
        return None;
    }
    let candidate = 10f64.powf(exponent);
    ((candidate - value).abs() < f64::EPSILON * candidate.max(1.0)).then_some(exponent as u32)
}

impl Money {
    /// Resolve the display unit from `config.yaml`, falling back to ERG.
    pub fn load(config: &Path) -> Self {
        let document = read_yaml(config).ok();
        let mu_per_nanoerg = yaml_scalar(
            document.as_ref(),
            &["ledgers", "ergo", "payments", "MU_PER_NANOERG"],
        )
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| *value > 0.0)
        .unwrap_or(1.0);

        let name = yaml_string(document.as_ref(), &["ui", "DISPLAY_UNIT"])
            .unwrap_or_else(|| "erg".to_string())
            .trim()
            .to_lowercase();

        match name.as_str() {
            "mu" => Self {
                unit_name: name,
                symbol: "MU".to_string(),
                mu_per_unit: 1.0,
                mu_per_unit_pow10: Some(0),
                decimals: 0,
                mu_per_nanoerg,
            },
            "erg" => {
                let mu_per_unit = mu_per_nanoerg * 1e9;
                Self {
                    unit_name: name,
                    symbol: "ERG".to_string(),
                    mu_per_unit_pow10: exact_pow10(mu_per_unit),
                    mu_per_unit,
                    decimals: 9,
                    mu_per_nanoerg,
                }
            }
            // A unit the operator declared under `ui.UNITS.<name>`. Its rate is static
            // and nothing refreshes it, exactly as on the node side.
            _ => {
                let unit_keys = ["ui", "UNITS", name.as_str()];
                let mu_per_unit = yaml_scalar(
                    document.as_ref(),
                    &[unit_keys[0], unit_keys[1], unit_keys[2], "MU_PER_UNIT"],
                )
                .and_then(|value| value.parse::<f64>().ok())
                .filter(|value| *value > 0.0)
                .unwrap_or(1.0);
                Self {
                    symbol: yaml_string(
                        document.as_ref(),
                        &[unit_keys[0], unit_keys[1], unit_keys[2], "SYMBOL"],
                    )
                    .unwrap_or_else(|| name.to_uppercase()),
                    decimals: yaml_scalar(
                        document.as_ref(),
                        &[unit_keys[0], unit_keys[1], unit_keys[2], "DECIMALS"],
                    )
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(2),
                    unit_name: name,
                    mu_per_unit_pow10: exact_pow10(mu_per_unit),
                    mu_per_unit,
                    mu_per_nanoerg,
                }
            }
        }
    }

    /// Render a raw MU amount (as stored in the catalogue) in the display unit.
    /// Anything unparseable is passed through untouched rather than guessed at.
    pub fn format_raw(&self, raw: &str) -> String {
        let digits = raw.trim();
        let (sign, digits) = match digits.strip_prefix('-') {
            Some(rest) => ("-", rest),
            None => ("", digits),
        };
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return raw.to_string();
        }

        let text = match self.mu_per_unit_pow10 {
            Some(0) => digits.trim_start_matches('0').to_string(),
            Some(shift) => {
                let shift = shift as usize;
                let padded = format!("{digits:0>width$}", width = shift + 1);
                let split = padded.len() - shift;
                let whole = &padded[..split];
                let fraction = padded[split..].trim_end_matches('0');
                if fraction.is_empty() {
                    whole.to_string()
                } else {
                    format!("{whole}.{fraction}")
                }
            }
            None => {
                let Ok(value) = digits.parse::<f64>() else {
                    return raw.to_string();
                };
                let text = format!("{:.*}", self.decimals, value / self.mu_per_unit);
                text.trim_end_matches('0').trim_end_matches('.').to_string()
            }
        };
        let text = if text.is_empty() { "0" } else { &text };
        format!("{sign}{text} {}", self.symbol)
    }

    pub fn format_mu(&self, mu: u64) -> String {
        self.format_raw(&mu.to_string())
    }
}

/// One configurable price. The catalogue mirrors `PRICE_KEYS` in
/// `src/utils/config_validation.py`; keeping the two in step is what makes the bars an
/// editor for the real config rather than a decoration.
#[derive(Debug, Clone)]
pub struct PriceEntry {
    /// Unique row id. The config key for a node-wide price, and `<arch>/<key>` for a
    /// per-architecture override -- the two would otherwise collide on the same key,
    /// and the selection is tracked by id.
    pub id: String,
    /// Key under `pricing:` (or under `pricing.BY_ARCH.<arch>:`) in config.yaml.
    pub key: &'static str,
    /// Short label for the bar.
    pub short: String,
    /// What the price is charged per, for the legend.
    pub per: &'static str,
    /// Recurring prices are charged for as long as a resource is held; one-off ones
    /// price an event. They are shown apart because their magnitudes are unrelated,
    /// and a shared axis would flatten one of the groups into nothing.
    pub recurring: bool,
    /// The architecture this price applies to, for a per-arch override; `None` for the
    /// node-wide price, which every arch pays unless overridden.
    pub arch: Option<&'static str>,
    /// True when this row is a per-arch override that is NOT written in config.yaml:
    /// it shows the scalar price it inherits, so the operator can see what an arch is
    /// charged and edit it in place, rather than having to know the block exists.
    pub inherited: bool,
    pub mu: u64,
}

impl PriceEntry {
    /// Where this price lives in config.yaml.
    pub fn config_path(&self) -> Vec<ConfigPathSegment> {
        let mut path = vec![ConfigPathSegment::Key("pricing".to_string())];
        if let Some(arch) = self.arch {
            path.push(ConfigPathSegment::Key(PRICING_BY_ARCH_KEY.to_string()));
            path.push(ConfigPathSegment::Key(arch.to_string()));
        }
        path.push(ConfigPathSegment::Key(self.key.to_string()));
        path
    }

    /// How this price is named in status lines and editor titles.
    pub fn config_label(&self) -> String {
        match self.arch {
            Some(arch) => format!("pricing.{PRICING_BY_ARCH_KEY}.{arch}.{}", self.key),
            None => format!("pricing.{}", self.key),
        }
    }
}

impl Identifiable for PriceEntry {
    fn id(&self) -> &str {
        &self.id
    }
}

const PRICE_CATALOGUE: [(&str, &str, &str, bool); 7] = [
    ("RAM_MU_PER_GIB_HOUR", "RAM", "per GiB-hour", true),
    ("CPU_MU_PER_VCPU_HOUR", "CPU", "per vCPU-hour", true),
    ("DISK_MU_PER_GIB_HOUR", "DISK", "per GiB-hour", true),
    ("NET_MU_PER_GIB", "NET", "per GiB relayed", true),
    ("BUILD_MU", "BUILD", "per container build", false),
    ("TUNNEL_OPEN_MU", "TUNNEL", "per tunnel opened", false),
    ("MODIFY_RESOURCES_MU", "RESIZE", "per resource change", false),
];

/// The block under `pricing:` holding per-architecture overrides. Mirrors
/// `PRICING_BY_ARCH_KEY` in `src/utils/monetary.py`.
pub const PRICING_BY_ARCH_KEY: &str = "BY_ARCH";

/// Prices that may be set per architecture, and the architectures they may be set for.
/// Mirrors `PER_ARCH_PRICE_KEYS` in `src/utils/config_validation.py`.
///
/// Only memory. It is the one resource whose real cost to the node depends on the
/// guest's architecture: the guest kernel reserve the node absorbs and never bills
/// differs per arch. The node hands a guest the vCPUs and the image it asked for
/// whatever architecture it is, so nothing else has a per-arch cost to recover.
const PER_ARCH_PRICE_KEYS: [&str; 1] = ["RAM_MU_PER_GIB_HOUR"];
pub const PRICED_ARCHITECTURES: [&str; 2] = ["linux/amd64", "linux/arm64"];

/// The guest kernel reserve, per architecture: how much MORE than a service's declared
/// memory the VM is booted with, so the service really gets what it declared.
///
/// Mirrors `_DEFAULT_GUEST_KERNEL_RESERVE` in `src/virtualizers/ch/limits.py`, and is
/// overridden from the same config keys the node reads, so what the operator is shown
/// here is what the node will actually reserve.
///
/// This matters on the PRICING page because **the node absorbs it**. An instance is
/// billed for the memory it declared and can use, never for the kernel underneath it,
/// so every GiB sold commits more than a GiB of host RAM -- and by a different amount
/// per architecture. A memory price set without it in view under-recovers, silently.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GuestKernelReserve {
    pub fixed_mib: u64,
    pub ratio: f64,
}

impl GuestKernelReserve {
    /// Bytes reserved on top of `usable_bytes`. The same model as
    /// `limits.guest_kernel_reserve_bytes`: a fixed part (the kernel image, percpu
    /// areas, reserved low memory -- what actually differs per arch) plus a share of
    /// the guest (one `struct page` per 4 KiB frame, near-identical on both).
    pub fn bytes_for(&self, usable_bytes: u64) -> u64 {
        if usable_bytes == 0 {
            return 0;
        }
        self.fixed_mib * 1024 * 1024 + (usable_bytes as f64 * self.ratio).ceil() as u64
    }

    /// What one GiB sold actually costs the node in host RAM, as a multiplier.
    ///
    /// The figure a memory price has to be multiplied by to recover the overhead: at
    /// 1.18, a node earning `p` per GiB-hour declared earns `p / 1.18` per GiB-hour of
    /// host RAM it committed.
    pub fn commitment_multiplier(&self, usable_bytes: u64) -> f64 {
        if usable_bytes == 0 {
            return 1.0;
        }
        (usable_bytes + self.bytes_for(usable_bytes)) as f64 / usable_bytes as f64
    }
}

/// The ratio is the same on both arches because the physics is: one `struct page`
/// per 4 KiB frame does not know what instruction set it describes. 2.5% clears the
/// fitted measurements (1.80% amd64, 2.10% arm64) and the 1.5625% `struct page`
/// floor with headroom, without scaling into hundreds of wasted MiB on a large guest
/// -- margin on a multiplier is multiplied too, and the node absorbs it unbilled.
/// The fixed part is what genuinely differs per arch, and is where margin is cheap.
const DEFAULT_GUEST_KERNEL_RESERVE: [(&str, u64, f64); 2] = [
    ("linux/amd64", 40, GUEST_KERNEL_RESERVE_RATIO),
    ("linux/arm64", 32, GUEST_KERNEL_RESERVE_RATIO),
];

/// Mirrors `_GUEST_KERNEL_RESERVE_RATIO` in `src/virtualizers/ch/limits.py`.
const GUEST_KERNEL_RESERVE_RATIO: f64 = 0.025;

/// The reserve for each priced architecture, config overrides applied.
fn get_guest_kernel_reserves(config: &Path) -> Vec<(&'static str, GuestKernelReserve)> {
    let document = read_yaml(config).ok();
    PRICED_ARCHITECTURES
        .iter()
        .map(|arch| {
            let (_, default_mib, default_ratio) = DEFAULT_GUEST_KERNEL_RESERVE
                .iter()
                .find(|(tag, ..)| tag == arch)
                .copied()
                .unwrap_or(("", 0, 0.0));
            let read = |leaf: &str| -> Option<String> {
                yaml_scalar(
                    document.as_ref(),
                    &["virtualizers", "ch", "GUEST_KERNEL_RESERVE", arch, leaf],
                )
            };
            let reserve = GuestKernelReserve {
                fixed_mib: read("MIB")
                    .and_then(|value| value.trim().parse::<u64>().ok())
                    .unwrap_or(default_mib),
                ratio: read("RATIO")
                    .and_then(|value| value.trim().parse::<f64>().ok())
                    .filter(|value| value.is_finite() && *value >= 0.0)
                    .unwrap_or(default_ratio),
            };
            (*arch, reserve)
        })
        .collect()
}

/// The scarcity surcharge, which bounds what any of these prices can become.
#[derive(Debug, Clone, Copy)]
pub struct Scarcity {
    pub max_multiplier: u64,
    pub curve: f64,
}

impl Default for Scarcity {
    fn default() -> Self {
        Self {
            max_multiplier: 1,
            curve: 1.0,
        }
    }
}

fn get_prices(config: &Path) -> (Vec<PriceEntry>, Scarcity) {
    let document = read_yaml(config).ok();
    let read = |key: &str| -> u64 {
        yaml_scalar(document.as_ref(), &["pricing", key])
            .and_then(|value| value.trim().parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value >= 0.0)
            .map(|value| value as u64)
            .unwrap_or(0)
    };
    let mut entries: Vec<PriceEntry> = PRICE_CATALOGUE
        .iter()
        .map(|(key, short, per, recurring)| PriceEntry {
            id: (*key).to_string(),
            key,
            short: (*short).to_string(),
            per,
            recurring: *recurring,
            arch: None,
            inherited: false,
            mu: read(key),
        })
        .collect();

    // One row per (architecture, per-arch-priceable key), always -- including the
    // architectures the operator has not written a price for, which show the scalar
    // they inherit. An arch that only appeared once it was already configured would
    // need the operator to know the block exists before they could reach it, and the
    // whole point of the page is that a price is editable where it is displayed.
    for arch in PRICED_ARCHITECTURES {
        for key in PER_ARCH_PRICE_KEYS {
            let (short, per, recurring) = PRICE_CATALOGUE
                .iter()
                .find(|(catalogue_key, ..)| *catalogue_key == key)
                .map(|(_, short, per, recurring)| (*short, *per, *recurring))
                .unwrap_or((key, "", true));
            let configured = yaml_scalar(
                document.as_ref(),
                &["pricing", PRICING_BY_ARCH_KEY, arch, key],
            )
            .and_then(|value| value.trim().parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value >= 0.0)
            .map(|value| value as u64);
            entries.push(PriceEntry {
                id: format!("{arch}/{key}"),
                key,
                // The arch after the resource, so the per-arch rows sort and read as
                // variations of the price above them rather than as separate prices.
                short: format!("{short}·{}", arch.rsplit('/').next().unwrap_or(arch)),
                per,
                recurring,
                arch: Some(arch),
                inherited: configured.is_none(),
                mu: configured.unwrap_or_else(|| read(key)),
            });
        }
    }
    let scarcity = Scarcity {
        max_multiplier: yaml_scalar(document.as_ref(), &["pricing", "SCARCITY_MAX_MULTIPLIER"])
            .and_then(|value| value.trim().parse::<u64>().ok())
            .unwrap_or(1)
            .max(1),
        curve: yaml_scalar(document.as_ref(), &["pricing", "SCARCITY_CURVE"])
            .and_then(|value| value.trim().parse::<f64>().ok())
            .filter(|value| *value > 0.0)
            .unwrap_or(1.0),
    };
    (entries, scarcity)
}

fn read_yaml(path: &Path) -> Result<Value, String> {
    let content = fs::read_to_string(path)
        .map_err(|error| format!("Unable to read {}: {error}", path.display()))?;
    serde_yaml::from_str(&content)
        .map_err(|error| format!("Unable to parse {}: {error}", path.display()))
}

fn yaml_string(document: Option<&Value>, keys: &[&str]) -> Option<String> {
    let mut value = document?;
    for key in keys {
        value = value.get(*key)?;
    }
    value.as_str().map(ToString::to_string)
}

/// Read a scalar as text, whatever YAML type it happens to be.
///
/// `yaml_string` only sees quoted strings, so a price written as a bare `1000000` --
/// which is how prices are written, they are numbers -- reads as absent and would
/// silently show as free. Anything that is not a scalar still reads as absent.
fn yaml_scalar(document: Option<&Value>, keys: &[&str]) -> Option<String> {
    let mut value = document?;
    for key in keys {
        value = value.get(*key)?;
    }
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(flag) => Some(flag.to_string()),
        _ => None,
    }
}

/// `network.GATEWAY_PORT` as written, or None when the file cannot be read.
///
/// The sentinel `auto` and a real port are both just text here: what matters is
/// whether the value changed, not what it means.
fn read_gateway_port(config: &Path) -> Option<String> {
    read_yaml(config)
        .ok()
        .and_then(|document| yaml_scalar(Some(&document), &["network", "GATEWAY_PORT"]))
}

fn resolve_config_path(value: &str, main_dir: &Path, storage: Option<&Path>) -> PathBuf {
    let main = main_dir.to_string_lossy();
    let storage_text = storage
        .map(|path| path.to_string_lossy().to_string())
        .unwrap_or_default();
    let expanded = value
        .replace("${main.MAIN_DIR}", &main)
        .replace("${main.STORAGE}", &storage_text);
    let path = PathBuf::from(expanded);
    if path.is_absolute() {
        path
    } else {
        main_dir.join(path)
    }
}

#[derive(Debug)]
pub struct TabsState {
    pub index: usize,
}

impl TabsState {
    pub fn page(&self) -> Page {
        Page::ALL[self.index]
    }

    pub fn next(&mut self) {
        self.index = (self.index + 1) % Page::ALL.len();
    }

    pub fn previous(&mut self) {
        self.index = if self.index == 0 {
            Page::ALL.len() - 1
        } else {
            self.index - 1
        };
    }
}

#[derive(Debug)]
pub struct StatefulList<T: Identifiable> {
    pub state: TableState,
    pub state_id: Option<String>,
    pub items: Vec<T>,
}

impl<T: Identifiable> StatefulList<T> {
    pub fn with_items(items: Vec<T>) -> Self {
        Self {
            state: TableState::default(),
            state_id: None,
            items,
        }
    }

    pub fn refresh(&mut self, items: Vec<T>) {
        let selected_id = self.state_id.clone();
        self.items = items;
        if self.items.is_empty() {
            self.state.select(None);
            self.state_id = None;
            return;
        }
        if let Some(id) = selected_id {
            if let Some(index) = self.items.iter().position(|item| item.id() == id) {
                self.state.select(Some(index));
                self.state_id = Some(id);
                return;
            }
        }
        self.state.select(None);
        self.state_id = None;
    }

    pub fn selected(&self) -> Option<&T> {
        self.state
            .selected()
            .and_then(|index| self.items.get(index))
    }

    /// Select the row `visible` places down from the first one on screen — what a
    /// click lands on, since a scrolled table's first visible row is `offset`, not 0.
    /// Ignores a click past the last row, so empty space below the table selects nothing.
    pub fn select_visible(&mut self, visible: usize) {
        let index = self.state.offset() + visible;
        if let Some(item) = self.items.get(index) {
            self.state_id = Some(item.id().to_string());
            self.state.select(Some(index));
        }
    }

    pub fn next(&mut self) {
        if self.items.is_empty() {
            return;
        }
        let index = match self.state.selected() {
            Some(index) if index + 1 < self.items.len() => index + 1,
            _ => 0,
        };
        self.state.select(Some(index));
        self.state_id = Some(self.items[index].id().to_string());
    }

    pub fn previous(&mut self) {
        if self.items.is_empty() {
            return;
        }
        let index = match self.state.selected() {
            Some(0) | None => self.items.len() - 1,
            Some(index) => index - 1,
        };
        self.state.select(Some(index));
        self.state_id = Some(self.items[index].id().to_string());
    }
}

/// Where the cursor is on the CELL page, and where the page last drew things so a
/// click can be resolved back to them.
///
/// The cursor is (organelle, lever) rather than a flat index: the levers are laid
/// out in boxes, and ←/→ moving between boxes while ↑/↓ moves inside one is what
/// makes the layout navigable without a mouse.
#[derive(Debug, Default)]
pub struct CellState {
    pub organelle: usize,
    pub lever: usize,
    /// Which profile the picker has highlighted.
    pub profile: usize,
    /// Where each organelle's box was drawn, for click routing.
    pub organelle_areas: Vec<(usize, Rect)>,
    /// Where each lever row was drawn: (organelle, lever, row).
    pub lever_areas: Vec<(usize, usize, Rect)>,
}

impl CellState {
    /// The organelle the cursor is in.
    pub fn organelle(&self) -> Organelle {
        Organelle::ALL[self.organelle.min(Organelle::ALL.len() - 1)]
    }

    /// The lever under the cursor, or None when the organelle is empty.
    pub fn selected(&self) -> Option<&'static Lever> {
        self.organelle().levers().get(self.lever).copied()
    }

    /// Move to another organelle, keeping the cursor on a row that exists there.
    fn go_to_organelle(&mut self, index: usize) {
        self.organelle = index % Organelle::ALL.len();
        let count = self.organelle().levers().len();
        self.lever = self.lever.min(count.saturating_sub(1));
    }
}

pub struct App {
    pub title: &'static str,
    pub tabs: TabsState,
    pub running: bool,
    pub peers: StatefulList<Peer>,
    pub clients: StatefulList<Client>,
    pub instances: StatefulList<Instance>,
    pub services: StatefulList<Service>,
    pub config_all: Vec<ConfigEntry>,
    pub config_filter: String,
    /// Open/selected state for the collapsible configuration tree. The identifier
    /// is the per-segment token path (e.g. `["virtualizers", "ch", "MIN_MEM_MIB"]`,
    /// or `["servers", "[1]", "id"]` for a sequence element), which lets the tree
    /// keep its expanded sections and selection stable across refreshes and edits.
    pub config_tree_state: TreeState<String>,
    /// Cursor and hit-test geometry for the CELL page.
    pub cell: CellState,
    /// config.yaml as a parsed document, from which every cell lever's position is
    /// derived. Cached and refreshed with the rest of the data rather than read per
    /// frame: the page is redrawn on every keystroke and tick, and re-parsing a
    /// 40 KB file that often is work nobody asked for.
    pub config_document: Option<Value>,
    /// Editable price vector, and how the operator's money is denominated.
    pub prices: StatefulList<PriceEntry>,
    pub scarcity: Scarcity,
    pub money: Money,
    /// The guest kernel reserve per architecture, as the node will apply it. Shown on
    /// the pricing page because the node absorbs it: it is the gap between memory sold
    /// and host RAM committed, and it is what a memory price has to cover.
    pub guest_kernel_reserves: Vec<(&'static str, GuestKernelReserve)>,
    /// Detail for the selected peer / client, reloaded when the selection moves or the
    /// data refreshes — never per frame, since drawing must not touch the database.
    pub peer_detail: Option<PeerDetail>,
    pub client_detail: Option<ClientDetail>,
    pub service_detail: Option<ServiceDetail>,
    pub instances_grouped: bool,
    pub app_logs: Vec<String>,
    pub node_logs: Vec<String>,
    pub cpu_history: VecDeque<u64>,
    pub ram_history: VecDeque<u64>,
    pub stats: DashboardStats,
    pub node_info: NodeInfo,
    pub paths: Paths,
    pub input_mode: InputMode,
    pub input: String,
    pub input_title: String,
    pub edit_config_path: Option<Vec<ConfigPathSegment>>,
    pub edit_config_secret: bool,
    /// Which widget the `EditConfig` popup should present for the value
    /// currently being edited (checkbox, stepper, enum picker, or freeform text).
    pub edit_kind: EditKind,
    /// Destructive action awaiting a y/N confirmation.
    pub pending_action: Option<PendingAction>,
    /// Client id and direction (true = debit) for the open `CreditClient` amount modal.
    pub credit_client_id: Option<String>,
    pub credit_client_decrement: bool,
    /// Contents of the read-only Details overlay, when open.
    pub details: Option<DetailsView>,
    pub status: String,
    /// Where the tab bar and the current page's selectable table were last drawn, so a
    /// click can be mapped back to a tab or a row. Written by the draw path each frame;
    /// `list_area` stays empty on pages with no table (Overview, Logs, Config — the
    /// config tree tracks its own rendered area).
    pub tabs_area: Rect,
    pub list_area: Rect,
    pub sys: System,
    /// Previous sweep's per-instance counters, keyed by instance id, so CPU and
    /// network *rates* can be derived across refresh ticks. Rebuilt every sweep, so
    /// an instance that disappears takes its entry with it.
    instance_counters: HashMap<String, InstanceCounters>,
    last_data_refresh: Instant,
    last_storage_refresh: Instant,
    last_wallet_refresh: Instant,
    wallet_task: Option<JoinHandle<Result<NodeInfo, String>>>,
    /// In-flight background `nodo` command, if any (keeps the UI responsive).
    command_task: Option<JoinHandle<CommandOutcome>>,
    /// In-flight configuration transaction: write, restart, and revert on failure.
    /// Separate from `command_task` because it holds config.yaml's backup for its
    /// whole duration, and only one may do that at a time.
    config_task: Option<JoinHandle<ConfigTransaction>>,
    /// What to do with the interface once that transaction lands.
    config_follow_up: ConfigFollowUp,
}

impl Default for App {
    fn default() -> Self {
        let paths = Paths::discover();
        let config_all = get_config_entries(&paths.config).unwrap_or_default();
        let (prices, scarcity) = get_prices(&paths.config);
        let now = Instant::now();
        Self {
            title: "NODO OPERATIONS",
            tabs: TabsState { index: 0 },
            running: true,
            peers: StatefulList::with_items(get_peers(&paths.database).unwrap_or_default()),
            clients: StatefulList::with_items(get_clients(&paths.database).unwrap_or_default()),
            instances: StatefulList::with_items(Vec::new()),
            services: StatefulList::with_items(Vec::new()),
            config_all,
            config_filter: String::new(),
            config_tree_state: TreeState::default(),
            cell: CellState::default(),
            config_document: read_yaml(&paths.config).ok(),
            prices: StatefulList::with_items(prices),
            scarcity,
            money: Money::load(&paths.config),
            guest_kernel_reserves: get_guest_kernel_reserves(&paths.config),
            peer_detail: None,
            client_detail: None,
            service_detail: None,
            instances_grouped: false,
            app_logs: vec!["TUI ready".to_string()],
            node_logs: read_last_lines(&paths.log, 250).unwrap_or_default(),
            cpu_history: VecDeque::from(vec![0; HISTORY_POINTS]),
            ram_history: VecDeque::from(vec![0; HISTORY_POINTS]),
            stats: DashboardStats::default(),
            node_info: NodeInfo {
                service_status: "checking…".to_string(),
                ..NodeInfo::default()
            },
            paths,
            input_mode: InputMode::Normal,
            input: String::new(),
            input_title: String::new(),
            edit_config_path: None,
            edit_config_secret: false,
            edit_kind: EditKind::Text,
            pending_action: None,
            credit_client_id: None,
            credit_client_decrement: false,
            details: None,
            status: "Press r to refresh • q to quit".to_string(),
            tabs_area: Rect::ZERO,
            list_area: Rect::ZERO,
            sys: System::new_all(),
            instance_counters: HashMap::new(),
            last_data_refresh: now.checked_sub(DATA_REFRESH_INTERVAL).unwrap_or(now),
            last_storage_refresh: now.checked_sub(Duration::from_secs(30)).unwrap_or(now),
            last_wallet_refresh: now.checked_sub(WALLET_REFRESH_INTERVAL).unwrap_or(now),
            wallet_task: None,
            command_task: None,
            config_task: None,
            config_follow_up: ConfigFollowUp::None,
        }
    }
}

impl App {
    pub fn new() -> Self {
        let mut app = Self::default();
        app.refresh_local(true);
        app
    }

    pub fn page(&self) -> Page {
        self.tabs.page()
    }

    pub fn next_page(&mut self) {
        self.tabs.next();
    }

    pub fn previous_page(&mut self) {
        self.tabs.previous();
    }

    /// →: enter the selected configuration branch (page-local, not page navigation —
    /// pages cycle with Tab/Shift+Tab). Ignored by every page that has no use for it.
    pub fn on_right(&mut self) {
        match self.page() {
            Page::Config => {
                self.config_tree_state.key_right();
            }
            // The cell is laid out in boxes, so ←/→ step between them.
            Page::Cell => {
                let next = (self.cell.organelle + 1) % Organelle::ALL.len();
                self.cell.go_to_organelle(next);
            }
            _ => {}
        }
    }

    /// ←: leave the current configuration branch — collapse it if it is open,
    /// otherwise step up to its parent, which is what `key_left` does.
    pub fn on_left(&mut self) {
        match self.page() {
            Page::Config => {
                self.config_tree_state.key_left();
            }
            Page::Cell => {
                let count = Organelle::ALL.len();
                let previous = (self.cell.organelle + count - 1) % count;
                self.cell.go_to_organelle(previous);
            }
            _ => {}
        }
    }

    pub fn on_up(&mut self) {
        match self.page() {
            Page::Instances => self.instances.previous(),
            Page::Services => {
                self.services.previous();
                self.load_selection_details();
            }
            Page::Peers => {
                self.peers.previous();
                self.load_selection_details();
            }
            Page::Clients => {
                self.clients.previous();
                self.load_selection_details();
            }
            Page::Pricing => self.prices.previous(),
            Page::Cell => {
                let count = self.cell.organelle().levers().len();
                if count > 0 {
                    self.cell.lever = (self.cell.lever + count - 1) % count;
                }
            }
            Page::Config => {
                self.config_tree_state.key_up();
            }
            _ => {}
        }
    }

    pub fn on_down(&mut self) {
        match self.page() {
            Page::Instances => self.instances.next(),
            Page::Services => {
                self.services.next();
                self.load_selection_details();
            }
            Page::Peers => {
                self.peers.next();
                self.load_selection_details();
            }
            Page::Clients => {
                self.clients.next();
                self.load_selection_details();
            }
            Page::Pricing => self.prices.next(),
            Page::Cell => {
                let count = self.cell.organelle().levers().len();
                if count > 0 {
                    self.cell.lever = (self.cell.lever + 1) % count;
                }
            }
            Page::Config => {
                self.config_tree_state.key_down();
            }
            _ => {}
        }
    }

    /// Route a left click to whatever was drawn under it: a tab, a config tree node, or
    /// a table row. Geometry comes from the last frame (`tabs_area` / `list_area`), which
    /// is always the frame the user was looking at when they clicked.
    pub fn click_at(&mut self, column: u16, row: u16) {
        let position = Position::new(column, row);
        if self.tabs_area.contains(position) {
            if let Some(index) = tab_at(column, self.tabs_area) {
                self.tabs.index = index;
            }
            return;
        }
        // The config tree remembers where it drew each node, so it can resolve the
        // click itself — including collapsing a section that was already selected.
        if self.page() == Page::Config {
            self.config_tree_state.click_at(position);
            return;
        }
        if self.page() == Page::Cell {
            self.click_cell(position);
            return;
        }
        if let Some(visible) = visible_row_at(row, self.list_area) {
            self.select_visible_row(visible);
        }
    }

    /// Select the row `visible` places below the top of the visible table, on whichever
    /// page owns a table. Mirrors `on_up`/`on_down`, detail reload included.
    fn select_visible_row(&mut self, visible: usize) {
        match self.page() {
            Page::Instances => self.instances.select_visible(visible),
            Page::Services => {
                self.services.select_visible(visible);
                self.load_selection_details();
            }
            Page::Peers => {
                self.peers.select_visible(visible);
                self.load_selection_details();
            }
            Page::Clients => {
                self.clients.select_visible(visible);
                self.load_selection_details();
            }
            Page::Pricing => self.prices.select_visible(visible),
            _ => {}
        }
    }

    /// Reload the payment and reputation history behind the selected peer and client.
    ///
    /// Called when the selection moves and after each data refresh, never from the
    /// draw path: a frame is redrawn on every keystroke and tick, and none of this
    /// changes that often.
    pub fn load_selection_details(&mut self) {
        let database = self.paths.database.clone();
        self.peer_detail = self
            .peers
            .selected()
            .map(|peer| peer.id.clone())
            .and_then(|peer_id| get_peer_detail(&database, &peer_id).ok());
        self.client_detail = self
            .clients
            .selected()
            .map(|client| client.id.clone())
            .and_then(|client_id| get_client_detail(&database, &client_id).ok());
        self.service_detail = self
            .services
            .selected()
            .map(|service| service.id.clone())
            .and_then(|service_id| get_service_detail(&database, &service_id).ok());
    }

    /// Expand or collapse the selected configuration section (Enter/Space on the
    /// Config page). A no-op on a scalar leaf, which has nothing to expand.
    pub fn toggle_selected_config_node(&mut self) {
        self.config_tree_state.toggle_selected();
    }

    /// Toggle the Instances page between the flat table and the dependency
    /// tree (grouped by father_id).
    pub fn toggle_instances_grouped(&mut self) {
        if self.page() == Page::Instances {
            self.instances_grouped = !self.instances_grouped;
            self.status = if self.instances_grouped {
                "Instances: dependency tree (g toggles)".to_string()
            } else {
                "Instances: flat list (g toggles)".to_string()
            };
        }
    }

    /// Increase or decrease the selected peer's local reputation score.
    pub fn adjust_selected_peer_reputation(&mut self, delta: i64) {
        if self.page() != Page::Peers {
            return;
        }
        let Some(peer) = self.peers.selected().cloned() else {
            self.status = "Select a peer first".to_string();
            return;
        };
        match adjust_peer_reputation(&self.paths.database, &peer.id, delta) {
            Ok(()) => {
                self.status = format!(
                    "Reputation {:+} on peer {}",
                    delta,
                    shorten(&peer.id, 16)
                );
                self.peers
                    .refresh(get_peers(&self.paths.database).unwrap_or_default());
                // The adjustment is an event like any other; show it without waiting
                // for the next refresh.
                self.load_selection_details();
            }
            Err(error) => self.status = format!("Reputation update failed: {error}"),
        }
    }

    pub fn quit(&mut self) {
        self.running = false;
    }

    pub fn close_input(&mut self) {
        self.input_mode = InputMode::Normal;
        self.input.clear();
        self.input_title.clear();
        self.edit_config_path = None;
        self.edit_config_secret = false;
        self.edit_kind = EditKind::Text;
        self.pending_action = None;
        self.credit_client_id = None;
    }

    /// True while a background `nodo` command is still running.
    pub fn command_running(&self) -> bool {
        self.command_task.is_some()
    }

    pub fn open_connect(&mut self) {
        self.input_mode = InputMode::Connect;
        self.input.clear();
        self.input_title = "Connect peer (host:port)".to_string();
        self.edit_kind = EditKind::Text;
    }

    pub fn open_config_filter(&mut self) {
        self.input_mode = InputMode::FilterConfig;
        self.input = self.config_filter.clone();
        self.input_title = "Filter configuration paths".to_string();
        self.edit_kind = EditKind::Text;
    }

    pub fn clear_config_filter(&mut self) {
        self.config_filter.clear();
        self.apply_config_filter();
        self.status = "Configuration filter cleared".to_string();
    }

    /// Resolve the config tree's current selection to the editable scalar it
    /// points at. Returns `None` when nothing is selected or the selection is a
    /// section (mapping/sequence) node, which has no single value to edit.
    pub fn selected_config_entry(&self) -> Option<ConfigEntry> {
        let selected = self.config_tree_state.selected();
        if selected.is_empty() {
            return None;
        }
        self.config_all
            .iter()
            .find(|entry| entry_tokens(entry).as_slice() == selected)
            .cloned()
    }

    /// The config path the tree selection points at, section or leaf alike.
    ///
    /// A section has no [`ConfigEntry`] of its own, so the segments come from the
    /// first leaf underneath it, truncated to the selection's depth: the path is read
    /// back out of the data rather than parsed out of the tree's `[1]`-style tokens,
    /// which a mapping key could otherwise imitate.
    fn selected_config_path(&self) -> Option<Vec<ConfigPathSegment>> {
        let selected = self.config_tree_state.selected();
        if selected.is_empty() {
            return None;
        }
        self.config_all.iter().find_map(|entry| {
            let tokens = entry_tokens(entry);
            (tokens.len() >= selected.len() && tokens[..selected.len()] == selected[..])
                .then(|| entry.path_segments[..selected.len()].to_vec())
        })
    }

    /// Whether `path` names a list.
    ///
    /// An empty list is a leaf that says so (`flatten_yaml` stops there), a populated
    /// one has no entry of its own and is recognised by its children being indexed.
    fn is_list_at(&self, path: &[ConfigPathSegment]) -> bool {
        self.config_all.iter().any(|entry| {
            if entry.path_segments == path {
                return entry.value_type == "list";
            }
            entry.path_segments.len() > path.len()
                && entry.path_segments[..path.len()] == *path
                && matches!(entry.path_segments[path.len()], ConfigPathSegment::Index(_))
        })
    }

    /// The list the selection can append to: itself when it is one, or its parent
    /// when the selection is an element of one -- so `a` works both on the list and
    /// with an element highlighted, which is where the cursor already is after
    /// adding one.
    fn selected_list_path(&self) -> Option<Vec<ConfigPathSegment>> {
        let path = self.selected_config_path()?;
        if self.is_list_at(&path) {
            return Some(path);
        }
        match path.last() {
            Some(ConfigPathSegment::Index(_)) => Some(path[..path.len() - 1].to_vec()),
            _ => None,
        }
    }

    /// The list element the selection points at: any path ending in an index, a
    /// scalar element and a whole object in a list alike.
    ///
    /// A leaf *inside* such an object (`network.FREE_PORTS_RANGE[0].START`) is not
    /// one: removing the element a key belongs to is not what selecting that key
    /// asks for, so the operator is told to select the element itself.
    fn selected_list_item(&self) -> Option<Vec<ConfigPathSegment>> {
        let path = self.selected_config_path()?;
        matches!(path.last(), Some(ConfigPathSegment::Index(_))).then_some(path)
    }

    /// Start appending an element to the list the selection points at.
    pub fn open_config_list_add(&mut self) {
        if self.page() != Page::Config {
            return;
        }
        if self.config_write_running() {
            self.status = "Busy: a configuration change is being applied".to_string();
            return;
        }
        let Some(path) = self.selected_list_path() else {
            self.status = "Select a list, or one of its elements, to add to".to_string();
            return;
        };
        self.input_mode = InputMode::AddConfigItem;
        self.input_title = format!("Add to {}", config_path_display(&path));
        self.input.clear();
        self.edit_config_path = Some(path);
        self.edit_config_secret = false;
        self.edit_kind = EditKind::Text;
    }

    /// Append what was typed to the list, as one more element.
    ///
    /// The value is parsed as YAML exactly as an ordinary edit is, so a number stays
    /// a number and an object stays an object; a glob has to be quoted (`"*.foo"`),
    /// because a bare leading `*` is a YAML alias and not a string.
    fn save_config_list_add(&mut self) {
        let Some(path) = self.edit_config_path.clone() else {
            self.status = "No list selected".to_string();
            self.close_input();
            return;
        };
        if self.input.trim().is_empty() {
            self.status = "Nothing to add: type a value".to_string();
            return;
        }
        if let Err(error) = serde_yaml::from_str::<Value>(&self.input) {
            self.status = format!("Invalid YAML value: {error}");
            return;
        }

        let value = self.input.clone();
        self.close_input();
        self.start_config_write(ConfigWrite {
            label: format!("Add to {}", config_path_display(&path)),
            expression: format!("{} += [env(NODO_TUI_V0)]", yq_path_expression(&path)),
            values: vec![("NODO_TUI_V0".to_string(), value)],
            follow_up: ConfigFollowUp::OpenBranch(path_tokens(&path)),
        });
    }

    /// Confirm removing the list element the selection points at.
    pub fn open_delete_config_item_confirm(&mut self) {
        if self.page() != Page::Config {
            return;
        }
        let Some(path) = self.selected_list_item() else {
            self.status = "Select a list element ([0], [1], ...) to remove".to_string();
            return;
        };
        let label = config_path_display(&path);
        self.input_mode = InputMode::Confirm;
        self.input_title = format!("Remove {label}? (y/N)");
        self.pending_action = Some(PendingAction::DeleteConfigItem { path, label });
    }

    /// Remove one element from a list in config.yaml.
    fn delete_config_list_item(&mut self, path: &[ConfigPathSegment], label: &str) {
        self.start_config_write(ConfigWrite {
            label: format!("Remove {label}"),
            expression: format!("del({})", yq_path_expression(path)),
            values: Vec::new(),
            // Every index after the removed one shifts down, so the selection moves
            // to the list rather than staying on a position that now holds a
            // different element than the one that was on screen.
            follow_up: ConfigFollowUp::SelectNode(
                path_tokens(&path[..path.len().saturating_sub(1)]),
            ),
        });
    }

    pub fn open_config_editor(&mut self) {
        if self.config_write_running() {
            self.status = "Busy: a configuration change is being applied".to_string();
            return;
        }
        let Some(entry) = self.selected_config_entry() else {
            self.status =
                "Select a value to edit (press Enter to expand a section)".to_string();
            return;
        };
        self.input_mode = InputMode::EditConfig;
        self.input_title = format!("Edit {} ({})", entry.path, entry.value_type);
        self.input = if entry.secret {
            String::new()
        } else {
            entry.edit_value.clone()
        };
        self.edit_config_path = Some(entry.path_segments);
        self.edit_config_secret = entry.secret;
        self.edit_kind = if entry.secret {
            // A secret is always retyped from scratch as plain text (see the blank
            // `self.input` above); a checkbox/stepper/picker would have nothing to
            // show without briefly displaying the value it exists to hide.
            EditKind::Text
        } else {
            infer_edit_kind(&entry.path, &entry.value_type)
        };
    }

    /// Apply Up/Down (and, for a checkbox, Space/←/→) inside the `EditConfig`
    /// popup: toggles a bool, steps a number by 1, or cycles an enum by one
    /// position. A no-op for freeform text, where arrow keys do nothing special.
    pub fn adjust_edit_value(&mut self, delta: i32) {
        match self.edit_kind.clone() {
            EditKind::Bool => {
                let current = self.input.trim() == "true";
                self.input = (!current).to_string();
            }
            EditKind::Number => {
                let trimmed = self.input.trim();
                let Ok(current) = trimmed.parse::<f64>() else {
                    return;
                };
                let next = current + delta as f64;
                self.input = if trimmed.contains('.') {
                    format!("{next}")
                } else {
                    format!("{}", next.round() as i64)
                };
            }
            EditKind::Enum(options) => {
                if options.is_empty() {
                    return;
                }
                let current_index = options
                    .iter()
                    .position(|option| option == self.input.trim())
                    .unwrap_or(0) as i32;
                let len = options.len() as i32;
                // Minus, where the number branch above adds: `delta` is the screen
                // direction the arrow points (+1 for Up, see the handler), and the
                // options are drawn as a vertical list in declaration order, so Up
                // has to reach the option *above* -- the one at a lower index. The
                // sign that makes Up increment a number is the opposite of the one
                // that walks a list upwards.
                let next_index = (current_index - delta).rem_euclid(len) as usize;
                self.input = options[next_index].clone();
            }
            EditKind::Text => {}
        }
    }

    /// Live side effect of a keystroke inside a text-entry popup: only
    /// `FilterConfig` reacts as-you-type (search results narrow immediately
    /// instead of waiting for Enter); `EditConfig`/`Connect` are unaffected.
    pub fn on_input_changed(&mut self) {
        if self.input_mode == InputMode::FilterConfig {
            self.config_filter = self.input.clone();
            self.apply_config_filter();
        }
    }

    /// Apply the `/` filter to the tree. Unlike the old flat table, a filter does
    /// not hide non-matching rows: it opens the ancestors of every match so the
    /// matching leaves become visible in place (their siblings stay for context),
    /// selects the first match, and lets [`draw_config`] highlight the matches.
    /// An empty needle leaves the tree's expansion/selection untouched, so
    /// clearing the filter keeps whatever the operator had open.
    pub fn apply_config_filter(&mut self) {
        let needle = self.config_filter.to_lowercase();
        if needle.is_empty() {
            return;
        }
        // Collect the opens/selection first so the immutable borrow of
        // `config_all` is released before mutating `config_tree_state`.
        let mut ancestors: Vec<Vec<String>> = Vec::new();
        let mut first_match: Option<Vec<String>> = None;
        for entry in &self.config_all {
            let matches = entry.path.to_lowercase().contains(&needle)
                || (!entry.secret && entry.value.to_lowercase().contains(&needle));
            if !matches {
                continue;
            }
            let tokens = entry_tokens(entry);
            for depth in 1..tokens.len() {
                ancestors.push(tokens[..depth].to_vec());
            }
            if first_match.is_none() {
                first_match = Some(tokens);
            }
        }
        for identifier in ancestors {
            self.config_tree_state.open(identifier);
        }
        if let Some(identifier) = first_match {
            self.config_tree_state.select(identifier);
        }
    }

    /// How many scalar values match the current filter (all of them when empty).
    fn config_match_count(&self) -> usize {
        let needle = self.config_filter.to_lowercase();
        if needle.is_empty() {
            return self.config_all.len();
        }
        self.config_all
            .iter()
            .filter(|entry| {
                entry.path.to_lowercase().contains(&needle)
                    || (!entry.secret && entry.value.to_lowercase().contains(&needle))
            })
            .count()
    }

    pub async fn submit_input(&mut self) {
        match self.input_mode {
            InputMode::Connect => self.connect(),
            InputMode::CreditClient => self.submit_credit_client(),
            InputMode::EditConfig => self.save_config_edit(),
            InputMode::AddConfigItem => self.save_config_list_add(),
            InputMode::FilterConfig => {
                self.config_filter = self.input.trim().to_string();
                self.apply_config_filter();
                let count = self.config_match_count();
                self.close_input();
                self.status = format!("Configuration filter: {count} matching values");
            }
            InputMode::PickProfile => self.submit_profile_selection(),
            // The writes confirmation answers y/n, never Enter: Enter on a
            // twelve-key diff would apply it on a keystroke meant to scroll.
            InputMode::Normal
            | InputMode::Confirm
            | InputMode::ConfirmWrites
            | InputMode::Details => {}
        }
    }

    fn connect(&mut self) {
        let target = self.input.trim().to_string();
        let valid_shape = Regex::new(r"^(\[[0-9a-fA-F:]+\]|[^:\s]+):\d{1,5}$")
            .expect("valid peer regex")
            .is_match(&target);
        let valid_port = target
            .rsplit_once(':')
            .and_then(|(_, port)| port.parse::<u16>().ok())
            .map(|port| port > 0)
            .unwrap_or(false);
        let valid = valid_shape && valid_port;
        if !valid {
            self.status = "Peer must be host:port (IPv6 may use [address]:port)".to_string();
            return;
        }
        self.close_input();
        self.spawn_command(
            CommandKind::Generic,
            "Connect peer".to_string(),
            vec!["connect".to_string(), target],
        );
    }

    // --- Clients ------------------------------------------------------------

    /// Open an amount-entry modal to credit or debit the selected client's balance.
    ///
    /// The amount is typed in `ui.DISPLAY_UNIT` -- the same unit the balance column
    /// already shows -- and, on submit, handed to `nodo credit_client`/`debit_client`
    /// (see `src/commands/credit_client.py`). Delegating to the CLI rather than
    /// writing `balance_mu` directly means the same MU conversion and client-existence
    /// check the operator gets from a shell apply here too, with one code path to keep
    /// correct instead of two.
    pub fn open_credit_client(&mut self, decrement: bool) {
        if self.page() != Page::Clients {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(client) = self.clients.selected().cloned() else {
            self.status = "Select a client first".to_string();
            return;
        };
        self.input_mode = InputMode::CreditClient;
        self.input.clear();
        self.input_title = format!(
            "{} client {} (amount, {})",
            if decrement { "Debit" } else { "Credit" },
            shorten(&client.id, 18),
            self.money.symbol
        );
        self.credit_client_id = Some(client.id);
        self.credit_client_decrement = decrement;
        self.edit_kind = EditKind::Text;
    }

    /// Validate the typed amount and run the credit/debit as a background `nodo`
    /// command, the same way a confirmed [`PendingAction`] does.
    fn submit_credit_client(&mut self) {
        let Some(client_id) = self.credit_client_id.clone() else {
            self.close_input();
            return;
        };
        let decrement = self.credit_client_decrement;
        let amount = self.input.trim().to_string();
        let valid = amount.parse::<f64>().map(|value| value > 0.0).unwrap_or(false);
        if !valid {
            self.status = "Amount must be a positive number".to_string();
            return;
        }
        self.close_input();
        let label = format!(
            "{} client {}",
            if decrement { "Debit" } else { "Credit" },
            shorten(&client_id, 18)
        );
        let command = if decrement { "debit_client" } else { "credit_client" };
        self.spawn_command(
            CommandKind::Generic,
            label,
            vec![command.to_string(), client_id, amount],
        );
    }

    fn save_config_edit(&mut self) {
        let Some(path) = self.edit_config_path.clone() else {
            self.status = "No configuration path selected".to_string();
            self.close_input();
            return;
        };
        if self.edit_config_secret && self.input.is_empty() {
            self.status = "Secret unchanged; type \"\" explicitly to clear it".to_string();
            self.close_input();
            return;
        }
        if let Err(error) = serde_yaml::from_str::<Value>(&self.input) {
            self.status = format!("Invalid YAML value: {error}");
            return;
        }

        let value = self.input.clone();
        let label = format!("Set {}", config_path_display(&path));
        self.close_input();
        self.write_config_value(label, &path, &value, ConfigFollowUp::None);
    }

    /// Write one value into config.yaml through `yq`, keeping a timestamped backup.
    ///
    /// Shared by the configuration editor, the pricing bars and the cell levers so a
    /// price is written exactly the way any other setting is -- same quoting, same
    /// backup, same restart, same failure reporting -- rather than through a second,
    /// subtly different path.
    fn write_config_value(
        &mut self,
        label: String,
        path: &[ConfigPathSegment],
        value: &str,
        follow_up: ConfigFollowUp,
    ) {
        self.start_config_write(ConfigWrite {
            label,
            expression: format!("{} = env(NODO_TUI_V0)", yq_path_expression(path)),
            values: vec![("NODO_TUI_V0".to_string(), value.to_string())],
            follow_up,
        });
    }

    /// Write several keys as one change: one backup, one `yq` run, one restart.
    ///
    /// What a lever or a profile needs. Applying its keys one at a time would leave
    /// the node briefly running a combination nobody chose, restart it once per key,
    /// and -- on a failure halfway through -- leave a posture half applied, which is
    /// the state this page exists to keep an operator out of.
    fn write_config_values(
        &mut self,
        label: String,
        writes: &[(String, String)],
        follow_up: ConfigFollowUp,
    ) {
        if writes.is_empty() {
            self.status = format!("{label}: nothing to change");
            return;
        }
        let (expression, values) = chained_write(writes);
        self.start_config_write(ConfigWrite {
            label,
            expression,
            values,
            follow_up,
        });
    }

    /// Begin a configuration change: back up, write, restart, and put the old file
    /// back if the node does not come back up.
    ///
    /// Runs in the background so the interface stays alive across a restart, which
    /// takes seconds. Only one at a time: two overlapping transactions would each
    /// hold a backup of a file the other had already changed, and a revert would
    /// then restore a state neither operator asked for.
    fn start_config_write(&mut self, write: ConfigWrite) {
        if self.config_task.is_some() {
            self.status = "Busy: a configuration change is being applied".to_string();
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        self.status = format!("{} • applying and restarting nodo…", write.label);
        self.config_follow_up = write.follow_up.clone();
        self.config_task = Some(tokio::spawn(apply_config_change(
            self.paths.yq.clone(),
            self.paths.config.clone(),
            self.paths.cache.clone(),
            write,
        )));
    }

    /// True while a configuration change is being applied, so nothing else edits
    /// config.yaml underneath it.
    pub fn config_write_running(&self) -> bool {
        self.config_task.is_some()
    }

    /// Collect a finished configuration transaction and tell the operator what
    /// state their node is in -- which, either way, is a state that matches the file
    /// on disk.
    async fn poll_config_task(&mut self) {
        if !self
            .config_task
            .as_ref()
            .map(|task| task.is_finished())
            .unwrap_or(false)
        {
            return;
        }
        let task = self.config_task.take().unwrap();
        let follow_up = std::mem::replace(&mut self.config_follow_up, ConfigFollowUp::None);
        let transaction = match task.await {
            Ok(transaction) => transaction,
            Err(error) => {
                self.status = format!("Configuration task failed: {error}");
                return;
            }
        };

        self.paths = Paths::discover();
        self.reload_after_config_write();

        match transaction.result {
            Ok(applied) => {
                match follow_up {
                    ConfigFollowUp::None => {}
                    ConfigFollowUp::OpenBranch(tokens) => {
                        self.config_tree_state.open(tokens);
                    }
                    ConfigFollowUp::SelectNode(tokens) => {
                        self.config_tree_state.select(tokens);
                    }
                }
                let note = match applied {
                    Applied::Restarted => "applied • nodo restarted".to_string(),
                    Applied::NotRunning => {
                        "applied • nodo is not serving, so it loads on next start".to_string()
                    }
                };
                self.status = format!("{} • {note}", transaction.label);
                self.app_logs
                    .push(format!("{} — {note}", transaction.label));
            }
            Err(error) => {
                self.status = error.clone();
                self.app_logs.push(format!("ERROR: {error}"));
            }
        }
        self.refresh_local(true);
    }

    // --- Cell ---------------------------------------------------------------

    /// What the selected lever is set to right now.
    pub fn cell_status(&self, lever: &Lever) -> LeverStatus {
        cell::status(lever, self.config_document.as_ref())
    }

    /// How close this node is to one of the catalogue's postures, which is what the
    /// profile bar names the page after.
    pub fn cell_profile(&self) -> cell::ProfileReport {
        cell::closest_profile(self.config_document.as_ref())
    }

    /// Route a click on the CELL page: a lever row selects it, anywhere else in an
    /// organelle's box moves the cursor into that box.
    fn click_cell(&mut self, position: Position) {
        if let Some((organelle, lever, _)) = self
            .cell
            .lever_areas
            .iter()
            .find(|(_, _, area)| area.contains(position))
            .copied()
        {
            self.cell.organelle = organelle;
            self.cell.lever = lever;
            return;
        }
        if let Some((organelle, _)) = self
            .cell
            .organelle_areas
            .iter()
            .find(|(_, area)| area.contains(position))
            .copied()
        {
            self.cell.go_to_organelle(organelle);
        }
    }

    /// Enter on the selected lever: move it to its next position, open the value
    /// editor, or jump to the page that owns the setting properly.
    pub fn toggle_selected_lever(&mut self) {
        if self.page() != Page::Cell {
            return;
        }
        if self.config_write_running() {
            self.status = "Busy: a configuration change is being applied".to_string();
            return;
        }

        let Some(lever) = self.cell.selected() else {
            return;
        };
        match lever.kind {
            LeverKind::Link(page) => {
                if let Some(index) = Page::ALL.iter().position(|candidate| *candidate == page) {
                    self.tabs.index = index;
                    self.status = format!("{} is edited here", lever.label);
                }
            }
            LeverKind::Scalar { .. } => self.open_lever_editor(),
            LeverKind::Cycle(states) => {
                let document = self.config_document.clone();
                let current = cell::status(lever, document.as_ref());
                let Some(next) = cell::next_state(lever, &current) else {
                    return;
                };
                let Some(state) = states.get(next) else {
                    return;
                };
                // DDNS with no hostname and no token is a manager that logs an error
                // every interval and publishes nothing, so this position is refused
                // rather than written: the operator is told what is missing instead
                // of being left with a setting that looks on and does nothing.
                if let Some(missing) = self.missing_prerequisite(state.writes, document.as_ref()) {
                    self.status = missing;
                    return;
                }
                let writes: Vec<(String, String)> = state
                    .writes
                    .iter()
                    .map(|(path, value)| ((*path).to_string(), (*value).to_string()))
                    .collect();
                let changes = cell::changes(state.writes, document.as_ref());
                if changes.is_empty() {
                    self.status = format!("{} is already {}", lever.label, state.label);
                    return;
                }
                self.confirm_writes(
                    format!("{} → {}", lever.label, state.label),
                    writes,
                    changes,
                    Some(lever.consequence),
                    lever.warning,
                );
            }
        }
    }

    /// Why a lever position cannot be written yet, if it cannot.
    ///
    /// Only DDNS has one: turning it on without the hostname and token it publishes
    /// to is not a posture, it is a misconfiguration. Everything else in the
    /// catalogue is writable on its own.
    fn missing_prerequisite(
        &self,
        writes: &[(&str, &str)],
        document: Option<&Value>,
    ) -> Option<String> {
        if !writes.iter().any(|(path, value)| *path == "ddns.ENABLED" && *value == "true") {
            return None;
        }
        let set = |path: &str| -> bool {
            yaml_scalar(document, &path.split('.').collect::<Vec<_>>())
                .map(|value| !value.trim().is_empty())
                .unwrap_or(false)
        };
        match (set("ddns.DOMAIN"), set("ddns.TOKEN")) {
            (true, true) => None,
            (false, true) => Some("Set the ddns hostname first (e on \"ddns hostname\")".to_string()),
            (true, false) => Some("Set the ddns token first (e on \"ddns token\")".to_string()),
            (false, false) => {
                Some("Set the ddns hostname and token first, then turn this on".to_string())
            }
        }
    }

    /// Open the ordinary config editor on the selected lever's key.
    ///
    /// A cycle lever has no single key to edit, so `e` there lists the keys it owns
    /// instead: the operator gets to see exactly which settings one named position
    /// stands for, and the Config page remains the place to break them apart.
    pub fn open_lever_editor(&mut self) {
        if self.page() != Page::Cell {
            return;
        }
        let Some(lever) = self.cell.selected() else {
            return;
        };
        let LeverKind::Scalar { path, .. } = lever.kind else {
            self.show_lever_keys(lever);
            return;
        };
        let document = self.config_document.clone();
        let current = yaml_scalar(document.as_ref(), &path.split('.').collect::<Vec<_>>())
            .unwrap_or_default();
        let segments = cell::path_segments(path);
        self.input_mode = InputMode::EditConfig;
        self.input_title = format!("Edit {path}");
        self.edit_config_path = Some(segments);
        self.edit_config_secret = lever.secret;
        self.edit_kind = if lever.secret {
            EditKind::Text
        } else {
            let value_type = document
                .as_ref()
                .and_then(|document| {
                    let mut value = document;
                    for key in path.split('.') {
                        value = value.get(key)?;
                    }
                    Some(yaml_type(value))
                })
                .unwrap_or("string");
            infer_edit_kind(path, value_type)
        };
        // A secret opens empty, so the plaintext is never on screen -- the same rule
        // the Config editor follows.
        self.input = if lever.secret { String::new() } else { current };
        self.status = lever.question.to_string();
    }

    /// Show which config keys one lever stands for, and what they say now.
    fn show_lever_keys(&mut self, lever: &'static Lever) {
        let document = self.config_document.clone();
        let mut lines = vec![
            lever.question.to_string(),
            String::new(),
            lever.consequence.to_string(),
            String::new(),
            "This one row stands for these keys:".to_string(),
        ];
        for path in lever.paths() {
            let value = yaml_scalar(document.as_ref(), &path.split('.').collect::<Vec<_>>())
                .unwrap_or_else(|| "(not a scalar or not set)".to_string());
            lines.push(format!("  {path} = {value}"));
        }
        lines.push(String::new());
        lines.push("Edit them one at a time on the CONFIG page.".to_string());
        self.details = Some(DetailsView {
            title: lever.label.to_string(),
            lines,
            scroll: 0,
        });
        self.input_mode = InputMode::Details;
    }

    /// Open the profile picker.
    pub fn open_profile_picker(&mut self) {
        if self.page() != Page::Cell {
            return;
        }
        if self.config_write_running() {
            self.status = "Busy: a configuration change is being applied".to_string();
            return;
        }

        let report = self.cell_profile();
        self.cell.profile = cell::profiles()
            .iter()
            .position(|profile| profile.id == report.profile.id)
            .unwrap_or(0);
        self.input_mode = InputMode::PickProfile;
        self.input_title = "Apply a profile".to_string();
        self.status = "↑/↓ choose • Enter see what changes • Esc cancel".to_string();
    }

    pub fn move_profile_selection(&mut self, delta: i32) {
        let count = cell::profiles().len();
        if count == 0 {
            return;
        }
        let current = self.cell.profile as i32;
        self.cell.profile = (current + delta).rem_euclid(count as i32) as usize;
    }

    /// Confirm the selected profile, showing every key it would change.
    pub fn submit_profile_selection(&mut self) {
        let Some(profile) = cell::profiles().get(self.cell.profile) else {
            self.close_input();
            return;
        };
        let document = self.config_document.clone();
        let changes = cell::changes(profile.writes, document.as_ref());
        self.close_input();
        if changes.is_empty() {
            self.status = format!("Already in {} — nothing to change", profile.label);
            return;
        }
        let writes: Vec<(String, String)> = profile
            .writes
            .iter()
            .map(|(path, value)| ((*path).to_string(), (*value).to_string()))
            .collect();
        self.confirm_writes(
            format!("Profile {}", profile.label),
            writes,
            changes,
            Some(profile.blurb),
            None,
        );
    }

    /// Show every key a change would touch, and hold it until the operator agrees.
    ///
    /// Only the keys that actually change are listed. A diff padded with keys that
    /// already hold the wanted value hides the ones that do not, which is the whole
    /// thing the operator is being asked to read.
    fn confirm_writes(
        &mut self,
        label: String,
        writes: Vec<(String, String)>,
        changes: Vec<cell::Change>,
        consequence: Option<&str>,
        warning: Option<&str>,
    ) {
        let mut lines = Vec::new();
        if let Some(warning) = warning {
            lines.push(format!("!! {warning}"));
            lines.push(String::new());
        }
        if let Some(consequence) = consequence {
            lines.push(consequence.to_string());
            lines.push(String::new());
        }
        lines.push(format!(
            "{} of {} {} change:",
            changes.len(),
            writes.len(),
            if writes.len() == 1 { "key" } else { "keys" }
        ));
        for change in &changes {
            lines.push(format!(
                "  {}   {}  →  {}",
                change.path,
                change.from.clone().unwrap_or_else(|| "(unset)".to_string()),
                change.to
            ));
        }
        lines.push(String::new());
        lines.push("config.yaml is backed up, written, and nodo is restarted.".to_string());
        lines.push("If it does not come back, the backup is put straight back.".to_string());
        self.details = Some(DetailsView {
            title: format!("{label}? (y/N)"),
            lines,
            scroll: 0,
        });
        self.input_mode = InputMode::ConfirmWrites;
        self.status = format!(
            "{} {} change • y applies and restarts nodo • n cancels",
            changes.len(),
            if changes.len() == 1 { "key" } else { "keys" }
        );
        self.pending_action = Some(PendingAction::ApplyWrites { label, writes });
    }

    /// Show how this node differs from the posture it is closest to.
    ///
    /// The most instructive thing on the page: an operator who has never opened
    /// config.yaml learns their own configuration by reading the handful of keys
    /// where they are not standard.
    pub fn show_profile_deviations(&mut self) {
        if self.page() != Page::Cell {
            return;
        }
        let report = self.cell_profile();
        let mut lines = vec![
            report.profile.blurb.to_string(),
            String::new(),
            format!("{} of {} keys match.", report.matched(), report.total),
            String::new(),
        ];
        if report.deviations.is_empty() {
            lines.push("This node is exactly in this posture.".to_string());
        } else {
            lines.push("Where this node differs (yours → the profile's):".to_string());
            for change in &report.deviations {
                lines.push(format!(
                    "  {}   {}  →  {}",
                    change.path,
                    change.from.clone().unwrap_or_else(|| "(unset)".to_string()),
                    change.to
                ));
            }
            lines.push(String::new());
            lines.push("Nothing is wrong with a deviation: it is a choice this".to_string());
            lines.push("profile does not make for you. Press p to apply the".to_string());
            lines.push("profile and drop them.".to_string());
        }
        self.details = Some(DetailsView {
            title: format!("Closest profile: {}", report.profile.label),
            lines,
            scroll: 0,
        });
        self.input_mode = InputMode::Details;
    }

    /// Show the router steps for making this node reachable (`nodo nat-guide`).
    pub fn open_nat_guide(&mut self) {
        self.spawn_command(
            CommandKind::Inspect("nat-guide".to_string()),
            "Router guide".to_string(),
            vec!["nat-guide".to_string()],
        );
    }

    // --- Pricing ----------------------------------------------------------

    /// Nudge the selected price up or down.
    ///
    /// Steps by 10 % rather than by 1, because prices span decades: +1 MU on a build
    /// price of 10 000 000 is not an edit anybody can see. The floor of 1 MU keeps a
    /// price that is being raised from zero from staying there, and a price is only
    /// ever taken to exactly 0 (free) by typing it, never by nudging.
    pub fn adjust_selected_price(&mut self, delta: i32) {
        if self.page() != Page::Pricing {
            return;
        }
        let Some(entry) = self.prices.selected().cloned() else {
            self.status = "Select a price first".to_string();
            return;
        };

        let step = ((entry.mu as f64) * 0.1).round() as u64;
        let step = step.max(1);
        let next = if delta >= 0 {
            entry.mu.saturating_add(step)
        } else {
            entry.mu.saturating_sub(step).max(1)
        };
        if next == entry.mu {
            return;
        }

        self.write_price(&entry, next.to_string());
    }

    /// Open the ordinary config editor on the selected price, for an exact value.
    pub fn open_price_editor(&mut self) {
        if self.page() != Page::Pricing {
            return;
        }
        if self.config_write_running() {
            self.status = "Busy: a configuration change is being applied".to_string();
            return;
        }

        let Some(entry) = self.prices.selected().cloned() else {
            self.status = "Select a price first".to_string();
            return;
        };
        self.input_mode = InputMode::EditConfig;
        self.input_title = format!("Edit {} (MU, {})", entry.config_label(), entry.per);
        self.input = entry.mu.to_string();
        self.edit_config_path = Some(entry.config_path());
        self.edit_config_secret = false;
        self.edit_kind = EditKind::Number;
    }

    /// Persist one price and reload, so the bars always show what is on disk rather
    /// than what the TUI hoped it wrote.
    ///
    /// A per-arch row writes under `pricing.BY_ARCH.<arch>`, which materialises the
    /// block on first edit -- so an operator who has never seen the block can still
    /// give one architecture its own price by nudging the row that shows what it
    /// currently inherits.
    fn write_price(&mut self, entry: &PriceEntry, value: String) {
        let path = entry.config_path();
        let label = format!("{} = {value} MU", entry.config_label());
        self.write_config_value(label, &path, &value, ConfigFollowUp::None);
    }

    /// Re-read prices, the scarcity ceiling and the display unit from config.yaml,
    /// keeping whatever price was selected selected.
    fn reload_money(&mut self) {
        let selected = self.prices.state_id.clone();
        let (prices, scarcity) = get_prices(&self.paths.config);
        self.prices.refresh(prices);
        if let Some(id) = selected {
            if let Some(index) = self.prices.items.iter().position(|entry| entry.key == id) {
                self.prices.state.select(Some(index));
                self.prices.state_id = Some(id);
            }
        }
        self.scarcity = scarcity;
        self.money = Money::load(&self.paths.config);
        self.guest_kernel_reserves = get_guest_kernel_reserves(&self.paths.config);
    }

    /// Everything a config write invalidates: the money view and the config table.
    fn reload_after_config_write(&mut self) {
        self.config_document = read_yaml(&self.paths.config).ok();
        self.reload_money();
        self.config_all = get_config_entries(&self.paths.config).unwrap_or_default();
        self.apply_config_filter();
    }

    /// What an hour of a reference instance costs at the current prices, with no
    /// scarcity surcharge. A worked example beats a price list for judging whether a
    /// change was the one intended.
    pub fn reference_hourly_mu(&self) -> u64 {
        let price = |key: &str| -> u64 {
            self.prices
                .items
                .iter()
                .find(|entry| entry.arch.is_none() && entry.key == key)
                .map(|entry| entry.mu)
                .unwrap_or(0)
        };
        // 256 MiB of memory, one vCPU, 10 GiB of disk -- the example in docs/PRICING.md.
        price("RAM_MU_PER_GIB_HOUR") / 4 + price("CPU_MU_PER_VCPU_HOUR") + price("DISK_MU_PER_GIB_HOUR") * 10
    }

    /// The reserve the node will apply to a guest of `arch`, if it prices that arch.
    pub fn reserve_for(&self, arch: &str) -> Option<GuestKernelReserve> {
        self.guest_kernel_reserves
            .iter()
            .find(|(tag, _)| *tag == arch)
            .map(|(_, reserve)| *reserve)
    }

    /// What a memory price actually earns the node, per GiB of HOST RAM committed.
    ///
    /// This is the number the operator is really setting and cannot see from the price
    /// alone. A service declaring one GiB is billed for one GiB, but the node had to
    /// boot its VM larger so the kernel's own footprint did not come out of the
    /// service's share -- and the node absorbs that difference deliberately, so a
    /// client never pays for the kernel underneath it. The price therefore has to
    /// cover it, and by a different amount on each architecture.
    ///
    /// Quoted against a 1 GiB guest: the ratio part of the reserve is scale-free, and
    /// the fixed part is not, so the effective rate depends on the size of the guest
    /// it is quoted for. One reference size, stated, beats a figure that silently
    /// means something different for every service.
    pub fn effective_memory_mu(&self, entry: &PriceEntry) -> Option<(f64, f64)> {
        if entry.key != "RAM_MU_PER_GIB_HOUR" {
            return None;
        }
        let arch = entry.arch?;
        let reserve = self.reserve_for(arch)?;
        let multiplier = reserve.commitment_multiplier(1024 * 1024 * 1024);
        if multiplier <= 0.0 {
            return None;
        }
        Some((entry.mu as f64 / multiplier, multiplier))
    }

    /// The memory price to set so the node earns `target` per GiB of host RAM it
    /// commits on `arch` -- i.e. the price that makes the overhead break even.
    pub fn suggested_memory_mu(&self, arch: &str, target_mu: u64) -> Option<u64> {
        let reserve = self.reserve_for(arch)?;
        let multiplier = reserve.commitment_multiplier(1024 * 1024 * 1024);
        Some((target_mu as f64 * multiplier).ceil() as u64)
    }

    pub fn execute_selected_service(&mut self) {
        if self.page() != Page::Services {
            return;
        }
        let Some(id) = self.services.state_id.clone() else {
            self.status = "Select a service first".to_string();
            return;
        };
        self.spawn_command(
            CommandKind::Generic,
            "Execute service".to_string(),
            vec!["execute".to_string(), id],
        );
    }

    /// Open the read-only Details overlay for the selected service by running
    /// `nodo inspect <id>` in the background.
    pub fn open_service_details(&mut self) {
        if self.page() != Page::Services {
            return;
        }
        let Some(id) = self.services.state_id.clone() else {
            self.status = "Select a service first".to_string();
            return;
        };
        self.status = format!("Inspecting {}…", shorten(&id, 18));
        self.spawn_command(
            CommandKind::Inspect(id.clone()),
            "Inspect service".to_string(),
            vec!["inspect".to_string(), id],
        );
    }

    /// Ask for confirmation before deleting the selected service.
    pub fn open_delete_service_confirm(&mut self) {
        if self.page() != Page::Services {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(service) = self.services.selected().cloned() else {
            self.status = "Select a service first".to_string();
            return;
        };
        let label = if service.tag.trim().is_empty() {
            shorten(&service.id, 18)
        } else {
            service.tag.clone()
        };
        self.input_mode = InputMode::Confirm;
        self.input_title = format!("Delete service {label}? (y/N)");
        self.pending_action = Some(PendingAction::DeleteService {
            id: service.id.clone(),
            label,
        });
    }

    /// Ask for confirmation before killing the selected instance.
    pub fn open_kill_instance_confirm(&mut self) {
        if self.page() != Page::Instances {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(instance) = self.instances.selected().cloned() else {
            self.status = "Select an instance first".to_string();
            return;
        };
        let label = if instance.name.trim().is_empty() {
            shorten(&instance.id, 18)
        } else {
            instance.name.clone()
        };
        self.input_mode = InputMode::Confirm;
        self.input_title = format!("Kill instance {label}? (y/N)");
        self.pending_action = Some(PendingAction::KillInstance {
            id: instance.id.clone(),
            label,
        });
    }

    /// Ask for confirmation before dropping the selected peer.
    ///
    /// `nodo disconnect` does the work, which deletes the peer row along with its
    /// addresses and contract instances — the same thing the operator would type. The
    /// peer is *forgotten*, not banned: it can re-introduce itself, or be reconnected
    /// with `c`. That is exactly what makes this useful — a peer whose addresses went
    /// stale (say another node claimed one, see `claim_uri`) is cleared out here.
    pub fn open_disconnect_peer_confirm(&mut self) {
        // Peers only. A client is not forgotten by hand -- it is ours, and expires on
        // its own -- and since the two now have a page each, `d` on Clients is simply
        // not bound rather than answered with an explanation.
        if self.page() != Page::Peers {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(peer) = self.peers.selected().cloned() else {
            self.status = "Select a peer first".to_string();
            return;
        };
        let label = shorten(&peer.id, 18);
        self.input_mode = InputMode::Confirm;
        self.input_title = format!("Forget peer {label}? (y/N)");
        self.pending_action = Some(PendingAction::DisconnectPeer {
            id: peer.id.clone(),
            label,
        });
    }

    /// Run the pending destructive action (called on `y` in a Confirm modal).
    pub async fn confirm_pending(&mut self) {
        let Some(action) = self.pending_action.take() else {
            self.close_input();
            return;
        };
        self.close_input();
        match action {
            PendingAction::DeleteConfigItem { path, label } => {
                self.delete_config_list_item(&path, &label)
            }
            PendingAction::ApplyWrites { label, writes } => {
                self.details = None;
                self.write_config_values(label, &writes, ConfigFollowUp::None);
            }
            other => {
                if let Some((label, args)) = pending_command(other) {
                    self.spawn_command(CommandKind::Generic, label, args);
                }
            }
        }
    }

    /// Scroll the Details overlay by `delta` lines (clamped).
    pub fn scroll_details(&mut self, delta: isize) {
        if let Some(details) = self.details.as_mut() {
            let max = details.lines.len().saturating_sub(1) as isize;
            details.scroll = (details.scroll as isize + delta).clamp(0, max) as usize;
        }
    }

    /// Dismiss a writes confirmation without writing anything.
    pub fn cancel_pending_writes(&mut self) {
        self.pending_action = None;
        self.close_details();
    }

    /// Close the Details overlay and return to normal mode.
    pub fn close_details(&mut self) {
        self.details = None;
        self.input_mode = InputMode::Normal;
        self.status = "Press r to refresh • q to quit".to_string();
    }

    /// Spawn a `nodo` command in the background so the UI stays responsive.
    /// Only one command runs at a time; new requests are rejected while busy.
    fn spawn_command(&mut self, kind: CommandKind, label: String, args: Vec<String>) {
        if self.command_task.is_some() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        self.status = format!("Running nodo {}…", args.join(" "));
        self.command_task = Some(tokio::spawn(run_command(kind, label, args)));
    }

    /// Collect a finished background command, route its output, and refresh.
    async fn poll_command_task(&mut self) {
        if !self
            .command_task
            .as_ref()
            .map(|task| task.is_finished())
            .unwrap_or(false)
        {
            return;
        }
        let task = self.command_task.take().unwrap();
        let outcome = match task.await {
            Ok(outcome) => outcome,
            Err(error) => {
                self.status = format!("Command task failed: {error}");
                return;
            }
        };

        self.app_logs
            .extend(outcome.stdout.lines().map(ToString::to_string));
        self.app_logs
            .extend(outcome.stderr.lines().map(|line| format!("ERROR: {line}")));

        match outcome.kind {
            CommandKind::Inspect(service_id) => {
                if outcome.success {
                    let mut lines: Vec<String> =
                        outcome.stdout.lines().map(ToString::to_string).collect();
                    if lines.is_empty() {
                        lines.push("(no output)".to_string());
                    }
                    self.details = Some(DetailsView {
                        title: format!("Service {}", shorten(&service_id, 24)),
                        lines,
                        scroll: 0,
                    });
                    self.input_mode = InputMode::Details;
                    self.status = "Service details • ↑/↓ scroll • Esc close".to_string();
                } else {
                    self.status = format!("nodo inspect failed: {}", first_line(&outcome.stderr));
                }
            }
            CommandKind::Generic => {
                self.status = if outcome.success {
                    format!("{} completed", outcome.label)
                } else {
                    format!("{} failed: {}", outcome.label, first_line(&outcome.stderr))
                };
            }
        }
        self.refresh_local(true);
    }

    pub async fn refresh(&mut self, force: bool) {
        self.refresh_local(force);
        self.poll_config_task().await;
        self.poll_command_task().await;
        self.poll_wallet_task().await;
        if self.wallet_task.is_none()
            && (force || self.last_wallet_refresh.elapsed() >= WALLET_REFRESH_INTERVAL)
        {
            self.last_wallet_refresh = Instant::now();
            self.wallet_task = Some(tokio::spawn(fetch_node_info()));
        }
    }

    fn refresh_local(&mut self, force: bool) {
        if !force && self.last_data_refresh.elapsed() < DATA_REFRESH_INTERVAL {
            return;
        }
        self.last_data_refresh = Instant::now();
        self.paths = Paths::discover();
        // Picks up an edit made on the Config page, or in a shell, so the cell's
        // levers describe the file as it is rather than as it was at start-up.
        self.config_document = read_yaml(&self.paths.config).ok();

        let services = get_services(&self.paths).unwrap_or_default();
        let service_names = services
            .iter()
            .map(|service| (service.id.clone(), service.tag.clone()))
            .collect::<HashMap<_, _>>();
        self.services.refresh(services);
        let mut instances = get_instances(&self.paths, &service_names).unwrap_or_default();
        self.derive_instance_rates(&mut instances, Instant::now());
        self.instances.refresh(instances);
        self.peers
            .refresh(get_peers(&self.paths.database).unwrap_or_default());
        self.clients
            .refresh(get_clients(&self.paths.database).unwrap_or_default());
        // After the lists, since a selection that vanished takes its detail with it.
        self.load_selection_details();
        self.node_logs = read_last_lines(&self.paths.log, 250).unwrap_or_default();

        // Prices and the display unit come from config.yaml, which the operator may be
        // editing from outside the TUI. Re-reading them here keeps the bars and every
        // balance on screen consistent with the file rather than with whatever was
        // loaded at startup.
        self.reload_money();

        self.sys.refresh_cpu();
        self.sys.refresh_memory();
        self.stats.cpu_percent = self.sys.global_cpu_info().cpu_usage().round() as u64;
        self.stats.memory_used = self.sys.used_memory();
        self.stats.memory_total = self.sys.total_memory();
        (self.stats.disk_used, self.stats.disk_total) = disk_usage(&self.paths.storage);
        if force || self.last_storage_refresh.elapsed() >= Duration::from_secs(30) {
            self.stats.storage_bytes = path_size(&self.paths.storage).unwrap_or(0);
            self.last_storage_refresh = Instant::now();
        }
        self.stats.instance_memory_current = self
            .instances
            .items
            .iter()
            .filter_map(|instance| instance.usage.memory_current)
            .sum();
        self.stats.instance_memory_reserved = self
            .instances
            .items
            .iter()
            .map(|instance| instance.memory_limit)
            .sum();
        self.stats.instance_disk_reserved = self
            .instances
            .items
            .iter()
            .map(|instance| instance.disk_limit)
            .sum();
        push_history(&mut self.cpu_history, self.stats.cpu_percent);
        let ram_percent = percent(self.stats.memory_used, self.stats.memory_total);
        push_history(&mut self.ram_history, ram_percent);
    }

    /// Turn the cumulative counters just read into rates, using the previous sweep's
    /// readings for the same instance id.
    ///
    /// The first tick after an instance appears has nothing to delta against, so its
    /// rate columns show `—` rather than `0`: the instance may well be busy, we just
    /// have not watched it for long enough to say. The counter map is rebuilt from the
    /// instances present now, which is what keeps it from growing with every instance
    /// that has ever run.
    ///
    /// `now` is passed in rather than read here so a test can advance the clock without
    /// sleeping through `MIN_RATE_INTERVAL`.
    fn derive_instance_rates(&mut self, instances: &mut [Instance], now: Instant) {
        let mut counters = HashMap::with_capacity(instances.len());
        for instance in instances.iter_mut() {
            let sample = match self.instance_counters.get(&instance.id) {
                // Too soon after the previous sweep for a trustworthy delta: keep the
                // older baseline (so the *next* sweep still measures a full interval)
                // along with the rates it produced.
                Some(previous) if now.duration_since(previous.sampled_at) < MIN_RATE_INTERVAL => {
                    previous.clone()
                }
                Some(previous) => {
                    let elapsed = now.duration_since(previous.sampled_at).as_secs_f64();
                    // usage_usec is CPU-microseconds; a microsecond of CPU per second of
                    // wall clock is 1/10_000 of a percent of one core. Dividing by
                    // 10_000 therefore reproduces observe's `(Δusage / Δwall) * 100`,
                    // deliberately un-normalised by the vCPU count so a 2-vCPU guest
                    // pinning both cores reads 200% and the two commands agree.
                    let cpu_percent = counter_rate(
                        previous.cpu_usage_usec,
                        instance.usage.cpu_usage_usec,
                        elapsed,
                    )
                    .map(|usec_per_sec| usec_per_sec / 10_000.0);
                    InstanceCounters {
                        sampled_at: now,
                        cpu_usage_usec: instance.usage.cpu_usage_usec,
                        net_rx_bytes: instance.usage.net_rx_bytes,
                        net_tx_bytes: instance.usage.net_tx_bytes,
                        cpu_percent,
                        net_rx_rate: counter_rate(
                            previous.net_rx_bytes,
                            instance.usage.net_rx_bytes,
                            elapsed,
                        ),
                        net_tx_rate: counter_rate(
                            previous.net_tx_bytes,
                            instance.usage.net_tx_bytes,
                            elapsed,
                        ),
                    }
                }
                None => InstanceCounters {
                    sampled_at: now,
                    cpu_usage_usec: instance.usage.cpu_usage_usec,
                    net_rx_bytes: instance.usage.net_rx_bytes,
                    net_tx_bytes: instance.usage.net_tx_bytes,
                    cpu_percent: None,
                    net_rx_rate: None,
                    net_tx_rate: None,
                },
            };
            instance.usage.cpu_percent = sample.cpu_percent;
            instance.usage.net_rx_rate = sample.net_rx_rate;
            instance.usage.net_tx_rate = sample.net_tx_rate;
            counters.insert(instance.id.clone(), sample);
        }
        self.instance_counters = counters;
    }

    async fn poll_wallet_task(&mut self) {
        if !self
            .wallet_task
            .as_ref()
            .map(|task| task.is_finished())
            .unwrap_or(false)
        {
            return;
        }
        let task = self.wallet_task.take().unwrap();
        match task.await {
            Ok(Ok(info)) => self.node_info = info,
            Ok(Err(error)) => self.node_info.error = error,
            Err(error) => self.node_info.error = format!("Wallet refresh failed: {error}"),
        }
    }
}

/// Run `nodo <args>` to completion off the UI thread, capturing its output.
async fn run_command(kind: CommandKind, label: String, args: Vec<String>) -> CommandOutcome {
    match Command::new("nodo")
        .args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
    {
        Ok(output) => CommandOutcome {
            kind,
            label,
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
            success: output.status.success(),
        },
        Err(error) => CommandOutcome {
            kind,
            label,
            stdout: String::new(),
            stderr: format!("Unable to launch nodo command: {error}"),
            success: false,
        },
    }
}

/// First non-blank line of `text`, trimmed — used for one-line status messages.
fn first_line(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("")
        .to_string()
}

async fn fetch_node_info() -> Result<NodeInfo, String> {
    let output = tokio::time::timeout(
        Duration::from_secs(20),
        Command::new("nodo").arg("info").output(),
    )
    .await
    .map_err(|_| "nodo info timed out after 20 seconds".to_string())?
    .map_err(|error| format!("Unable to run nodo info: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    Ok(parse_node_info(&String::from_utf8_lossy(&output.stdout)))
}

pub fn parse_node_info(output: &str) -> NodeInfo {
    let mut info = NodeInfo::default();
    for line in output.lines().map(str::trim) {
        if let Some(value) = line.strip_prefix("Nodo service is currently ") {
            info.service_status = value.trim_end_matches('.').to_string();
        } else if let Some(value) = line.strip_prefix("Nodo version: ") {
            info.version = value.to_string();
        } else if let Some(value) = line.strip_prefix("Nodo address: ") {
            info.address = value.to_string();
        } else if let Some(value) = line.strip_prefix("Reputation Proof ID: ") {
            info.reputation_proof = value.to_string();
        } else if let Some(value) = line.strip_prefix("Wallet: ") {
            let (address, balance) = parse_wallet_line(value, ", Amount:");
            info.wallet_address = address;
            info.wallet_balance = balance;
        } else if let Some(value) = line.strip_prefix("Cold Wallet: ") {
            info.cold_wallet_address = value.trim().to_string();
        }
    }
    info
}

fn parse_wallet_line(value: &str, separator: &str) -> (String, Option<f64>) {
    match value.split_once(separator) {
        Some((address, amount)) => (address.trim().to_string(), parse_erg_amount(amount)),
        None => (value.to_string(), None),
    }
}

fn parse_erg_amount(value: &str) -> Option<f64> {
    value
        .trim()
        .trim_end_matches("ERGs")
        .trim()
        .parse::<f64>()
        .ok()
}

fn get_peers(database: &Path) -> SqlResult<Vec<Peer>> {
    let connection = Connection::open(database)?;
    // Our balance on a peer lives on the `peer` table's own `balance_mu` column. The old
    // `LEFT JOIN clients c ON p.client_id = c.id` was wrong: `peer.remote_client_id`
    // is our client id *inside the remote peer*, never a key into our local
    // `clients` table, so that join surfaced a bogus balance (issue #178).
    let mut statement = connection.prepare(
        "SELECT p.id,
                COALESCE(GROUP_CONCAT(u.ip || ':' || u.port, ', '), ''),
                p.balance_mu,
                p.advertisement,
                p.reputation_score,
                COALESCE(p.remote_client_id, '')
         FROM peer p
         LEFT JOIN uri u ON p.id = u.peer_id
         GROUP BY p.id",
    )?;
    let peers = statement
        .query_map([], |row| {
            let reputation_score = row
                .get::<_, Option<i64>>(4)?
                .map(|score| score.to_string())
                .unwrap_or_else(|| "0".to_string());
            // Straight out of the advertisement the peer signed, which we store
            // verbatim: it carries every proof the peer holds, where a column of our
            // own could only ever keep the last one announced (issue #281).
            let proof_ids = row
                .get::<_, Option<Vec<u8>>>(3)?
                .and_then(|bytes| protos::Peer::decode(&*bytes).ok())
                .map(|announced| {
                    announced
                        .reputation_proofs
                        .into_iter()
                        .filter_map(|contract| {
                            contract
                                .xattrs
                                .get("token_id")
                                .and_then(|value| String::from_utf8(value.clone()).ok())
                        })
                        .filter(|token_id| !token_id.is_empty())
                        .collect()
                })
                .unwrap_or_default();
            let id: String = row.get(0)?;
            Ok(Peer {
                uris: row.get(1)?,
                balance: row.get::<_, String>(2)?,
                proof_ids,
                reputation_score,
                remote_client_id: row.get(5)?,
                // `contract_instance` isn't touched by the join above (it isn't
                // keyed by uri), so its rows are fetched per peer below.
                contracts: Vec::new(),
                id,
            })
        })?
        .collect::<SqlResult<Vec<_>>>()?;

    peers
        .into_iter()
        .map(|mut peer| {
            peer.contracts = get_peer_contracts(&connection, &peer.id)?;
            Ok(peer)
        })
        .collect()
}

/// Every payment contract instance a peer has registered. A peer's
/// `contract_instance` rows aren't reachable from the uri join `get_peers`
/// already runs, and before this the TUI surfaced none of it at all (issue #231).
fn get_peer_contracts(connection: &Connection, peer_id: &str) -> SqlResult<Vec<PeerContract>> {
    let mut statement = connection.prepare(
        "SELECT ci.contract_hash, ci.ledger_hash, ci.address, ci.mu_per_unit, l.content
         FROM contract_instance ci
         LEFT JOIN ledger l ON ci.ledger_hash = l.hash
         WHERE ci.peer_id = ?1",
    )?;
    let contracts = statement
        .query_map([peer_id], |row| {
            let ledger_hash: String = row.get(1)?;
            let ledger_content: Option<Vec<u8>> = row.get(4)?;
            // Peers only ever name a ledger by tag, so show the tag; the stored
            // hash is the fallback when the row is unresolvable or untagged.
            let ledger = ledger_content
                .and_then(|bytes| protos::contract::Ledger::decode(&*bytes).ok())
                .and_then(|ledger| ledger.tags.into_iter().next())
                .unwrap_or(ledger_hash);
            Ok(PeerContract {
                ledger,
                contract_hash: row.get(0)?,
                address: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                // Not ERG-formatted: this is a rate (MU per unit of the contract),
                // not a balance. For ERG the rate is the peg itself, 1e9.
                mu_per_unit: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
            })
        })?
        .collect();
    contracts
}

/// Adjust a peer's local reputation score by `delta`, mirroring
/// `sql_connection.update_reputation_peer`: add `delta` to the score, increment the
/// index, and record the event that explains it. Works when `reputation_proof_id` is
/// NULL (score-only), so no on-chain proof is required.
///
/// The event matters as much as the score here. Every other mover of a score writes
/// one, so a hand adjustment that did not would be the single unexplained step in a
/// peer's history — and the one an operator is most likely to have to justify later.
fn adjust_peer_reputation(database: &Path, peer_id: &str, delta: i64) -> SqlResult<()> {
    let mut connection = Connection::open(database)?;
    let transaction = connection.transaction()?;
    let (score, index): (i64, i64) = transaction.query_row(
        "SELECT COALESCE(reputation_score, 0), COALESCE(reputation_index, 0)
         FROM peer WHERE id = ?1",
        [peer_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    transaction.execute(
        "UPDATE peer SET reputation_score = ?1, reputation_index = ?2 WHERE id = ?3",
        rusqlite::params![score + delta, index + 1, peer_id],
    )?;
    // Same string as `reasons.Reason.OPERATOR_ADJUSTMENT` on the Python side.
    transaction.execute(
        "INSERT INTO reputation_events (subject_kind, subject_id, amount, reason, score_after)
         VALUES ('peer', ?1, ?2, 'operator_adjustment', ?3)",
        rusqlite::params![peer_id, delta, score + delta],
    )?;
    transaction.commit()?;
    Ok(())
}

fn get_clients(database: &Path) -> SqlResult<Vec<Client>> {
    let connection = Connection::open(database)?;
    let mut statement =
        connection.prepare("SELECT id, balance_mu, last_usage, unmetered FROM clients")?;
    let clients = statement
        .query_map([], |row| {
            let last_usage = row
                .get::<_, Option<f64>>(2)?
                .map(|value| format!("{value:.0}"))
                .unwrap_or_else(|| "—".to_string());
            Ok(Client {
                id: row.get(0)?,
                balance: row.get::<_, String>(1)?,
                last_usage,
                unmetered: row.get::<_, Option<i64>>(3)?.unwrap_or(0) != 0,
            })
        })?
        .collect();
    clients
}

/// How many rows of history a detail card asks for. Enough to read a pattern, few
/// enough that the card cannot push the table it belongs to off a short terminal.
const DETAIL_ROWS: usize = 8;

/// What we paid a peer and why its score is where it is.
fn get_peer_detail(database: &Path, peer_id: &str) -> SqlResult<PeerDetail> {
    let connection = Connection::open(database)?;
    Ok(PeerDetail {
        peer_id: peer_id.to_string(),
        payments: get_payments(&connection, "peer_id", peer_id)?,
        events: get_reputation_events(&connection, "peer", peer_id)?,
    })
}

/// A service's score and the events behind it. Scored by `service_id`, so this is the
/// history of every instance of it that ever ran here, not of the one running now.
fn get_service_detail(database: &Path, service_id: &str) -> SqlResult<ServiceDetail> {
    let connection = Connection::open(database)?;
    let score = connection
        .query_row(
            "SELECT reputation_score FROM service_reputation WHERE service_id = ?1",
            [service_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()?;
    Ok(ServiceDetail {
        service_id: service_id.to_string(),
        score,
        events: get_reputation_events(&connection, "service", service_id)?,
    })
}

/// What a client paid us, what it was given a token for, and what it is running here.
fn get_client_detail(database: &Path, client_id: &str) -> SqlResult<ClientDetail> {
    let connection = Connection::open(database)?;
    Ok(ClientDetail {
        client_id: client_id.to_string(),
        deposits: get_deposit_tokens(&connection, client_id)?,
        instances: get_client_instances(&connection, client_id)?,
        payments: get_payments(&connection, "client_id", client_id)?,
    })
}

/// Payment rows for one counterparty, newest first.
///
/// `column` is the caller's choice of `peer_id` or `client_id` and is interpolated,
/// which is safe only because both are literals in this file — the *value* is bound.
fn get_payments(connection: &Connection, column: &str, id: &str) -> SqlResult<Vec<PaymentRow>> {
    let mut statement = connection.prepare(&format!(
        "SELECT created_at, amount_mu, status, COALESCE(tx_id, ''), COALESCE(deposit_token, '')
         FROM payments WHERE {column} = ?1 ORDER BY created_at DESC, id DESC LIMIT {DETAIL_ROWS}"
    ))?;
    let payments = statement
        .query_map([id], |row| {
            Ok(PaymentRow {
                created_at: row.get(0)?,
                amount: row.get::<_, String>(1)?,
                status: row.get(2)?,
                tx_id: row.get(3)?,
                deposit_token: row.get(4)?,
            })
        })?
        .collect();
    payments
}

/// Reputation events for one subject, newest first.
fn get_reputation_events(
    connection: &Connection,
    kind: &str,
    id: &str,
) -> SqlResult<Vec<ReputationEvent>> {
    let mut statement = connection.prepare(
        "SELECT created_at, amount, reason, score_after
         FROM reputation_events
         WHERE subject_kind = ?1 AND subject_id = ?2
         ORDER BY created_at DESC, id DESC LIMIT ?3",
    )?;
    let events = statement
        .query_map(rusqlite::params![kind, id, DETAIL_ROWS as i64], |row| {
            Ok(ReputationEvent {
                created_at: row.get(0)?,
                amount: row.get(1)?,
                reason: row.get(2)?,
                score_after: row.get(3)?,
            })
        })?
        .collect();
    events
}

fn get_deposit_tokens(connection: &Connection, client_id: &str) -> SqlResult<Vec<DepositToken>> {
    let mut statement = connection.prepare(
        "SELECT id, status, created_at FROM deposit_tokens
         WHERE client_id = ?1 ORDER BY created_at DESC LIMIT ?2",
    )?;
    let tokens = statement
        .query_map(rusqlite::params![client_id, DETAIL_ROWS as i64], |row| {
            Ok(DepositToken {
                id: row.get(0)?,
                status: row.get(1)?,
                created_at: row.get(2)?,
            })
        })?
        .collect();
    tokens
}

/// The instances a client started here. `local_instances.father_id` holds the client
/// id for a top-level instance (see `start_service_iterable`), which is the only link
/// between a client and anything it runs.
fn get_client_instances(
    connection: &Connection,
    client_id: &str,
) -> SqlResult<Vec<ClientInstance>> {
    let mut statement = connection.prepare(
        "SELECT id, COALESCE(name, '') FROM local_instances
         WHERE father_id = ?1 ORDER BY name LIMIT ?2",
    )?;
    let instances = statement
        .query_map(rusqlite::params![client_id, DETAIL_ROWS as i64], |row| {
            Ok(ClientInstance {
                id: row.get(0)?,
                name: row.get(1)?,
            })
        })?
        .collect();
    instances
}

fn get_instances(
    paths: &Paths,
    service_names: &HashMap<String, String>,
) -> SqlResult<Vec<Instance>> {
    let connection = Connection::open(&paths.database)?;
    // The burn-rate columns live in `instance_consumption`, LEFT JOINed so an instance
    // with no samples yet reads NULL (→ `—`) rather than a fabricated 0. The table is
    // created by the node's migration on first run; guard for the case where the TUI
    // opens a database the node has never migrated, so a fresh DB still lists instances.
    let has_consumption = table_exists(&connection, "instance_consumption");
    let base = "SELECT li.id, li.name, li.ip, li.balance_mu, li.service_id, li.mem_limit,
                li.disk_space, li.virtualizer, li.father_id, li.cpu_period, li.cpu_quota";
    let sql = if has_consumption {
        format!(
            "{base}, ic.mu_per_second, ic.sample_count,
                CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', ic.last_refresh) AS INTEGER)
             FROM local_instances li
             LEFT JOIN instance_consumption ic ON ic.instance_id = li.id"
        )
    } else {
        format!("{base} FROM local_instances li")
    };
    let mut statement = connection.prepare(&sql)?;
    let mut instances: Vec<Instance> = statement
        .query_map([], |row| {
            let id: String = row.get(0)?;
            let service_id: String = row.get::<_, Option<String>>(4)?.unwrap_or_default();
            let service = service_names
                .get(&service_id)
                .cloned()
                .unwrap_or_else(|| shorten(&service_id, 18));
            let cpu_period: Option<i64> = row.get(9)?;
            let cpu_quota: Option<i64> = row.get(10)?;
            let cgroup = instance_cgroup_dir(paths, &id);
            // The persisted CFS pair from the `local_instances` row is the source of
            // truth (PR #251 makes both the launch and hotplug paths write the *resolved*
            // period/quota there), and it is the only answer for delegated instances,
            // which have no local cgroup. `read_cpu_max_allowance` stays as a defensive
            // fallback for a row written before #251 that still holds `0 / 0`.
            let vcpus = vcpu_allowance(cpu_period, cpu_quota)
                .or_else(|| read_cpu_max_allowance(&cgroup.join("cpu.max")));
            // Burn-rate columns are only present when the join ran; a per-second
            // average scales to per-minute / per-hour, and all four fields stay `None`
            // when the instance has no consumption row yet.
            let mu_per_second: Option<f64> = if has_consumption { row.get(11)? } else { None };
            let consumption_samples: Option<i64> = if has_consumption { row.get(12)? } else { None };
            let consumption_age_secs: Option<i64> = if has_consumption { row.get(13)? } else { None };
            Ok(Instance {
                usage: read_instance_usage(&cgroup, &id),
                vcpus,
                id,
                name: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                ip: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                balance: row.get::<_, String>(3)?,
                service,
                memory_limit: row.get::<_, Option<u64>>(5)?.unwrap_or(0),
                disk_limit: row.get::<_, Option<u64>>(6)?.unwrap_or(0),
                virtualizer: row
                    .get::<_, Option<String>>(7)?
                    .unwrap_or_else(|| "ch".to_string()),
                location: "local".to_string(),
                father_id: row.get::<_, Option<String>>(8)?.unwrap_or_default(),
                mu_per_minute: mu_per_second.map(|rate| rate * 60.0),
                mu_per_hour: mu_per_second.map(|rate| rate * 3600.0),
                consumption_samples: consumption_samples.map(|count| count as u64),
                consumption_age_secs: consumption_age_secs.map(|secs| secs as f64),
            })
        })?
        .collect::<SqlResult<Vec<_>>>()?;

    // Delegated (remote) instances live on other peers. The table carries no
    // balance / memory / disk columns, and a remote balance needs an async gRPC call
    // (see manager/metrics.py __get_metrics_external) that would block the UI.
    // We therefore surface them here with placeholders ("—") and location set
    // to the owning peer id, without any blocking network round-trip.
    let remote = get_delegated_instances(&connection, service_names)?;
    instances.extend(remote);
    Ok(instances)
}

fn get_delegated_instances(
    connection: &Connection,
    service_names: &HashMap<String, String>,
) -> SqlResult<Vec<Instance>> {
    let mut statement = connection.prepare(
        "SELECT id, peer_id, service_id, father_id FROM delegated_instances",
    )?;
    let instances = statement
        .query_map([], |row| {
            let id: String = row.get::<_, Option<String>>(0)?.unwrap_or_default();
            let peer_id: String = row.get::<_, Option<String>>(1)?.unwrap_or_default();
            let service_id: String = row.get::<_, Option<String>>(2)?.unwrap_or_default();
            let service = service_names
                .get(&service_id)
                .cloned()
                .unwrap_or_else(|| shorten(&service_id, 18));
            let location = if peer_id.is_empty() {
                "remote".to_string()
            } else {
                peer_id
            };
            Ok(Instance {
                id,
                name: String::new(),
                ip: String::new(),
                balance: "—".to_string(),
                service,
                // A delegated instance runs inside another peer: there is no local
                // cgroup and no local tap to read, so every live figure stays unset
                // and the UI shows "—" rather than a fabricated zero.
                usage: InstanceUsage::default(),
                vcpus: None,
                memory_limit: 0,
                disk_limit: 0,
                virtualizer: "remote".to_string(),
                location,
                father_id: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                // A delegated instance is charged on the owning peer, so there is no
                // local consumption row to read and no rate to show — reading it would
                // mean the same blocking gRPC round-trip already ruled out for balance.
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
            })
        })?
        .collect();
    instances
}

/// Whether a table exists, so a query can degrade gracefully against a database the
/// node has not migrated yet (e.g. a brand-new install the TUI opens first).
fn table_exists(connection: &Connection, name: &str) -> bool {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1",
            [name],
            |_| Ok(()),
        )
        .is_ok()
}

fn get_services(paths: &Paths) -> Result<Vec<Service>, io::Error> {
    let mut services = Vec::new();
    let entries = match fs::read_dir(&paths.registry) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(services),
        Err(error) => return Err(error),
    };
    for entry in entries {
        let entry = entry?;
        let id = entry.file_name().to_string_lossy().into_owned();
        let tag = read_service_tag(&paths.metadata.join(&id)).unwrap_or_else(|| "—".to_string());
        let size_bytes = path_size(&entry.path()).unwrap_or(0);
        services.push(Service {
            id,
            tag,
            size_bytes,
        });
    }
    services.sort_by(|left, right| left.tag.cmp(&right.tag).then(left.id.cmp(&right.id)));
    Ok(services)
}

fn read_service_tag(path: &Path) -> Option<String> {
    let mut bytes = Vec::new();
    File::open(path).ok()?.read_to_end(&mut bytes).ok()?;
    let metadata = protos::Metadata::decode(&*bytes).ok()?;
    metadata.hashtag?.tag.first().cloned()
}

fn get_config_entries(path: &Path) -> Result<Vec<ConfigEntry>, String> {
    let document = read_yaml(path)?;
    let mut entries = Vec::new();
    flatten_yaml(&document, &mut Vec::new(), &mut entries);
    Ok(entries)
}

fn flatten_yaml(value: &Value, path: &mut Vec<ConfigPathSegment>, entries: &mut Vec<ConfigEntry>) {
    match value {
        Value::Mapping(mapping) if !mapping.is_empty() => {
            for (key, child) in mapping {
                let key = key
                    .as_str()
                    .map(ToString::to_string)
                    .unwrap_or_else(|| yaml_value(key));
                path.push(ConfigPathSegment::Key(key));
                flatten_yaml(child, path, entries);
                path.pop();
            }
        }
        Value::Sequence(sequence) if !sequence.is_empty() => {
            for (index, child) in sequence.iter().enumerate() {
                path.push(ConfigPathSegment::Index(index));
                flatten_yaml(child, path, entries);
                path.pop();
            }
        }
        _ => {
            let display_path = config_path_display(path);
            entries.push(ConfigEntry {
                secret: is_secret_path(&display_path),
                path: display_path,
                path_segments: path.clone(),
                value: yaml_value(value),
                edit_value: yaml_edit_value(value),
                value_type: yaml_type(value).to_string(),
            });
        }
    }
}

fn yaml_value(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => value.clone(),
        Value::Sequence(value) if value.is_empty() => "[]".to_string(),
        Value::Mapping(value) if value.is_empty() => "{}".to_string(),
        _ => serde_yaml::to_string(value)
            .unwrap_or_else(|_| "<unprintable>".to_string())
            .trim()
            .to_string(),
    }
}

fn yaml_edit_value(value: &Value) -> String {
    serde_yaml::to_string(value)
        .unwrap_or_else(|_| yaml_value(value))
        .trim()
        .to_string()
}

/// Which editor widget a key gets: the closed value set some keys document, else
/// the widget for its YAML type. Shared by the Config page and the cell levers, so
/// one key is edited the same way whichever page opened it.
fn infer_edit_kind(path: &str, value_type: &str) -> EditKind {
    if let Some(options) = known_enum_values(path) {
        return EditKind::Enum(options.iter().map(|value| value.to_string()).collect());
    }
    match value_type {
        "bool" => EditKind::Bool,
        "number" => EditKind::Number,
        _ => EditKind::Text,
    }
}

fn yaml_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Sequence(_) => "list",
        Value::Mapping(_) => "object",
        Value::Tagged(_) => "tagged",
    }
}

/// The tree-identifier token for a single path segment: the key itself, or a
/// bracketed index (`[1]`) for a sequence element. Joined across a path these
/// tokens form the `Vec<String>` identifier that `tui-tree-widget` keys the
/// tree's open/selected state by, and that maps a selection back to its entry.
pub(crate) fn segment_token(segment: &ConfigPathSegment) -> String {
    match segment {
        ConfigPathSegment::Key(key) => key.clone(),
        ConfigPathSegment::Index(index) => format!("[{index}]"),
    }
}

pub(crate) fn entry_tokens(entry: &ConfigEntry) -> Vec<String> {
    path_tokens(&entry.path_segments)
}

/// The tree identifier for a path, whether or not a leaf sits at the end of it.
pub(crate) fn path_tokens(path: &[ConfigPathSegment]) -> Vec<String> {
    path.iter().map(segment_token).collect()
}

fn config_path_display(path: &[ConfigPathSegment]) -> String {
    let mut output = String::new();
    for segment in path {
        match segment {
            ConfigPathSegment::Key(key) => {
                if !output.is_empty() {
                    output.push('.');
                }
                output.push_str(key);
            }
            ConfigPathSegment::Index(index) => output.push_str(&format!("[{index}]")),
        }
    }
    output
}

pub fn yq_path_expression(path: &[ConfigPathSegment]) -> String {
    let mut output = ".".to_string();
    for segment in path {
        match segment {
            ConfigPathSegment::Key(key) => {
                let quoted = serde_json::to_string(key).expect("JSON string serialization");
                output.push_str(&format!("[{quoted}]"));
            }
            ConfigPathSegment::Index(index) => output.push_str(&format!("[{index}]")),
        }
    }
    output
}

fn is_secret_path(path: &str) -> bool {
    let normalized = path.to_ascii_lowercase();
    if ["mnemonic", "password", "secret", "private_key", "api_key"]
        .iter()
        .any(|marker| normalized.contains(marker))
    {
        return true;
    }
    let leaf = normalized
        .rsplit(['.', ']'])
        .find(|part| !part.is_empty() && !part.chars().all(|character| character.is_ascii_digit()))
        .unwrap_or(&normalized)
        .trim_start_matches('[');
    leaf == "token" || leaf.ends_with("_token")
}

fn read_last_lines(path: &Path, count: usize) -> io::Result<Vec<String>> {
    const TAIL_BYTES: u64 = 256 * 1024;
    let mut file = File::open(path)?;
    let length = file.metadata()?.len();
    let offset = length.saturating_sub(TAIL_BYTES);
    file.seek(SeekFrom::Start(offset))?;
    let mut content = Vec::new();
    file.read_to_end(&mut content)?;
    let content = String::from_utf8_lossy(&content);
    let content = if offset > 0 {
        content.split_once('\n').map(|(_, rest)| rest).unwrap_or("")
    } else {
        &content
    };
    let mut lines = VecDeque::with_capacity(count);
    for line in content.lines() {
        if lines.len() == count {
            lines.pop_front();
        }
        lines.push_back(line.to_string());
    }
    Ok(lines.into_iter().collect())
}

fn path_size(path: &Path) -> io::Result<u64> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.is_file() {
        return Ok(metadata.len());
    }
    if !metadata.is_dir() {
        return Ok(0);
    }
    let mut total = 0;
    for entry in fs::read_dir(path)? {
        total += path_size(&entry?.path()).unwrap_or(0);
    }
    Ok(total)
}

fn disk_usage(storage: &Path) -> (u64, u64) {
    let disks = Disks::new_with_refreshed_list();
    disks
        .iter()
        .filter(|disk| storage.starts_with(disk.mount_point()))
        .max_by_key(|disk| disk.mount_point().as_os_str().len())
        .map(|disk| {
            let total = disk.total_space();
            (total.saturating_sub(disk.available_space()), total)
        })
        .unwrap_or((0, 0))
}

fn read_u64(path: &Path) -> Option<u64> {
    fs::read_to_string(path).ok()?.trim().parse().ok()
}

/// Where the CH virtualizer puts an instance's cgroup: `<CGROUPS_BASE_DIR>/nodo-ch/<id>`
/// (`ch/cgroups.py::_vm_cgroup_dir`). The base comes from config, so an operator who
/// moves it keeps working readings.
fn instance_cgroup_dir(paths: &Paths, instance_id: &str) -> PathBuf {
    paths.cgroups.join("nodo-ch").join(instance_id)
}

/// One sweep of an instance's live counters, read straight from cgroupfs and sysfs.
///
/// This is the TUI's port of what `nodo observe` samples per tick. `cgroup` is the
/// instance's leaf directory (`instance_cgroup_dir`) and the tap name is re-derived from
/// the id, so nothing here needs a catalogue column or any privilege beyond reading.
/// Every read is independently fallible and independently `None`: a leaf cgroup that
/// carries `cpu`/`memory` but no delegated `io` controller yields CPU and memory but no
/// disk figures, which is the common case on a real node rather than an error worth
/// reporting.
fn read_instance_usage(cgroup: &Path, instance_id: &str) -> InstanceUsage {
    let (disk_read_bytes, disk_write_bytes) = read_cgroup_io_bytes(&cgroup.join("io.stat"));
    let tap = tap_ifname_for_instance(instance_id);
    InstanceUsage {
        memory_current: read_u64(&cgroup.join("memory.current")),
        cpu_usage_usec: read_cgroup_keyed_u64(&cgroup.join("cpu.stat"), "usage_usec"),
        disk_read_bytes,
        disk_write_bytes,
        net_rx_bytes: read_net_counter(&tap, "rx_bytes"),
        net_tx_bytes: read_net_counter(&tap, "tx_bytes"),
        // Rates need a previous sample; `App::derive_instance_rates` fills them in.
        cpu_percent: None,
        net_rx_rate: None,
        net_tx_rate: None,
    }
}

/// Read one `key value` line out of a flat cgroup v2 stat file (`cpu.stat` and
/// friends). Mirrors `observe.py::_read_cgroup_cpu_usage_usec`.
fn read_cgroup_keyed_u64(path: &Path, key: &str) -> Option<u64> {
    let contents = fs::read_to_string(path).ok()?;
    contents.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        if fields.next()? != key {
            return None;
        }
        fields.next()?.parse().ok()
    })
}

/// Cumulative block-IO from cgroup v2 `io.stat`, summed over every backing device.
///
/// Each line is `MAJ:MIN key=value …` carrying `rbytes`/`wbytes`. A port of
/// `observability.py::_cgroup_io_snapshot`, including its central caveat: when the
/// file exists but names no counters, the answer is `None` rather than `(0, 0)` — the
/// `io` controller simply is not delegated to this leaf.
fn read_cgroup_io_bytes(path: &Path) -> (Option<u64>, Option<u64>) {
    let Ok(contents) = fs::read_to_string(path) else {
        return (None, None);
    };
    let mut read_total = 0u64;
    let mut write_total = 0u64;
    let mut saw_any = false;
    for field in contents
        .lines()
        .flat_map(|line| line.split_whitespace().skip(1))
    {
        let Some((key, value)) = field.split_once('=') else {
            continue;
        };
        let Ok(number) = value.parse::<u64>() else {
            continue;
        };
        match key {
            "rbytes" => {
                read_total = read_total.saturating_add(number);
                saw_any = true;
            }
            "wbytes" => {
                write_total = write_total.saturating_add(number);
                saw_any = true;
            }
            _ => {}
        }
    }
    if saw_any {
        (Some(read_total), Some(write_total))
    } else {
        (None, None)
    }
}

/// The host tap interface the CH virtualizer creates for `instance_id`: `tap` plus the
/// first 10 hex chars of its sha1. A pure re-derivation of
/// `ch/execute.py::_create_tap`, matching `observe.py::tap_ifname_for_instance`, so it
/// can never drift from the name the runtime actually programmed.
fn tap_ifname_for_instance(instance_id: &str) -> String {
    let digest = Sha1::digest(instance_id.as_bytes());
    let mut name = String::from("tap");
    for byte in digest.iter().take(5) {
        name.push_str(&format!("{byte:02x}"));
    }
    name
}

/// A `/sys/class/net/<ifname>/statistics/<counter>` reading, or `None` when the
/// interface is gone (delegated instance, non-`ch` virtualizer, VM already torn down).
fn read_net_counter(ifname: &str, counter: &str) -> Option<u64> {
    if ifname.is_empty() {
        return None;
    }
    read_u64(
        &PathBuf::from("/sys/class/net")
            .join(ifname)
            .join("statistics")
            .join(counter),
    )
}

/// The vCPU allowance the runtime actually programmed, read from the cgroup's `cpu.max`.
///
/// `apply_cpu_limit` (`ch/cgroups.py:117`) writes the resolved CFS pair to `cpu.max`.
/// Since PR #251 that same resolved pair is persisted to the `local_instances` row (the
/// launch path stores what `_resolve_initial_resources` produced, and hotplugs persist
/// their resizes), so `get_instances` trusts the row first. This cgroupfs read is kept
/// only as a defensive fallback for a row written before #251 that still holds `0 / 0`;
/// it has no answer for delegated instances, which have no local cgroup.
///
/// The file is `"<quota|max> <period>"`; a literal `max` means unbounded, which is `None`
/// here — the same answer as an unreadable file, because in both cases there is no
/// ceiling to compare the percentage against.
fn read_cpu_max_allowance(path: &Path) -> Option<f64> {
    let contents = fs::read_to_string(path).ok()?;
    let mut fields = contents.split_whitespace();
    let quota = fields.next()?;
    let period: f64 = fields.next()?.parse().ok()?;
    if quota == "max" || period <= 0.0 {
        return None;
    }
    let quota: f64 = quota.parse().ok()?;
    (quota > 0.0).then_some(quota / period)
}

/// vCPU allowance from a CFS pair, as the maintenance tick prices it: `quota / period`.
/// `None` when either half is missing or non-positive, i.e. the instance is unbounded.
fn vcpu_allowance(period: Option<i64>, quota: Option<i64>) -> Option<f64> {
    match (period, quota) {
        (Some(period), Some(quota)) if period > 0 && quota > 0 => {
            Some(quota as f64 / period as f64)
        }
        _ => None,
    }
}

/// A monotonic counter's per-second rate between two samples.
///
/// `None` — never `0` — whenever the delta cannot be trusted: an endpoint is missing,
/// the samples are too close together to measure, or the counter went *backwards*,
/// which means the cgroup was recreated (the instance restarted) and the two readings
/// belong to different lifetimes. Same guards as `observe.py::compute_cpu_percent`.
fn counter_rate(previous: Option<u64>, current: Option<u64>, elapsed_secs: f64) -> Option<f64> {
    let (previous, current) = (previous?, current?);
    if elapsed_secs <= 0.0 || current < previous {
        return None;
    }
    Some((current - previous) as f64 / elapsed_secs)
}

fn push_history(history: &mut VecDeque<u64>, value: u64) {
    if history.len() >= HISTORY_POINTS {
        history.pop_front();
    }
    history.push_back(value);
}

pub fn percent(used: u64, total: u64) -> u64 {
    if total == 0 {
        0
    } else {
        ((used as f64 / total as f64) * 100.0).round() as u64
    }
}

pub fn format_bytes(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes} {}", UNITS[unit])
    } else {
        format!("{value:.1} {}", UNITS[unit])
    }
}

/// `format_bytes` squeezed into a table cell: a single-letter unit and no space, so a
/// used/allocated pair (`412M / 1.0G`) fits where one `format_bytes` value used to.
/// The detail card keeps the spelled-out form.
pub fn format_bytes_compact(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "K", "M", "G", "T"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes}B")
    } else if value < 10.0 {
        format!("{value:.1}{}", UNITS[unit])
    } else {
        format!("{value:.0}{}", UNITS[unit])
    }
}

/// A byte-per-second rate for a table cell, or `—` when there is no reading yet.
/// Deliberately unit-suffixed only in the header (`Net ↓/↑ B/s`) to save width.
pub fn format_rate_compact(rate: Option<f64>) -> String {
    match rate {
        Some(rate) if rate.is_finite() && rate >= 0.0 => format_bytes_compact(rate.round() as u64),
        _ => "—".to_string(),
    }
}

pub fn shorten(value: &str, max: usize) -> String {
    if value.chars().count() <= max {
        return value.to_string();
    }
    if max < 5 {
        return value.chars().take(max).collect();
    }
    let front = (max - 1) / 2;
    let back = max - front - 1;
    let start: String = value.chars().take(front).collect();
    let end: String = value
        .chars()
        .rev()
        .take(back)
        .collect::<String>()
        .chars()
        .rev()
        .collect();
    format!("{start}…{end}")
}

/// How many timestamped config backups to keep. The Python ConfigManager
/// (src/utils/config.py) prunes to the same count, so a node's backup directory
/// looks identical however the last write happened (issue #255).
const CONFIG_BACKUP_RETENTION: usize = 10;

/// Snapshot `config` to `config-<YYYYMMDDHHMMSS>.yaml` beside it, then prune to the
/// newest `CONFIG_BACKUP_RETENTION`. Timestamps are UTC so the filename sorts the
/// same whatever the machine's timezone and matches the Python path byte for byte.
/// Returns the backup path written.
fn backup_config(config: &Path) -> io::Result<PathBuf> {
    let backup = config.with_file_name(format!("config-{}.yaml", utc_stamp(SystemTime::now())));
    fs::copy(config, &backup)?;
    prune_config_backups(config)?;
    Ok(backup)
}

/// Delete all but the newest `CONFIG_BACKUP_RETENTION` `config-<stamp>.yaml` files
/// in `config`'s directory. A lexical sort is a time sort because the stamp is
/// zero-padded, so "keep the last N names" is "keep the N most recent".
fn prune_config_backups(config: &Path) -> io::Result<()> {
    let dir = config.parent().filter(|p| !p.as_os_str().is_empty());
    let dir = dir.unwrap_or_else(|| Path::new("."));
    let mut backups: Vec<PathBuf> = fs::read_dir(dir)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| is_config_backup(path))
        .collect();
    if backups.len() <= CONFIG_BACKUP_RETENTION {
        return Ok(());
    }
    backups.sort();
    for stale in &backups[..backups.len() - CONFIG_BACKUP_RETENTION] {
        let _ = fs::remove_file(stale);
    }
    Ok(())
}

/// True for a `config-<14 digits>.yaml` backup name, so pruning never touches
/// `config.yaml` itself or anything a user dropped in the directory.
fn is_config_backup(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let Some(stamp) = name
        .strip_prefix("config-")
        .and_then(|rest| rest.strip_suffix(".yaml"))
    else {
        return false;
    };
    stamp.len() == 14 && stamp.bytes().all(|byte| byte.is_ascii_digit())
}

/// Format a `SystemTime` as `YYYYMMDDHHMMSS` in UTC, without pulling in a date
/// crate. Times before the epoch (a badly wrong clock) fall back to the epoch
/// rather than panicking -- a backup is never worth aborting a config write over.
fn utc_stamp(time: SystemTime) -> String {
    let secs = time.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let days = (secs / 86_400) as i64;
    let tod = secs % 86_400;
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}{month:02}{day:02}{:02}{:02}{:02}",
        tod / 3_600,
        (tod % 3_600) / 60,
        tod % 60,
    )
}

/// Days-since-epoch to (year, month, day), UTC. Howard Hinnant's `civil_from_days`
/// -- the same arithmetic every date library uses, small enough to inline rather
/// than take a dependency for six filename characters.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    let year = yoe as i64 + era * 400 + if month <= 2 { 1 } else { 0 };
    (year, month, day)
}

#[cfg(test)]
mod tests {

    /// Mouse hit tests. Both retrace geometry the widgets do not expose, so they are
    /// pinned here: a wrong offset silently selects the neighbouring tab or row.
    mod mouse_geometry {
        use super::super::{tab_at, visible_row_at, Page, Rect};

        const BAR: Rect = Rect {
            x: 0,
            y: 0,
            width: 120,
            height: 3,
        };

        #[test]
        fn every_tab_title_maps_to_its_own_page() {
            // Walk the bar cell by cell and collect which tab each column resolves to;
            // every page must claim its title, in order, with gaps only on dividers.
            let claimed: Vec<usize> = (BAR.x..BAR.x + BAR.width)
                .filter_map(|x| tab_at(x, BAR))
                .collect();
            let mut seen: Vec<usize> = claimed.clone();
            seen.dedup();
            assert_eq!(seen, (0..Page::ALL.len()).collect::<Vec<_>>());
            for (index, page) in Page::ALL.iter().enumerate() {
                let width = claimed.iter().filter(|claim| **claim == index).count();
                assert_eq!(width, page.title().chars().count() + 2, "{page:?}");
            }
        }

        #[test]
        fn clicks_outside_any_tab_select_nothing() {
            assert_eq!(tab_at(BAR.x, BAR), None, "left border");
            assert_eq!(
                tab_at(BAR.x + BAR.width - 1, BAR),
                None,
                "past the last tab"
            );
        }

        #[test]
        fn rows_start_below_the_border_header_and_its_margin() {
            let table = Rect {
                x: 0,
                y: 4,
                width: 80,
                height: 10,
            };
            assert_eq!(visible_row_at(4, table), None, "top border");
            assert_eq!(visible_row_at(5, table), None, "header");
            assert_eq!(visible_row_at(6, table), None, "the header's bottom margin");
            assert_eq!(visible_row_at(7, table), Some(0), "first row");
            assert_eq!(visible_row_at(12, table), Some(5), "last row");
            assert_eq!(visible_row_at(13, table), None, "bottom border");
            assert_eq!(visible_row_at(99, table), None, "below the table");
        }

        #[test]
        fn a_table_with_no_room_for_rows_has_none() {
            let squeezed = Rect {
                x: 0,
                y: 0,
                width: 80,
                height: 3,
            };
            assert_eq!(visible_row_at(2, squeezed), None);
            assert_eq!(visible_row_at(0, Rect::ZERO), None, "unrendered page");
        }
    }

    /// Timestamped config.yaml backups + retention (issue #255). The prune is
    /// pinned on hand-made filenames rather than real writes so nothing here has to
    /// sleep a second per backup to get distinct UTC stamps.
    /// The cursor is two-dimensional: ←/→ walk the organelles, ↑/↓ the levers inside
    /// the one in focus. Getting that wrong makes a lever unreachable by keyboard.
    mod cell_navigation {
        use crate::app::{App, Page};
        use crate::cell::{LeverKind, LeverStatus, Organelle};

        fn on_cell_page() -> App {
            let mut app = App::default();
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Cell)
                .unwrap();
            app
        }

        #[test]
        fn left_and_right_walk_the_organelles_and_wrap() {
            let mut app = on_cell_page();
            assert_eq!(app.cell.organelle(), Organelle::Channels);
            app.on_right();
            assert_eq!(app.cell.organelle(), Organelle::Ribosomes);
            app.on_left();
            app.on_left();
            assert_eq!(
                app.cell.organelle(),
                Organelle::ALL[Organelle::ALL.len() - 1],
                "stepping left off the first organelle wraps to the last"
            );
        }

        #[test]
        fn up_and_down_move_within_the_focused_organelle() {
            let mut app = on_cell_page();
            let first = app.cell.selected().unwrap().id;
            app.on_down();
            assert_ne!(app.cell.selected().unwrap().id, first);
            app.on_up();
            assert_eq!(app.cell.selected().unwrap().id, first);
        }

        /// Moving to an organelle with fewer levers than the cursor's current row
        /// must not leave the cursor pointing past the end of it.
        #[test]
        fn the_cursor_never_points_past_the_end_of_an_organelle() {
            let mut app = on_cell_page();
            for _ in 0..6 {
                app.on_down();
            }
            for index in 0..Organelle::ALL.len() {
                let _ = index;
                app.on_right();
                assert!(
                    app.cell.selected().is_some(),
                    "{} left the cursor on nothing",
                    app.cell.organelle().title()
                );
            }
        }

        /// A lever that points at another page navigates there rather than writing
        /// a second, cruder version of what that page already does properly.
        #[test]
        fn the_prices_lever_jumps_to_the_pricing_page() {
            let mut app = on_cell_page();
            while app.cell.organelle() != Organelle::Mitochondria {
                app.on_right();
            }
            app.cell.lever = Organelle::Mitochondria
                .levers()
                .iter()
                .position(|lever| matches!(lever.kind, LeverKind::Link(_)))
                .unwrap();
            app.toggle_selected_lever();
            assert_eq!(app.page(), Page::Pricing);
        }

        /// Turning DDNS on without the hostname and token it publishes to would
        /// leave a manager logging an error every interval, so the position is
        /// refused with what is missing rather than written.
        #[test]
        fn ddns_cannot_be_turned_on_before_it_has_a_hostname() {
            let mut app = on_cell_page();
            app.config_document = Some(
                serde_yaml::from_str(
                    "ddns:\n  ENABLED: false\n  DOMAIN: \"\"\n  TOKEN: \"\"\ngeneral_flags:\n  SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF: true\n",
                )
                .unwrap(),
            );
            let lever = crate::cell::lever("findable").unwrap();
            // At "address", the next position is "+ hostname", which needs both.
            assert_eq!(app.cell_status(lever), LeverStatus::State(1));
            app.cell.lever = Organelle::Channels
                .levers()
                .iter()
                .position(|candidate| candidate.id == "findable")
                .unwrap();
            app.toggle_selected_lever();
            assert!(
                app.status.contains("hostname"),
                "the operator is told what is missing: {}",
                app.status
            );
            assert!(app.pending_action.is_none(), "nothing was queued to write");
        }

        /// A change is never written on the keystroke that asks for it: the diff is
        /// shown first, and only y applies it.
        #[test]
        fn changing_a_lever_asks_before_it_writes() {
            let mut app = on_cell_page();
            app.config_document =
                Some(serde_yaml::from_str("client:\n  ACCEPT_NEW_DEPOSITS: false\n").unwrap());
            app.cell.lever = Organelle::Ribosomes
                .levers()
                .iter()
                .position(|lever| lever.id == "outside-work")
                .unwrap();
            app.on_right();
            assert_eq!(app.cell.organelle(), Organelle::Ribosomes);
            app.toggle_selected_lever();
            assert!(app.pending_action.is_some(), "the write is held pending");
            let details = app.details.expect("the diff is shown");
            let body = details.lines.join("\n");
            assert!(body.contains("client.ACCEPT_NEW_DEPOSITS"), "{body}");
            assert!(body.contains("1 of 1 key change"), "{body}");
            assert!(
                body.contains("restarted"),
                "the confirmation says the node is restarted: {body}"
            );
        }

        /// A lever whose position can break something says so in the confirmation,
        /// where it is read before the write rather than discovered after it. The
        /// service-egress case is the one that matters: "nothing" refuses this
        /// node's own core services too.
        #[test]
        fn a_dangerous_position_carries_its_warning_into_the_confirmation() {
            let mut app = on_cell_page();
            app.config_document = Some(
                serde_yaml::from_str(
                    "service_networks:\n  blacklist: []\n  whitelist: []\n",
                )
                .unwrap(),
            );
            app.cell.organelle = Organelle::ALL
                .iter()
                .position(|organelle| *organelle == Organelle::Immune)
                .unwrap();
            app.cell.lever = Organelle::Immune
                .levers()
                .iter()
                .position(|lever| lever.id == "service-egress")
                .unwrap();
            app.toggle_selected_lever();
            let body = app.details.expect("the diff is shown").lines.join("\n");
            assert!(body.contains("core services"), "no warning shown:\n{body}");
            assert!(body.contains("service_networks.blacklist"), "{body}");
            // Flow style, the way the catalogue and the editor write a list.
            assert!(body.contains("[\"*\"]"), "{body}");
        }

        /// A lever already in the position asked for writes nothing, so Enter on it
        /// cannot cost the operator a restart for no change.
        #[test]
        fn a_lever_already_where_it_is_asked_to_go_writes_nothing() {
            let mut app = on_cell_page();
            app.config_document = Some(
                serde_yaml::from_str("network:\n  DELEGATION_TUNNEL_POLICY: always\n").unwrap(),
            );
            let levers = Organelle::Vesicles.levers();
            app.cell.organelle = Organelle::ALL
                .iter()
                .position(|organelle| *organelle == Organelle::Vesicles)
                .unwrap();
            app.cell.lever = levers
                .iter()
                .position(|lever| lever.id == "tunnel-policy")
                .unwrap();
            // "always" is position 1, so the next one is "never" -- a real change.
            app.toggle_selected_lever();
            assert!(app.pending_action.is_some());
        }
    }

    /// A configuration change and the restart that makes the node read it are one
    /// step, and the file goes back if the node does not come back. These pin the
    /// pieces of that transaction that can be exercised without systemd.
    mod config_transactions {
        use super::super::{chained_write, revert, serving_on};
        use std::fs;
        use std::path::PathBuf;

        struct TempDir(PathBuf);

        impl TempDir {
            fn new(name: &str) -> Self {
                let path = std::env::temp_dir().join(format!("nodo-tui-txn-{name}"));
                let _ = fs::remove_dir_all(&path);
                fs::create_dir_all(&path).unwrap();
                Self(path)
            }
            fn file(&self, name: &str) -> PathBuf {
                self.0.join(name)
            }
        }

        impl Drop for TempDir {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        /// A profile is a dozen keys and has to be one `yq` run: applying them one
        /// at a time would restart the node per key and could leave a posture half
        /// written.
        #[test]
        fn several_keys_become_one_chained_expression() {
            let writes = vec![
                ("client.ACCEPT_NEW_DEPOSITS".to_string(), "true".to_string()),
                ("pricing.SCARCITY_CURVE".to_string(), "2.0".to_string()),
                ("service_networks.blacklist".to_string(), "[\"*\"]".to_string()),
            ];
            let (expression, values) = chained_write(&writes);
            assert_eq!(
                expression,
                ".[\"client\"][\"ACCEPT_NEW_DEPOSITS\"] = env(NODO_TUI_V0) | \
                 .[\"pricing\"][\"SCARCITY_CURVE\"] = env(NODO_TUI_V1) | \
                 .[\"service_networks\"][\"blacklist\"] = env(NODO_TUI_V2)"
            );
            assert_eq!(values.len(), 3);
            assert_eq!(values[2].0, "NODO_TUI_V2");
            assert_eq!(values[2].1, "[\"*\"]");
        }

        /// Values travel in the environment and are never interpolated into the
        /// expression, so nothing an operator types can be read as yq syntax.
        #[test]
        fn a_value_never_appears_in_the_expression() {
            let writes = vec![(
                "ddns.TOKEN".to_string(),
                "\" | .[\"identity\"][\"MNEMONIC\"] = \"stolen\"".to_string(),
            )];
            let (expression, values) = chained_write(&writes);
            assert!(!expression.contains("stolen"), "value leaked into: {expression}");
            assert!(!expression.contains("MNEMONIC"));
            assert_eq!(values[0].1, writes[0].1);
        }

        /// The whole point of the transaction: a change that could not be restarted
        /// into is undone, so the file always describes the node that is running.
        #[test]
        fn reverting_restores_the_previous_file_byte_for_byte() {
            let dir = TempDir::new("revert");
            let config = dir.file("config.yaml");
            let backup = dir.file("config-20260101000000.yaml");
            fs::write(&backup, "network:\n  GATEWAY_PORT: 58443\n").unwrap();
            fs::write(&config, "network:\n  GATEWAY_PORT: 1\n").unwrap();

            let message = revert(&backup, &config, "Set port NOT applied: nodo did not restart");
            assert_eq!(
                fs::read_to_string(&config).unwrap(),
                "network:\n  GATEWAY_PORT: 58443\n"
            );
            assert!(message.contains("config.yaml restored"), "{message}");
            // The restored backup is byte-identical to the file beside it and records
            // no change that was ever kept, so it is not left behind as clutter.
            assert!(!backup.exists(), "the restored backup was left behind");
        }

        /// If the backup cannot be put back it is the only copy of the working
        /// configuration, so it is kept and named rather than quietly dropped.
        #[test]
        fn a_backup_that_cannot_be_restored_is_named_in_the_error() {
            let dir = TempDir::new("norestore");
            let config = dir.file("config.yaml");
            let missing = dir.file("config-20260101000000.yaml");
            fs::write(&config, "x").unwrap();
            let message = revert(&missing, &config, "Set port NOT applied");
            assert!(message.contains("COULD NOT RESTORE"), "{message}");
            assert!(message.contains("config-20260101000000.yaml"), "{message}");
        }

        /// `auto` is what an unassigned port reads as, and it is not a port: treating
        /// it as one would have the transaction wait for a node that cannot be there.
        #[tokio::test]
        async fn an_unassigned_port_reads_as_nothing_serving() {
            assert!(!serving_on(Some("auto")).await);
            assert!(!serving_on(Some("0")).await);
            assert!(!serving_on(Some("")).await);
            assert!(!serving_on(None).await);
        }

        /// The end-to-end property, against the real `yq`: one invocation writes
        /// every key, each keeping its YAML type -- a bool as a bool, a float as a
        /// float, a list as a list. Skipped where `yq` is not installed, since it is
        /// a node dependency rather than a build one.
        #[tokio::test]
        async fn one_yq_run_writes_every_key_with_its_type() {
            let Some(yq) = which_yq() else {
                eprintln!("yq not on PATH; skipping the end-to-end write");
                return;
            };
            let dir = TempDir::new("yq");
            let config = dir.file("config.yaml");
            fs::write(
                &config,
                "client:\n  ACCEPT_NEW_DEPOSITS: false\npricing:\n  SCARCITY_CURVE: 1.0\nservice_networks:\n  blacklist: []\n",
            )
            .unwrap();

            let writes = vec![
                ("client.ACCEPT_NEW_DEPOSITS".to_string(), "true".to_string()),
                ("pricing.SCARCITY_CURVE".to_string(), "2.0".to_string()),
                ("service_networks.blacklist".to_string(), "[\"*\"]".to_string()),
            ];
            let (expression, values) = chained_write(&writes);
            let mut command = tokio::process::Command::new(yq);
            command.arg("e").arg("-i").arg(&expression).arg(&config);
            for (variable, value) in &values {
                command.env(variable, value);
            }
            let status = command.status().await.unwrap();
            assert!(status.success(), "yq failed on: {expression}");

            let document: serde_yaml::Value =
                serde_yaml::from_str(&fs::read_to_string(&config).unwrap()).unwrap();
            assert_eq!(
                document["client"]["ACCEPT_NEW_DEPOSITS"],
                serde_yaml::Value::Bool(true)
            );
            assert_eq!(document["pricing"]["SCARCITY_CURVE"].as_f64(), Some(2.0));
            assert_eq!(
                document["service_networks"]["blacklist"],
                serde_yaml::from_str::<serde_yaml::Value>("[\"*\"]").unwrap()
            );
        }

        fn which_yq() -> Option<PathBuf> {
            let configured = super::super::Paths::discover().yq;
            if configured.exists() {
                return Some(configured);
            }
            std::env::var_os("PATH").and_then(|path| {
                std::env::split_paths(&path)
                    .map(|directory| directory.join("yq"))
                    .find(|candidate| candidate.exists())
            })
        }
    }

    mod config_backups {
        use super::super::{
            backup_config, civil_from_days, is_config_backup, prune_config_backups, utc_stamp,
            CONFIG_BACKUP_RETENTION,
        };
        use std::fs;
        use std::path::{Path, PathBuf};
        use std::time::{Duration, UNIX_EPOCH};

        /// A temp dir removed on drop, so a failed assertion can't strand files for
        /// the next run.
        struct TempDir(PathBuf);

        impl TempDir {
            fn new(name: &str) -> Self {
                let path = std::env::temp_dir().join(format!("nodo-tui-backup-{name}"));
                let _ = fs::remove_dir_all(&path);
                fs::create_dir_all(&path).unwrap();
                Self(path)
            }
            fn config(&self) -> PathBuf {
                self.0.join("config.yaml")
            }
            fn touch(&self, name: &str) {
                fs::write(self.0.join(name), "x").unwrap();
            }
            fn names(&self) -> Vec<String> {
                let mut names: Vec<String> = fs::read_dir(&self.0)
                    .unwrap()
                    .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
                    .collect();
                names.sort();
                names
            }
        }

        impl Drop for TempDir {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        /// The stamp is the acceptance criterion of the whole feature: it has to be
        /// UTC and it has to match Python's `time.strftime("%Y%m%d%H%M%S", gmtime)`.
        /// 1_700_000_000 is 2023-11-14 22:13:20 UTC.
        #[test]
        fn stamp_is_zero_padded_utc() {
            let t = UNIX_EPOCH + Duration::from_secs(1_700_000_000);
            assert_eq!(utc_stamp(t), "20231114221320");
            assert_eq!(utc_stamp(UNIX_EPOCH), "19700101000000");
            // A day with single-digit month/day/time must stay 14 chars wide.
            let jan = UNIX_EPOCH + Duration::from_secs(1_704_070_805); // 2024-01-01 01:00:05
            assert_eq!(utc_stamp(jan), "20240101010005");
        }

        /// The civil-date arithmetic across a leap day, since that is the one place
        /// the formula earns its keep.
        #[test]
        fn civil_from_days_handles_leap_years() {
            // 2000-02-29 is day 11016 since the 1970 epoch.
            assert_eq!(civil_from_days(11_016), (2000, 2, 29));
            assert_eq!(civil_from_days(0), (1970, 1, 1));
        }

        /// Only `config-<14 digits>.yaml` is a backup. `config.yaml` itself, and
        /// anything else in the directory, must survive a prune.
        #[test]
        fn recognises_only_timestamped_backups() {
            assert!(is_config_backup(Path::new("/x/config-20231114221320.yaml")));
            assert!(!is_config_backup(Path::new("/x/config.yaml")));
            assert!(!is_config_backup(Path::new("/x/config-2023.yaml")));
            assert!(!is_config_backup(Path::new("/x/config-20231114221320.yaml.bak")));
            assert!(!is_config_backup(Path::new("/x/notes.yaml")));
        }

        /// Twelve backups in, exactly ten survive, and they are the ten newest.
        #[test]
        fn prune_keeps_the_ten_newest() {
            let dir = TempDir::new("prune");
            fs::write(dir.config(), "current").unwrap();
            for i in 0..12 {
                dir.touch(&format!("config-202401010000{:02}.yaml", i));
            }
            dir.touch("keep-me.txt"); // a foreign file must be untouched
            prune_config_backups(&dir.config()).unwrap();

            let backups: Vec<String> = dir
                .names()
                .into_iter()
                .filter(|n| n.starts_with("config-") && n.ends_with(".yaml"))
                .collect();
            assert_eq!(backups.len(), CONFIG_BACKUP_RETENTION);
            assert_eq!(backups.first().unwrap(), "config-20240101000002.yaml");
            assert_eq!(backups.last().unwrap(), "config-20240101000011.yaml");
            assert!(dir.names().contains(&"config.yaml".to_string()));
            assert!(dir.names().contains(&"keep-me.txt".to_string()));
        }

        /// Under the cap, nothing is pruned.
        #[test]
        fn prune_is_a_noop_below_the_cap() {
            let dir = TempDir::new("under");
            for i in 0..5 {
                dir.touch(&format!("config-202401010000{:02}.yaml", i));
            }
            prune_config_backups(&dir.config()).unwrap();
            assert_eq!(dir.names().len(), 5);
        }

        /// End-to-end mirror of the Python demo in the PR: twelve *real*
        /// `backup_config` calls -- the exact function `write_config_value` runs on
        /// each save -- a second apart so every UTC stamp is distinct, must leave
        /// exactly ten backups, the oldest two pruned. `#[ignore]`d because it sleeps
        /// ~13s; run with `cargo test -- --ignored`.
        #[test]
        #[ignore]
        fn demo_twelve_real_writes_keep_ten() {
            let dir = TempDir::new("demo12");
            let cfg = dir.config();
            for i in 0..12 {
                fs::write(&cfg, format!("write-{i}")).unwrap();
                backup_config(&cfg).unwrap();
                std::thread::sleep(std::time::Duration::from_millis(1_050));
            }
            let mut backups: Vec<String> = dir
                .names()
                .into_iter()
                .filter(|n| n.starts_with("config-") && n.ends_with(".yaml"))
                .collect();
            backups.sort();
            eprintln!("RUST PATH (backup_config x12): {} kept", backups.len());
            eprintln!("  oldest kept: {}", backups.first().unwrap());
            eprintln!("  newest kept: {}", backups.last().unwrap());
            assert_eq!(backups.len(), CONFIG_BACKUP_RETENTION);
        }

        /// The write path: `backup_config` copies the live file's bytes to a
        /// timestamped name and, past the cap, leaves exactly ten behind.
        #[test]
        fn backup_config_copies_and_bounds_to_ten() {
            let dir = TempDir::new("write");
            fs::write(dir.config(), "the-current-config").unwrap();
            // Pre-seed ten old backups so this write has to prune one.
            for i in 0..10 {
                dir.touch(&format!("config-202401010000{:02}.yaml", i));
            }
            let backup = backup_config(&dir.config()).unwrap();
            assert!(is_config_backup(&backup));
            assert_eq!(fs::read_to_string(&backup).unwrap(), "the-current-config");

            let backups: Vec<String> = dir
                .names()
                .into_iter()
                .filter(|n| n.starts_with("config-") && n.ends_with(".yaml"))
                .collect();
            assert_eq!(backups.len(), CONFIG_BACKUP_RETENTION);
        }
    }

    /// Live per-instance usage. The point of these numbers is that an operator can
    /// trust them against `nodo observe`, so what is pinned here is the arithmetic and
    /// — just as importantly — every case that must read as "unknown" rather than
    /// "idle": a missing cgroup, a single sample, a restarted instance.
    mod usage {
        use super::super::{
            counter_rate, format_bytes_compact, format_rate_compact, read_cgroup_io_bytes,
            read_cgroup_keyed_u64, read_cpu_max_allowance, tap_ifname_for_instance, vcpu_allowance,
            App, Instance,
            InstanceUsage,
        };
        use std::fs;
        use std::path::PathBuf;
        use std::time::{Duration, Instant};

        fn instance(id: &str, usage: InstanceUsage) -> Instance {
            Instance {
                id: id.to_string(),
                name: id.to_string(),
                ip: String::new(),
                service: String::new(),
                balance: "0".to_string(),
                virtualizer: "ch".to_string(),
                memory_limit: 1 << 30,
                disk_limit: 0,
                vcpus: Some(2.0),
                usage,
                location: "local".to_string(),
                father_id: String::new(),
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
            }
        }

        fn cpu_only(usage_usec: Option<u64>) -> InstanceUsage {
            InstanceUsage {
                cpu_usage_usec: usage_usec,
                ..InstanceUsage::default()
            }
        }

        /// A directory under the system temp dir, removed when the guard drops, so a
        /// failing assertion cannot leave the next run reading a stale file.
        struct TempDir(PathBuf);

        impl TempDir {
            fn new(name: &str) -> Self {
                let path = std::env::temp_dir().join(format!("nodo-tui-usage-{name}"));
                let _ = fs::remove_dir_all(&path);
                fs::create_dir_all(&path).unwrap();
                Self(path)
            }

            fn write(&self, name: &str, contents: &str) -> PathBuf {
                let path = self.0.join(name);
                fs::write(&path, contents).unwrap();
                path
            }

            fn path(&self, name: &str) -> PathBuf {
                self.0.join(name)
            }
        }

        impl Drop for TempDir {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        /// The acceptance criterion of issue #245: the same counter and the same
        /// normalisation as `observe.py::compute_cpu_percent`, which is deliberately
        /// *not* divided by the vCPU count. Two seconds of wall clock against four
        /// CPU-seconds is 200% — a 2-vCPU guest pinning both cores — and if this were
        /// normalised it would read 100% and disagree with `nodo observe`.
        #[test]
        fn cpu_percent_is_cumulative_core_time_like_observe() {
            let mut app = App::default();
            let start = Instant::now();

            let mut first = vec![instance("busy", cpu_only(Some(1_000_000)))];
            app.derive_instance_rates(&mut first, start);
            assert_eq!(
                first[0].usage.cpu_percent, None,
                "one sample is not a measurement"
            );

            let mut second = vec![instance("busy", cpu_only(Some(5_000_000)))];
            app.derive_instance_rates(&mut second, start + Duration::from_secs(2));
            let cpu = second[0].usage.cpu_percent.expect("two samples, one rate");
            assert!((cpu - 200.0).abs() < 1e-6, "expected 200%, got {cpu}");
            // And the figure is legible only next to the allowance it saturates.
            assert_eq!(second[0].cpu_allowance_percent(), Some(200.0));
        }

        /// An instance that restarts gets a fresh cgroup, so its counter falls back to
        /// near zero. Subtracting the old value would produce a huge negative — or, if
        /// clamped, a fake zero. Neither is a reading.
        #[test]
        fn a_restarted_instance_reports_unknown_not_zero() {
            let mut app = App::default();
            let start = Instant::now();
            app.derive_instance_rates(&mut [instance("r", cpu_only(Some(9_000_000)))], start);

            let mut after = vec![instance("r", cpu_only(Some(12_000)))];
            app.derive_instance_rates(&mut after, start + Duration::from_secs(2));
            assert_eq!(after[0].usage.cpu_percent, None);
        }

        /// A delegated instance has no local cgroup and no local tap. Every live field
        /// stays unset however many times it is swept, because "we cannot see it from
        /// here" is not the same claim as "it is doing nothing".
        #[test]
        fn an_instance_with_no_cgroup_never_gains_a_zero() {
            let mut app = App::default();
            let start = Instant::now();
            for tick in 0..3 {
                let mut instances = vec![instance("delegated", InstanceUsage::default())];
                app.derive_instance_rates(&mut instances, start + Duration::from_secs(2 * tick));
                assert_eq!(instances[0].usage.cpu_percent, None);
                assert_eq!(instances[0].usage.net_rx_rate, None);
                assert_eq!(instances[0].usage.net_tx_rate, None);
            }
        }

        /// A forced refresh (an `r` keypress, or the sweep after a kill) can land
        /// milliseconds after the previous one. Dividing a counter delta by that is
        /// noise, so the last real rate is carried forward and the *baseline is kept*,
        /// leaving the following sweep a full interval to measure over.
        #[test]
        fn a_refresh_too_soon_carries_the_last_rate_rather_than_flickering() {
            let mut app = App::default();
            let start = Instant::now();
            app.derive_instance_rates(&mut [instance("busy", cpu_only(Some(0)))], start);
            let mut measured = vec![instance("busy", cpu_only(Some(2_000_000)))];
            app.derive_instance_rates(&mut measured, start + Duration::from_secs(2));
            let established = measured[0].usage.cpu_percent.unwrap();
            assert!((established - 100.0).abs() < 1e-6);

            let mut forced = vec![instance("busy", cpu_only(Some(2_000_100)))];
            app.derive_instance_rates(&mut forced, start + Duration::from_millis(2_050));
            assert_eq!(forced[0].usage.cpu_percent, Some(established));

            // The baseline was not advanced, so the next sweep still measures against
            // the 2s mark: 2_000_000 → 6_000_000 over 2s is another 200%.
            let mut next = vec![instance("busy", cpu_only(Some(6_000_000)))];
            app.derive_instance_rates(&mut next, start + Duration::from_secs(4));
            let cpu = next[0].usage.cpu_percent.unwrap();
            assert!((cpu - 200.0).abs() < 1e-6, "got {cpu}");
        }

        /// The counter map is rebuilt from the instances present, so a node that has
        /// churned through instances does not carry their entries forever.
        #[test]
        fn counters_for_vanished_instances_are_dropped() {
            let mut app = App::default();
            let start = Instant::now();
            app.derive_instance_rates(
                &mut [
                    instance("kept", cpu_only(Some(1))),
                    instance("gone", cpu_only(Some(1))),
                ],
                start,
            );
            assert_eq!(app.instance_counters.len(), 2);

            app.derive_instance_rates(
                &mut [instance("kept", cpu_only(Some(2)))],
                start + Duration::from_secs(2),
            );
            assert_eq!(app.instance_counters.len(), 1);
            assert!(app.instance_counters.contains_key("kept"));
        }

        #[test]
        fn net_rates_are_bytes_per_second_of_wall_clock() {
            let mut app = App::default();
            let start = Instant::now();
            let sample = |rx: u64, tx: u64| InstanceUsage {
                net_rx_bytes: Some(rx),
                net_tx_bytes: Some(tx),
                ..InstanceUsage::default()
            };
            app.derive_instance_rates(&mut [instance("n", sample(1_000, 500))], start);
            let mut after = vec![instance("n", sample(5_000, 2_500))];
            app.derive_instance_rates(&mut after, start + Duration::from_secs(2));
            assert_eq!(after[0].usage.net_rx_rate, Some(2_000.0));
            assert_eq!(after[0].usage.net_tx_rate, Some(1_000.0));
        }

        /// The tap name is re-derived rather than stored, so it has to match what
        /// `ch/execute.py::_create_tap` programmed, byte for byte. These expectations
        /// are `sha1(id)` truncated to 10 hex chars, cross-checked against
        /// `observe.py::tap_ifname_for_instance`.
        #[test]
        fn tap_name_matches_the_virtualizers_derivation() {
            assert_eq!(tap_ifname_for_instance("instance-a"), "tap494d457064");
            assert_eq!(tap_ifname_for_instance("8f4e2c"), "tapaed13fbbf7");
            // 3 for "tap" + 10 hex chars: longer would exceed IFNAMSIZ.
            assert_eq!(tap_ifname_for_instance("anything").len(), 13);
        }

        #[test]
        fn cpu_stat_is_read_by_key_not_by_line_number() {
            let dir = TempDir::new("cpu-stat");
            // `usage_usec` is first in practice, but nothing guarantees the order and
            // the neighbouring keys are all plausible-looking integers.
            let path = dir.write(
                "cpu.stat",
                "nr_periods 12\nnr_throttled 3\nusage_usec 4815162342\nuser_usec 900\n",
            );
            assert_eq!(
                read_cgroup_keyed_u64(&path, "usage_usec"),
                Some(4_815_162_342)
            );
            assert_eq!(read_cgroup_keyed_u64(&path, "system_usec"), None);
            assert_eq!(
                read_cgroup_keyed_u64(&dir.path("absent.stat"), "usage_usec"),
                None
            );
        }

        #[test]
        fn io_stat_sums_every_backing_device() {
            let dir = TempDir::new("io-stat");
            let path = dir.write(
                "io.stat",
                "8:0 rbytes=1000 wbytes=200 rios=5 wios=2\n\
                 8:16 rbytes=24 wbytes=8 rios=1 wios=1\n",
            );
            assert_eq!(read_cgroup_io_bytes(&path), (Some(1024), Some(208)));
        }

        /// The `io` controller is often not delegated to the instance's leaf cgroup, so
        /// the file exists but names no counters. That is "unknown", not "no I/O" —
        /// `observability.py::_cgroup_io_snapshot` draws the same distinction.
        #[test]
        fn an_io_stat_with_no_counters_is_unknown_not_zero() {
            let dir = TempDir::new("io-stat-empty");
            assert_eq!(
                read_cgroup_io_bytes(&dir.write("io.stat", "")),
                (None, None)
            );
            assert_eq!(read_cgroup_io_bytes(&dir.path("absent")), (None, None));
        }

        /// `read_cpu_max_allowance` parses the enforced `cpu.max`. Since PR #251 the row
        /// is the primary source (see `get_instances`); this covers the defensive fallback
        /// the TUI drops to only when a pre-#251 row still reads `0 / 0`.
        #[test]
        fn cpu_max_allowance_parses_the_enforced_cpu_max() {
            let dir = TempDir::new("cpu-max");
            assert_eq!(
                read_cpu_max_allowance(&dir.write("one", "100000 100000\n")),
                Some(1.0)
            );
            assert_eq!(
                read_cpu_max_allowance(&dir.write("two", "200000 100000\n")),
                Some(2.0)
            );
            // "max" is unbounded: there is no ceiling to read the percentage against,
            // which is the same answer as not being able to read the file at all.
            assert_eq!(read_cpu_max_allowance(&dir.write("un", "max 100000\n")), None);
            assert_eq!(read_cpu_max_allowance(&dir.path("absent")), None);
            assert_eq!(read_cpu_max_allowance(&dir.write("junk", "hello\n")), None);
        }

        #[test]
        fn vcpu_allowance_comes_from_the_cfs_pair() {
            assert_eq!(vcpu_allowance(Some(100_000), Some(200_000)), Some(2.0));
            assert_eq!(vcpu_allowance(Some(100_000), Some(50_000)), Some(0.5));
            // An unbounded instance has no allowance to be judged against.
            assert_eq!(vcpu_allowance(Some(100_000), None), None);
            assert_eq!(vcpu_allowance(Some(100_000), Some(-1)), None);
            assert_eq!(vcpu_allowance(Some(0), Some(200_000)), None);
        }

        #[test]
        fn counter_rate_refuses_what_it_cannot_measure() {
            assert_eq!(counter_rate(Some(0), Some(100), 2.0), Some(50.0));
            assert_eq!(counter_rate(None, Some(100), 2.0), None);
            assert_eq!(counter_rate(Some(0), None, 2.0), None);
            assert_eq!(counter_rate(Some(100), Some(0), 2.0), None);
            assert_eq!(counter_rate(Some(0), Some(100), 0.0), None);
        }

        /// Table cells are a few characters wide, and a truncated figure is worse than
        /// a rounded one.
        #[test]
        fn compact_bytes_stay_inside_a_table_cell() {
            assert_eq!(format_bytes_compact(0), "0B");
            assert_eq!(format_bytes_compact(512), "512B");
            assert_eq!(format_bytes_compact(432 * 1024), "432K");
            assert_eq!(format_bytes_compact(1024 * 1024), "1.0M");
            assert_eq!(format_bytes_compact(3 * 1024 * 1024 * 1024), "3.0G");
            // Widest unit is TiB, as in `format_bytes`; anything an instance can
            // actually be allocated or can actually transfer fits in five columns.
            for bytes in [0, 999, 1 << 20, 1 << 34, 900 * (1 << 40)] {
                assert!(format_bytes_compact(bytes).len() <= 5, "{bytes}");
            }
        }

        #[test]
        fn an_absent_rate_renders_as_unknown() {
            assert_eq!(format_rate_compact(None), "—");
            assert_eq!(format_rate_compact(Some(f64::NAN)), "—");
            assert_eq!(format_rate_compact(Some(2048.0)), "2.0K");
        }
    }

    /// The display layer, mirroring `tests/test_pricing_model.py` on the node side.
    /// The TUI reads the catalogue database directly, so if these two drift the
    /// operator sees a different number in the TUI than in the CLI.
    mod money {
        use super::super::Money;

        fn erg(mu_per_nanoerg: f64) -> Money {
            let mu_per_unit = mu_per_nanoerg * 1e9;
            Money {
                unit_name: "erg".to_string(),
                symbol: "ERG".to_string(),
                mu_per_unit_pow10: super::super::exact_pow10(mu_per_unit),
                mu_per_unit,
                decimals: 9,
                mu_per_nanoerg,
            }
        }

        #[test]
        fn erg_is_an_exact_digit_shift_not_a_float_division() {
            let money = erg(1.0);
            assert_eq!(money.format_raw("1000000000"), "1 ERG");
            assert_eq!(money.format_raw("5000"), "0.000005 ERG");
            // Beyond f64's integer precision, which is why formatting shifts digits.
            assert_eq!(
                money.format_raw("123456789012345678901"),
                "123456789012.345678901 ERG"
            );
        }

        #[test]
        fn the_ledger_rate_rescales_what_an_mu_is_worth() {
            // One MU is a thousand nanoERG, so the same balance is worth 1000x more.
            assert_eq!(erg(0.001).format_raw("1000000"), "1 ERG");
        }

        #[test]
        fn raw_mu_can_be_shown_untouched() {
            let money = Money {
                unit_name: "mu".to_string(),
                symbol: "MU".to_string(),
                mu_per_unit: 1.0,
                mu_per_unit_pow10: Some(0),
                decimals: 0,
                mu_per_nanoerg: 1.0,
            };
            assert_eq!(money.format_raw("14582"), "14582 MU");
        }

        #[test]
        fn a_custom_unit_rounds_to_its_declared_decimals() {
            let money = Money {
                unit_name: "usd".to_string(),
                symbol: "USD".to_string(),
                mu_per_unit: 5e8,
                mu_per_unit_pow10: None,
                decimals: 2,
                mu_per_nanoerg: 1.0,
            };
            assert_eq!(money.format_raw("5000000000"), "10 USD");
            assert_eq!(money.format_raw("1250000000"), "2.5 USD");
        }

        #[test]
        fn an_unparseable_balance_is_passed_through_not_guessed_at() {
            assert_eq!(erg(1.0).format_raw("N/A"), "N/A");
            assert_eq!(erg(1.0).format_raw(""), "");
        }

        #[test]
        fn negative_balances_keep_their_sign() {
            // Reachable: costs.ALLOW_DEBT lets an instance run past zero.
            assert_eq!(erg(1.0).format_raw("-2500000000"), "-2.5 ERG");
        }
    }

    #[test]
    fn the_price_catalogue_matches_the_config_keys() {
        // Every key the node validates must be editable here, or the bars silently
        // stop covering part of what the node charges for.
        let keys: Vec<&str> = super::PRICE_CATALOGUE.iter().map(|(key, ..)| *key).collect();
        assert_eq!(
            keys,
            vec![
                "RAM_MU_PER_GIB_HOUR",
                "CPU_MU_PER_VCPU_HOUR",
                "DISK_MU_PER_GIB_HOUR",
                "NET_MU_PER_GIB",
                "BUILD_MU",
                "TUNNEL_OPEN_MU",
                "MODIFY_RESOURCES_MU",
            ]
        );
    }

    #[test]
    fn prices_are_read_from_the_config_file() {
        let dir = std::env::temp_dir().join(format!("nodo-tui-prices-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let config = dir.join("config.yaml");
        fs::write(
            &config,
            "pricing:\n  RAM_MU_PER_GIB_HOUR: 1000000\n  BUILD_MU: 7\n  SCARCITY_MAX_MULTIPLIER: 4\n",
        )
        .unwrap();

        let (prices, scarcity) = super::get_prices(&config);
        let node_wide = |key: &str| {
            prices
                .iter()
                .find(|p| p.arch.is_none() && p.key == key)
                .unwrap()
        };
        let ram = node_wide("RAM_MU_PER_GIB_HOUR");
        assert_eq!(ram.mu, 1_000_000);
        assert!(ram.recurring);
        let build = node_wide("BUILD_MU");
        assert_eq!(build.mu, 7);
        assert!(!build.recurring);
        // An absent key is a free resource, not a crash.
        assert_eq!(node_wide("NET_MU_PER_GIB").mu, 0);
        assert_eq!(scarcity.max_multiplier, 4);

        let _ = fs::remove_dir_all(&dir);
    }

    /// Per-architecture memory pricing, as the pricing page reads it out of config.yaml.
    ///
    /// The rows are an editor for the real file, so what matters is that an arch the
    /// operator priced reads back its own price, an arch they did not is visibly
    /// inheriting the scalar rather than absent, and each row writes to the place it
    /// was read from.
    mod per_arch_pricing {
        use super::super::{get_prices, PriceEntry, PRICED_ARCHITECTURES};
        use std::fs;

        fn prices_for(name: &str, body: &str) -> Vec<PriceEntry> {
            let dir = std::env::temp_dir()
                .join(format!("nodo-tui-arch-{name}-{}", std::process::id()));
            fs::create_dir_all(&dir).unwrap();
            let config = dir.join("config.yaml");
            fs::write(&config, body).unwrap();
            let (prices, _) = get_prices(&config);
            let _ = fs::remove_dir_all(&dir);
            prices
        }

        fn arch_row<'a>(prices: &'a [PriceEntry], arch: &str) -> &'a PriceEntry {
            prices
                .iter()
                .find(|entry| entry.arch == Some(arch) && entry.key == "RAM_MU_PER_GIB_HOUR")
                .unwrap_or_else(|| panic!("no memory row for {arch}"))
        }

        #[test]
        fn every_priced_arch_gets_a_row_even_with_no_by_arch_block() {
            // A config written before per-arch pricing existed is the common case, and
            // it must not be the case where the feature is unreachable: an arch that
            // only appeared once configured would need the operator to know the block
            // exists before they could edit it anywhere.
            let prices = prices_for("absent", "pricing:\n  RAM_MU_PER_GIB_HOUR: 1000000\n");
            for arch in PRICED_ARCHITECTURES {
                let row = arch_row(&prices, arch);
                assert_eq!(row.mu, 1_000_000, "{arch} should show the scalar it inherits");
                assert!(row.inherited, "{arch} is not configured, so it is inheriting");
            }
        }

        #[test]
        fn a_configured_arch_reads_back_its_own_price() {
            let prices = prices_for(
                "configured",
                "pricing:\n  RAM_MU_PER_GIB_HOUR: 1000000\n  BY_ARCH:\n    linux/arm64:\n      RAM_MU_PER_GIB_HOUR: 1400000\n",
            );
            let arm = arch_row(&prices, "linux/arm64");
            assert_eq!(arm.mu, 1_400_000);
            assert!(!arm.inherited, "a price that is written is not inherited");

            // And the arch that was NOT given one still inherits, rather than picking
            // up its neighbour's.
            let amd = arch_row(&prices, "linux/amd64");
            assert_eq!(amd.mu, 1_000_000);
            assert!(amd.inherited);
        }

        #[test]
        fn a_per_arch_row_writes_under_its_own_architecture() {
            // The row is read from `pricing.BY_ARCH.<arch>.<key>` and must be written
            // back there. Writing to `pricing.<key>` instead would silently retune
            // every architecture from a row labelled with one.
            let prices = prices_for(
                "paths",
                "pricing:\n  RAM_MU_PER_GIB_HOUR: 1000000\n",
            );
            let arm = arch_row(&prices, "linux/arm64");
            assert_eq!(
                super::super::yq_path_expression(&arm.config_path()),
                ".[\"pricing\"][\"BY_ARCH\"][\"linux/arm64\"][\"RAM_MU_PER_GIB_HOUR\"]"
            );
            assert_eq!(arm.config_label(), "pricing.BY_ARCH.linux/arm64.RAM_MU_PER_GIB_HOUR");

            let scalar = prices
                .iter()
                .find(|entry| entry.arch.is_none() && entry.key == "RAM_MU_PER_GIB_HOUR")
                .unwrap();
            assert_eq!(
                super::super::yq_path_expression(&scalar.config_path()),
                ".[\"pricing\"][\"RAM_MU_PER_GIB_HOUR\"]"
            );
            assert_eq!(scalar.config_label(), "pricing.RAM_MU_PER_GIB_HOUR");
        }

        #[test]
        fn only_memory_is_offered_per_architecture() {
            // The node hands a guest the vCPUs and the image it asked for whatever
            // architecture it is, so nothing else has a per-arch cost to recover.
            // Offering a per-arch CPU price here would let an operator write config the
            // node rejects (`config_validation._validate_pricing_by_arch`).
            let prices = prices_for("scope", "pricing:\n  RAM_MU_PER_GIB_HOUR: 1000000\n");
            let per_arch: Vec<&str> = prices
                .iter()
                .filter(|entry| entry.arch.is_some())
                .map(|entry| entry.key)
                .collect();
            assert!(
                per_arch.iter().all(|key| *key == "RAM_MU_PER_GIB_HOUR"),
                "a non-memory price was offered per arch: {per_arch:?}"
            );
        }

        #[test]
        fn the_reserve_defaults_match_the_nodes_and_config_overrides_win() {
            // The page advises against the overhead the node will ACTUALLY apply, so an
            // operator who has measured their own guest kernel and corrected the config
            // must be advised against their figure, not against the shipped default.
            let dir = std::env::temp_dir()
                .join(format!("nodo-tui-reserve-{}", std::process::id()));
            fs::create_dir_all(&dir).unwrap();
            let config = dir.join("config.yaml");

            fs::write(&config, "pricing:\n  RAM_MU_PER_GIB_HOUR: 1\n").unwrap();
            let defaults = super::super::get_guest_kernel_reserves(&config);
            let amd = defaults
                .iter()
                .find(|(arch, _)| *arch == "linux/amd64")
                .unwrap()
                .1;
            let arm = defaults
                .iter()
                .find(|(arch, _)| *arch == "linux/arm64")
                .unwrap()
                .1;
            // The measured difference: amd64's kernel image, percpu areas and reserved
            // low memory cost more than arm64's. If these ever match, the per-arch
            // constant has stopped being per-arch.
            assert!(
                amd.fixed_mib > arm.fixed_mib,
                "amd64 measured costlier than arm64; the defaults must say so"
            );

            fs::write(
                &config,
                "virtualizers:\n  ch:\n    GUEST_KERNEL_RESERVE:\n      linux/amd64:\n        MIB: 64\n        RATIO: 0.1\n",
            )
            .unwrap();
            let overridden = super::super::get_guest_kernel_reserves(&config);
            let amd = overridden
                .iter()
                .find(|(arch, _)| *arch == "linux/amd64")
                .unwrap()
                .1;
            assert_eq!(amd.fixed_mib, 64);
            assert!((amd.ratio - 0.1).abs() < f64::EPSILON);
            // The arch that was not overridden keeps its measured default.
            let arm = overridden
                .iter()
                .find(|(arch, _)| *arch == "linux/arm64")
                .unwrap()
                .1;
            assert_eq!(arm.fixed_mib, 32);

            let _ = fs::remove_dir_all(&dir);
        }
    }

    use super::*;

    /// A database with just the tables `get_peers` touches, one peer, and
    /// whatever contract instances the caller asks for.
    fn peer_database(dir: &Path, instances: &[(&str, &str, &str, Option<&[u8]>)]) -> PathBuf {
        let path = dir.join("database.sqlite");
        let connection = Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE peer (id TEXT PRIMARY KEY, balance_mu TEXT, advertisement BLOB,
                                    reputation_score INTEGER, remote_client_id TEXT);
                 CREATE TABLE uri (id INTEGER PRIMARY KEY, peer_id TEXT, ip TEXT, port INTEGER);
                 CREATE TABLE ledger (hash TEXT PRIMARY KEY, content BLOB);
                 CREATE TABLE contract_instance (id INTEGER PRIMARY KEY, address TEXT,
                                    ledger_hash TEXT, contract_hash TEXT, peer_id TEXT,
                                    mu_per_unit TEXT);
                 INSERT INTO peer VALUES ('peer-1', '1000', NULL, 7, 'cli-7f3a');",
            )
            .unwrap();
        for (contract_hash, ledger_hash, address, ledger_content) in instances {
            connection
                .execute(
                    "INSERT INTO contract_instance (address, ledger_hash, contract_hash, peer_id, mu_per_unit)
                     VALUES (?1, ?2, ?3, 'peer-1', '500')",
                    rusqlite::params![address, ledger_hash, contract_hash],
                )
                .unwrap();
            if let Some(content) = ledger_content {
                connection
                    .execute(
                        "INSERT OR IGNORE INTO ledger (hash, content) VALUES (?1, ?2)",
                        rusqlite::params![ledger_hash, content],
                    )
                    .unwrap();
            }
        }
        path
    }

    fn ergo_ledger_bytes() -> Vec<u8> {
        let ledger = protos::contract::Ledger {
            tags: vec!["ergo".to_string()],
            prose: String::new(),
            formal: Vec::new(),
        };
        ledger.encode_to_vec()
    }

    #[test]
    fn peer_contracts_resolve_the_ledger_tag() {
        // Peers name a ledger by tag; the stored hash is meaningless to a human.
        let dir = std::env::temp_dir().join("nodo-tui-test-ledger-tag");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let bytes = ergo_ledger_bytes();
        let database = peer_database(
            &dir,
            &[("contract-hash-1", "ledger-hash-1", "addr-1", Some(&bytes))],
        );

        let peers = get_peers(&database).unwrap();
        assert_eq!(peers.len(), 1);
        assert_eq!(peers[0].contracts.len(), 1);
        let contract = &peers[0].contracts[0];
        assert_eq!(contract.ledger, "ergo");
        assert_eq!(contract.contract_hash, "contract-hash-1");
        assert_eq!(contract.address, "addr-1");
        assert_eq!(contract.mu_per_unit, "500");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn peer_contracts_fall_back_to_the_raw_hash_when_the_ledger_is_unresolvable() {
        let dir = std::env::temp_dir().join("nodo-tui-test-ledger-fallback");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        // No matching `ledger` row at all: better to show the hash than nothing.
        let database = peer_database(&dir, &[("contract-hash-1", "ledger-hash-1", "addr-1", None)]);

        let peers = get_peers(&database).unwrap();
        assert_eq!(peers[0].contracts[0].ledger, "ledger-hash-1");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn every_contract_instance_of_a_peer_is_returned() {
        // The pre-#231 lookup could only ever surface a single instance.
        let dir = std::env::temp_dir().join("nodo-tui-test-multi-contract");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let bytes = ergo_ledger_bytes();
        let database = peer_database(
            &dir,
            &[
                ("contract-a", "ledger-hash-1", "addr-a", Some(&bytes)),
                ("contract-b", "ledger-hash-2", "addr-b", None),
            ],
        );

        let peers = get_peers(&database).unwrap();
        let hashes: Vec<&str> = peers[0]
            .contracts
            .iter()
            .map(|contract| contract.contract_hash.as_str())
            .collect();
        assert_eq!(hashes, vec!["contract-a", "contract-b"]);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_peer_without_contracts_still_loads() {
        let dir = std::env::temp_dir().join("nodo-tui-test-no-contract");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let database = peer_database(&dir, &[]);

        let peers = get_peers(&database).unwrap();
        assert_eq!(peers.len(), 1);
        assert!(peers[0].contracts.is_empty());
        // Read off the `peer` row itself, never joined against our own `clients`
        // table -- that join is the bug #178 fixed.
        assert_eq!(peers[0].remote_client_id, "cli-7f3a");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn parses_nodo_info_wallets() {
        let output = "Nodo service is currently running.\n\
Nodo version: abc123\n\
Nodo address: 10.0.0.1:5000\n\
Reputation Proof ID: proof-id\n\
Wallet: 9wallet, Amount: 1.25 ERGs\n\
Cold Wallet: 9cold\n";
        let info = parse_node_info(output);
        assert_eq!(info.service_status, "running");
        assert_eq!(info.wallet_address, "9wallet");
        assert_eq!(info.wallet_balance, Some(1.25));
        assert_eq!(info.cold_wallet_address, "9cold");
    }

    #[test]
    fn flattens_all_yaml_leaf_values_and_masks_secrets() {
        let value: Value = serde_yaml::from_str(
            "network:\n  port: 5000\nidentity:\n  MNEMONIC: name words\nledgers:\n  ergo:\n    WALLET_MNEMONIC: secret words\nservers:\n  - name: packer\n    id: abc\nempty: []\n",
        )
        .unwrap();
        let mut entries = Vec::new();
        flatten_yaml(&value, &mut Vec::new(), &mut entries);
        let paths: Vec<_> = entries.iter().map(|entry| entry.path.as_str()).collect();
        assert!(paths.contains(&"network.port"));
        assert!(paths.contains(&"servers[0].id"));
        assert!(paths.contains(&"empty"));
        let mnemonic = entries
            .iter()
            .find(|entry| entry.path.ends_with("WALLET_MNEMONIC"))
            .unwrap();
        assert!(mnemonic.secret);
        assert_eq!(mnemonic.display_value(), "•••••••• (set)");

        // The node's name is a secret of the same kind, and losing it is worse: it
        // orphans every deposit and every reputation entry recorded against this node.
        let identity = entries
            .iter()
            .find(|entry| entry.path == "identity.MNEMONIC")
            .unwrap();
        assert!(identity.secret);
        assert_eq!(identity.display_value(), "•••••••• (set)");
    }

    #[test]
    fn editor_preserves_ambiguous_string_types() {
        let value: Value = serde_yaml::from_str("value: 'true'\nempty: ''\n").unwrap();
        let mut entries = Vec::new();
        flatten_yaml(&value, &mut Vec::new(), &mut entries);
        let string_bool = entries.iter().find(|entry| entry.path == "value").unwrap();
        assert_eq!(string_bool.value_type, "string");
        assert!(matches!(
            serde_yaml::from_str::<Value>(&string_bool.edit_value).unwrap(),
            Value::String(_)
        ));
    }

    fn config_entry(path: &str, value: &str, edit_value: &str, value_type: &str, secret: bool) -> ConfigEntry {
        ConfigEntry {
            path: path.to_string(),
            path_segments: path
                .split('.')
                .map(|key| ConfigPathSegment::Key(key.to_string()))
                .collect(),
            value: value.to_string(),
            edit_value: edit_value.to_string(),
            value_type: value_type.to_string(),
            secret,
        }
    }

    fn select_config_entry(app: &mut App, entry: ConfigEntry) {
        app.config_tree_state.select(entry_tokens(&entry));
        app.config_all = vec![entry];
    }

    /// An app on the Config page whose tree is the real `flatten_yaml` reading of
    /// `yaml`, so a list is a leaf or a section here for exactly the reason it is one
    /// on screen.
    fn on_config_page(yaml: &str) -> App {
        let document: Value = serde_yaml::from_str(yaml).unwrap();
        let mut entries = Vec::new();
        flatten_yaml(&document, &mut Vec::new(), &mut entries);
        let mut app = App::default();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Config)
            .unwrap();
        app.config_all = entries;
        app
    }

    fn key(path: &str) -> Vec<String> {
        path.split('.').map(ToString::to_string).collect()
    }

    #[test]
    fn an_empty_list_can_be_added_to_where_it_sits() {
        // `service_networks.blacklist: []` is a leaf: there is no element to select,
        // so `a` has to work on the list itself or the list can never be filled.
        let mut app = on_config_page("service_networks:\n  blacklist: []\n");
        app.config_tree_state
            .select(key("service_networks.blacklist"));

        assert_eq!(
            app.selected_list_path(),
            Some(vec![
                ConfigPathSegment::Key("service_networks".to_string()),
                ConfigPathSegment::Key("blacklist".to_string()),
            ])
        );
    }

    #[test]
    fn a_populated_list_can_be_added_to_from_the_list_or_from_an_element() {
        // Once it has elements the list is a section with no entry of its own, and
        // the cursor sits on an element after every add -- both have to work.
        let mut app = on_config_page("service_networks:\n  blacklist: [\"*.a\", \"*.b\"]\n");
        let list = vec![
            ConfigPathSegment::Key("service_networks".to_string()),
            ConfigPathSegment::Key("blacklist".to_string()),
        ];

        app.config_tree_state
            .select(key("service_networks.blacklist"));
        assert_eq!(app.selected_list_path(), Some(list.clone()));

        app.config_tree_state.select(vec![
            "service_networks".to_string(),
            "blacklist".to_string(),
            "[1]".to_string(),
        ]);
        assert_eq!(app.selected_list_path(), Some(list));
    }

    #[test]
    fn a_scalar_is_not_a_list_to_add_to() {
        let mut app = on_config_page("network:\n  GATEWAY_PORT: 8080\n");
        app.config_tree_state.select(key("network.GATEWAY_PORT"));

        assert_eq!(app.selected_list_path(), None);

        app.open_config_list_add();
        assert_eq!(app.input_mode, InputMode::Normal);
        assert!(app.status.contains("Select a list"), "{}", app.status);
    }

    #[test]
    fn only_an_element_can_be_removed() {
        let mut app = on_config_page(
            "network:\n  FREE_PORTS_RANGE:\n    - START: 50000\n      END: 60000\n",
        );

        // The element itself: removing it takes the whole range with it.
        app.config_tree_state.select(vec![
            "network".to_string(),
            "FREE_PORTS_RANGE".to_string(),
            "[0]".to_string(),
        ]);
        assert_eq!(
            app.selected_list_item(),
            Some(vec![
                ConfigPathSegment::Key("network".to_string()),
                ConfigPathSegment::Key("FREE_PORTS_RANGE".to_string()),
                ConfigPathSegment::Index(0),
            ])
        );

        // A key inside it is not: selecting START does not ask for the range to go.
        app.config_tree_state.select(vec![
            "network".to_string(),
            "FREE_PORTS_RANGE".to_string(),
            "[0]".to_string(),
            "START".to_string(),
        ]);
        assert_eq!(app.selected_list_item(), None);

        app.open_delete_config_item_confirm();
        assert_eq!(app.input_mode, InputMode::Normal);
        assert!(app.pending_action.is_none());
        assert!(app.status.contains("Select a list element"), "{}", app.status);
    }

    #[test]
    fn removing_an_element_asks_first_and_names_it() {
        let mut app = on_config_page("service_networks:\n  blacklist: [\"*.a\", \"*.b\"]\n");
        app.config_tree_state.select(vec![
            "service_networks".to_string(),
            "blacklist".to_string(),
            "[1]".to_string(),
        ]);

        app.open_delete_config_item_confirm();

        assert_eq!(app.input_mode, InputMode::Confirm);
        assert!(
            app.input_title.contains("service_networks.blacklist[1]"),
            "{}",
            app.input_title
        );
        assert!(matches!(
            app.pending_action,
            Some(PendingAction::DeleteConfigItem { ref path, .. })
                if path.last() == Some(&ConfigPathSegment::Index(1))
        ));
    }

    #[test]
    fn a_config_deletion_is_not_a_nodo_invocation() {
        // Every other confirmable action turns into the CLI command the operator
        // would type; this one goes through the same `yq` path as any config write,
        // because no `nodo` subcommand edits one key.
        assert!(pending_command(PendingAction::DeleteConfigItem {
            path: vec![ConfigPathSegment::Index(0)],
            label: "blacklist[0]".to_string(),
        })
        .is_none());
    }

    #[test]
    fn the_add_popup_appends_rather_than_overwriting() {
        let mut app = on_config_page("service_networks:\n  blacklist: []\n");
        app.config_tree_state
            .select(key("service_networks.blacklist"));
        app.open_config_list_add();

        assert_eq!(app.input_mode, InputMode::AddConfigItem);
        assert!(app.input.is_empty(), "the field starts blank, it is a new element");
        assert!(app.input_title.starts_with("Add to "), "{}", app.input_title);
        assert_eq!(
            yq_path_expression(app.edit_config_path.as_ref().unwrap()),
            ".[\"service_networks\"][\"blacklist\"]"
        );
    }

    #[test]
    fn a_leading_star_has_to_be_quoted_and_only_a_leading_one() {
        // Why the add popup's hint singles out a leading `*`: there it is YAML's alias
        // indicator, so the same check that keeps an ordinary edit well-formed rejects
        // it before yq is ever run. Anywhere else a `*` is ordinary text, and a
        // pattern like `dns:*` needs no quoting at all.
        assert!(serde_yaml::from_str::<Value>("*google.com").is_err());
        assert!(serde_yaml::from_str::<Value>("\"*google.com\"").is_ok());
        assert_eq!(
            serde_yaml::from_str::<Value>("dns:*").unwrap(),
            Value::String("dns:*".to_string())
        );
    }

    #[test]
    fn known_enum_values_covers_only_the_documented_keys() {
        assert_eq!(
            known_enum_values("network.DELEGATION_TUNNEL_POLICY"),
            Some(["auto", "always", "never"].as_slice())
        );
        assert_eq!(
            known_enum_values("hashing.HASH"),
            Some(["sha2_256", "sha3_256", "shake_256", "blake2b_256"].as_slice())
        );
        assert_eq!(known_enum_values("packer.local"), None);
    }

    #[test]
    fn opening_the_editor_picks_the_widget_from_the_inferred_type() {
        let mut app = App::default();

        select_config_entry(&mut app, config_entry("virtualizers.qemu.ENABLE", "true", "true", "bool", false));
        app.open_config_editor();
        assert_eq!(app.edit_kind, EditKind::Bool);

        select_config_entry(&mut app, config_entry("timing.MANAGER_ITERATION_TIME", "30", "30", "number", false));
        app.open_config_editor();
        assert_eq!(app.edit_kind, EditKind::Number);

        select_config_entry(
            &mut app,
            config_entry("network.DELEGATION_TUNNEL_POLICY", "auto", "auto", "string", false),
        );
        app.open_config_editor();
        assert_eq!(
            app.edit_kind,
            EditKind::Enum(vec!["auto".to_string(), "always".to_string(), "never".to_string()])
        );

        select_config_entry(
            &mut app,
            config_entry("hashing.HASH", "sha3_256", "sha3_256", "string", false),
        );
        app.open_config_editor();
        assert_eq!(
            app.edit_kind,
            EditKind::Enum(vec![
                "sha2_256".to_string(),
                "sha3_256".to_string(),
                "shake_256".to_string(),
                "blake2b_256".to_string(),
            ])
        );

        select_config_entry(&mut app, config_entry("publisher.REPOSITORY", "owner/repo", "owner/repo", "string", false));
        app.open_config_editor();
        assert_eq!(app.edit_kind, EditKind::Text);

        // A secret always gets the plain text field, whatever its inferred type --
        // there's nothing meaningful to check/step/cycle in a value that is never shown.
        select_config_entry(
            &mut app,
            config_entry("ledgers.ergo.WALLET_MNEMONIC", "word word word", "word word word", "string", true),
        );
        app.open_config_editor();
        assert_eq!(app.edit_kind, EditKind::Text);
        assert!(app.input.is_empty());
    }

    #[test]
    fn arrow_keys_toggle_a_checkbox_both_ways() {
        let mut app = App::default();
        app.edit_kind = EditKind::Bool;
        app.input = "false".to_string();
        app.adjust_edit_value(1);
        assert_eq!(app.input, "true");
        app.adjust_edit_value(1);
        assert_eq!(app.input, "false");
    }

    #[test]
    fn arrow_keys_step_a_number_preserving_int_vs_float_shape() {
        let mut app = App::default();
        app.edit_kind = EditKind::Number;

        app.input = "30".to_string();
        app.adjust_edit_value(1);
        assert_eq!(app.input, "31");
        app.adjust_edit_value(-1);
        assert_eq!(app.input, "30");

        app.input = "2.5".to_string();
        app.adjust_edit_value(1);
        assert_eq!(app.input, "3.5");
    }

    #[test]
    fn the_enum_picker_walks_the_way_the_arrow_points_and_wraps_at_both_ends() {
        // Up is `adjust_edit_value(1)` (see the handler) and the options are drawn as
        // a vertical list in declaration order, so Up has to land on the option
        // *above* the marker. It used to land on the one below: the picker was the
        // only ↑/↓ in the TUI that moved against the key.
        let mut app = App {
            edit_kind: EditKind::Enum(vec![
                "auto".to_string(),
                "always".to_string(),
                "never".to_string(),
            ]),
            ..Default::default()
        };

        app.input = "always".to_string();
        app.adjust_edit_value(1);
        assert_eq!(app.input, "auto", "Up moves to the option above");
        app.adjust_edit_value(-1);
        assert_eq!(app.input, "always", "Down moves to the option below");
        app.adjust_edit_value(-1);
        assert_eq!(app.input, "never");
        app.adjust_edit_value(-1);
        assert_eq!(app.input, "auto", "Down past the last option wraps to the first");
        app.adjust_edit_value(1);
        assert_eq!(app.input, "never", "Up before the first wraps to the last");
    }

    #[test]
    fn the_hash_picker_does_not_discard_an_existing_hex_id() {
        // hashing.HASH also accepts an arbitrary hex hash-id (see
        // src/utils/hashing.py's resolve_hash_config), which the picker's four
        // canonical names cannot represent. Opening the editor on a node
        // already configured that way must show the hex id as-is, not snap it
        // to one of the four -- and character typing is unrestricted for Enum
        // (only Bool blocks it, in handler.rs), so it stays editable by hand.
        let mut app = App::default();
        let hex_id = "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a";
        select_config_entry(
            &mut app,
            config_entry("hashing.HASH", hex_id, hex_id, "string", false),
        );
        app.open_config_editor();
        assert_eq!(app.input, hex_id, "the existing value must survive opening the editor");
        assert_eq!(
            app.edit_kind,
            EditKind::Enum(vec![
                "sha2_256".to_string(),
                "sha3_256".to_string(),
                "shake_256".to_string(),
                "blake2b_256".to_string(),
            ])
        );

        // Cycling from a value the list does not contain must not panic --
        // adjust_edit_value falls back to treating it as index 0.
        app.adjust_edit_value(1);
        assert_eq!(app.input, "blake2b_256");
    }

    #[test]
    fn typing_in_a_filter_expands_and_selects_the_match_live_without_hiding_the_rest() {
        let mut app = App::default();
        app.config_all = vec![
            config_entry("network.GATEWAY_PORT", "5000", "5000", "number", false),
            config_entry("packer.local", "false", "false", "bool", false),
        ];
        app.input_mode = InputMode::FilterConfig;

        app.input = "gateway".to_string();
        app.on_input_changed();

        // The filter no longer hides anything: both sections are still present so
        // the surrounding context stays visible.
        assert_eq!(app.config_all.len(), 2);
        // The matching branch's ancestor is opened so the match is revealed...
        assert!(app
            .config_tree_state
            .opened()
            .contains(&vec!["network".to_string()]));
        // ...and the match itself is selected.
        assert_eq!(
            app.config_tree_state.selected(),
            &["network".to_string(), "GATEWAY_PORT".to_string()]
        );
        // Live expansion shouldn't require Enter to have been pressed.
        assert_eq!(app.config_filter, "gateway");
    }

    #[test]
    fn typing_while_editing_a_value_does_not_touch_the_filter() {
        let mut app = App::default();
        app.config_filter = "kept".to_string();
        app.input_mode = InputMode::EditConfig;
        app.input = "anything".to_string();
        app.on_input_changed();
        assert_eq!(app.config_filter, "kept");
    }

    #[test]
    fn only_actual_tokens_are_masked() {
        assert!(is_secret_path("publisher.TOKEN"));
        assert!(is_secret_path("publisher.FALLBACK_TOKEN"));
        assert!(!is_secret_path("reputation.TOTAL_REPUTATION_TOKEN_AMOUNT"));
        assert!(!is_secret_path("reputation.PLAIN_TEXT_TYPE_NFT_ID"));
    }

    #[test]
    fn current_example_config_is_fully_navigable() {
        let document: Value =
            serde_yaml::from_str(include_str!("../../../../config.example.yaml")).unwrap();
        let mut entries = Vec::new();
        flatten_yaml(&document, &mut Vec::new(), &mut entries);
        assert!(entries.len() > 100);
        assert!(entries
            .iter()
            .any(|entry| entry.path == "virtualizers.ch.MIN_MEM_MIB"));
        // core_services is a role -> id mapping (issue #232), not an array.
        assert!(entries
            .iter()
            .any(|entry| entry.path == "core_services.packer"));
        assert!(
            entries
                .iter()
                .find(|entry| entry.path == "ledgers.ergo.WALLET_MNEMONIC")
                .unwrap()
                .secret
        );
    }

    #[test]
    fn emits_safe_yq_paths() {
        let path = vec![
            ConfigPathSegment::Key("servers".to_string()),
            ConfigPathSegment::Index(1),
            ConfigPathSegment::Key("id".to_string()),
        ];
        assert_eq!(yq_path_expression(&path), ".[\"servers\"][1][\"id\"]");
    }

    #[test]
    fn formats_sizes_and_percentages() {
        assert_eq!(format_bytes(1_073_741_824), "1.0 GiB");
        assert_eq!(percent(25, 100), 25);
        assert_eq!(percent(1, 0), 0);
    }

    /// ←/→ walk the Config tree; pages are cycled with Tab/Shift+Tab only.
    mod config_tree_navigation {
        use super::*;

        fn on_config_page() -> App {
            let mut app = App::default();
            app.tabs.index = Page::ALL.iter().position(|page| *page == Page::Config).unwrap();
            app
        }

        #[test]
        fn right_enters_a_branch_and_left_leaves_it() {
            let mut app = on_config_page();
            app.config_tree_state.select(vec!["network".to_string()]);

            app.on_right();
            assert!(
                app.config_tree_state.opened().contains(&vec!["network".to_string()]),
                "→ opens the selected branch"
            );

            app.on_left();
            assert!(
                !app.config_tree_state.opened().contains(&vec!["network".to_string()]),
                "← closes the branch it is standing in"
            );
        }

        #[test]
        fn left_steps_out_to_the_parent_once_the_branch_is_closed() {
            // The way out of a nested value: ← until there is nothing left to leave.
            let mut app = on_config_page();
            app.config_tree_state.select(vec![
                "virtualizers".to_string(),
                "ch".to_string(),
                "MIN_MEM_MIB".to_string(),
            ]);

            app.on_left();
            assert_eq!(
                app.config_tree_state.selected(),
                &["virtualizers".to_string(), "ch".to_string()]
            );
            app.on_left();
            assert_eq!(app.config_tree_state.selected(), &["virtualizers".to_string()]);
        }

        #[test]
        fn the_arrows_never_change_page() {
            // They used to be page navigation; a stray ← on Config must no longer
            // throw the operator onto another page mid-edit.
            let mut app = on_config_page();
            app.config_tree_state.select(vec!["network".to_string()]);
            app.on_left();
            app.on_right();
            assert_eq!(app.page(), Page::Config);

            // And on a page with no tree they do nothing at all.
            app.tabs.index = Page::ALL.iter().position(|page| *page == Page::Logs).unwrap();
            app.on_left();
            app.on_right();
            assert_eq!(app.page(), Page::Logs);
        }
    }

    mod forgetting_a_peer {
        use super::*;

        fn peer(id: &str) -> Peer {
            Peer {
                id: id.to_string(),
                uris: "10.0.0.1:8080".to_string(),
                balance: "0".to_string(),
                remote_client_id: String::new(),
                proof_ids: Vec::new(),
                reputation_score: "0".to_string(),
                contracts: Vec::new(),
            }
        }

        fn on_peers_page(peers: Vec<Peer>) -> App {
            let mut app = App::default();
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Peers)
                .unwrap();
            app.peers = StatefulList::with_items(peers);
            app.peers.next();
            app
        }

        #[test]
        fn it_asks_before_doing_anything() {
            let mut app = on_peers_page(vec![peer("peer-abc")]);
            app.open_disconnect_peer_confirm();

            assert_eq!(app.input_mode, InputMode::Confirm);
            assert!(app.input_title.contains("peer-abc"), "{}", app.input_title);
            assert!(matches!(
                app.pending_action,
                Some(PendingAction::DisconnectPeer { ref id, .. }) if id == "peer-abc"
            ));
        }

        #[test]
        fn confirming_runs_nodo_disconnect_on_the_selected_peer() {
            // The whole point: the same command the operator would type, so the peer
            // row, its addresses and its contract instances all go together.
            let (label, args) = pending_command(PendingAction::DisconnectPeer {
                id: "peer-abc".to_string(),
                label: "peer-abc".to_string(),
            })
            .expect("a peer disconnect is a `nodo` invocation");
            assert_eq!(args, vec!["disconnect".to_string(), "peer-abc".to_string()]);
            assert_eq!(label, "Forget peer peer-abc");
        }

        #[test]
        fn with_nothing_selected_it_says_so_instead_of_asking() {
            let mut app = on_peers_page(Vec::new());
            app.open_disconnect_peer_confirm();
            assert_eq!(app.input_mode, InputMode::Normal);
            assert!(app.pending_action.is_none());
            assert!(app.status.contains("Select a peer"), "{}", app.status);
        }

        #[test]
        fn clients_have_no_such_action() {
            // A client is ours and expires on its own; there is nothing to forget.
            // The page split is what enforces it now -- `d` is not bound on Clients --
            // so the guard here is the second line of defence, not the first.
            let mut app = on_peers_page(vec![peer("peer-abc")]);
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Clients)
                .unwrap();
            app.open_disconnect_peer_confirm();
            assert_eq!(app.input_mode, InputMode::Normal);
            assert!(app.pending_action.is_none());
        }
    }

    /// The history behind a peer and a client, read straight out of SQLite.
    ///
    /// These queries are the whole point of the two detail cards, and a card that
    /// silently renders nothing looks exactly like a peer with no history -- the same
    /// confusion issue #231 was about, one table over. So they are exercised against a
    /// real database rather than through the widgets.
    mod payment_and_reputation_history {
        use super::*;

        fn history_database(dir: &Path) -> PathBuf {
            let path = dir.join("history.sqlite");
            let connection = Connection::open(&path).unwrap();
            connection
                .execute_batch(
                    "CREATE TABLE peer (id TEXT PRIMARY KEY, balance_mu TEXT,
                                        advertisement BLOB, reputation_score INTEGER,
                                        reputation_index INTEGER);
                     CREATE TABLE clients (id TEXT PRIMARY KEY, balance_mu TEXT,
                                        last_usage FLOAT, unmetered INTEGER NOT NULL DEFAULT 0);
                     CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT,
                                        direction TEXT, status TEXT, peer_id TEXT, client_id TEXT,
                                        deposit_token TEXT, ledger TEXT, contract_hash TEXT,
                                        address TEXT, amount_mu TEXT NOT NULL,
                                        created_at DATETIME);
                     CREATE TABLE reputation_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                        subject_kind TEXT, subject_id TEXT, amount INTEGER,
                                        reason TEXT, score_after INTEGER, created_at DATETIME);
                     CREATE TABLE deposit_tokens (id TEXT PRIMARY KEY, client_id TEXT,
                                        status TEXT, created_at DATETIME);
                     CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT, father_id TEXT);
                     INSERT INTO peer VALUES ('peer-1', '1000', NULL, 7, 3);
                     INSERT INTO clients VALUES ('client-1', '500', NULL, 1);
                     INSERT INTO payments (tx_id, direction, status, peer_id, amount_mu, created_at)
                        VALUES ('tx-old', 'out', 'communicated', 'peer-1', '1000', '2026-01-01 10:00:00');
                     INSERT INTO payments (tx_id, direction, status, peer_id, amount_mu, created_at)
                        VALUES ('tx-new', 'out', 'unacknowledged', 'peer-1', '2000', '2026-01-02 10:00:00');
                     INSERT INTO payments (direction, status, client_id, deposit_token, amount_mu, created_at)
                        VALUES ('in', 'accepted', 'client-1', 'token-1', '750', '2026-01-03 10:00:00');
                     INSERT INTO reputation_events (subject_kind, subject_id, amount, reason, score_after, created_at)
                        VALUES ('peer', 'peer-1', -100, 'payment_unacknowledged', -93, '2026-01-02 10:00:01');
                     INSERT INTO reputation_events (subject_kind, subject_id, amount, reason, score_after, created_at)
                        VALUES ('service', 'peer-1', -100, 'instance_lost', -100, '2026-01-02 10:00:02');
                     INSERT INTO deposit_tokens VALUES ('token-1', 'client-1', 'payed', '2026-01-03 09:59:00');
                     INSERT INTO local_instances VALUES ('instance-1', 'demo', 'client-1');
                     INSERT INTO local_instances VALUES ('instance-2', 'other', 'someone-else');",
                )
                .unwrap();
            path
        }

        fn temp_dir(name: &str) -> PathBuf {
            let dir = std::env::temp_dir()
                .join(format!("nodo-tui-history-{name}-{}", std::process::id()));
            fs::create_dir_all(&dir).unwrap();
            dir
        }

        #[test]
        fn a_peer_carries_its_payments_and_the_events_behind_its_score() {
            let dir = temp_dir("peer");
            let database = history_database(&dir);

            let detail = get_peer_detail(&database, "peer-1").unwrap();

            // Newest first: the payment an operator is looking for is the last one.
            assert_eq!(detail.payments.len(), 2);
            assert_eq!(detail.payments[0].tx_id, "tx-new");
            assert_eq!(detail.payments[0].status, "unacknowledged");
            assert_eq!(detail.payments[0].amount, "2000");
            // A service event that happens to share the id is not this peer's history.
            assert_eq!(detail.events.len(), 1);
            assert_eq!(detail.events[0].reason, "payment_unacknowledged");
            assert_eq!(detail.events[0].score_after, Some(-93));

            let _ = fs::remove_dir_all(&dir);
        }

        #[test]
        fn a_client_carries_what_it_paid_what_it_was_given_and_what_it_runs() {
            let dir = temp_dir("client");
            let database = history_database(&dir);

            let detail = get_client_detail(&database, "client-1").unwrap();

            assert_eq!(detail.payments.len(), 1);
            assert_eq!(detail.payments[0].deposit_token, "token-1");
            assert_eq!(detail.deposits.len(), 1);
            assert_eq!(detail.deposits[0].status, "payed");
            // Only its own instances: father_id is the client that started them.
            assert_eq!(detail.instances.len(), 1);
            assert_eq!(detail.instances[0].name, "demo");

            let _ = fs::remove_dir_all(&dir);
        }

        #[test]
        fn the_unmetered_flag_reaches_the_table() {
            let dir = temp_dir("unmetered");
            let database = history_database(&dir);

            let clients = get_clients(&database).unwrap();

            assert_eq!(clients.len(), 1);
            assert!(clients[0].unmetered, "a dev client is never charged");

            let _ = fs::remove_dir_all(&dir);
        }

        #[test]
        fn adjusting_a_score_by_hand_records_why() {
            // Every other mover of a score writes an event. One that did not would be
            // the single unexplained step in a peer's history.
            let dir = temp_dir("adjust");
            let database = history_database(&dir);

            adjust_peer_reputation(&database, "peer-1", -3).unwrap();

            let connection = Connection::open(&database).unwrap();
            let (score, index): (i64, i64) = connection
                .query_row(
                    "SELECT reputation_score, reputation_index FROM peer WHERE id = 'peer-1'",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .unwrap();
            assert_eq!((score, index), (4, 4));

            let (amount, reason, after): (i64, String, i64) = connection
                .query_row(
                    "SELECT amount, reason, score_after FROM reputation_events
                     WHERE subject_id = 'peer-1' ORDER BY id DESC LIMIT 1",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .unwrap();
            assert_eq!(amount, -3);
            assert_eq!(reason, "operator_adjustment");
            assert_eq!(after, 4);

            let _ = fs::remove_dir_all(&dir);
        }

        #[test]
        fn a_peer_that_has_done_nothing_yet_has_an_empty_history_not_an_error() {
            let dir = temp_dir("empty");
            let database = history_database(&dir);

            let detail = get_peer_detail(&database, "peer-unknown").unwrap();

            assert!(detail.payments.is_empty());
            assert!(detail.events.is_empty());

            let _ = fs::remove_dir_all(&dir);
        }
    }
}
