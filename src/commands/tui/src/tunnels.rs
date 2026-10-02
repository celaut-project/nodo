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

use crate::app::{shorten, App, CommandKind, DetailsView, Identifiable, InputMode, PendingAction, Page};
use crate::layout_util::Column;
use crate::ui::{accent, fitted_table, format_duration_compact, metric_line, muted, section_block, selected_style, text_colour};
use ratatui::prelude::*;
use std::path::{Path, PathBuf};

/// Overrides where the registry lives; the same variable the Python side reads.
const DIR_ENV: &str = "NODO_TUNNELS_DIR";

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
        .filter_map(|path| std::fs::read_to_string(path).ok())
        .filter_map(|text| parse_tunnel(&text))
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

/// The INSTANCES card's line about tunnels to the selected instance.
pub fn instance_summary(tunnels: &[Tunnel], instance_id: &str, instance_name: &str) -> String {
    let reaching: Vec<String> = tunnels
        .iter()
        .filter(|tunnel| tunnel.reaches(instance_id, instance_name))
        .map(|tunnel| format!("{} -> {}", tunnel.listen(), tunnel.slot))
        .collect();
    if reaching.is_empty() {
        "none • t opens one".to_string()
    } else {
        format!("{} • t opens another", reaching.join(", "))
    }
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

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let layout = crate::ui::list_and_card(area, 8, 11);
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

    pub(crate) fn submit_new_tunnel(&mut self) {
        match open_command(self.tunnel_instance.as_deref(), &self.input) {
            Ok((label, args)) => {
                self.close_input();
                self.tunnel_instance = None;
                self.spawn_command(CommandKind::Tunnel, label, args);
            }
            Err(message) => self.status = message,
        }
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
        self.input_title = format!("Close tunnel {label}? Its clients are cut off. (y/N)");
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

        let ids: Vec<String> = read_tunnels(&dir).into_iter().map(|t| t.id).collect();

        assert!(!ids.contains(&"dead0001".to_string()), "{ids:?}");
        assert_eq!(ids, vec!["live0001".to_string()]);
        let _ = child.kill();
        let _ = child.wait();
        let _ = fs::remove_dir_all(&dir);
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
            "127.0.0.1:9000/tcp -> 8080 • t opens another"
        );
        assert_eq!(instance_summary(&tunnels, "other", "web"), "127.0.0.1:9000/tcp -> 8080 • t opens another");
        assert_eq!(instance_summary(&tunnels, "other", "other"), "none • t opens one");
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
        fn the_keys_mean_nothing_on_other_pages() {
            let mut app = on_page(Page::Services);
            app.open_new_tunnel();
            app.open_close_tunnel_confirm();
            app.open_tunnel_details();
            assert_eq!(app.input_mode, InputMode::Normal);
        }
    }
}
