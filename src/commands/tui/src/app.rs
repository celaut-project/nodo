use prost::Message;
use ratatui::widgets::TableState;
use regex::Regex;
use rusqlite::{Connection, Result as SqlResult};
use serde_yaml::Value;
use std::collections::{HashMap, VecDeque};
use std::error;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};
use sysinfo::{Disks, System};
use tokio::process::Command;
use tokio::task::JoinHandle;

/// Application result type.
pub type AppResult<T> = std::result::Result<T, Box<dyn error::Error>>;

pub const HISTORY_POINTS: usize = 120;
const DATA_REFRESH_INTERVAL: Duration = Duration::from_secs(2);
const WALLET_REFRESH_INTERVAL: Duration = Duration::from_secs(60);

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
    Network,
    Config,
    Logs,
}

impl Page {
    pub const ALL: [Page; 6] = [
        Page::Overview,
        Page::Instances,
        Page::Services,
        Page::Network,
        Page::Config,
        Page::Logs,
    ];

    pub fn title(self) -> &'static str {
        match self {
            Page::Overview => "OVERVIEW",
            Page::Instances => "INSTANCES",
            Page::Services => "SERVICES",
            Page::Network => "NETWORK",
            Page::Config => "CONFIG",
            Page::Logs => "LOGS",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputMode {
    Normal,
    Connect,
    EditConfig,
    FilterConfig,
    /// Yes/no confirmation before a destructive action (delete service, kill instance).
    Confirm,
    /// Read-only, scrollable overlay (e.g. `nodo inspect` output).
    Details,
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
/// documents a closed set of options. Deliberately small and explicit: a key
/// like `hashing.HASH` documents aliases but also accepts an arbitrary hex
/// hash-id, so it stays freeform text rather than a misleadingly restrictive
/// picker. Add an entry here only when every accepted value is enumerable.
fn known_enum_values(path: &str) -> Option<&'static [&'static str]> {
    match path {
        "network.DELEGATION_TUNNEL_POLICY" => Some(&["auto", "always", "never"]),
        _ => None,
    }
}

/// A destructive action awaiting user confirmation.
#[derive(Debug, Clone)]
pub enum PendingAction {
    DeleteService { id: String, label: String },
    KillInstance { id: String, label: String },
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
    /// Our gas balance on this peer. Source of truth is the `gas` column on the
    /// `peer` table itself — NOT the local `clients` table. `peer.remote_client_id`
    /// identifies our client *inside the remote peer*, so it can never be joined
    /// against our local `clients` table (see issue #178).
    pub gas: String,
    pub reputation: String,
    /// Local reputation score (nodo-managed, independent of the on-chain proof).
    pub reputation_score: String,
}

impl Identifiable for Peer {
    fn id(&self) -> &str {
        &self.id
    }
}

#[derive(Debug, Clone)]
pub struct Client {
    pub id: String,
    pub gas: String,
    pub last_usage: String,
}

impl Identifiable for Client {
    fn id(&self) -> &str {
        &self.id
    }
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

#[derive(Debug, Clone)]
pub struct Instance {
    pub id: String,
    pub name: String,
    pub ip: String,
    pub service: String,
    pub gas: String,
    pub virtualizer: String,
    pub memory_current: Option<u64>,
    pub memory_limit: u64,
    pub disk_limit: u64,
    /// "local" for locally-run instances, otherwise the owning peer id for
    /// delegated/remote instances.
    pub location: String,
    /// Parent instance id (from `father_id`); empty when this is a root.
    pub father_id: String,
}

impl Instance {
    pub fn is_local(&self) -> bool {
        self.location == "local"
    }
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
            yq: resolve(&["dependencies", "yq", "BIN"], PathBuf::from("yq")),
            storage,
        }
    }
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

pub struct App {
    pub title: &'static str,
    pub tabs: TabsState,
    pub running: bool,
    pub peers: StatefulList<Peer>,
    pub clients: StatefulList<Client>,
    pub instances: StatefulList<Instance>,
    pub services: StatefulList<Service>,
    pub config: StatefulList<ConfigEntry>,
    pub config_all: Vec<ConfigEntry>,
    pub config_filter: String,
    pub network_focus: usize,
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
    /// Contents of the read-only Details overlay, when open.
    pub details: Option<DetailsView>,
    pub status: String,
    pub sys: System,
    last_data_refresh: Instant,
    last_storage_refresh: Instant,
    last_wallet_refresh: Instant,
    wallet_task: Option<JoinHandle<Result<NodeInfo, String>>>,
    /// In-flight background `nodo` command, if any (keeps the UI responsive).
    command_task: Option<JoinHandle<CommandOutcome>>,
}

impl Default for App {
    fn default() -> Self {
        let paths = Paths::discover();
        let config_all = get_config_entries(&paths.config).unwrap_or_default();
        let now = Instant::now();
        Self {
            title: "NODO OPERATIONS",
            tabs: TabsState { index: 0 },
            running: true,
            peers: StatefulList::with_items(get_peers(&paths.database).unwrap_or_default()),
            clients: StatefulList::with_items(get_clients(&paths.database).unwrap_or_default()),
            instances: StatefulList::with_items(Vec::new()),
            services: StatefulList::with_items(Vec::new()),
            config: StatefulList::with_items(config_all.clone()),
            config_all,
            config_filter: String::new(),
            network_focus: 0,
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
            details: None,
            status: "Press r to refresh • q to quit".to_string(),
            sys: System::new_all(),
            last_data_refresh: now.checked_sub(DATA_REFRESH_INTERVAL).unwrap_or(now),
            last_storage_refresh: now.checked_sub(Duration::from_secs(30)).unwrap_or(now),
            last_wallet_refresh: now.checked_sub(WALLET_REFRESH_INTERVAL).unwrap_or(now),
            wallet_task: None,
            command_task: None,
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

    pub fn on_right(&mut self) {
        self.tabs.next();
    }

    pub fn on_left(&mut self) {
        self.tabs.previous();
    }

    pub fn on_up(&mut self) {
        match self.page() {
            Page::Instances => self.instances.previous(),
            Page::Services => self.services.previous(),
            Page::Network if self.network_focus == 0 => self.peers.previous(),
            Page::Network => self.clients.previous(),
            Page::Config => self.config.previous(),
            _ => {}
        }
    }

    pub fn on_down(&mut self) {
        match self.page() {
            Page::Instances => self.instances.next(),
            Page::Services => self.services.next(),
            Page::Network if self.network_focus == 0 => self.peers.next(),
            Page::Network => self.clients.next(),
            Page::Config => self.config.next(),
            _ => {}
        }
    }

    pub fn toggle_focus(&mut self) {
        if self.page() == Page::Network {
            self.network_focus = (self.network_focus + 1) % 2;
        }
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
        if self.page() != Page::Network || self.network_focus != 0 {
            return;
        }
        let Some(peer) = self.peers.selected().cloned() else {
            self.status = "Select a peer first (Tab focuses peers)".to_string();
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

    pub fn open_config_editor(&mut self) {
        let Some(entry) = self.config.selected().cloned() else {
            self.status = "Select a configuration value first".to_string();
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
        } else if let Some(options) = known_enum_values(&entry.path) {
            EditKind::Enum(options.iter().map(|value| value.to_string()).collect())
        } else {
            match entry.value_type.as_str() {
                "bool" => EditKind::Bool,
                "number" => EditKind::Number,
                _ => EditKind::Text,
            }
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
                let next_index = (current_index + delta).rem_euclid(len) as usize;
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

    pub fn apply_config_filter(&mut self) {
        let needle = self.config_filter.to_lowercase();
        let filtered = self
            .config_all
            .iter()
            .filter(|entry| {
                needle.is_empty()
                    || entry.path.to_lowercase().contains(&needle)
                    || (!entry.secret && entry.value.to_lowercase().contains(&needle))
            })
            .cloned()
            .collect();
        self.config.refresh(filtered);
    }

    pub async fn submit_input(&mut self) {
        match self.input_mode {
            InputMode::Connect => self.connect(),
            InputMode::EditConfig => self.save_config_edit().await,
            InputMode::FilterConfig => {
                self.config_filter = self.input.trim().to_string();
                self.apply_config_filter();
                let count = self.config.items.len();
                self.close_input();
                self.status = format!("Configuration filter: {count} matching values");
            }
            InputMode::Normal | InputMode::Confirm | InputMode::Details => {}
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

    async fn save_config_edit(&mut self) {
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

        let expression = format!("{} = env(NODO_TUI_VALUE)", yq_path_expression(&path));
        let backup = self.paths.config.with_extension("yaml.tui.bak");
        if let Err(error) = fs::copy(&self.paths.config, &backup) {
            self.status = format!("Could not create config backup: {error}");
            return;
        }

        let output = Command::new(&self.paths.yq)
            .arg("e")
            .arg("-i")
            .arg(expression)
            .arg(&self.paths.config)
            .env("NODO_TUI_VALUE", &self.input)
            .output()
            .await;

        match output {
            Ok(output) if output.status.success() => {
                self.paths = Paths::discover();
                self.config_all = get_config_entries(&self.paths.config).unwrap_or_default();
                self.apply_config_filter();
                self.close_input();
                self.status = format!(
                    "Configuration saved • backup: {} • restart nodo to apply runtime changes",
                    backup.display()
                );
            }
            Ok(output) => {
                let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
                self.status = format!("yq could not update configuration: {message}");
            }
            Err(error) => {
                self.status = format!("Could not run {}: {error}", self.paths.yq.display());
            }
        }
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

    /// Run the pending destructive action (called on `y` in a Confirm modal).
    pub fn confirm_pending(&mut self) {
        let Some(action) = self.pending_action.take() else {
            self.close_input();
            return;
        };
        let (label, args) = match action {
            PendingAction::DeleteService { id, label } => (
                format!("Delete service {label}"),
                vec!["remove".to_string(), id],
            ),
            PendingAction::KillInstance { id, label } => {
                (format!("Kill instance {label}"), vec!["kill".to_string(), id])
            }
        };
        self.close_input();
        self.spawn_command(CommandKind::Generic, label, args);
    }

    /// Scroll the Details overlay by `delta` lines (clamped).
    pub fn scroll_details(&mut self, delta: isize) {
        if let Some(details) = self.details.as_mut() {
            let max = details.lines.len().saturating_sub(1) as isize;
            details.scroll = (details.scroll as isize + delta).clamp(0, max) as usize;
        }
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

        let services = get_services(&self.paths).unwrap_or_default();
        let service_names = services
            .iter()
            .map(|service| (service.id.clone(), service.tag.clone()))
            .collect::<HashMap<_, _>>();
        self.services.refresh(services);
        self.instances
            .refresh(get_instances(&self.paths, &service_names).unwrap_or_default());
        self.peers
            .refresh(get_peers(&self.paths.database).unwrap_or_default());
        self.clients
            .refresh(get_clients(&self.paths.database).unwrap_or_default());
        self.node_logs = read_last_lines(&self.paths.log, 250).unwrap_or_default();

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
            .filter_map(|instance| instance.memory_current)
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
    // Our gas on a peer lives on the `peer` table's own `gas` column. The old
    // `LEFT JOIN clients c ON p.client_id = c.id` was wrong: `peer.remote_client_id`
    // is our client id *inside the remote peer*, never a key into our local
    // `clients` table, so that join surfaced a bogus gas value (issue #178).
    let mut statement = connection.prepare(
        "SELECT p.id,
                COALESCE(GROUP_CONCAT(u.ip || ':' || u.port, ', '), ''),
                p.gas,
                COALESCE(p.reputation_proof_id, ''),
                p.reputation_score
         FROM peer p
         LEFT JOIN slot s ON p.id = s.peer_id
         LEFT JOIN uri u ON s.id = u.slot_id
         GROUP BY p.id, p.gas, p.reputation_proof_id, p.reputation_score",
    )?;
    let peers = statement
        .query_map([], |row| {
            let reputation_score = row
                .get::<_, Option<i64>>(4)?
                .map(|score| score.to_string())
                .unwrap_or_else(|| "0".to_string());
            Ok(Peer {
                id: row.get(0)?,
                uris: row.get(1)?,
                gas: format_gas(row.get::<_, String>(2)?),
                reputation: row.get(3)?,
                reputation_score,
            })
        })?
        .collect();
    peers
}

/// Adjust a peer's local reputation score by `delta`, mirroring
/// `sql_connection.update_reputation_peer`: add `delta` to the score and
/// increment the index. Works when `reputation_proof_id` is NULL (score-only),
/// so no on-chain proof is required.
fn adjust_peer_reputation(database: &Path, peer_id: &str, delta: i64) -> SqlResult<()> {
    let connection = Connection::open(database)?;
    let (score, index): (i64, i64) = connection.query_row(
        "SELECT COALESCE(reputation_score, 0), COALESCE(reputation_index, 0)
         FROM peer WHERE id = ?1",
        [peer_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    connection.execute(
        "UPDATE peer SET reputation_score = ?1, reputation_index = ?2 WHERE id = ?3",
        rusqlite::params![score + delta, index + 1, peer_id],
    )?;
    Ok(())
}

fn get_clients(database: &Path) -> SqlResult<Vec<Client>> {
    let connection = Connection::open(database)?;
    let mut statement = connection.prepare("SELECT id, gas, last_usage FROM clients")?;
    let clients = statement
        .query_map([], |row| {
            let last_usage = row
                .get::<_, Option<f64>>(2)?
                .map(|value| format!("{value:.0}"))
                .unwrap_or_else(|| "—".to_string());
            Ok(Client {
                id: row.get(0)?,
                gas: format_gas(row.get::<_, String>(1)?),
                last_usage,
            })
        })?
        .collect();
    clients
}

fn get_instances(
    paths: &Paths,
    service_names: &HashMap<String, String>,
) -> SqlResult<Vec<Instance>> {
    let connection = Connection::open(&paths.database)?;
    let mut statement = connection.prepare(
        "SELECT id, name, ip, gas, service_id, mem_limit, disk_space, virtualizer, father_id
         FROM local_instances",
    )?;
    let mut instances: Vec<Instance> = statement
        .query_map([], |row| {
            let id: String = row.get(0)?;
            let service_id: String = row.get::<_, Option<String>>(4)?.unwrap_or_default();
            let service = service_names
                .get(&service_id)
                .cloned()
                .unwrap_or_else(|| shorten(&service_id, 18));
            let memory_current = read_u64(
                &paths
                    .cgroups
                    .join("nodo-ch")
                    .join(&id)
                    .join("memory.current"),
            );
            Ok(Instance {
                id,
                name: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                ip: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                gas: format_gas(row.get::<_, String>(3)?),
                service,
                memory_current,
                memory_limit: row.get::<_, Option<u64>>(5)?.unwrap_or(0),
                disk_limit: row.get::<_, Option<u64>>(6)?.unwrap_or(0),
                virtualizer: row
                    .get::<_, Option<String>>(7)?
                    .unwrap_or_else(|| "ch".to_string()),
                location: "local".to_string(),
                father_id: row.get::<_, Option<String>>(8)?.unwrap_or_default(),
            })
        })?
        .collect::<SqlResult<Vec<_>>>()?;

    // Delegated (remote) instances live on other peers. The table carries no
    // gas / memory / disk columns, and remote gas needs an async gRPC call
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
                gas: "—".to_string(),
                service,
                memory_current: None,
                memory_limit: 0,
                disk_limit: 0,
                virtualizer: "remote".to_string(),
                location,
                father_id: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
            })
        })?
        .collect();
    instances
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

fn format_gas(value: String) -> String {
    value
        .parse::<f64>()
        .map(|number| format!("{number:.3e}"))
        .unwrap_or(value)
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

#[cfg(test)]
mod tests {
    use super::*;

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
            "network:\n  port: 5000\nledgers:\n  ergo:\n    WALLET_MNEMONIC: secret words\ncore_services:\n  - name: packer\n    id: abc\nempty: []\n",
        )
        .unwrap();
        let mut entries = Vec::new();
        flatten_yaml(&value, &mut Vec::new(), &mut entries);
        let paths: Vec<_> = entries.iter().map(|entry| entry.path.as_str()).collect();
        assert!(paths.contains(&"network.port"));
        assert!(paths.contains(&"core_services[0].id"));
        assert!(paths.contains(&"empty"));
        let mnemonic = entries
            .iter()
            .find(|entry| entry.path.ends_with("WALLET_MNEMONIC"))
            .unwrap();
        assert!(mnemonic.secret);
        assert_eq!(mnemonic.display_value(), "•••••••• (set)");
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
        app.config = StatefulList::with_items(vec![entry]);
        app.config.state.select(Some(0));
    }

    #[test]
    fn known_enum_values_covers_the_documented_policy_and_nothing_else() {
        assert_eq!(
            known_enum_values("network.DELEGATION_TUNNEL_POLICY"),
            Some(["auto", "always", "never"].as_slice())
        );
        assert_eq!(known_enum_values("packer.local"), None);
    }

    #[test]
    fn opening_the_editor_picks_the_widget_from_the_inferred_type() {
        let mut app = App::default();

        select_config_entry(&mut app, config_entry("builder.ARM_SUPPORT", "true", "true", "bool", false));
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
    fn arrow_keys_cycle_an_enum_and_wrap_at_both_ends() {
        let mut app = App::default();
        app.edit_kind = EditKind::Enum(vec!["auto".to_string(), "always".to_string(), "never".to_string()]);

        app.input = "auto".to_string();
        app.adjust_edit_value(1);
        assert_eq!(app.input, "always");
        app.adjust_edit_value(1);
        assert_eq!(app.input, "never");
        app.adjust_edit_value(1);
        assert_eq!(app.input, "auto", "cycling past the last option wraps to the first");
        app.adjust_edit_value(-1);
        assert_eq!(app.input, "never", "cycling before the first option wraps to the last");
    }

    #[test]
    fn typing_in_a_filter_narrows_results_live_without_pressing_enter() {
        let mut app = App::default();
        app.config_all = vec![
            config_entry("network.GATEWAY_PORT", "5000", "5000", "number", false),
            config_entry("packer.local", "false", "false", "bool", false),
        ];
        app.config = StatefulList::with_items(app.config_all.clone());
        app.input_mode = InputMode::FilterConfig;

        app.input = "network".to_string();
        app.on_input_changed();
        assert_eq!(app.config.items.len(), 1);
        assert_eq!(app.config.items[0].path, "network.GATEWAY_PORT");
        // Live narrowing shouldn't require Enter to have been pressed.
        assert_eq!(app.config_filter, "network");
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
        assert!(entries
            .iter()
            .any(|entry| entry.path == "core_services[1].id"));
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
            ConfigPathSegment::Key("core_services".to_string()),
            ConfigPathSegment::Index(1),
            ConfigPathSegment::Key("id".to_string()),
        ];
        assert_eq!(yq_path_expression(&path), ".[\"core_services\"][1][\"id\"]");
    }

    #[test]
    fn formats_sizes_and_percentages() {
        assert_eq!(format_bytes(1_073_741_824), "1.0 GiB");
        assert_eq!(percent(25, 100), 25);
        assert_eq!(percent(1, 0), 0);
    }
}
