//! The TUNNELS page: the `nodo tunnel` processes running on this host.
//!
//! A tunnel is a process, not a catalogue row: each one leaves
//! `<main.STORAGE>/tunnels/<id>.json` for as long as it runs
//! (`src/utils/tunnel_registry.py`). This page reads those files directly -- the same
//! way the other pages read the SQLite catalogue rather than asking `nodo` -- and every
//! action goes through the CLI an operator would type: `nodo tunnel ... --detach` to
//! open one (here, or with `t` on INSTANCES), `nodo tunnel_close <id>` to close it.
//!
//! The field names are the contract with the Python side; a file whose process is
//! gone is not a tunnel and is not listed (`nodo tunnels` sweeps it).
//!
//! Under them, the other end: the `ServiceTunnel` streams this node is relaying for
//! others (`src/tunneling/inbound.py`), from `inbound.snapshot` in the same
//! directory -- `nodo tunnels --inbound`. Listed, not closed: nothing outside the
//! daemon can reach those streams.

use crate::app::{shorten, App, CommandKind, DetailsView, Identifiable, InputMode, PendingAction, Page};
use crate::layout_util::Column;
use crate::ui::{accent, fitted_table, format_duration_compact, metric_line, muted, section_block, selected_style, text_colour};
use ratatui::prelude::*;
use std::path::{Path, PathBuf};

/// Overrides where the registry lives; the same variable the Python side reads.
const DIR_ENV: &str = "NODO_TUNNELS_DIR";

/// The daemon's snapshot of the streams it relays (`tunnel_registry.INBOUND_FILE`).
const INBOUND_FILE: &str = "inbound.snapshot";
/// `tunnel_registry.INBOUND_STALE_S`: a snapshot listing streams that is older than
/// this was left by a daemon that is gone.
const INBOUND_STALE_SECS: i64 = 30;

/// Log lines the details overlay shows of a detached tunnel.
const LOG_TAIL_LINES: usize = 40;

#[derive(Debug, Clone, PartialEq)]
pub struct Tunnel {
    pub id: String,
    pub pid: i64,
    /// What the operator typed: an instance name, id, or (with `--peer`) a remote token.
    pub instance: String,
    /// The instance token the tunnel actually reaches.
    pub token: String,
    pub slot: i64,
    /// `tcp` or `udp`: the local socket type.
    pub transport: String,
    pub listen_host: String,
    pub listen_port: i64,
    /// The node relaying it: this one (`127.0.0.1:<gateway port>`) or `--peer`.
    pub gateway: String,
    pub peer: Option<String>,
    /// Started with `--detach` (here, always): its output is in `log`.
    pub detached: bool,
    pub log: Option<String>,
    pub started_at: Option<i64>,
    /// What each connection through it spends of the instance's balance to open
    /// (`pricing.TUNNEL_OPEN_MU` when it was opened). `None` through `--peer`: the
    /// remote node charges its own price.
    pub open_fee_mu: Option<u64>,
    /// Has a `<id>.spec` beside it: the daemon reopens it after a restart, until it is
    /// closed on purpose (`tunnel_registry.restore`). `persistent` in `nodo tunnels`.
    pub persistent: bool,
}

impl Identifiable for Tunnel {
    fn id(&self) -> &str {
        &self.id
    }
}

impl Tunnel {
    /// `127.0.0.1:9000/tcp` -- what a client connects to.
    pub fn listen(&self) -> String {
        format!("{}:{}/{}", self.listen_host, self.listen_port, self.transport)
    }

    pub fn via(&self) -> String {
        self.peer.clone().unwrap_or_else(|| "this node".to_string())
    }

    pub fn age_secs(&self, now: Option<i64>) -> Option<f64> {
        Some((now? - self.started_at?).max(0) as f64)
    }

    /// Whether this tunnel reaches `instance` (by its id, or by what was typed).
    pub fn reaches(&self, instance_id: &str, instance_name: &str) -> bool {
        self.peer.is_none()
            && (self.token == instance_id
                || self.instance == instance_id
                || (!instance_name.is_empty() && self.instance == instance_name))
    }
}

/// `<storage>/tunnels`, unless `NODO_TUNNELS_DIR` says otherwise.
pub fn tunnels_dir(storage: &Path) -> PathBuf {
    std::env::var_os(DIR_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|| storage.join("tunnels"))
}

pub fn parse_tunnel(text: &str) -> Option<Tunnel> {
    let value: serde_json::Value = serde_json::from_str(text).ok()?;
    let string = |key: &str| value.get(key).and_then(|v| v.as_str()).map(ToString::to_string);
    let integer = |key: &str| value.get(key).and_then(|v| v.as_i64());
    Some(Tunnel {
        id: string("id").filter(|id| !id.is_empty())?,
        pid: integer("pid")?,
        instance: string("instance").unwrap_or_default(),
        token: string("token").unwrap_or_default(),
        slot: integer("slot")?,
        transport: string("transport").unwrap_or_else(|| "tcp".to_string()),
        listen_host: string("listen_host").unwrap_or_else(|| "127.0.0.1".to_string()),
        listen_port: integer("listen_port")?,
        gateway: string("gateway").unwrap_or_default(),
        peer: string("peer"),
        detached: value.get("detached").and_then(|v| v.as_bool()).unwrap_or(false),
        log: string("log"),
        started_at: integer("started_at"),
        open_fee_mu: value.get("open_fee_mu").and_then(|v| v.as_u64()),
        persistent: value.get("persistent").and_then(|v| v.as_bool()).unwrap_or(false),
    })
}

/// Whether `pid` is a running `nodo tunnel` -- `pid_alive` in the registry, in Rust.
///
/// `kill(pid, 0)` says whether there is a process (EPERM: there is, someone else's);
/// where `/proc` exists its command line says whether it is still a tunnel, so a pid
/// recycled after a reboot does not keep a dead tunnel on screen.
pub fn pid_alive(pid: i64) -> bool {
    if pid <= 0 || pid > i32::MAX as i64 {
        return false;
    }
    // SAFETY: signal 0 delivers nothing; it only asks the kernel whether the pid exists.
    let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if result != 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::EPERM) {
        return false;
    }
    match std::fs::read(format!("/proc/{pid}/cmdline")) {
        Ok(cmdline) => cmdline.split(|byte| *byte == 0).any(|arg| arg == b"tunnel"),
        Err(_) => true,
    }
}

/// One `ServiceTunnel` stream this node is relaying for someone else.
#[derive(Debug, Clone, PartialEq)]
pub struct InboundStream {
    pub id: String,
    /// Who it is relayed for, as gRPC names the connection (`ipv4:1.2.3.4:5678`).
    pub caller: String,
    /// The instance it reaches.
    pub token: String,
    pub slot: String,
    pub transport: String,
    pub started_at: Option<i64>,
    /// Caller -> service.
    pub bytes_in: u64,
    /// Service -> caller.
    pub bytes_out: u64,
}

impl InboundStream {
    pub fn age_secs(&self, now: Option<i64>) -> Option<f64> {
        Some((now? - self.started_at?).max(0) as f64)
    }
}

/// Whether a process exists at all (the daemon; EPERM: it does, it is root's).
fn process_exists(pid: i64) -> bool {
    if pid <= 0 || pid > i32::MAX as i64 {
        return false;
    }
    // SAFETY: signal 0 delivers nothing; it only asks the kernel whether the pid exists.
    let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// The streams in a snapshot, or none when it was left by a daemon that is gone --
/// `read_inbound` in the registry, in Rust.
pub fn parse_inbound(text: &str, now: Option<i64>, alive: impl Fn(i64) -> bool) -> Vec<InboundStream> {
    let Ok(document) = serde_json::from_str::<serde_json::Value>(text) else {
        return Vec::new();
    };
    if !document.get("pid").and_then(|pid| pid.as_i64()).map(&alive).unwrap_or(false) {
        return Vec::new();
    }
    let Some(entries) = document.get("streams").and_then(|streams| streams.as_array()) else {
        return Vec::new();
    };
    let written_at = document.get("written_at").and_then(|at| at.as_i64());
    let fresh = match (now, written_at) {
        (Some(now), Some(at)) => now - at <= INBOUND_STALE_SECS,
        _ => false,
    };
    if !entries.is_empty() && !fresh {
        return Vec::new();
    }
    let text_of = |value: &serde_json::Value, key: &str| match value.get(key) {
        Some(serde_json::Value::String(text)) => text.clone(),
        Some(serde_json::Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    };
    let mut streams: Vec<InboundStream> = entries
        .iter()
        .filter_map(|entry| {
            Some(InboundStream {
                id: Some(text_of(entry, "id")).filter(|id| !id.is_empty())?,
                caller: text_of(entry, "caller"),
                token: text_of(entry, "token"),
                slot: text_of(entry, "slot"),
                transport: text_of(entry, "transport"),
                started_at: entry.get("started_at").and_then(|at| at.as_i64()),
                bytes_in: entry.get("bytes_in").and_then(|count| count.as_u64()).unwrap_or(0),
                bytes_out: entry.get("bytes_out").and_then(|count| count.as_u64()).unwrap_or(0),
            })
        })
        .collect();
    streams.sort_by(|a, b| (a.started_at, &a.id).cmp(&(b.started_at, &b.id)));
    streams
}

pub fn read_inbound(directory: &Path) -> Vec<InboundStream> {
    match std::fs::read_to_string(directory.join(INBOUND_FILE)) {
        Ok(text) => parse_inbound(&text, crate::app::unix_now(), process_exists),
        Err(_) => Vec::new(),
    }
}

/// Every running tunnel in `directory`, oldest first. Read-only: dead files are left
/// for `nodo tunnels` to sweep, since the operator may not own them.
pub fn read_tunnels(directory: &Path) -> Vec<Tunnel> {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut tunnels: Vec<Tunnel> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().map(|ext| ext == "json").unwrap_or(false))
        .filter_map(|path| {
            let mut tunnel = parse_tunnel(&std::fs::read_to_string(&path).ok()?)?;
            tunnel.persistent = path.with_extension("spec").exists();
            Some(tunnel)
        })
        .filter(|tunnel| pid_alive(tunnel.pid))
        .collect();
    tunnels.sort_by(|a, b| (a.started_at, &a.id).cmp(&(b.started_at, &b.id)));
    tunnels
}

/// The `nodo tunnel` a typed line turns into, or why it does not.
///
/// From INSTANCES the instance is the selected row and the line is `<slot> [flags]`;
/// from TUNNELS it is `<instance> <slot> [flags]`. Only the flags `nodo tunnel` takes
/// are accepted, and `--detach --json` are always added: the TUI cannot hold a
/// terminal open for the tunnel's lifetime, and reads the outcome from the JSON.
pub fn open_command(instance: Option<&str>, input: &str) -> Result<(String, Vec<String>), String> {
    let mut words = input.split_whitespace();
    let instance = match instance {
        Some(instance) => instance.to_string(),
        None => words
            .next()
            .ok_or_else(|| "Type an instance and a slot, e.g. my-instance 8080".to_string())?
            .to_string(),
    };
    let slot = words
        .next()
        .ok_or_else(|| "Type the slot (the instance's port) to reach".to_string())?;
    let slot_number: u16 = slot
        .parse()
        .ok()
        .filter(|port| *port > 0)
        .ok_or_else(|| format!("'{slot}' is not a port number"))?;

    let mut args = vec!["tunnel".to_string(), instance.clone(), slot_number.to_string()];
    while let Some(word) = words.next() {
        match word {
            "--udp" => args.push(word.to_string()),
            "--listen" | "--host" | "--peer" | "--idle" => {
                let value = words
                    .next()
                    .ok_or_else(|| format!("{word} needs a value"))?;
                if word == "--listen" && value.parse::<u16>().is_err() {
                    return Err(format!("--listen takes a port number, not '{value}'"));
                }
                if word == "--idle" && value.parse::<f64>().is_err() {
                    return Err(format!("--idle takes seconds, not '{value}'"));
                }
                args.push(word.to_string());
                args.push(value.to_string());
            }
            other => {
                return Err(format!(
                    "Unexpected '{other}': only --listen, --udp, --host, --peer, --idle"
                ))
            }
        }
    }
    args.push("--detach".to_string());
    args.push("--json".to_string());
    Ok((
        format!("Open tunnel to slot {slot_number} of {}", shorten(&instance, 18)),
        args,
    ))
}

/// The status line for a finished `nodo tunnel --detach --json` / `tunnel_close --json`.
///
/// Both print one JSON object, and its `error` is the reason -- on stdout, which is
/// where `--json` puts everything, so the generic stderr-first-line report would say
/// nothing.
pub fn outcome_status(label: &str, success: bool, stdout: &str, stderr: &str) -> String {
    let document = crate::app::report_line(stdout)
        .and_then(|line| serde_json::from_str::<serde_json::Value>(line).ok());
    let error = document
        .as_ref()
        .and_then(|doc| doc.get("error"))
        .and_then(|error| error.as_str())
        .map(ToString::to_string);
    if !success || error.is_some() {
        let reason = error
            .or_else(|| stderr.lines().map(str::trim).find(|line| !line.is_empty()).map(ToString::to_string))
            .unwrap_or_else(|| "no reason given".to_string());
        return format!("{label} failed: {reason}");
    }
    if let Some(tunnel) = document
        .as_ref()
        .and_then(|doc| doc.get("tunnel"))
        .and_then(|tunnel| parse_tunnel(&tunnel.to_string()))
    {
        return format!(
            "Tunnel {} open: {} -> slot {} of {}",
            tunnel.id,
            tunnel.listen(),
            tunnel.slot,
            shorten(&tunnel.token, 18)
        );
    }
    if let Some(closed) = document
        .as_ref()
        .and_then(|doc| doc.get("closed"))
        .and_then(|closed| closed.as_array())
    {
        let ids: Vec<&str> = closed.iter().filter_map(|id| id.as_str()).collect();
        return if ids.is_empty() {
            "No tunnel was running".to_string()
        } else {
            format!("Closed tunnel {}", ids.join(", "))
        };
    }
    format!("{label} completed")
}

/// The tunnels that reach an instance: the INSTANCES page's relationship table, and
/// `nodo tunnels --instance <id>` on the CLI.
pub fn reaching<'a>(tunnels: &'a [Tunnel], instance_id: &str, instance_name: &str) -> Vec<&'a Tunnel> {
    tunnels
        .iter()
        .filter(|tunnel| tunnel.reaches(instance_id, instance_name))
        .collect()
}

/// The y/N question before a tunnel opens: what each connection through it costs.
///
/// `fee` is this node's `pricing.TUNNEL_OPEN_MU`, already formatted; it does not apply
/// through `--peer`, whose node charges its own price.
pub fn open_confirmation(label: &str, args: &[String], fee: Option<(u64, String)>) -> String {
    let through_peer = args.iter().any(|arg| arg == "--peer");
    let cost = match fee {
        _ if through_peer => {
            "The remote node charges each connection to the instance at its own price.".to_string()
        }
        Some((0, _)) => "Connections are free to open here (TUNNEL_OPEN_MU 0); traffic is billed.".to_string(),
        Some((_, text)) => format!(
            "Each connection spends {text} of the instance's balance (TUNNEL_OPEN_MU), plus traffic."
        ),
        None => "Each connection spends pricing.TUNNEL_OPEN_MU of the instance's balance.".to_string(),
    };
    format!("{label}? {cost} (y/N)")
}

/// The INSTANCES card's line about tunnels to the selected instance; the table under
/// the card lists them.
pub fn instance_summary(tunnels: &[Tunnel], instance_id: &str, instance_name: &str) -> String {
    match reaching(tunnels, instance_id, instance_name).len() {
        0 => "none • t opens one".to_string(),
        1 => "1, listed below • t opens another".to_string(),
        count => format!("{count}, listed below • t opens another"),
    }
}

/// The relationship table's columns: the instance is the selected row already.
pub(crate) const INSTANCE_TUNNEL_COLUMNS: [Column; 5] = [
    Column::new("Listen", Constraint::Min(22), 14, 0),
    Column::new("Slot", Constraint::Length(7), 5, 1),
    Column::new("ID", Constraint::Length(10), 8, 2),
    Column::new("Via", Constraint::Length(22), 9, 3),
    Column::new("Age", Constraint::Length(6), 4, 4),
];

/// Rows the relationship table wants: its borders, its header (and the gap under it)
/// and one per tunnel.
pub fn instance_tunnels_height(count: usize) -> u16 {
    if count == 0 {
        0
    } else {
        4 + count.min(12) as u16
    }
}

/// The tunnels of the selected instance, under its card on INSTANCES. Read-only: the
/// TUNNELS page is where one is closed or inspected.
pub fn draw_instance_tunnels(frame: &mut Frame, tunnels: &[Tunnel], label: &str, area: Rect) {
    if area.height < 3 {
        return;
    }
    let now = crate::app::unix_now();
    let rows = tunnels
        .iter()
        .map(|tunnel| {
            let cells: Vec<crate::ui::TextCell> = vec![
                tunnel.listen().into(),
                tunnel.slot.to_string().into(),
                tunnel.id.clone().into(),
                tunnel.via().into(),
                format_duration_compact(tunnel.age_secs(now)).into(),
            ];
            (cells, Style::default())
        })
        .collect();
    let (table, _) = fitted_table(&INSTANCE_TUNNEL_COLUMNS, rows, area, false);
    let table = table.block(section_block(
        format!(" TUNNELS → {} • {} ", shorten(label, 24), tunnels.len()),
        accent(),
    ));
    frame.render_widget(table, area);
}

fn last_lines(path: &str, count: usize) -> Vec<String> {
    match std::fs::read_to_string(path) {
        Ok(text) => {
            let lines: Vec<&str> = text.lines().collect();
            lines[lines.len().saturating_sub(count)..]
                .iter()
                .map(|line| line.to_string())
                .collect()
        }
        Err(_) => Vec::new(),
    }
}

/// The TUNNELS table's columns: where to connect outlasts everything else.
pub(crate) const TUNNEL_COLUMNS: [Column; 6] = [
    Column::new("Listen", Constraint::Min(22), 14, 0),
    Column::new("Slot", Constraint::Length(7), 5, 1),
    Column::new("Instance", Constraint::Length(20), 10, 2),
    Column::new("ID", Constraint::Length(10), 8, 3),
    Column::new("Via", Constraint::Length(22), 9, 4),
    Column::new("Age", Constraint::Length(6), 4, 5),
];

/// The inbound table's columns.
pub(crate) const INBOUND_COLUMNS: [Column; 7] = [
    Column::new("Caller", Constraint::Min(22), 12, 0),
    Column::new("Instance", Constraint::Length(18), 10, 1),
    Column::new("Slot", Constraint::Length(7), 5, 2),
    Column::new("Proto", Constraint::Length(6), 5, 5),
    Column::new("In", Constraint::Length(10), 7, 3),
    Column::new("Out", Constraint::Length(10), 7, 4),
    Column::new("Age", Constraint::Length(6), 4, 6),
];

/// Rows the inbound table wants: borders, header and the gap under it, and a row per
/// stream (or the one line saying there are none).
pub fn inbound_height(count: usize) -> u16 {
    if count == 0 {
        3
    } else {
        4 + count.min(10) as u16
    }
}

fn draw_inbound(frame: &mut Frame, streams: &[InboundStream], area: Rect) {
    if area.height < 3 {
        return;
    }
    let title = format!(" INBOUND • {} relayed for others • list only ", streams.len());
    if streams.is_empty() {
        let block = section_block(title, accent());
        let line = Line::from(Span::styled(
            "Nobody is tunnelling through this node right now (nodo tunnels --inbound).",
            Style::default().fg(muted()),
        ));
        frame.render_widget(ratatui::widgets::Paragraph::new(line).block(block), area);
        return;
    }
    let now = crate::app::unix_now();
    let rows = streams
        .iter()
        .map(|stream| {
            let cells: Vec<crate::ui::TextCell> = vec![
                stream.caller.clone().into(),
                shorten(&stream.token, 18).into(),
                stream.slot.clone().into(),
                stream.transport.clone().into(),
                crate::app::format_bytes_compact(stream.bytes_in).into(),
                crate::app::format_bytes_compact(stream.bytes_out).into(),
                format_duration_compact(stream.age_secs(now)).into(),
            ];
            (cells, Style::default())
        })
        .collect();
    let (table, _) = fitted_table(&INBOUND_COLUMNS, rows, area, false);
    frame.render_widget(table.block(section_block(title, accent())), area);
}

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    // The tunnels this host opened (selectable), their card, and under them the
    // streams this node relays for others. The inbound table gives way first.
    let heights = crate::layout_util::allocate_heights(
        area.height,
        &[(5, 8), (3, 13), (3, inbound_height(app.inbound_tunnels.len()))],
        &[0, 1, 2],
    );
    let rects = crate::layout_util::stack(
        area,
        &[area.height - heights[1] - heights[2], heights[1], heights[2]],
    );
    let layout = [rects[0], rects[1]];
    if rects[2].height > 0 {
        draw_inbound(frame, &app.inbound_tunnels, rects[2]);
    }
    let now = crate::app::unix_now();
    let rows = app
        .tunnels
        .items
        .iter()
        .map(|tunnel| {
            let cells: Vec<crate::ui::TextCell> = vec![
                tunnel.listen().into(),
                tunnel.slot.to_string().into(),
                shorten(&tunnel.instance, 20).into(),
                tunnel.id.clone().into(),
                tunnel.via().into(),
                format_duration_compact(tunnel.age_secs(now)).into(),
            ];
            (cells, Style::default())
        })
        .collect();
    let has_selection = app.tunnels.state.selected().is_some();
    let (table, _) = fitted_table(&TUNNEL_COLUMNS, rows, layout[0], has_selection);
    let table = table
        .block(section_block(
            format!(" TUNNELS • {} running ", app.tunnels.items.len()),
            accent(),
        ))
        .highlight_style(selected_style())
        .highlight_symbol("▸ ");
    app.list_area = layout[0];
    frame.render_stateful_widget(table, layout[0], &mut app.tunnels.state);

    let detail = match app.tunnels.selected() {
        Some(tunnel) => vec![
            metric_line("Tunnel", tunnel.id.clone()),
            metric_line("Listening", tunnel.listen()),
            metric_line("Reaches", format!("slot {} of {}", tunnel.slot, tunnel.token)),
            metric_line("Instance", tunnel.instance.clone()),
            metric_line(
                "Via",
                match &tunnel.peer {
                    Some(peer) => format!("{peer} (remote node)"),
                    None => format!("{} (this node)", tunnel.gateway),
                },
            ),
            metric_line(
                "Process",
                format!(
                    "pid {}{} • up {}",
                    tunnel.pid,
                    if tunnel.detached { " (detached)" } else { "" },
                    format_duration_compact(tunnel.age_secs(now))
                ),
            ),
            metric_line(
                "Fee",
                match tunnel.open_fee_mu {
                    Some(mu) => format!("{} per connection, plus traffic", app.money.format_mu(mu)),
                    None if tunnel.peer.is_some() => "the remote node's prices".to_string(),
                    None => "pricing.TUNNEL_OPEN_MU per connection".to_string(),
                },
            ),
            metric_line(
                "Restart",
                if tunnel.persistent {
                    "reopened by the node, until closed"
                } else {
                    "not reopened (started in a terminal)"
                },
            ),
            metric_line("Log", tunnel.log.clone().unwrap_or_else(|| "its terminal".to_string())),
            metric_line("Close", format!("d here, or nodo tunnel_close {}", tunnel.id)),
        ],
        None if app.tunnels.items.is_empty() => vec![
            Line::from(Span::styled(
                "No tunnels running.",
                Style::default().fg(text_colour()),
            )),
            Line::from(Span::styled(
                "n opens one here; t on INSTANCES opens one to the selected instance.",
                Style::default().fg(muted()),
            )),
        ],
        None => vec![Line::from(Span::styled(
            "Select a tunnel to see where it listens and what it reaches.",
            Style::default().fg(muted()),
        ))],
    };
    crate::ui::draw_card(frame, layout[1], "SELECTED TUNNEL", detail, accent());
}

impl App {
    /// Re-read the registry; called with the rest of the local data.
    pub(crate) fn refresh_tunnels(&mut self) {
        let directory = tunnels_dir(&self.paths.storage);
        self.tunnels.refresh(read_tunnels(&directory));
        self.inbound_tunnels = read_inbound(&directory);
    }

    /// Ask for a new tunnel: `t` on INSTANCES (to the selected instance) or `n` on
    /// TUNNELS (instance typed in).
    pub fn open_new_tunnel(&mut self) {
        let page = self.page();
        if page != Page::Instances && page != Page::Tunnels {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        self.tunnel_instance = None;
        self.input_title = if page == Page::Instances {
            let Some(instance) = self.instances.selected().cloned() else {
                self.status = "Select an instance first".to_string();
                return;
            };
            let label = if instance.name.trim().is_empty() {
                shorten(&instance.id, 18)
            } else {
                instance.name.clone()
            };
            self.tunnel_instance = Some(instance.id);
            format!("Tunnel to {label}: <slot> [--listen <port>] [--udp]")
        } else {
            "New tunnel: <instance> <slot> [--listen <port>] [--udp] [--peer <host:port>]".to_string()
        };
        self.input_mode = InputMode::NewTunnel;
        self.input.clear();
        self.edit_kind = crate::app::EditKind::Text;
    }

    /// The typed line, checked; then a y/N that names the fee before anything runs.
    pub(crate) fn submit_new_tunnel(&mut self) {
        match open_command(self.tunnel_instance.as_deref(), &self.input) {
            Ok((label, args)) => {
                self.close_input();
                self.tunnel_instance = None;
                let fee = self.tunnel_open_fee().map(|mu| (mu, self.money.format_mu(mu)));
                self.input_mode = InputMode::Confirm;
                self.input_title = open_confirmation(&label, &args, fee);
                self.pending_action = Some(PendingAction::OpenTunnel { label, args });
            }
            Err(message) => self.status = message,
        }
    }

    /// This node's `pricing.TUNNEL_OPEN_MU`, as the PRICES page read it.
    pub(crate) fn tunnel_open_fee(&self) -> Option<u64> {
        self.prices
            .items
            .iter()
            .find(|price| price.key == "TUNNEL_OPEN_MU" && price.arch.is_none())
            .map(|price| price.mu)
    }

    /// `d` on TUNNELS: confirm, then `nodo tunnel_close <id>`.
    pub fn open_close_tunnel_confirm(&mut self) {
        if self.page() != Page::Tunnels {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(tunnel) = self.tunnels.selected().cloned() else {
            self.status = "Select a tunnel first".to_string();
            return;
        };
        let label = format!("{} ({})", tunnel.id, tunnel.listen());
        self.input_mode = InputMode::Confirm;
        let restart = if tunnel.persistent { " It is not reopened after a restart." } else { "" };
        self.input_title = format!("Close tunnel {label}? Its clients are cut off.{restart} (y/N)");
        self.pending_action = Some(PendingAction::CloseTunnel { id: tunnel.id, label });
    }

    /// `i` on TUNNELS: the selected tunnel and the tail of its log, in the overlay.
    pub fn open_tunnel_details(&mut self) {
        if self.page() != Page::Tunnels {
            return;
        }
        let Some(tunnel) = self.tunnels.selected().cloned() else {
            self.status = "Select a tunnel first".to_string();
            return;
        };
        let mut lines = vec![
            format!("listening   {}", tunnel.listen()),
            format!("reaches     slot {} of {}", tunnel.slot, tunnel.token),
            format!("instance    {}", tunnel.instance),
            format!("via         {}", tunnel.gateway),
            format!("pid         {}", tunnel.pid),
        ];
        match &tunnel.log {
            Some(log) => {
                lines.push(format!("log         {log}"));
                lines.push(String::new());
                lines.push("Last log lines:".to_string());
                let tail = last_lines(log, LOG_TAIL_LINES);
                if tail.is_empty() {
                    lines.push("  (empty or unreadable)".to_string());
                }
                lines.extend(tail.into_iter().map(|line| format!("  {line}")));
            }
            None => lines.push("log         the terminal it was started in".to_string()),
        }
        self.details = Some(DetailsView {
            title: format!("Tunnel {}", tunnel.id),
            lines,
            scroll: 0,
        });
        self.input_mode = InputMode::Details;
        self.status = "Tunnel details • ↑/↓ scroll • Esc close".to_string();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn record(id: &str, pid: i64) -> String {
        format!(
            r#"{{"id": "{id}", "pid": {pid}, "instance": "web", "token": "abcdef0123456789",
                "slot": 8080, "transport": "tcp", "listen_host": "127.0.0.1",
                "listen_port": 9000, "gateway": "127.0.0.1:8090", "peer": null,
                "detached": true, "log": null, "started_at": 1790000000}}"#
        )
    }

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("nodo-tui-tunnels-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_registry_file_parses_into_a_tunnel() {
        let tunnel = parse_tunnel(&record("ab12cd34", 42)).unwrap();
        assert_eq!(tunnel.id, "ab12cd34");
        assert_eq!(tunnel.listen(), "127.0.0.1:9000/tcp");
        assert_eq!(tunnel.via(), "this node");
        assert_eq!(tunnel.age_secs(Some(1790000060)), Some(60.0));
        assert_eq!(tunnel.age_secs(None), None);
        assert!(parse_tunnel("{not json").is_none());
        assert!(parse_tunnel(r#"{"id": "", "pid": 1}"#).is_none());
    }

    #[test]
    fn only_tunnels_whose_process_runs_are_listed() {
        let dir = temp_dir("alive");
        // A child with `tunnel` in its argv, as the registry requires. `; true` keeps
        // sh from exec'ing sleep, which would replace that argv.
        let mut child = std::process::Command::new("sh")
            .args(["-c", "sleep 30; true", "tunnel"])
            .spawn()
            .unwrap();
        fs::write(dir.join("live0001.json"), record("live0001", child.id() as i64)).unwrap();
        fs::write(dir.join("dead0001.json"), record("dead0001", i32::MAX as i64 - 1)).unwrap();
        fs::write(dir.join("notes.txt"), "not a tunnel").unwrap();
        fs::write(dir.join("live0001.spec"), "{}").unwrap();
        fs::write(dir.join("gone0001.spec"), "{}").unwrap();

        let tunnels = read_tunnels(&dir);
        let ids: Vec<String> = tunnels.iter().map(|t| t.id.clone()).collect();

        assert!(!ids.contains(&"dead0001".to_string()), "{ids:?}");
        assert_eq!(ids, vec!["live0001".to_string()], "a spec alone is not a tunnel");
        assert!(tunnels[0].persistent, "its spec says the node reopens it");
        let _ = child.kill();
        let _ = child.wait();
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_inbound_snapshot_is_read_only_from_a_live_fresh_daemon() {
        let snapshot = r#"{"pid": 77, "written_at": 1790000000, "streams": [
            {"id": "in2", "caller": "ipv4:203.0.113.8:1", "token": "tok", "slot": 53,
             "transport": "udp", "started_at": 1789999990, "bytes_in": 10, "bytes_out": 20},
            {"id": "in1", "caller": "ipv4:203.0.113.7:51000", "token": "abcdef", "slot": 8080,
             "transport": "tcp", "started_at": 1789999900, "bytes_in": 2048, "bytes_out": 4096},
            {"caller": "no id: skipped"}]}"#;
        let streams = parse_inbound(snapshot, Some(1790000010), |pid| pid == 77);
        let ids: Vec<&str> = streams.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["in1", "in2"], "oldest first, id-less entries dropped");
        assert_eq!(streams[0].slot, "8080");
        assert_eq!(streams[0].bytes_out, 4096);
        assert_eq!(streams[0].age_secs(Some(1790000000)), Some(100.0));

        assert!(parse_inbound(snapshot, Some(1790000010), |_| false).is_empty(), "daemon gone");
        assert!(parse_inbound(snapshot, Some(1790000100), |_| true).is_empty(), "stale");
        assert!(parse_inbound("{not json", Some(1), |_| true).is_empty());
        let idle = r#"{"pid": 77, "written_at": 1, "streams": []}"#;
        assert!(parse_inbound(idle, Some(1790000000), |_| true).is_empty());
    }

    #[test]
    fn the_inbound_file_is_not_a_tunnel() {
        let dir = temp_dir("inbound");
        fs::write(
            dir.join(INBOUND_FILE),
            format!(r#"{{"pid": {}, "written_at": 1, "streams": []}}"#, std::process::id()),
        )
        .unwrap();
        assert!(read_tunnels(&dir).is_empty());
        assert!(read_inbound(&dir).is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_tunnels_page_draws_both_tables() {
        let mut app = App::default();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Tunnels).unwrap();
        app.tunnels = crate::app::StatefulList::with_items(vec![parse_tunnel(&record("ab12cd34", 1)).unwrap()]);
        app.inbound_tunnels = vec![InboundStream {
            id: "in1".into(),
            caller: "ipv4:203.0.113.7:51000".into(),
            token: "abcdef".into(),
            slot: "8080".into(),
            transport: "tcp".into(),
            started_at: None,
            bytes_in: 2048,
            bytes_out: 0,
        }];
        let mut terminal = ratatui::Terminal::new(ratatui::backend::TestBackend::new(110, 40)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app, frame.size())).unwrap();
        let text: String = terminal.backend().buffer().content().iter().map(|cell| cell.symbol()).collect();
        for expected in ["TUNNELS • 1 running", "SELECTED TUNNEL", "INBOUND • 1 relayed", "ipv4:203.0.113.7:51000"] {
            assert!(text.contains(expected), "{expected} missing from {text}");
        }
    }

    #[test]
    fn a_missing_registry_is_an_empty_page() {
        assert!(read_tunnels(Path::new("/nonexistent/nodo/tunnels")).is_empty());
    }

    #[test]
    fn from_instances_the_line_is_a_slot_and_flags() {
        let (label, args) = open_command(Some("inst-1"), " 8080 --listen 9000 --udp ").unwrap();
        assert_eq!(
            args,
            ["tunnel", "inst-1", "8080", "--listen", "9000", "--udp", "--detach", "--json"]
        );
        assert!(label.contains("slot 8080"), "{label}");
    }

    #[test]
    fn from_tunnels_the_instance_comes_first() {
        let (_, args) = open_command(None, "web 53 --peer 10.0.0.2:4040").unwrap();
        assert_eq!(
            args,
            ["tunnel", "web", "53", "--peer", "10.0.0.2:4040", "--detach", "--json"]
        );
    }

    #[test]
    fn a_bad_line_is_answered_rather_than_run() {
        assert!(open_command(Some("i"), "").is_err());
        assert!(open_command(Some("i"), "http").unwrap_err().contains("port number"));
        assert!(open_command(Some("i"), "0").is_err());
        assert!(open_command(Some("i"), "8080 --listen").unwrap_err().contains("needs a value"));
        assert!(open_command(Some("i"), "8080 --listen x").is_err());
        assert!(open_command(Some("i"), "8080 --rm -rf").unwrap_err().contains("Unexpected"));
        assert!(open_command(None, "").is_err());
        assert!(open_command(None, "web").is_err());
    }

    #[test]
    fn the_outcome_is_read_from_the_json_object() {
        // `--json` prints one object on one line, after whatever start-up noise.
        let one_line = serde_json::from_str::<serde_json::Value>(&record("ab12cd34", 1))
            .unwrap()
            .to_string();
        let opened = format!("noise\n{{\"tunnel\": {one_line}, \"read_at\": 1}}\n");
        assert_eq!(
            outcome_status("Open tunnel", true, &opened, ""),
            "Tunnel ab12cd34 open: 127.0.0.1:9000/tcp -> slot 8080 of abcdef0123456789"
        );
        assert_eq!(
            outcome_status("Open tunnel", false, "{\"error\": \"Error: cannot bind\"}\n", ""),
            "Open tunnel failed: Error: cannot bind"
        );
        assert_eq!(
            outcome_status("Close tunnel x", true, "{\"closed\": [\"x\"], \"failed\": []}", ""),
            "Closed tunnel x"
        );
        assert_eq!(
            outcome_status("Close", false, "", "Traceback\n"),
            "Close failed: Traceback"
        );
    }

    #[test]
    fn the_instance_card_names_the_tunnels_that_reach_it() {
        let tunnel = parse_tunnel(&record("ab12cd34", 1)).unwrap();
        let remote = Tunnel { peer: Some("10.0.0.2:4040".into()), ..tunnel.clone() };
        let tunnels = vec![tunnel, remote];
        assert_eq!(
            instance_summary(&tunnels, "abcdef0123456789", ""),
            "1, listed below • t opens another"
        );
        assert_eq!(instance_summary(&tunnels, "other", "web"), "1, listed below • t opens another");
        assert_eq!(instance_summary(&tunnels, "other", "other"), "none • t opens one");
    }

    #[test]
    fn the_relationship_table_holds_the_tunnels_of_one_instance() {
        let mine = parse_tunnel(&record("ab12cd34", 1)).unwrap();
        let second = Tunnel { id: "ef56ab78".into(), listen_port: 9001, ..mine.clone() };
        let remote = Tunnel { id: "remote01".into(), peer: Some("10.0.0.2:4040".into()), ..mine.clone() };
        let other = Tunnel { id: "other001".into(), instance: "db".into(), token: "ffff".into(), ..mine.clone() };
        let tunnels = vec![mine, second, remote, other];

        let ids: Vec<&str> = reaching(&tunnels, "abcdef0123456789", "web")
            .iter()
            .map(|tunnel| tunnel.id.as_str())
            .collect();
        assert_eq!(ids, ["ab12cd34", "ef56ab78"]);
        assert_eq!(instance_summary(&tunnels, "abcdef0123456789", "web"), "2, listed below • t opens another");
        assert_eq!(instance_tunnels_height(0), 0, "no table without tunnels");
        assert_eq!(instance_tunnels_height(2), 6);
        assert_eq!(instance_tunnels_height(40), 16, "capped");
    }

    #[test]
    fn the_relationship_table_draws_each_tunnel_with_its_columns() {
        let tunnel = parse_tunnel(&record("ab12cd34", 1)).unwrap();
        let backend = ratatui::backend::TestBackend::new(80, 6);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_instance_tunnels(frame, &[tunnel.clone()], "web", frame.size()))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        for expected in ["TUNNELS → web", "Listen", "127.0.0.1:9000/tcp", "8080", "ab12cd34", "this node"] {
            assert!(text.contains(expected), "{expected} missing from {text}");
        }
    }

    mod keys {
        use super::*;
        use crate::app::{pending_command, Instance, InstanceClient, InstanceUsage, StatefulList};
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

        fn on_page(page: Page) -> App {
            let mut app = App::default();
            app.tabs.index = Page::ALL.iter().position(|p| *p == page).unwrap();
            app
        }

        fn press(app: &mut App, key: char) {
            let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
            rt.block_on(crate::handler::handle_key_events(
                KeyEvent::new(KeyCode::Char(key), KeyModifiers::NONE),
                app,
            ))
            .unwrap();
        }

        fn instance(id: &str, name: &str) -> Instance {
            Instance {
                id: id.to_string(),
                name: name.to_string(),
                ip: String::new(),
                service: String::new(),
                balance: "0".to_string(),
                virtualizer: "ch".to_string(),
                memory_limit: 0,
                disk_limit: 0,
                vcpus: None,
                usage: InstanceUsage::default(),
                location: "local".to_string(),
                father_id: String::new(),
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
                energy_watts: None,
                energy_share: None,
                age_secs: None,
                client: InstanceClient::None,
            }
        }

        #[test]
        fn t_on_instances_asks_for_a_slot_of_the_selected_instance() {
            let mut app = on_page(Page::Instances);
            app.instances = StatefulList::with_items(vec![instance("inst-1", "web")]);
            app.instances.next();

            press(&mut app, 't');

            assert_eq!(app.input_mode, InputMode::NewTunnel);
            assert_eq!(app.tunnel_instance.as_deref(), Some("inst-1"));
            assert!(app.input_title.contains("web"), "{}", app.input_title);
            assert!(app.command_task.is_none(), "nothing runs before a slot is given");
        }

        #[test]
        fn t_without_a_selection_says_so() {
            let mut app = on_page(Page::Instances);
            press(&mut app, 't');
            assert_eq!(app.input_mode, InputMode::Normal);
            assert!(app.status.contains("Select an instance"), "{}", app.status);
        }

        #[test]
        fn n_on_tunnels_asks_for_the_instance_too() {
            let mut app = on_page(Page::Tunnels);
            press(&mut app, 'n');
            assert_eq!(app.input_mode, InputMode::NewTunnel);
            assert_eq!(app.tunnel_instance, None);
        }

        #[test]
        fn opening_asks_first_and_names_the_fee() {
            let mut app = on_page(Page::Instances);
            app.instances = StatefulList::with_items(vec![instance("inst-1", "web")]);
            app.instances.next();
            press(&mut app, 't');
            app.input = "8080".to_string();

            app.submit_new_tunnel();

            assert_eq!(app.input_mode, InputMode::Confirm);
            assert!(app.command_task.is_none(), "nothing runs before y");
            assert!(app.input_title.contains("TUNNEL_OPEN_MU"), "{}", app.input_title);
            assert!(app.input_title.ends_with("(y/N)"), "{}", app.input_title);
            let action = app.pending_action.clone().expect("a pending open");
            let (_, args) = pending_command(action).unwrap();
            assert_eq!(args, ["tunnel", "inst-1", "8080", "--detach", "--json"]);
        }

        #[test]
        fn the_question_states_the_amount() {
            let label = "Open tunnel to slot 8080 of web";
            let local = vec!["tunnel".to_string(), "web".to_string(), "8080".to_string()];
            assert_eq!(
                open_confirmation(label, &local, Some((10000, "10,000 MU".to_string()))),
                "Open tunnel to slot 8080 of web? Each connection spends 10,000 MU of the \
                 instance's balance (TUNNEL_OPEN_MU), plus traffic. (y/N)"
            );
            assert!(open_confirmation(label, &local, Some((0, "0 MU".to_string()))).contains("free to open"));
            let remote = vec!["tunnel".into(), "tok".into(), "80".into(), "--peer".into(), "h:1".into()];
            assert!(open_confirmation(label, &remote, Some((10000, "x".into()))).contains("remote node"));
        }

        #[test]
        fn a_typo_is_answered_in_the_prompt_rather_than_run() {
            let mut app = on_page(Page::Tunnels);
            press(&mut app, 'n');
            app.input = "web http".to_string();

            app.submit_new_tunnel();

            assert_eq!(app.input_mode, InputMode::NewTunnel, "still open to fix it");
            assert!(app.command_task.is_none());
            assert!(app.status.contains("port number"), "{}", app.status);
        }

        #[test]
        fn d_on_tunnels_confirms_before_closing() {
            let mut app = on_page(Page::Tunnels);
            let tunnel = parse_tunnel(&record("ab12cd34", 1)).unwrap();
            app.tunnels = StatefulList::with_items(vec![tunnel]);
            app.tunnels.next();

            press(&mut app, 'd');

            assert_eq!(app.input_mode, InputMode::Confirm);
            let action = app.pending_action.clone().expect("a pending close");
            let (_, args) = pending_command(action).unwrap();
            assert_eq!(args, ["tunnel_close", "ab12cd34", "--json"]);
            assert!(!app.input_title.contains("restart"), "{}", app.input_title);
        }

        #[test]
        fn closing_a_persistent_tunnel_says_it_will_not_come_back() {
            let mut app = on_page(Page::Tunnels);
            let tunnel = Tunnel { persistent: true, ..parse_tunnel(&record("ab12cd34", 1)).unwrap() };
            app.tunnels = StatefulList::with_items(vec![tunnel]);
            app.tunnels.next();

            press(&mut app, 'd');

            assert!(app.input_title.contains("not reopened after a restart"), "{}", app.input_title);
        }

        #[test]
        fn i_on_tunnels_shows_the_tunnel_in_the_overlay() {
            let mut app = on_page(Page::Tunnels);
            let tunnel = parse_tunnel(&record("ab12cd34", 1)).unwrap();
            app.tunnels = StatefulList::with_items(vec![tunnel]);
            app.tunnels.next();

            press(&mut app, 'i');

            assert_eq!(app.input_mode, InputMode::Details);
            let details = app.details.as_ref().unwrap();
            assert!(details.lines.iter().any(|line| line.contains("slot 8080")));
        }

        #[test]
        fn killing_an_instance_says_its_tunnels_close_with_it() {
            let mut app = on_page(Page::Instances);
            app.instances = StatefulList::with_items(vec![instance("abcdef0123456789", "web")]);
            app.instances.next();
            app.tunnels = StatefulList::with_items(vec![parse_tunnel(&record("ab12cd34", 1)).unwrap()]);

            app.open_kill_instance_confirm();

            assert_eq!(app.input_mode, InputMode::Confirm);
            assert!(app.input_title.contains("Its tunnel is closed too"), "{}", app.input_title);

            app.close_input();
            app.tunnels = StatefulList::with_items(Vec::new());
            app.open_kill_instance_confirm();
            assert_eq!(app.input_title, "Kill instance web? (y/N)");
        }

        #[test]
        fn the_keys_mean_nothing_on_other_pages() {
            let mut app = on_page(Page::Services);
            app.open_new_tunnel();
            app.open_close_tunnel_confirm();
            app.open_tunnel_details();
            assert_eq!(app.input_mode, InputMode::Normal);
        }
    }
}
