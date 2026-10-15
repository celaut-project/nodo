//! The PACKS page: the `nodo pack` runs on this host, current and recent.
//!
//! Every `nodo pack` records itself in `<main.STORAGE>/packs/<id>.json` and keeps it
//! current while it runs -- status, stage, and at the end the service id or why there
//! is none (`src/utils/pack_registry.py`). This page reads those files directly, as
//! TUNNELS reads its registry, and every action is the CLI an operator would type:
//! `n` launches `nodo pack <folder | https git URL> --detach --json`, `c` runs
//! `nodo pack_cancel <id> --json`, `i` shows the record with the tail of its log.
//!
//! The field names are the contract with the Python side. A record that says
//! `queued`/`running` but whose process is gone did not finish: it is shown as failed
//! (`nodo packs` writes that back; this page only reads).

use crate::app::{shorten, App, CommandKind, DetailsView, Identifiable, InputMode, PendingAction, Page};
use crate::layout_util::Column;
use crate::ui::{accent, fitted_table, format_duration_compact, metric_line, muted, section_block, selected_style, text_colour};
use ratatui::prelude::*;
use std::path::{Path, PathBuf};

/// Overrides where the registry lives; the same variable the Python side reads.
const DIR_ENV: &str = "NODO_PACKS_DIR";
/// Log lines the details overlay shows.
const LOG_TAIL_LINES: usize = 60;
/// `pack_registry.LOST_ERROR`.
pub const LOST_ERROR: &str =
    "the pack process exited without reporting (killed, or the host restarted)";
/// Folders the launch prompt suggests under what is typed.
pub const SUGGESTIONS: usize = 4;

#[derive(Debug, Clone, PartialEq)]
pub struct Pack {
    pub id: String,
    pub pid: i64,
    /// An absolute folder, or the https git URL (with any `#subdir`) as typed.
    pub source: String,
    /// `dir` or `git`.
    pub kind: String,
    /// `local` (`packer.local`, nodo's rootless builder) or `service` (a packer VM).
    pub packer: String,
    /// `queued`, `running`, `done`, `failed` or `cancelled`.
    pub status: String,
    /// Where a running pack is: `cloning`, `building`, `importing`, ...
    pub stage: Option<String>,
    pub detached: bool,
    pub log: Option<String>,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub service_id: Option<String>,
    pub error: Option<String>,
}

impl Identifiable for Pack {
    fn id(&self) -> &str {
        &self.id
    }
}

impl Pack {
    pub fn is_active(&self) -> bool {
        self.status == "queued" || self.status == "running"
    }

    pub fn age_secs(&self, now: Option<i64>) -> Option<f64> {
        Some((now? - self.started_at?).max(0) as f64)
    }

    /// How long it ran, or has been running.
    pub fn duration_secs(&self, now: Option<i64>) -> Option<f64> {
        let end = self.finished_at.or(now)?;
        Some((end - self.started_at?).max(0) as f64)
    }

    /// What it came to, or what it is doing: one cell of the table.
    pub fn result(&self) -> String {
        match self.status.as_str() {
            "done" => self.service_id.clone().unwrap_or_else(|| "?".to_string()),
            "queued" | "running" => self.stage.clone().unwrap_or_else(|| self.status.clone()),
            _ => self.error.clone().unwrap_or_else(|| self.status.clone()),
        }
    }

    /// The end of a long source is the part that tells packs apart.
    pub fn source_label(&self, width: usize) -> String {
        let count = self.source.chars().count();
        if count <= width || width < 2 {
            return self.source.clone();
        }
        let tail: String = self.source.chars().skip(count - (width - 1)).collect();
        format!("…{tail}")
    }
}

/// `<storage>/packs`, unless `NODO_PACKS_DIR` says otherwise.
pub fn packs_dir(storage: &Path) -> PathBuf {
    std::env::var_os(DIR_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|| storage.join("packs"))
}

pub fn parse_pack(text: &str) -> Option<Pack> {
    let value: serde_json::Value = serde_json::from_str(text).ok()?;
    let string = |key: &str| value.get(key).and_then(|v| v.as_str()).map(ToString::to_string);
    let integer = |key: &str| value.get(key).and_then(|v| v.as_i64());
    Some(Pack {
        id: string("id").filter(|id| !id.is_empty())?,
        pid: integer("pid").unwrap_or(0),
        source: string("source").unwrap_or_default(),
        kind: string("kind").unwrap_or_else(|| "dir".to_string()),
        packer: string("packer").unwrap_or_default(),
        status: string("status").unwrap_or_else(|| "running".to_string()),
        stage: string("stage"),
        detached: value.get("detached").and_then(|v| v.as_bool()).unwrap_or(false),
        log: string("log"),
        started_at: integer("started_at"),
        finished_at: integer("finished_at"),
        service_id: string("service_id"),
        error: string("error"),
    })
}

/// A pack that says it is running but whose process is gone, as `nodo packs` reports
/// it: failed, with the reason. In memory only.
pub fn settle(mut pack: Pack, alive: impl Fn(i64) -> bool) -> Pack {
    if pack.is_active() && !alive(pack.pid) {
        pack.status = "failed".to_string();
        pack.stage = None;
        if pack.error.is_none() {
            pack.error = Some(LOST_ERROR.to_string());
        }
    }
    pack
}

/// Every pack on record in `directory`, newest first.
pub fn read_packs(directory: &Path) -> Vec<Pack> {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut packs: Vec<Pack> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().map(|ext| ext == "json").unwrap_or(false))
        .filter_map(|path| parse_pack(&std::fs::read_to_string(&path).ok()?))
        .map(|pack| settle(pack, |pid| crate::tunnels::process_running_as(pid, b"pack")))
        .collect();
    packs.sort_by(|a, b| (b.started_at, &b.id).cmp(&(a.started_at, &a.id)));
    packs
}

// -- What to pack ------------------------------------------------------------------------

/// What the launch prompt's text names.
#[derive(Debug, Clone, PartialEq)]
pub enum PackSource {
    /// An https git URL, as typed (`#subdir` included).
    Git(String),
    /// A folder on this machine, made absolute.
    Dir(PathBuf),
}

/// `~`, and a path relative to where the TUI was started, made absolute.
pub fn resolve_path(text: &str, base: &Path, home: Option<&Path>) -> PathBuf {
    let expanded = match (text.strip_prefix('~'), home) {
        (Some(rest), Some(home)) if rest.is_empty() || rest.starts_with('/') => {
            home.join(rest.trim_start_matches('/'))
        }
        _ => PathBuf::from(text),
    };
    if expanded.is_absolute() {
        expanded
    } else {
        base.join(expanded)
    }
}

/// `pack_registry.validate_source`, in Rust: checked before anything runs so a typo
/// is answered in the prompt rather than by a failed background pack.
pub fn classify_source(input: &str, base: &Path, home: Option<&Path>) -> Result<PackSource, String> {
    let text = input.trim();
    if text.is_empty() {
        return Err("Type a folder or an https git URL".to_string());
    }
    let lowered = text.to_lowercase();
    if lowered.starts_with("https://") {
        let (url, subdir) = text.split_once('#').unwrap_or((text, ""));
        let rest = &url["https://".len()..];
        let (host, path) = rest.split_once('/').unwrap_or((rest, ""));
        if host.is_empty() || path.trim_matches('/').is_empty() {
            return Err(format!("'{text}' is not a repository URL (https://host/owner/repo.git)"));
        }
        if text.chars().any(char::is_whitespace) {
            return Err("A git URL cannot contain spaces".to_string());
        }
        if subdir.starts_with('/') || subdir.split('/').any(|part| part == "..") {
            return Err(format!("'#{subdir}' must be a subdirectory inside the repository"));
        }
        return Ok(PackSource::Git(text.to_string()));
    }
    if lowered.starts_with("http://") {
        return Err("Plain http:// is refused (the code could be changed in transit): use https://".to_string());
    }
    let first = text.split('/').next().unwrap_or("");
    if ["ssh://", "git://", "git@", "file://"].iter().any(|scheme| lowered.starts_with(scheme))
        || (first.contains(':') && first.contains('@'))
    {
        return Err("Only https git URLs: the repository is cloned without credentials".to_string());
    }
    let path = resolve_path(text, base, home);
    if !path.exists() {
        return Err(format!("{} does not exist", path.display()));
    }
    if !path.is_dir() {
        return Err(format!("{} is a file; give the project's folder", path.display()));
    }
    Ok(PackSource::Dir(path))
}

/// The `nodo pack` a typed line turns into, or why it does not. Always detached: the
/// TUI cannot hold a terminal open for a build, and reads the outcome from the JSON.
pub fn launch_command(input: &str, base: &Path, home: Option<&Path>) -> Result<(String, Vec<String>), String> {
    let source = match classify_source(input, base, home)? {
        PackSource::Git(url) => url,
        PackSource::Dir(path) => path.to_string_lossy().to_string(),
    };
    Ok((
        format!("Pack {}", shorten(&source, 40)),
        vec!["pack".to_string(), source, "--detach".to_string(), "--json".to_string()],
    ))
}

/// The prompt's live line under what is typed: what it will pack, or why it won't.
pub fn input_feedback(input: &str, base: &Path, home: Option<&Path>) -> Result<String, String> {
    match classify_source(input, base, home)? {
        PackSource::Git(_) => Ok("git repository: cloned over https, then packed".to_string()),
        PackSource::Dir(path) => {
            let has = |name: &str| path.join(name).exists() || path.join(".service").join(name).exists();
            Ok(match (has("Dockerfile"), has("service.json")) {
                (true, true) => format!("folder {} (Dockerfile + service.json)", path.display()),
                (true, false) => format!("folder {} (Dockerfile; no service.json)", path.display()),
                _ => format!("folder {} (no Dockerfile: see docs/PACKING.md)", path.display()),
            })
        }
    }
}

/// Split typed text into the folder to list and the prefix of the name being typed.
fn completion_parts(input: &str, base: &Path, home: Option<&Path>) -> Option<(PathBuf, String)> {
    let lowered = input.to_lowercase();
    if lowered.starts_with("http") || input.contains('@') {
        return None;
    }
    let (directory, prefix) = match input.rfind('/') {
        Some(index) => (&input[..=index], &input[index + 1..]),
        None if input == "~" => return None,
        None => ("", input),
    };
    let directory = if directory.is_empty() { base.to_path_buf() } else { resolve_path(directory, base, home) };
    Some((directory, prefix.to_string()))
}

/// Subfolders of the typed folder whose names start with what follows the last `/`.
/// Hidden ones only when a `.` was typed.
pub fn folder_matches(input: &str, base: &Path, home: Option<&Path>) -> Vec<String> {
    let Some((directory, prefix)) = completion_parts(input, base, home) else {
        return Vec::new();
    };
    let Ok(entries) = std::fs::read_dir(&directory) else {
        return Vec::new();
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name.starts_with(&prefix) && (prefix.starts_with('.') || !name.starts_with('.')))
        .collect();
    names.sort();
    names
}

/// Tab in the launch prompt: complete the folder name being typed -- all of it when one
/// folder matches (with a `/` to go on into it), as far as the matches agree otherwise.
/// `None` when there is nothing to add.
pub fn complete_folder(input: &str, base: &Path, home: Option<&Path>) -> Option<String> {
    if input == "~" {
        return Some("~/".to_string());
    }
    let (_, prefix) = completion_parts(input, base, home)?;
    let matches = folder_matches(input, base, home);
    let first = matches.first()?;
    let completion = if matches.len() == 1 {
        format!("{first}/")
    } else {
        let mut common: String = first.clone();
        for name in &matches[1..] {
            let shared = common
                .chars()
                .zip(name.chars())
                .take_while(|(a, b)| a == b)
                .count();
            common = common.chars().take(shared).collect();
        }
        common
    };
    if completion.len() <= prefix.len() {
        return None;
    }
    Some(format!("{input}{}", &completion[prefix.len()..]))
}

/// The status line for a finished `nodo pack --detach --json` / `pack_cancel --json`.
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
    if let Some(pack) = document
        .as_ref()
        .and_then(|doc| doc.get("pack"))
        .and_then(|pack| parse_pack(&pack.to_string()))
    {
        return format!("Pack {} started: {} (i shows its log)", pack.id, shorten(&pack.source, 40));
    }
    if let Some(cancelled) = document
        .as_ref()
        .and_then(|doc| doc.get("cancelled"))
        .and_then(|cancelled| cancelled.as_array())
    {
        let ids: Vec<&str> = cancelled.iter().filter_map(|id| id.as_str()).collect();
        if !ids.is_empty() {
            return format!("Cancelled pack {}", ids.join(", "));
        }
    }
    format!("{label} completed")
}

fn last_lines(path: &str, count: usize) -> Vec<String> {
    match std::fs::read(path) {
        Ok(bytes) => {
            let text = String::from_utf8_lossy(&bytes);
            let lines: Vec<&str> = text.split('\n').collect();
            // A trailing newline leaves an empty last piece that is not a line.
            let lines = match lines.last() {
                Some(last) if last.is_empty() => &lines[..lines.len() - 1],
                _ => &lines[..],
            };
            lines[lines.len().saturating_sub(count)..]
                .iter()
                // A line redrawn with \r (a progress bar) shows its last state.
                .map(|line| line.rsplit('\r').next().unwrap_or("").trim_end().to_string())
                .collect()
        }
        Err(_) => Vec::new(),
    }
}

/// The last thing the pack said, for the card.
pub fn last_said(pack: &Pack) -> Option<String> {
    let log = pack.log.as_deref()?;
    last_lines(log, 10)
        .into_iter()
        .rev()
        .map(|line| line.trim().to_string())
        .find(|line| !line.is_empty())
}

/// The PACKS table's columns: what it packs and what came of it outlast the rest.
pub(crate) const PACK_COLUMNS: [Column; 6] = [
    Column::new("Source", Constraint::Min(24), 12, 0),
    Column::new("Status", Constraint::Length(10), 7, 1),
    Column::new("Result / stage", Constraint::Min(24), 12, 2),
    Column::new("ID", Constraint::Length(10), 8, 3),
    Column::new("Age", Constraint::Length(6), 4, 4),
    Column::new("Took", Constraint::Length(6), 4, 5),
];

fn status_style(pack: &Pack) -> Style {
    match pack.status.as_str() {
        "done" => Style::default().fg(crate::ui::good()),
        "failed" => Style::default().fg(crate::ui::warn()),
        "cancelled" => Style::default().fg(muted()),
        _ => Style::default().fg(accent()),
    }
}

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let heights = crate::layout_util::allocate_heights(area.height, &[(5, 8), (3, 14)], &[0, 1]);
    let rects = crate::layout_util::stack(area, &[area.height - heights[1], heights[1]]);
    let now = crate::app::unix_now();
    let rows = app
        .packs
        .items
        .iter()
        .map(|pack| {
            let cells: Vec<crate::ui::TextCell> = vec![
                pack.source_label(40).into(),
                pack.status.clone().into(),
                pack.result().into(),
                pack.id.clone().into(),
                format_duration_compact(pack.age_secs(now)).into(),
                format_duration_compact(pack.duration_secs(now)).into(),
            ];
            (cells, status_style(pack))
        })
        .collect();
    let running = app.packs.items.iter().filter(|pack| pack.is_active()).count();
    let has_selection = app.packs.state.selected().is_some();
    let (table, _) = fitted_table(&PACK_COLUMNS, rows, rects[0], has_selection);
    let table = table
        .block(section_block(
            format!(" PACKS • {running} running • {} on record ", app.packs.items.len()),
            accent(),
        ))
        .highlight_style(selected_style())
        .highlight_symbol("▸ ");
    app.list_area = rects[0];
    frame.render_stateful_widget(table, rects[0], &mut app.packs.state);

    let detail = match app.packs.selected() {
        Some(pack) => {
            let mut lines = vec![
                metric_line("Pack", format!("{} • {}", pack.id, pack.status)),
                metric_line(
                    "Source",
                    format!("{} ({})", pack.source, if pack.kind == "git" { "git" } else { "folder" }),
                ),
                metric_line(
                    "Packer",
                    match pack.packer.as_str() {
                        "local" => "this host (packer.local, rootless builder)".to_string(),
                        "service" => "packer service VM".to_string(),
                        other => other.to_string(),
                    },
                ),
                metric_line(
                    "Process",
                    format!(
                        "pid {}{} • {} {}",
                        pack.pid,
                        if pack.detached { " (detached)" } else { " (a terminal)" },
                        if pack.is_active() { "up" } else { "took" },
                        format_duration_compact(pack.duration_secs(now))
                    ),
                ),
            ];
            if let Some(stage) = &pack.stage {
                lines.push(metric_line("Stage", stage.clone()));
            }
            if let Some(service_id) = &pack.service_id {
                lines.push(metric_line("Service", service_id.clone()));
            }
            if let Some(error) = &pack.error {
                lines.push(metric_line("Error", error.clone()));
            }
            if let Some(said) = last_said(pack) {
                lines.push(metric_line("Last line", said));
            }
            lines.push(metric_line(
                "Log",
                pack.log.clone().unwrap_or_else(|| "the terminal it was started in".to_string()),
            ));
            lines.push(metric_line(
                "Keys",
                if pack.is_active() {
                    format!("i log • c cancel (nodo pack_cancel {})", pack.id)
                } else {
                    format!("i log (nodo packs {})", pack.id)
                },
            ));
            lines
        }
        None if app.packs.items.is_empty() => vec![
            Line::from(Span::styled("No packs on record.", Style::default().fg(text_colour()))),
            Line::from(Span::styled(
                "n packs a folder or an https git URL in the background (nodo pack … --detach).",
                Style::default().fg(muted()),
            )),
        ],
        None => vec![Line::from(Span::styled(
            "Select a pack to see where it is and what it produced.",
            Style::default().fg(muted()),
        ))],
    };
    crate::ui::draw_card(frame, rects[1], "SELECTED PACK", detail, accent());
}

impl App {
    /// Re-read the registry; called with the rest of the local data.
    pub(crate) fn refresh_packs(&mut self) {
        self.packs.refresh(read_packs(&packs_dir(&self.paths.storage)));
    }

    /// Where a relative folder in the launch prompt is resolved from: the shell `nodo
    /// tui` was typed in (the wrapper's `ORIGINAL_DIR`), else this process's cwd.
    pub(crate) fn pack_base_dir(&self) -> PathBuf {
        std::env::var_os("ORIGINAL_DIR")
            .map(PathBuf::from)
            .or_else(|| std::env::current_dir().ok())
            .unwrap_or_else(|| PathBuf::from("/"))
    }

    pub(crate) fn home_dir() -> Option<PathBuf> {
        std::env::var_os("HOME").map(PathBuf::from)
    }

    /// `n` on PACKS, `p` on SERVICES: ask what to pack.
    pub fn open_new_pack(&mut self) {
        let page = self.page();
        if page != Page::Packs && page != Page::Services {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        self.input_title = "Pack: a project folder or an https git URL (#subdir allowed)".to_string();
        self.input_mode = InputMode::NewPack;
        self.input.clear();
        self.edit_kind = crate::app::EditKind::Text;
    }

    /// Tab in the launch prompt.
    pub fn complete_pack_input(&mut self) {
        let home = Self::home_dir();
        if let Some(completed) = complete_folder(&self.input, &self.pack_base_dir(), home.as_deref()) {
            self.input = completed;
        }
    }

    /// The typed line, checked, then launched in the background. No y/N: a pack spends
    /// nothing and can be cancelled.
    pub(crate) fn submit_new_pack(&mut self) {
        let home = Self::home_dir();
        match launch_command(&self.input, &self.pack_base_dir(), home.as_deref()) {
            Ok((label, args)) => {
                self.close_input();
                if self.page() != Page::Packs {
                    // Where it can be watched.
                    if let Some(index) = Page::ALL.iter().position(|page| *page == Page::Packs) {
                        self.tabs.index = index;
                    }
                }
                self.spawn_command(CommandKind::Pack, label, args);
            }
            Err(message) => self.status = message,
        }
    }

    /// `c` on PACKS: confirm, then `nodo pack_cancel <id>`.
    pub fn open_cancel_pack_confirm(&mut self) {
        if self.page() != Page::Packs {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(pack) = self.packs.selected().cloned() else {
            self.status = "Select a pack first".to_string();
            return;
        };
        if !pack.is_active() {
            self.status = format!("Pack {} is not running ({})", pack.id, pack.status);
            return;
        }
        let label = format!("{} ({})", pack.id, shorten(&pack.source, 30));
        let remote = if pack.packer == "service" && pack.stage.as_deref().map(|s| s.contains("building")).unwrap_or(false) {
            " The packer VM finishes the build it was sent; its result is dropped."
        } else {
            ""
        };
        self.input_mode = InputMode::Confirm;
        self.input_title = format!("Cancel pack {label}?{remote} (y/N)");
        self.pending_action = Some(PendingAction::CancelPack { id: pack.id, label });
    }

    /// `i` on PACKS: the record and the tail of its log, in the overlay.
    pub fn open_pack_details(&mut self) {
        if self.page() != Page::Packs {
            return;
        }
        let Some(pack) = self.packs.selected().cloned() else {
            self.status = "Select a pack first".to_string();
            return;
        };
        let mut lines = vec![
            format!("status      {}{}", pack.status, pack.stage.as_ref().map(|s| format!(" ({s})")).unwrap_or_default()),
            format!("source      {} ({})", pack.source, pack.kind),
            format!("packer      {}", pack.packer),
            format!("pid         {}", pack.pid),
        ];
        if let Some(service_id) = &pack.service_id {
            lines.push(format!("service id  {service_id}"));
        }
        if let Some(error) = &pack.error {
            lines.push(format!("error       {error}"));
        }
        match &pack.log {
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
            title: format!("Pack {}", pack.id),
            lines,
            scroll: 0,
        });
        self.input_mode = InputMode::Details;
        self.status = "Pack details • ↑/↓ scroll • Esc close".to_string();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(status: &str) -> String {
        format!(
            r#"{{"id": "ab12cd34", "pid": 4242, "source": "/home/me/hello", "kind": "dir",
               "packer": "local", "status": "{status}", "stage": "building", "detached": true,
               "log": "/nodo/storage/packs/ab12cd34.log", "started_at": 100,
               "finished_at": null, "service_id": null, "error": null}}"#
        )
    }

    #[test]
    fn parses_the_python_record() {
        let pack = parse_pack(&record("running")).unwrap();
        assert_eq!(pack.id, "ab12cd34");
        assert_eq!(pack.status, "running");
        assert_eq!(pack.stage.as_deref(), Some("building"));
        assert!(pack.detached && pack.is_active());
        assert_eq!(pack.result(), "building");
        assert_eq!(pack.age_secs(Some(160)), Some(60.0));
        assert_eq!(pack.duration_secs(Some(160)), Some(60.0));
        assert!(parse_pack("{}").is_none());
        assert!(parse_pack("not json").is_none());
    }

    #[test]
    fn result_says_what_it_came_to() {
        let mut pack = parse_pack(&record("done")).unwrap();
        pack.service_id = Some("ff".repeat(32));
        assert_eq!(pack.result(), "ff".repeat(32));
        pack.status = "failed".to_string();
        pack.error = Some("COPY failed".to_string());
        assert_eq!(pack.result(), "COPY failed");
        pack.finished_at = Some(130);
        assert_eq!(pack.duration_secs(Some(999)), Some(30.0));
    }

    #[test]
    fn a_running_pack_whose_process_is_gone_reads_as_failed() {
        let pack = settle(parse_pack(&record("running")).unwrap(), |_| false);
        assert_eq!(pack.status, "failed");
        assert_eq!(pack.error.as_deref(), Some(LOST_ERROR));
        let alive = settle(parse_pack(&record("queued")).unwrap(), |_| true);
        assert_eq!(alive.status, "queued");
        let done = settle(parse_pack(&record("done")).unwrap(), |_| false);
        assert_eq!(done.status, "done");
    }

    #[test]
    fn reads_the_directory_newest_first() {
        let dir = std::env::temp_dir().join(format!("nodo-packs-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for (id, started) in [("old00001", 100), ("new00001", 200)] {
            let text = record("done").replace("ab12cd34", id).replace("\"started_at\": 100", &format!("\"started_at\": {started}"));
            std::fs::write(dir.join(format!("{id}.json")), text).unwrap();
        }
        std::fs::write(dir.join("old00001.log"), "x\n").unwrap();
        std::fs::write(dir.join("junk.json"), "{").unwrap();
        let packs = read_packs(&dir);
        assert_eq!(packs.iter().map(|p| p.id.as_str()).collect::<Vec<_>>(), vec!["new00001", "old00001"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn source_label_keeps_the_end() {
        let pack = Pack { source: "/very/long/path/to/my-service".to_string(), ..parse_pack(&record("done")).unwrap() };
        assert_eq!(pack.source_label(100), "/very/long/path/to/my-service");
        assert_eq!(pack.source_label(11), "…my-service");
    }

    fn scratch() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("nodo-pack-src-{}-{:?}", std::process::id(), std::thread::current().id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("projects/hello-world/.service")).unwrap();
        std::fs::create_dir_all(dir.join("projects/hello-there")).unwrap();
        std::fs::create_dir_all(dir.join("projects/.hidden")).unwrap();
        std::fs::write(dir.join("projects/hello-world/.service/Dockerfile"), "FROM scratch\n").unwrap();
        std::fs::write(dir.join("projects/notes.txt"), "x").unwrap();
        dir
    }

    #[test]
    fn classifies_urls() {
        let base = Path::new("/");
        assert_eq!(
            classify_source("https://github.com/celaut-project/nodo.git", base, None),
            Ok(PackSource::Git("https://github.com/celaut-project/nodo.git".to_string()))
        );
        assert!(matches!(
            classify_source("https://github.com/a/b#services/hello", base, None),
            Ok(PackSource::Git(_))
        ));
        for refused in [
            "http://github.com/a/b.git",
            "ssh://git@github.com/a/b.git",
            "git@github.com:a/b.git",
            "git://github.com/a/b.git",
            "https://github.com/",
            "https://github.com/a/b.git#../x",
            "",
        ] {
            assert!(classify_source(refused, base, None).is_err(), "{refused}");
        }
    }

    #[test]
    fn classifies_folders_relative_to_where_the_tui_started() {
        let dir = scratch();
        let home = dir.join("projects");
        assert_eq!(
            classify_source("projects/hello-world", &dir, None),
            Ok(PackSource::Dir(dir.join("projects/hello-world")))
        );
        assert_eq!(
            classify_source("~/hello-there", Path::new("/"), Some(&home)),
            Ok(PackSource::Dir(home.join("hello-there")))
        );
        assert!(classify_source("projects/nope", &dir, None).unwrap_err().contains("does not exist"));
        assert!(classify_source("projects/notes.txt", &dir, None).unwrap_err().contains("is a file"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn launch_builds_a_detached_json_pack_with_an_absolute_path() {
        let dir = scratch();
        let (label, args) = launch_command(" projects/hello-world ", &dir, None).unwrap();
        assert_eq!(
            args,
            vec![
                "pack".to_string(),
                dir.join("projects/hello-world").to_string_lossy().to_string(),
                "--detach".to_string(),
                "--json".to_string()
            ]
        );
        assert!(label.starts_with("Pack "));
        let (_, args) = launch_command("https://github.com/a/b.git", &dir, None).unwrap();
        assert_eq!(args[1], "https://github.com/a/b.git");
        assert!(launch_command("http://github.com/a/b.git", &dir, None).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn feedback_says_what_the_folder_holds() {
        let dir = scratch();
        let ok = input_feedback("projects/hello-world", &dir, None).unwrap();
        assert!(ok.contains("Dockerfile; no service.json"), "{ok}");
        let bare = input_feedback("projects/hello-there", &dir, None).unwrap();
        assert!(bare.contains("no Dockerfile"), "{bare}");
        assert!(input_feedback("https://github.com/a/b.git", &dir, None).unwrap().contains("git"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tab_completes_folders() {
        let dir = scratch();
        assert_eq!(complete_folder("proj", &dir, None).as_deref(), Some("projects/"));
        // Two matches: as far as they agree.
        assert_eq!(complete_folder("projects/h", &dir, None).as_deref(), Some("projects/hello-"));
        assert_eq!(complete_folder("projects/hello-w", &dir, None).as_deref(), Some("projects/hello-world/"));
        // Nothing more to add, files are not offered, hidden only when asked for.
        assert_eq!(complete_folder("projects/hello-", &dir, None), None);
        assert_eq!(complete_folder("projects/no", &dir, None), None);
        assert_eq!(complete_folder("projects/.h", &dir, None).as_deref(), Some("projects/.hidden/"));
        assert_eq!(folder_matches("projects/", &dir, None), vec!["hello-there", "hello-world"]);
        assert_eq!(complete_folder("https://github.com/a", &dir, None), None);
        assert_eq!(complete_folder("~", &dir, None).as_deref(), Some("~/"));
        let home = dir.join("projects");
        assert_eq!(complete_folder("~/hello-t", Path::new("/"), Some(&home)).as_deref(), Some("~/hello-there/"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn outcome_reads_the_json_on_stdout() {
        let started = format!("Error adding column\n{{\"pack\": {}}}\n", record("running").replace('\n', " "));
        assert_eq!(
            outcome_status("Pack x", true, &started, ""),
            "Pack ab12cd34 started: /home/me/hello (i shows its log)"
        );
        assert_eq!(
            outcome_status("Pack x", false, "{\"error\": \"Error: /x does not exist.\"}\n", ""),
            "Pack x failed: Error: /x does not exist."
        );
        assert_eq!(
            outcome_status("Cancel pack a", true, "{\"cancelled\": [\"ab12cd34\"], \"failed\": []}\n", ""),
            "Cancelled pack ab12cd34"
        );
        assert_eq!(
            outcome_status("Cancel pack a", false, "{\"cancelled\": [], \"failed\": [\"a\"], \"error\": \"Pack a is not running (done).\"}\n", ""),
            "Cancel pack a failed: Pack a is not running (done)."
        );
        assert_eq!(outcome_status("Pack x", false, "", "boom\n"), "Pack x failed: boom");
    }

    #[test]
    fn last_lines_keep_the_last_state_of_a_redrawn_line() {
        let path = std::env::temp_dir().join(format!("nodo-pack-log-{}", std::process::id()));
        std::fs::write(&path, "one\nProcessing |\rProcessing /\n\nService ID ->  abc\n").unwrap();
        let path_text = path.to_string_lossy().to_string();
        assert_eq!(last_lines(&path_text, 10), vec!["one", "Processing /", "", "Service ID ->  abc"]);
        let pack = Pack { log: Some(path_text), ..parse_pack(&record("running")).unwrap() };
        assert_eq!(last_said(&pack).as_deref(), Some("Service ID ->  abc"));
        std::fs::remove_file(&path).unwrap();
    }

    /// The keys, driven through the real handler.
    mod keys {
        use super::*;
        use crate::app::PendingAction;
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

        fn on(page: Page) -> App {
            let mut app = App::default();
            app.tabs.index = Page::ALL.iter().position(|candidate| *candidate == page).unwrap();
            app
        }

        fn press(app: &mut App, code: KeyCode) {
            let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
            rt.block_on(crate::handler::handle_key_events(KeyEvent::new(code, KeyModifiers::NONE), app))
                .unwrap();
        }

        fn with_packs(app: &mut App, statuses: &[&str]) {
            let packs = statuses
                .iter()
                .enumerate()
                .map(|(index, status)| Pack {
                    id: format!("pk{index:06}"),
                    ..parse_pack(&record(status)).unwrap()
                })
                .collect();
            app.packs.refresh(packs);
            app.packs.state.select(Some(0));
        }

        #[test]
        fn packs_is_a_workload_page_beside_services() {
            let services = Page::ALL.iter().position(|page| *page == Page::Services).unwrap();
            assert_eq!(Page::ALL[services + 1], Page::Packs);
            assert_eq!(Page::Packs.group(), crate::app::PageGroup::Activity);
            assert_eq!(Page::Packs.title(), "PACKS");
        }

        #[test]
        fn n_on_packs_and_p_on_services_open_the_prompt() {
            let mut app = on(Page::Packs);
            press(&mut app, KeyCode::Char('n'));
            assert_eq!(app.input_mode, InputMode::NewPack);
            assert!(app.input.is_empty());

            let mut app = on(Page::Services);
            press(&mut app, KeyCode::Char('p'));
            assert_eq!(app.input_mode, InputMode::NewPack);

            let mut app = on(Page::Tunnels);
            app.open_new_pack();
            assert_eq!(app.input_mode, InputMode::Normal);
        }

        #[test]
        fn typing_tab_and_a_refused_source_in_the_prompt() {
            let dir = scratch();
            let mut app = on(Page::Packs);
            press(&mut app, KeyCode::Char('n'));
            for character in format!("{}/proj", dir.display()).chars() {
                press(&mut app, KeyCode::Char(character));
            }
            press(&mut app, KeyCode::Tab);
            assert_eq!(app.input, format!("{}/projects/", dir.display()));
            assert_eq!(app.input_mode, InputMode::NewPack, "Tab completes; it does not change page");

            app.input = "http://github.com/a/b.git".to_string();
            press(&mut app, KeyCode::Enter);
            assert_eq!(app.input_mode, InputMode::NewPack, "the prompt stays open to fix the line");
            assert!(app.status.contains("https"), "{}", app.status);
            assert!(app.command_task.is_none(), "nothing runs on a refused source");
            press(&mut app, KeyCode::Esc);
            assert_eq!(app.input_mode, InputMode::Normal);
            std::fs::remove_dir_all(&dir).unwrap();
        }

        #[test]
        fn c_asks_before_cancelling_a_running_pack() {
            let mut app = on(Page::Packs);
            with_packs(&mut app, &["running", "done"]);
            press(&mut app, KeyCode::Char('c'));
            assert_eq!(app.input_mode, InputMode::Confirm);
            assert!(app.input_title.starts_with("Cancel pack pk000000"), "{}", app.input_title);
            match app.pending_action.clone() {
                Some(PendingAction::CancelPack { id, .. }) => assert_eq!(id, "pk000000"),
                other => panic!("unexpected {other:?}"),
            }
            assert_eq!(
                crate::app::pending_command(app.pending_action.take().unwrap()).unwrap().1,
                vec!["pack_cancel".to_string(), "pk000000".to_string(), "--json".to_string()]
            );
            // n answers no.
            press(&mut app, KeyCode::Char('n'));
            assert_eq!(app.input_mode, InputMode::Normal);
            assert!(app.command_task.is_none());
        }

        #[test]
        fn c_on_a_finished_pack_says_so() {
            let mut app = on(Page::Packs);
            with_packs(&mut app, &["done"]);
            press(&mut app, KeyCode::Char('c'));
            assert_eq!(app.input_mode, InputMode::Normal);
            assert!(app.status.contains("not running (done)"), "{}", app.status);
        }

        #[test]
        fn i_shows_the_record_and_its_log() {
            let mut app = on(Page::Packs);
            with_packs(&mut app, &["failed"]);
            app.packs.items[0].error = Some("COPY failed".to_string());
            app.packs.items[0].log = None;
            press(&mut app, KeyCode::Char('i'));
            assert_eq!(app.input_mode, InputMode::Details);
            let details = app.details.clone().unwrap();
            assert_eq!(details.title, "Pack pk000000");
            assert!(details.lines.iter().any(|line| line == "error       COPY failed"));
        }

        #[test]
        fn the_context_menu_offers_the_packs_keys() {
            let keys: Vec<KeyCode> = crate::context_menu::page_actions(Page::Packs)
                .iter()
                .map(|item| item.key)
                .collect();
            assert_eq!(keys, vec![KeyCode::Char('i'), KeyCode::Char('c'), KeyCode::Char('n')]);
        }
    }
}

