//! The RECEIPTS page: the execution receipts of the instances this node delegated to
//! peers, and the faults found when a peer's statement contradicts them.
//!
//! Two blocks, two commands, both run as child processes off the UI thread like
//! `nodo reputation --json`: the hops table is `nodo receipt hops --json` and the
//! faults table is `nodo statement faults --json`. A receipt is a separate signed
//! record of a service run or tunnel stream; the node checks it against the signed
//! statement of the same peer, and a contradiction is a fault.

use crate::app::{App, Page};
use crate::layout_util::Column;
use crate::ledger::{
    field_bool, field_int, field_text, format_ts, parse_object, run_json, short_hash, step,
};
use crate::ui::{
    accent, bad, fitted_table, label_colour, muted, section_block, selected_style, text_colour,
    warn, TextCell,
};
use ratatui::prelude::*;
use ratatui::widgets::{Paragraph, TableState, Wrap};
use serde_json::Value;
use std::time::{Duration, Instant};
use tokio::task::JoinHandle;

/// How often both tables are re-read while the page is on screen.
const REFRESH: Duration = Duration::from_secs(30);

/// The reasons of a fault that comes from a peer's statement, which the page
/// highlights.
pub const STATEMENT_FAULTS: [&str; 4] = [
    "statement_contradicts_receipt",
    "killed_with_mu",
    "statement_inconsistent",
    "maintenance_while_unreachable",
];

/// Whether a fault `reason` is one of the faults of statements.
pub fn is_statement_fault(reason: &str) -> bool {
    STATEMENT_FAULTS.contains(&reason)
}

/// The latest receipt of a hop, as `nodo receipt hops --json` reports it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Receipt {
    /// `open`, `close`, ...
    pub kind: String,
    /// `tunnel` or the service plane.
    pub plane: String,
    pub session: String,
    pub seq: Option<i64>,
    pub ts: Option<i64>,
    /// MU as a decimal string.
    pub mu_charged: String,
    pub in_bytes: Option<i64>,
    pub out_bytes: Option<i64>,
    pub digest: String,
    pub valid: bool,
    pub problems: Vec<String>,
}

/// One instance delegated to a peer, and the last receipt the peer signed for it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Hop {
    pub peer_id: String,
    pub instance_ref: String,
    pub linked_ts: Option<i64>,
    pub first_linked_ts: Option<i64>,
    pub head_seq: Option<i64>,
    pub head_ts: Option<i64>,
    pub last_ok_ts: Option<i64>,
    pub failed_at: Option<i64>,
    pub last_failed_at: Option<i64>,
    pub receipt: Option<Receipt>,
}

impl Hop {
    /// The first problem the receipt check found, if the receipt is not valid.
    pub fn first_problem(&self) -> Option<&str> {
        self.receipt.as_ref().filter(|r| !r.valid).map(|r| {
            r.problems.first().map(String::as_str).unwrap_or("no reason given")
        })
    }
}

/// One fault recorded against a peer's statement.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Fault {
    pub ts: Option<i64>,
    pub peer_id: String,
    pub account_ref: String,
    pub reason: String,
    pub seq: Option<i64>,
}

fn parse_receipt(value: &Value) -> Receipt {
    Receipt {
        kind: field_text(value, "kind"),
        plane: field_text(value, "plane"),
        session: field_text(value, "session"),
        seq: field_int(value, "seq"),
        ts: field_int(value, "ts"),
        mu_charged: field_text(value, "mu_charged"),
        in_bytes: field_int(value, "in_bytes"),
        out_bytes: field_int(value, "out_bytes"),
        digest: field_text(value, "digest"),
        valid: field_bool(value, "valid"),
        problems: value
            .get("problems")
            .and_then(Value::as_array)
            .map(|list| {
                list.iter()
                    .map(|p| p.as_str().map_or_else(|| p.to_string(), String::from))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

fn parse_hop(value: &Value) -> Hop {
    Hop {
        peer_id: field_text(value, "peer_id"),
        instance_ref: field_text(value, "instance_ref"),
        linked_ts: field_int(value, "linked_ts"),
        first_linked_ts: field_int(value, "first_linked_ts"),
        head_seq: field_int(value, "head_seq"),
        head_ts: field_int(value, "head_ts"),
        last_ok_ts: field_int(value, "last_ok_ts"),
        failed_at: field_int(value, "failed_at"),
        last_failed_at: field_int(value, "last_failed_at"),
        receipt: value.get("receipt").filter(|r| r.is_object()).map(parse_receipt),
    }
}

/// Parse `nodo receipt hops --json`.
pub fn parse_hops(output: &str) -> Result<Vec<Hop>, String> {
    let value = parse_object(output)?;
    let hops = value
        .get("hops")
        .and_then(Value::as_array)
        .ok_or("nodo receipt hops returned no list")?;
    Ok(hops.iter().map(parse_hop).collect())
}

/// Parse `nodo statement faults --json`.
pub fn parse_faults(output: &str) -> Result<Vec<Fault>, String> {
    let value = parse_object(output)?;
    let faults = value
        .get("faults")
        .and_then(Value::as_array)
        .ok_or("nodo statement faults returned no list")?;
    Ok(faults
        .iter()
        .map(|fault| Fault {
            ts: field_int(fault, "ts"),
            peer_id: field_text(fault, "peer_id"),
            account_ref: field_text(fault, "account_ref"),
            reason: field_text(fault, "reason"),
            seq: field_int(fault, "seq"),
        })
        .collect())
}

/// A date to the minute (`YYYY-MM-DD HH:MM`), or `—`.
pub fn format_minute(ts: Option<i64>) -> String {
    format_ts(ts).chars().take(16).collect()
}

/// What the RECEIPTS page holds: both tables, their read errors and the reads in flight.
#[derive(Default)]
pub struct ReceiptsState {
    pub hops: Vec<Hop>,
    pub hops_error: String,
    pub hops_loaded: bool,
    pub faults: Vec<Fault>,
    pub faults_error: String,
    pub faults_loaded: bool,
    /// The cursor of the hops table.
    pub table: TableState,
    last_refresh: Option<Instant>,
    hops_task: Option<JoinHandle<Result<Vec<Hop>, String>>>,
    faults_task: Option<JoinHandle<Result<Vec<Fault>, String>>>,
}

impl std::fmt::Debug for ReceiptsState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReceiptsState")
            .field("hops", &self.hops.len())
            .field("faults", &self.faults.len())
            .finish()
    }
}

impl ReceiptsState {
    /// The hop under the cursor.
    pub fn selected_hop(&self) -> Option<&Hop> {
        self.table.selected().and_then(|index| self.hops.get(index))
    }

    fn set_hops(&mut self, hops: Vec<Hop>) {
        let keep = self.selected_hop().map(|h| (h.peer_id.clone(), h.instance_ref.clone()));
        self.hops = hops;
        let index = keep
            .and_then(|(peer, instance)| {
                self.hops.iter().position(|h| h.peer_id == peer && h.instance_ref == instance)
            })
            .or(if self.hops.is_empty() { None } else { Some(0) });
        self.table.select(index);
    }
}

impl App {
    /// ↑/↓ on RECEIPTS: the cursor of the hops table.
    pub fn receipts_move(&mut self, delta: i32) {
        let len = self.receipts.hops.len();
        step(&mut self.receipts.table, len, delta);
    }

    /// Collect finished reads and start both when due. Called on every tick; only reads
    /// while the RECEIPTS page is on screen. `force` is the `r` key.
    pub(crate) async fn refresh_receipts(&mut self, force: bool) {
        if self.receipts.hops_task.as_ref().is_some_and(|t| t.is_finished()) {
            let task = self.receipts.hops_task.take().unwrap();
            match task.await {
                Ok(Ok(hops)) => {
                    self.receipts.hops_error.clear();
                    self.receipts.set_hops(hops);
                }
                Ok(Err(error)) => self.receipts.hops_error = error,
                Err(error) => self.receipts.hops_error = format!("Receipt read failed: {error}"),
            }
            self.receipts.hops_loaded = true;
        }
        if self.receipts.faults_task.as_ref().is_some_and(|t| t.is_finished()) {
            let task = self.receipts.faults_task.take().unwrap();
            match task.await {
                Ok(Ok(faults)) => {
                    self.receipts.faults_error.clear();
                    self.receipts.faults = faults;
                }
                Ok(Err(error)) => self.receipts.faults_error = error,
                Err(error) => self.receipts.faults_error = format!("Fault read failed: {error}"),
            }
            self.receipts.faults_loaded = true;
        }
        if self.page() != Page::Receipts {
            return;
        }
        let idle = self.receipts.hops_task.is_none() && self.receipts.faults_task.is_none();
        let due = self.receipts.last_refresh.map_or(true, |at| at.elapsed() >= REFRESH);
        if idle && (force || due) {
            self.receipts.last_refresh = Some(Instant::now());
            self.receipts.hops_task = Some(tokio::spawn(async {
                let args = ["receipt", "hops", "--json"].map(String::from).to_vec();
                parse_hops(&run_json(args, "nodo receipt hops").await?)
            }));
            self.receipts.faults_task = Some(tokio::spawn(async {
                let args = ["statement", "faults", "--json"].map(String::from).to_vec();
                parse_faults(&run_json(args, "nodo statement faults").await?)
            }));
        }
    }
}

// --- Drawing ---------------------------------------------------------------

const HOP_COLUMNS: [Column; 8] = [
    Column::new("Peer", Constraint::Length(12), 8, 0),
    Column::new("Instance", Constraint::Length(12), 8, 1),
    Column::new("Valid", Constraint::Length(9), 9, 2),
    Column::new("Receipt", Constraint::Length(20), 12, 3),
    Column::new("MU", Constraint::Min(8), 6, 4),
    Column::new("Tunnel ok", Constraint::Length(16), 16, 5),
    Column::new("Failed", Constraint::Length(16), 16, 6),
    Column::new("Session", Constraint::Length(12), 8, 7),
];

const FAULT_COLUMNS: [Column; 4] = [
    Column::new("Reason", Constraint::Min(20), 12, 0),
    Column::new("When (UTC)", Constraint::Length(19), 19, 1),
    Column::new("Peer", Constraint::Length(12), 8, 2),
    Column::new("Seq", Constraint::Length(6), 3, 3),
];

fn hop_row(hop: &Hop) -> (Vec<TextCell>, Style) {
    let (valid, receipt, mu, session, style) = match &hop.receipt {
        None => ("none", String::new(), String::new(), String::new(), Style::default().fg(muted())),
        Some(r) => {
            let style = if r.valid { Style::default() } else { Style::default().fg(bad()).bold() };
            (
                if r.valid { "valid" } else { "NOT VALID" },
                format!("{} {}", r.kind, r.plane).trim().to_string(),
                r.mu_charged.clone(),
                short_hash(&r.session),
                style,
            )
        }
    };
    let failed = hop.failed_at.or(hop.last_failed_at);
    let failed_style = Style::default().fg(if hop.failed_at.is_some() { warn() } else { muted() });
    let cells = vec![
        short_hash(&hop.peer_id).into(),
        short_hash(&hop.instance_ref).into(),
        valid.into(),
        receipt.into(),
        mu.into(),
        format_minute(hop.last_ok_ts).into(),
        TextCell::from(format_minute(failed)).style(failed_style),
        session.into(),
    ];
    (cells, style)
}

fn fault_row(fault: &Fault) -> (Vec<TextCell>, Style) {
    let style = if is_statement_fault(&fault.reason) {
        Style::default().fg(bad()).bold()
    } else {
        Style::default()
    };
    let cells = vec![
        crate::ui::nonempty(&fault.reason, "(no reason)").to_string().into(),
        format_ts(fault.ts).into(),
        short_hash(&fault.peer_id).into(),
        fault.seq.map(|s| s.to_string()).unwrap_or_default().into(),
    ];
    (cells, style)
}

/// The detail card of the selected hop: its receipt, and the first problem when the
/// receipt is not valid.
pub fn hop_detail(hop: Option<&Hop>) -> Vec<Line<'static>> {
    let Some(hop) = hop else {
        return vec![Line::from(Span::styled(
            "Select a hop to read its receipt.",
            Style::default().fg(muted()),
        ))];
    };
    let Some(receipt) = &hop.receipt else {
        return vec![Line::from(Span::styled(
            "No receipt has been signed for this instance yet.",
            Style::default().fg(muted()),
        ))];
    };
    let bytes = |n: Option<i64>| n.map(|n| n.to_string()).unwrap_or_else(|| "?".into());
    let summary = format!(
        "{} {} session {} seq {} \u{2022} digest {} \u{2022} in {} out {} bytes \u{2022} {}",
        receipt.kind,
        receipt.plane,
        short_hash(&receipt.session),
        receipt.seq.map(|s| s.to_string()).unwrap_or_else(|| "?".into()),
        short_hash(&receipt.digest),
        bytes(receipt.in_bytes),
        bytes(receipt.out_bytes),
        format_minute(receipt.ts),
    );
    let verdict = match hop.first_problem() {
        Some(problem) => Line::from(Span::styled(
            format!("NOT VALID: {problem}"),
            Style::default().fg(bad()).bold(),
        )),
        None => Line::from(Span::styled("receipt is valid", Style::default().fg(label_colour()))),
    };
    vec![Line::from(Span::styled(summary, Style::default().fg(text_colour()))), verdict]
}

fn note(text: String, colour: Color) -> Paragraph<'static> {
    Paragraph::new(Span::styled(text, Style::default().fg(colour))).wrap(Wrap { trim: true })
}

/// What an empty or failed block says instead of rows.
fn empty_text(error: &str, loaded: bool, what: &str, empty: &str) -> (String, Color) {
    if !error.is_empty() {
        (format!("Cannot read {what}: {error}"), bad())
    } else if !loaded {
        (format!("Reading {what}\u{2026}"), muted())
    } else {
        (empty.to_string(), muted())
    }
}

/// The RECEIPTS page: the hops table and the faults table. Shows
/// `nodo receipt hops --json` and `nodo statement faults --json`.
pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let state = &mut app.receipts;
    let detail = hop_detail(state.selected_hop());
    let heights = crate::layout_util::allocate_heights(
        area.height,
        &[
            (5, state.hops.len() as u16 + 4),
            (3, detail.len() as u16 + 2),
            (5, state.faults.len() as u16 + 4),
        ],
        &[0, 2, 1],
    );
    let parts = crate::layout_util::stack(area, &heights);
    if parts[0].height > 0 {
        draw_hops(frame, state, parts[0]);
    }
    if parts[1].height > 0 {
        let block = section_block(" SELECTED HOP ", muted());
        frame.render_widget(Paragraph::new(detail).block(block).wrap(Wrap { trim: true }), parts[1]);
    }
    if parts[2].height > 0 {
        draw_faults(frame, state, parts[2]);
    }
}

fn draw_hops(frame: &mut Frame, state: &mut ReceiptsState, area: Rect) {
    let block = section_block(format!(" HOPS \u{2022} {} delegated ", state.hops.len()), accent());
    if state.hops.is_empty() {
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let (text, colour) = empty_text(
            &state.hops_error,
            state.hops_loaded,
            "the receipts",
            "No instance is delegated to a peer, so there are no receipts to show.",
        );
        frame.render_widget(note(text, colour), inner);
        return;
    }
    let rows = state.hops.iter().map(hop_row).collect();
    let (table, _) = fitted_table(&HOP_COLUMNS, rows, area, state.table.selected().is_some());
    let table = table.block(block).highlight_style(selected_style()).highlight_symbol("\u{25b8} ");
    frame.render_stateful_widget(table, area, &mut state.table);
}

fn draw_faults(frame: &mut Frame, state: &ReceiptsState, area: Rect) {
    let flagged = state.faults.iter().filter(|f| is_statement_fault(&f.reason)).count();
    let colour = if flagged > 0 { bad() } else { accent() };
    let block = section_block(format!(" FAULTS \u{2022} {} recorded ", state.faults.len()), colour);
    if state.faults.is_empty() {
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let (text, colour) = empty_text(
            &state.faults_error,
            state.faults_loaded,
            "the faults",
            "No faults recorded: no peer's statement contradicts its receipts.",
        );
        frame.render_widget(note(text, colour), inner);
        return;
    }
    let rows = state.faults.iter().map(fault_row).collect();
    let (table, _) = fitted_table(&FAULT_COLUMNS, rows, area, false);
    frame.render_widget(table.block(block), area);
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOPS: &str = r#"{"hops":[{"peer_id":"aabbccddeeff00112233","instance_ref":"1122334455667788","linked_ts":1,"first_linked_ts":1,"head_seq":5,"head_ts":1,"last_ok_ts":1794136198,"failed_at":null,"last_failed_at":null,"receipt":{"kind":"close","plane":"tunnel","session":"abcdabcdabcdabcd","seq":2,"ts":1,"mu_charged":"15","in_bytes":4,"out_bytes":4,"digest":"ffeeddccbbaa99887766","valid":true,"problems":[]}},{"peer_id":"cc","instance_ref":"dd","linked_ts":null,"first_linked_ts":null,"head_seq":null,"head_ts":null,"last_ok_ts":null,"failed_at":1794136100,"last_failed_at":1794000000,"receipt":{"kind":"close","plane":"tunnel","session":"s","seq":3,"ts":2,"mu_charged":"340282366920938463463374607431768211456","in_bytes":0,"out_bytes":0,"digest":"aa","valid":false,"problems":["receipt signed by another peer","second"]}},{"peer_id":"ee","instance_ref":"ff","receipt":null}],"read_at":5}"#;

    const FAULTS: &str = r#"{"faults":[{"ts":1,"peer_id":"aabbccddeeff00112233","account_ref":"ab","reason":"statement_contradicts_receipt","seq":3},{"ts":null,"peer_id":"cc","account_ref":"","reason":"other","seq":null}],"read_at":2}"#;

    #[test]
    fn hops_parse_with_nulls_and_a_missing_receipt() {
        let hops = parse_hops(HOPS).unwrap();
        assert_eq!(hops.len(), 3);
        let first = &hops[0];
        assert_eq!(first.last_ok_ts, Some(1794136198));
        assert_eq!(first.failed_at, None);
        let receipt = first.receipt.as_ref().unwrap();
        assert!(receipt.valid && receipt.problems.is_empty());
        assert_eq!(receipt.mu_charged, "15");
        assert_eq!(first.first_problem(), None);
        assert_eq!(hops[1].linked_ts, None);
        assert_eq!(hops[1].first_problem(), Some("receipt signed by another peer"));
        assert_eq!(
            hops[1].receipt.as_ref().unwrap().mu_charged,
            "340282366920938463463374607431768211456"
        );
        assert!(hops[2].receipt.is_none(), "an absent or null receipt is none");
        assert_eq!(hops[2].first_problem(), None);
    }

    #[test]
    fn an_invalid_receipt_with_no_problem_text_still_says_so() {
        let json = r#"{"hops":[{"peer_id":"a","receipt":{"valid":false}}]}"#;
        assert_eq!(parse_hops(json).unwrap()[0].first_problem(), Some("no reason given"));
    }

    #[test]
    fn faults_parse_and_the_statement_ones_are_known() {
        let faults = parse_faults(FAULTS).unwrap();
        assert_eq!(faults.len(), 2);
        assert_eq!(faults[0].seq, Some(3));
        assert_eq!(faults[1].ts, None);
        assert!(is_statement_fault(&faults[0].reason));
        assert!(!is_statement_fault(&faults[1].reason));
        for reason in STATEMENT_FAULTS {
            assert!(is_statement_fault(reason));
        }
    }

    #[test]
    fn errors_and_empty_lists_are_told_apart() {
        assert_eq!(parse_hops(r#"{"error":"no daemon"}"#).unwrap_err(), "no daemon");
        assert_eq!(parse_faults(r#"{"error":"no daemon"}"#).unwrap_err(), "no daemon");
        assert!(parse_hops("").is_err());
        assert!(parse_faults(r#"{"read_at":1}"#).is_err());
        assert!(parse_hops(r#"{"hops":[]}"#).unwrap().is_empty());
        assert!(parse_faults(r#"{"faults":[]}"#).unwrap().is_empty());
    }

    #[test]
    fn dates_are_cut_to_the_minute() {
        assert_eq!(format_minute(Some(1794136198)), "2026-11-08 11:09");
        assert_eq!(format_minute(None), "\u{2014}");
    }

    #[test]
    fn the_detail_names_the_first_problem() {
        let hops = parse_hops(HOPS).unwrap();
        let text = |hop: &Hop| {
            hop_detail(Some(hop)).iter().map(|l| l.to_string()).collect::<Vec<_>>().join("\n")
        };
        assert!(text(&hops[1]).contains("NOT VALID: receipt signed by another peer"));
        assert!(!text(&hops[1]).contains("second"));
        assert!(text(&hops[0]).contains("receipt is valid"));
        assert!(text(&hops[0]).contains("ffeeddccbbaa") && !text(&hops[0]).contains("ffeeddccbbaa9"));
        assert!(text(&hops[2]).contains("No receipt"));
    }

    #[test]
    fn the_cursor_follows_a_hop_across_a_re_read() {
        let mut state = ReceiptsState::default();
        let hops = parse_hops(HOPS).unwrap();
        state.set_hops(hops.clone());
        assert_eq!(state.table.selected(), Some(0));
        state.table.select(Some(1));
        let mut reordered = hops;
        reordered.reverse();
        state.set_hops(reordered);
        assert_eq!(state.table.selected(), Some(1), "hop `cc` is the middle one both ways");
        state.set_hops(Vec::new());
        assert_eq!(state.table.selected(), None);
    }

    #[test]
    fn the_page_draws_every_state_at_small_and_normal_sizes() {
        for (width, height) in [(40u16, 11u16), (60, 14), (80, 24), (120, 40)] {
            let mut app = crate::responsive_sweep::on_page(Page::Receipts);
            let shown = |app: &mut App| {
                crate::responsive_sweep::text(&crate::responsive_sweep::draw(app, width, height))
            };
            app.receipts.hops_error = "no daemon".into();
            assert!(shown(&mut app).contains("Cannot read"), "{width}x{height}");
            app.receipts.hops_error.clear();
            app.receipts.hops_loaded = true;
            app.receipts.faults_loaded = true;
            assert!(shown(&mut app).contains("No instance is delegated"), "{width}x{height}");
            app.receipts.set_hops(parse_hops(HOPS).unwrap());
            app.receipts.faults = parse_faults(FAULTS).unwrap();
            let text = shown(&mut app);
            assert!(text.contains("aabbccddeeff"), "{width}x{height}\n{text}");
            app.receipts_move(1);
            let text = shown(&mut app);
            if height >= 24 {
                assert!(text.contains("NOT VALID"), "{width}x{height}\n{text}");
                assert!(text.contains("statement_contradicts_receipt"), "{width}x{height}\n{text}");
            }
        }
    }
}
