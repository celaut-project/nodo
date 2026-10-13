//! The DEAD page: instances that were killed or died.
//!
//! The node keeps a dead instance. Its row stays in `local_instances` or
//! `delegated_instances` with `killed_at` set (NULL means alive) and a
//! `death_reason`. A dead row never becomes alive again. INSTANCES lists only the
//! live rows; this page lists only the dead ones, local and delegated together,
//! newest death first.
//!
//! For a local instance the node also saves its logs in
//! `<main.STORAGE>/instances_dead/<id>/`: `stdout.log`, `stderr.log`, `serial.log`,
//! `runtime_state.json` and `death.json`. This page reads those files directly. It is
//! read-only: nothing here kills, stops or changes an instance.
//!
//! An old database has no `killed_at` column. Then the list is empty.

use crate::app::{column_exists, read_last_lines, shorten, table_exists, App, Identifiable, Money};
use crate::layout_util::Column;
use crate::ui::{
    accent, bad, fitted_column_x, fitted_table, format_duration_compact, metric_line, muted,
    section_block, selected_style, text_colour, warn, TextCell,
};
use ratatui::prelude::*;
use ratatui::widgets::Paragraph;
use rusqlite::{Connection, Result as SqlResult};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// The folder the node saves a dead local instance's files in, under `main.STORAGE`.
const DEAD_DIR: &str = "instances_dead";

/// Log lines kept in memory for the selected row.
const LOG_LINES: usize = 500;

/// The saved logs, in the order `s` cycles them.
pub(crate) const STREAMS: [&str; 3] = ["stdout", "stderr", "serial"];

#[derive(Debug, Clone, PartialEq)]
pub struct DeadInstance {
    pub id: String,
    /// Empty for a delegated instance, and for a local one that had no name.
    pub name: String,
    /// True for an instance that ran on this node.
    pub local: bool,
    /// The peer that ran it, for a delegated instance.
    pub peer_id: String,
    pub father_id: String,
    /// The service tag when this node knows it, the shortened id when not.
    pub service: String,
    pub service_id: String,
    /// The balance when it died, as stored (MU).
    pub balance: String,
    pub launched_at: Option<String>,
    pub killed_at: String,
    /// Seconds from `launched_at` to `killed_at`, when both are known.
    pub lived_secs: Option<f64>,
    pub reason: String,
}

impl Identifiable for DeadInstance {
    fn id(&self) -> &str {
        &self.id
    }
}

/// The saved files of the selected row, read when the selection changes and on each
/// refresh. A frame never reads the disk.
#[derive(Debug, Clone, Default)]
pub struct DeadView {
    /// The id the lines below belong to.
    pub for_id: Option<String>,
    /// Which of [`STREAMS`] is shown.
    pub stream: usize,
    /// Lines of the shown stream, oldest first.
    pub lines: Vec<String>,
    /// How many lines up from the end the view is. Zero follows the end.
    pub scroll: usize,
    /// What `death.json` says, one `key: value` per line. Empty if it is not there.
    pub death: Vec<String>,
    /// False when the log files are not on this host.
    pub has_files: bool,
}

pub fn dead_dir(storage: &Path) -> PathBuf {
    storage.join(DEAD_DIR)
}

/// The dead rows of both tables, newest death first.
///
/// A table, or a `killed_at` column, that is not there gives no rows rather than an
/// error: the TUI opens whatever database is on disk.
pub fn get_dead(
    database: &Path,
    service_names: &HashMap<String, String>,
) -> SqlResult<Vec<DeadInstance>> {
    let connection = Connection::open(database)?;
    let mut dead = Vec::new();
    let name_of = |service_id: &str| {
        service_names
            .get(service_id)
            .cloned()
            .unwrap_or_else(|| shorten(service_id, 18))
    };
    if has_dead_columns(&connection, "local_instances") {
        let mut statement = connection.prepare(
            "SELECT id, COALESCE(name, ''), COALESCE(father_id, ''), COALESCE(service_id, ''),
                    CAST(COALESCE(balance_mu, '') AS TEXT), launched_at, killed_at,
                    COALESCE(death_reason, ''),
                    CAST(strftime('%s', killed_at) AS INTEGER)
                        - CAST(strftime('%s', launched_at) AS INTEGER)
             FROM local_instances WHERE killed_at IS NOT NULL",
        )?;
        let rows = statement.query_map([], |row| {
            let service_id: String = row.get(3)?;
            Ok(DeadInstance {
                id: row.get(0)?,
                name: row.get(1)?,
                local: true,
                peer_id: String::new(),
                father_id: row.get(2)?,
                service: name_of(&service_id),
                service_id,
                balance: row.get(4)?,
                launched_at: row.get(5)?,
                killed_at: row.get::<_, Option<String>>(6)?.unwrap_or_default(),
                lived_secs: lived(row.get(8)?),
                reason: row.get(7)?,
            })
        })?;
        for row in rows {
            dead.push(row?);
        }
    }
    if table_exists(&connection, "delegated_instances")
        && has_dead_columns(&connection, "delegated_instances")
    {
        let mut statement = connection.prepare(
            "SELECT COALESCE(id, token_delegation, ''), COALESCE(peer_id, ''),
                    COALESCE(father_id, ''), COALESCE(service_id, ''),
                    CAST(COALESCE(balance_mu, '') AS TEXT), launched_at, killed_at,
                    COALESCE(death_reason, ''),
                    CAST(strftime('%s', killed_at) AS INTEGER)
                        - CAST(strftime('%s', launched_at) AS INTEGER)
             FROM delegated_instances WHERE killed_at IS NOT NULL",
        )?;
        let rows = statement.query_map([], |row| {
            let service_id: String = row.get(3)?;
            Ok(DeadInstance {
                id: row.get(0)?,
                name: String::new(),
                local: false,
                peer_id: row.get(1)?,
                father_id: row.get(2)?,
                service: name_of(&service_id),
                service_id,
                balance: row.get(4)?,
                launched_at: row.get(5)?,
                killed_at: row.get::<_, Option<String>>(6)?.unwrap_or_default(),
                lived_secs: lived(row.get(8)?),
                reason: row.get(7)?,
            })
        })?;
        for row in rows {
            dead.push(row?);
        }
    }
    // The timestamps are `YYYY-MM-DD HH:MM:SS`, so text order is time order. The sort
    // is stable: two deaths in the same second keep the order of the query.
    dead.sort_by(|a, b| b.killed_at.cmp(&a.killed_at));
    Ok(dead)
}

/// A negative span is a clock that went back: not an uptime.
fn lived(secs: Option<i64>) -> Option<f64> {
    secs.filter(|secs| *secs >= 0).map(|secs| secs as f64)
}

/// Whether `table` has the columns this page reads.
fn has_dead_columns(connection: &Connection, table: &str) -> bool {
    table_exists(connection, table)
        && column_exists(connection, table, "killed_at")
        && column_exists(connection, table, "death_reason")
}

/// `YYYY-MM-DD HH:MM` of a stored timestamp, for a narrow column.
fn short_time(stamp: &str) -> String {
    stamp.replace('T', " ").chars().take(16).collect()
}

fn read_stream(dir: &Path, stream: &str) -> Vec<String> {
    read_last_lines(&dir.join(format!("{stream}.log")), LOG_LINES).unwrap_or_default()
}

/// `death.json` as `key: value` lines. Text that is not a JSON object is kept as it is.
fn read_death(dir: &Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(dir.join("death.json")) else {
        return Vec::new();
    };
    match serde_json::from_str::<serde_json::Value>(&text) {
        Ok(serde_json::Value::Object(map)) => map
            .iter()
            .map(|(key, value)| match value {
                serde_json::Value::String(text) => format!("{key}: {text}"),
                other => format!("{key}: {other}"),
            })
            .collect(),
        _ => text.lines().map(str::to_string).collect(),
    }
}

impl App {
    /// Re-read the dead rows with the rest of the data, then the selected row's files.
    pub(crate) fn refresh_dead(&mut self, service_names: &HashMap<String, String>) {
        let rows = get_dead(&self.paths.database, service_names).unwrap_or_default();
        self.dead.refresh(rows);
        self.load_dead_view(false);
    }

    /// Read the saved files of the selected row. `reset` is true when the selection
    /// moved: the log goes back to its end. A refresh keeps the place the operator
    /// scrolled to.
    pub(crate) fn load_dead_view(&mut self, reset: bool) {
        let selected = self.dead.selected().cloned();
        let same_row = selected.as_ref().map(|row| &row.id) == self.dead_view.for_id.as_ref();
        if reset || !same_row {
            self.dead_view.scroll = 0;
        }
        let Some(row) = selected.filter(|row| row.local) else {
            self.dead_view.for_id = self.dead.selected().map(|row| row.id.clone());
            self.dead_view.lines.clear();
            self.dead_view.death.clear();
            self.dead_view.has_files = false;
            return;
        };
        let dir = dead_dir(&self.paths.storage).join(&row.id);
        self.dead_view.has_files = dir.is_dir();
        self.dead_view.lines = read_stream(&dir, STREAMS[self.dead_view.stream % STREAMS.len()]);
        self.dead_view.death = read_death(&dir);
        self.dead_view.for_id = Some(row.id);
    }

    /// `s`: show the next saved log.
    pub fn cycle_dead_stream(&mut self) {
        self.dead_view.stream = (self.dead_view.stream + 1) % STREAMS.len();
        self.dead_view.scroll = 0;
        self.load_dead_view(false);
    }

    /// Scroll the log `lines` up from the end. A negative number goes down. A very
    /// large one stops at the top, and a very negative one at the end.
    pub fn scroll_dead_log(&mut self, lines: i64) {
        let top = self.dead_view.lines.len() as i64;
        let next = (self.dead_view.scroll as i64).saturating_add(lines).clamp(0, top);
        self.dead_view.scroll = next as usize;
    }
}

/// The DEAD table's columns: who it was and why it died outlast the times.
pub(crate) const DEAD_COLUMNS: [Column; 6] = [
    Column::new("Name", Constraint::Min(16), 10, 0),
    Column::new("Reason", Constraint::Length(16), 8, 1),
    Column::new("Killed", Constraint::Length(16), 11, 2),
    Column::new("Service", Constraint::Length(20), 8, 3),
    Column::new("Loc", Constraint::Length(9), 5, 4),
    Column::new("Launched", Constraint::Length(16), 11, 5),
];

/// The page: the table, and under it the selected row and its saved log.
pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    const MIN_TABLE_HEIGHT: u16 = 6;
    const MAX_DETAIL_HEIGHT: u16 = 18;
    let below = area
        .height
        .saturating_sub(MIN_TABLE_HEIGHT)
        .min(MAX_DETAIL_HEIGHT);
    let split = Layout::vertical([Constraint::Min(MIN_TABLE_HEIGHT), Constraint::Length(below)])
        .split(area);

    let rows = app
        .dead
        .items
        .iter()
        .map(|row| {
            let cells: Vec<TextCell> = vec![
                display_name(row).into(),
                TextCell::from(row.reason.clone()).style(Style::default().fg(reason_color(&row.reason))),
                short_time(&row.killed_at).into(),
                row.service.clone().into(),
                if row.local { "local" } else { "delegated" }.into(),
                row.launched_at
                    .as_deref()
                    .map(short_time)
                    .unwrap_or_else(|| "—".to_string())
                    .into(),
            ];
            (cells, Style::default())
        })
        .collect();
    let has_selection = app.dead.state.selected().is_some();
    let (table, columns) = fitted_table(&DEAD_COLUMNS, rows, split[0], has_selection);
    let table = table
        .block(section_block(
            format!(" DEAD • {} instance(s) ", app.dead.items.len()),
            accent(),
        ))
        .highlight_style(selected_style())
        .highlight_symbol("▸ ");
    app.list_area = split[0];
    app.id_column_x = fitted_column_x(split[0], &columns, 0, has_selection);
    frame.render_stateful_widget(table, split[0], &mut app.dead.state);

    if below < 3 {
        return;
    }
    let selected = app.dead.selected();
    let detail = detail_lines(&app.money, selected, &app.dead_view.death);
    let wide = split[1].width >= 100;
    let (detail_area, log_area) = if wide {
        let parts =
            Layout::horizontal([Constraint::Percentage(45), Constraint::Percentage(55)]).split(split[1]);
        (parts[0], parts[1])
    } else {
        // One above the other. The detail takes what it needs, up to half.
        let height = (detail.len() as u16 + 2).min(split[1].height / 2).max(3);
        let parts = Layout::vertical([Constraint::Length(height), Constraint::Min(0)]).split(split[1]);
        (parts[0], parts[1])
    };
    crate::ui::draw_card(frame, detail_area, "SELECTED INSTANCE", detail, accent());
    if log_area.height >= 3 {
        draw_log(frame, app, log_area);
    }
}

/// The name, or the start of the id for an instance that had none.
fn display_name(row: &DeadInstance) -> String {
    if row.name.is_empty() {
        shorten(&row.id, 12)
    } else {
        row.name.clone()
    }
}

/// A death somebody asked for or that the rules cause is quiet. A failure is not.
fn reason_color(reason: &str) -> Color {
    match reason {
        "manual_kill" | "burnall" | "peer_stopped" | "activity_window" => muted(),
        "out_of_balance" => warn(),
        "" => muted(),
        _ => bad(),
    }
}

fn detail_lines(money: &Money, row: Option<&DeadInstance>, death: &[String]) -> Vec<Line<'static>> {
    let Some(row) = row else {
        return vec![Line::from(Span::styled(
            "Select a dead instance to see how it ended.",
            Style::default().fg(muted()),
        ))];
    };
    let dash = |text: &str| if text.is_empty() { "—".to_string() } else { text.to_string() };
    let mut lines = vec![
        metric_line("ID", row.id.clone()),
        metric_line("Name", dash(&row.name)),
        metric_line("Where", if row.local { "local".to_string() } else { format!("delegated to {}", dash(&row.peer_id)) }),
        metric_line("Parent", dash(&row.father_id)),
        metric_line("Service", format!("{} ({})", row.service, shorten(&row.service_id, 18))),
        metric_line("Balance at death", money.format_raw(&row.balance)),
        metric_line(
            "Launched",
            row.launched_at.clone().unwrap_or_else(|| "—".to_string()),
        ),
        metric_line("Killed", dash(&row.killed_at)),
        metric_line("Lived", format_duration_compact(row.lived_secs)),
        Line::from(vec![
            Span::styled(format!("{:<18}", "Reason"), Style::default().fg(muted())),
            Span::styled(dash(&row.reason), Style::default().fg(reason_color(&row.reason)).bold()),
        ]),
    ];
    // What the node wrote when it archived the instance, when that file is there.
    if !death.is_empty() {
        lines.push(Line::from(Span::styled("death.json", Style::default().fg(accent()).bold())));
        lines.extend(death.iter().map(|line| Line::from(Span::styled(format!("  {line}"), Style::default().fg(text_colour())))));
    }
    lines
}

fn draw_log(frame: &mut Frame, app: &App, area: Rect) {
    let view = &app.dead_view;
    let Some(row) = app.dead.selected() else {
        let block = section_block(" SAVED LOG ", muted());
        frame.render_widget(Paragraph::new("").block(block), area);
        return;
    };
    if !row.local {
        let block = section_block(" SAVED LOG ", muted());
        let text = Line::from(Span::styled(
            "No local logs. They are on the peer.",
            Style::default().fg(muted()),
        ));
        frame.render_widget(Paragraph::new(text).block(block), area);
        return;
    }
    let stream = STREAMS[view.stream % STREAMS.len()];
    let title = format!(" SAVED LOG • {stream} (s next) ");
    let block = section_block(title, text_colour());
    let height = area.height.saturating_sub(2) as usize;
    let mut lines: Vec<Line> = Vec::new();
    if view.lines.is_empty() {
        let note = if view.has_files {
            format!("{stream}.log is empty or not saved.")
        } else {
            "The saved files are not on this host.".to_string()
        };
        lines.push(Line::from(Span::styled(note, Style::default().fg(muted()))));
    } else {
        let end = view.lines.len().saturating_sub(view.scroll);
        let start = end.saturating_sub(height);
        lines.extend(view.lines[start..end].iter().map(|line| Line::from(line.clone())));
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Page;
    use std::fs;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("nodo-tui-dead-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn database(dir: &Path) -> PathBuf {
        let path = dir.join("database.sqlite");
        let connection = Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT, father_id TEXT,
                        service_id TEXT, balance_mu TEXT, launched_at DATETIME,
                        killed_at DATETIME, death_reason TEXT);
                 CREATE TABLE delegated_instances (token_delegation TEXT, id TEXT, peer_id TEXT,
                        father_id TEXT, service_id TEXT, balance_mu TEXT, launched_at DATETIME,
                        killed_at DATETIME, death_reason TEXT);
                 INSERT INTO local_instances VALUES ('live', 'alive', '', 's1', '5',
                        '2026-01-01 10:00:00', NULL, NULL);
                 INSERT INTO local_instances VALUES ('old', 'older', 'c1', 's1', '0',
                        '2026-01-01 10:00:00', '2026-01-01 11:00:00', 'manual_kill');
                 INSERT INTO local_instances VALUES ('new', NULL, 'c1', 's2', '7',
                        '2026-01-02 10:00:00', '2026-01-02 10:30:00', 'guest_panic');
                 INSERT INTO delegated_instances VALUES ('t', 'remote-live', 'p1', 'c1', 's1', '1',
                        '2026-01-01 10:00:00', NULL, NULL);
                 INSERT INTO delegated_instances VALUES ('t', 'remote-dead', 'p1', 'c1', 's1', '1',
                        '2026-01-01 10:00:00', '2026-01-01 12:00:00', 'peer_stopped');",
            )
            .unwrap();
        path
    }

    #[test]
    fn only_dead_rows_come_back_newest_first_from_both_tables() {
        let dir = temp_dir("query");
        let rows = get_dead(&database(&dir), &HashMap::new()).unwrap();

        let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        assert_eq!(ids, ["new", "remote-dead", "old"]);
        assert!(rows[0].local && !rows[1].local);
        assert_eq!(rows[0].reason, "guest_panic");
        assert_eq!(rows[0].lived_secs, Some(1800.0));
        assert_eq!(rows[1].peer_id, "p1");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn tables_without_the_columns_give_no_rows() {
        let dir = temp_dir("old-schema");
        let path = dir.join("database.sqlite");
        Connection::open(&path)
            .unwrap()
            .execute_batch(
                "CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT);
                 CREATE TABLE delegated_instances (id TEXT, peer_id TEXT);
                 INSERT INTO local_instances VALUES ('a', 'x');
                 INSERT INTO delegated_instances VALUES ('b', 'p');",
            )
            .unwrap();
        assert!(get_dead(&path, &HashMap::new()).unwrap().is_empty());

        // No tables at all.
        let empty = dir.join("empty.sqlite");
        Connection::open(&empty).unwrap();
        assert!(get_dead(&empty, &HashMap::new()).unwrap().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_name_falls_back_to_the_id() {
        let dir = temp_dir("name");
        let rows = get_dead(&database(&dir), &HashMap::new()).unwrap();
        let unnamed = rows.iter().find(|row| row.id == "new").unwrap();
        assert_eq!(display_name(unnamed), "new");
        let named = rows.iter().find(|row| row.id == "old").unwrap();
        assert_eq!(display_name(named), "older");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_saved_files_of_a_local_row_are_read_and_scrolled() {
        let dir = temp_dir("files");
        let mut app = App::default();
        app.paths.database = database(&dir);
        app.paths.storage = dir.clone();
        let saved = dead_dir(&dir).join("new");
        fs::create_dir_all(&saved).unwrap();
        fs::write(saved.join("stdout.log"), "one\ntwo\nthree\n").unwrap();
        fs::write(saved.join("serial.log"), "boot\n").unwrap();
        fs::write(saved.join("death.json"), r#"{"reason": "guest_panic", "leftover_mu": 7}"#).unwrap();

        app.refresh_dead(&HashMap::new());
        app.dead.next();
        app.load_dead_view(true);

        assert_eq!(app.dead.selected().unwrap().id, "new");
        assert_eq!(app.dead_view.lines, ["one", "two", "three"]);
        assert!(app.dead_view.death.contains(&"reason: guest_panic".to_string()));
        app.scroll_dead_log(2);
        assert_eq!(app.dead_view.scroll, 2);
        app.scroll_dead_log(i64::MAX);
        assert_eq!(app.dead_view.scroll, 3, "stops at the top");
        app.scroll_dead_log(i64::MIN);
        assert_eq!(app.dead_view.scroll, 0);
        app.cycle_dead_stream();
        assert!(app.dead_view.lines.is_empty(), "no stderr.log was saved");
        app.cycle_dead_stream();
        assert_eq!(app.dead_view.lines, ["boot"]);

        // A delegated row has no local files.
        app.dead.next();
        app.load_dead_view(true);
        assert!(!app.dead.selected().unwrap().local);
        assert!(app.dead_view.lines.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_page_draws_at_small_and_normal_sizes() {
        for (width, height) in [(40u16, 14u16), (80, 24), (120, 40)] {
            let mut app = crate::responsive_sweep::on_page(Page::Dead);
            app.dead = crate::app::StatefulList::with_items(vec![
                DeadInstance {
                    id: "abcdef0123456789".into(),
                    name: String::new(),
                    local: true,
                    peer_id: String::new(),
                    father_id: "client".into(),
                    service: "svc".into(),
                    service_id: "sid".into(),
                    balance: "42".into(),
                    launched_at: Some("2026-01-02 10:00:00".into()),
                    killed_at: "2026-01-02 10:30:00".into(),
                    lived_secs: Some(1800.0),
                    reason: "guest_panic".into(),
                },
                DeadInstance {
                    id: "remote".into(),
                    name: String::new(),
                    local: false,
                    peer_id: "peer".into(),
                    father_id: String::new(),
                    service: "svc".into(),
                    service_id: "sid".into(),
                    balance: "1".into(),
                    launched_at: None,
                    killed_at: "2026-01-01 10:30:00".into(),
                    lived_secs: None,
                    reason: "peer_stopped".into(),
                },
            ]);
            app.dead.next();
            let buffer = crate::responsive_sweep::draw(&mut app, width, height);
            let text = crate::responsive_sweep::text(&buffer);
            assert!(text.contains("DEAD"), "{width}x{height}");
            assert!(text.contains("abcd"), "{width}x{height}\n{text}");
        }
    }
}
