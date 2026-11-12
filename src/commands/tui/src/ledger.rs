//! The LEDGER page: the balance statement of every account this node keeps, and the
//! movements behind each one.
//!
//! Data comes from `nodo statement --json` (the accounts) and
//! `nodo statement <ref> --json [--from SEQ] [--limit N] [--kind KIND]` (one chain),
//! run as child processes off the UI thread the way `nodo reputation --json` is. The
//! node signs the chain; this page only reads and shows it. A chain that does not
//! verify is the serious case and is drawn in the theme's error colour.
//!
//! Everything that can be decided without a terminal is a small pure function here
//! (`short_hash`, `format_ts`, `next_kind`, `next_from`, `chain_status`), so the tests
//! can pin it.

use crate::app::{App, Page};
use crate::layout_util::Column;
use crate::ui::{
    accent, bad, fitted_table, good, label_colour, muted, section_block, selected_style,
    text_colour, warn, TextCell,
};
use ratatui::prelude::*;
use ratatui::widgets::{Paragraph, TableState, Wrap};
use serde_json::Value;
use std::time::{Duration, Instant};
use tokio::process::Command;
use tokio::task::JoinHandle;

/// How many movements one `nodo statement <ref>` call asks for.
pub const PAGE_SIZE: usize = 100;

/// How long a read of the accounts may take before it is given up.
const READ_TIMEOUT: Duration = Duration::from_secs(60);

/// How often the account list is re-read while it is on screen.
const ACCOUNTS_REFRESH: Duration = Duration::from_secs(30);

/// The kinds of movement the filter key cycles through; `""` is every kind. The order
/// is the one `src/database/ledger.py` `KINDS` documents, with the common ones first.
pub const KIND_FILTERS: [&str; 16] = [
    "",
    "tunnel",
    "maintenance",
    "refund",
    "stop",
    "start",
    "deposit",
    "credit",
    "debit",
    "top_up",
    "withdrawal",
    "build",
    "child_start",
    "child_bound",
    "resource_change",
    "statement_query",
];

/// One account of the node, as `nodo statement --json` lists it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Account {
    /// `client`, `local` or `delegated`.
    pub kind: String,
    /// The 64-hex reference the account is asked for by.
    pub reference: String,
    pub label: String,
    pub alive: bool,
    /// MU as a decimal string: it can exceed `u64`.
    pub balance_mu: String,
    pub service_id: String,
    pub father_ref: String,
    pub peer_id: String,
    pub head_seq: Option<i64>,
    pub head_hash: String,
    pub rows: Option<u64>,
    pub last_ts: Option<i64>,
}

/// Whether a chain verified, as `nodo statement <ref> --json` reports it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Chain {
    pub ok: bool,
    /// Why the chain is broken; empty when it is not.
    pub problem: String,
    /// The row the chain is broken at.
    pub seq: Option<i64>,
    pub checked: u64,
    /// Checked from the first row that is held, not from the genesis.
    pub partial: bool,
}

/// One row of an account's ledger.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Movement {
    pub seq: i64,
    pub ts: Option<i64>,
    pub kind: String,
    pub amount_mu: String,
    pub balance_after_mu: String,
    pub instance_ref: String,
    /// The row's `detail` object, as sorted `(key, value-as-text)` pairs.
    pub detail: Vec<(String, String)>,
    pub hash: String,
    pub prev_hash: String,
}

impl Movement {
    /// One field of the row's detail.
    pub fn detail_value(&self, key: &str) -> Option<&str> {
        self.detail
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.as_str())
    }

    /// Who asked for the movement: `operator`, `client` or `instance`, from
    /// `detail.actor`. A row with none is the node's own rule at work (maintenance, a
    /// tunnel, a refund), and reads `node`.
    pub fn actor(&self) -> &str {
        match self.detail_value("actor") {
            Some(actor @ ("operator" | "client" | "instance")) => actor,
            _ => "node",
        }
    }

    /// A `tunnel` row that closes a stream: it moves 0 MU and anchors a signed
    /// execution receipt, whose digest its detail holds.
    pub fn is_close_anchor(&self) -> bool {
        self.kind == "tunnel" && self.detail_value("close") == Some("true")
    }

    /// The digest of the receipt this row anchors, if it anchors one.
    pub fn receipt_digest(&self) -> Option<&str> {
        self.is_close_anchor()
            .then(|| self.detail_value("receipt_digest"))
            .flatten()
            .filter(|digest| !digest.is_empty())
    }
}

/// One answer of `nodo statement <ref> --json`: the account, its chain check and a page
/// of movements.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Statement {
    pub account: Account,
    pub chain: Option<Chain>,
    pub movements: Vec<Movement>,
    pub has_more: bool,
    pub read_at: Option<i64>,
}

// --- Parsing ---------------------------------------------------------------

/// The JSON object a `nodo statement`/`nodo receipt` command printed, or the text of its
/// `{"error": ...}`. Shared with the RECEIPTS page.
pub(crate) fn parse_object(output: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_str(output.trim())
        .map_err(|error| format!("Unreadable output from nodo: {error}"))?;
    if !value.is_object() {
        return Err("Unreadable output from nodo: not a JSON object".to_string());
    }
    match value.get("error") {
        Some(Value::Null) | None => Ok(value),
        Some(Value::String(text)) => Err(nonblank_or(text, "nodo reported an error")),
        Some(other) => Err(other.to_string()),
    }
}

fn nonblank_or(text: &str, fallback: &str) -> String {
    if text.trim().is_empty() {
        fallback.to_string()
    } else {
        text.trim().to_string()
    }
}

/// A field as text: strings as they are, numbers and booleans written out, a missing or
/// `null` field as `""`.
pub(crate) fn field_text(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

/// A field as an integer, from a JSON number or a numeric string; `None` for `null`.
pub(crate) fn field_int(value: &Value, key: &str) -> Option<i64> {
    match value.get(key)? {
        Value::Number(number) => number.as_i64().or_else(|| number.as_f64().map(|f| f as i64)),
        Value::String(text) => text.trim().parse().ok(),
        _ => None,
    }
}

pub(crate) fn field_bool(value: &Value, key: &str) -> bool {
    matches!(value.get(key), Some(Value::Bool(true)))
}

fn parse_account(value: &Value) -> Account {
    Account {
        kind: field_text(value, "kind"),
        reference: field_text(value, "ref"),
        label: field_text(value, "label"),
        alive: field_bool(value, "alive"),
        balance_mu: field_text(value, "balance_mu"),
        service_id: field_text(value, "service_id"),
        father_ref: field_text(value, "father_ref"),
        peer_id: field_text(value, "peer_id"),
        head_seq: field_int(value, "head_seq"),
        head_hash: field_text(value, "head_hash"),
        rows: field_int(value, "rows").map(|rows| rows.max(0) as u64),
        last_ts: field_int(value, "last_ts"),
    }
}

/// Parse `nodo statement --json`.
pub fn parse_accounts(output: &str) -> Result<Vec<Account>, String> {
    let value = parse_object(output)?;
    let accounts = value
        .get("accounts")
        .and_then(Value::as_array)
        .ok_or("nodo statement returned no account list")?;
    Ok(accounts.iter().map(parse_account).collect())
}

fn parse_chain(value: &Value) -> Chain {
    Chain {
        ok: field_bool(value, "ok"),
        problem: field_text(value, "problem"),
        seq: field_int(value, "seq"),
        checked: field_int(value, "checked").unwrap_or(0).max(0) as u64,
        partial: field_bool(value, "partial"),
    }
}

/// A row's `detail` as sorted `(key, text)` pairs. Anything that is not an object is
/// kept as one `detail` pair rather than dropped.
fn parse_detail(value: Option<&Value>) -> Vec<(String, String)> {
    let render = |value: &Value| match value {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    };
    match value {
        Some(Value::Object(map)) => map.iter().map(|(k, v)| (k.clone(), render(v))).collect(),
        Some(Value::Null) | None => Vec::new(),
        Some(other) => vec![("detail".to_string(), render(other))],
    }
}

fn parse_movement(value: &Value) -> Movement {
    Movement {
        seq: field_int(value, "seq").unwrap_or(0),
        ts: field_int(value, "ts"),
        kind: field_text(value, "kind"),
        amount_mu: field_text(value, "amount_mu"),
        balance_after_mu: field_text(value, "balance_after_mu"),
        instance_ref: field_text(value, "instance_ref"),
        detail: parse_detail(value.get("detail")),
        hash: field_text(value, "hash"),
        prev_hash: field_text(value, "prev_hash"),
    }
}

/// Parse `nodo statement <ref> --json`.
pub fn parse_statement(output: &str) -> Result<Statement, String> {
    let value = parse_object(output)?;
    Ok(Statement {
        account: value.get("account").map(parse_account).unwrap_or_default(),
        chain: value.get("chain").filter(|c| c.is_object()).map(parse_chain),
        movements: value
            .get("movements")
            .and_then(Value::as_array)
            .map(|rows| rows.iter().map(parse_movement).collect())
            .unwrap_or_default(),
        has_more: field_bool(&value, "has_more"),
        read_at: field_int(&value, "read_at"),
    })
}

// --- Pure helpers ----------------------------------------------------------

/// The first 12 characters of a hash or reference; `—` when there is none. A hash is
/// never shown longer than this on these pages.
pub fn short_hash(hash: &str) -> String {
    if hash.trim().is_empty() {
        "\u{2014}".to_string()
    } else {
        hash.chars().take(12).collect()
    }
}

/// A unix time as `YYYY-MM-DD HH:MM:SS` UTC; `—` for none.
pub fn format_ts(ts: Option<i64>) -> String {
    let Some(ts) = ts else {
        return "\u{2014}".to_string();
    };
    let days = ts.div_euclid(86_400);
    let secs = ts.rem_euclid(86_400);
    // Civil date from a day count (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02} {:02}:{:02}:{:02}",
        secs / 3600,
        secs % 3600 / 60,
        secs % 60
    )
}

/// The next position in [`KIND_FILTERS`], wrapping to "all".
pub fn next_kind(index: usize) -> usize {
    (index + 1) % KIND_FILTERS.len()
}

/// The `--kind` argument of filter `index`; `None` for "all".
pub fn kind_arg(index: usize) -> Option<&'static str> {
    KIND_FILTERS
        .get(index)
        .copied()
        .filter(|kind| !kind.is_empty())
}

/// How the filter is named on screen.
pub fn kind_label(index: usize) -> &'static str {
    kind_arg(index).unwrap_or("all")
}

/// The `--from` for the page after `movements`: one past the highest seq held. `None`
/// when nothing is held (the first page is asked for without `--from`).
pub fn next_from(movements: &[Movement]) -> Option<i64> {
    movements.iter().map(|movement| movement.seq).max().map(|seq| seq + 1)
}

/// The words of the chain line and whether it is an error: `OK`, `OK from the first
/// row held`, or `BROKEN at seq N: problem`.
pub fn chain_status(chain: Option<&Chain>) -> (String, bool) {
    let Some(chain) = chain else {
        return ("chain: not reported".to_string(), false);
    };
    if !chain.ok {
        let at = chain.seq.map(|seq| format!(" at seq {seq}")).unwrap_or_default();
        let why = if chain.problem.trim().is_empty() {
            "no reason given".to_string()
        } else {
            chain.problem.trim().to_string()
        };
        return (format!("chain BROKEN{at}: {why}"), true);
    }
    let how = if chain.partial { "OK from the first row held" } else { "OK" };
    (format!("chain {how} ({} rows checked)", chain.checked), false)
}

/// How an amount is coloured: gained, spent, or nothing moved.
fn amount_colour(amount: &str) -> Color {
    let amount = amount.trim();
    if amount.starts_with('-') {
        warn()
    } else if amount.is_empty() || amount.trim_start_matches('+').chars().all(|c| c == '0') {
        muted()
    } else {
        good()
    }
}

// --- State -----------------------------------------------------------------

/// An account that has been opened, and what has been read of its chain.
#[derive(Debug, Default)]
pub struct OpenAccount {
    pub account: Account,
    pub chain: Option<Chain>,
    pub movements: Vec<Movement>,
    pub has_more: bool,
    /// The text of a failed read, shown on the page.
    pub error: String,
    pub loading: bool,
    pub table: TableState,
    /// Whether the selected row's detail is drawn as one field per line.
    pub expanded: bool,
}

struct StatementJob {
    handle: JoinHandle<Result<Statement, String>>,
    reference: String,
    append: bool,
}

/// What the LEDGER page holds: the account list, the open account, and the reads in
/// flight.
#[derive(Default)]
pub struct LedgerState {
    pub accounts: Vec<Account>,
    pub table: TableState,
    /// The text of a failed read of the account list.
    pub error: String,
    pub loaded: bool,
    pub open: Option<OpenAccount>,
    /// Index into [`KIND_FILTERS`] for the open account.
    pub kind_index: usize,
    last_refresh: Option<Instant>,
    accounts_task: Option<JoinHandle<Result<Vec<Account>, String>>>,
    statement_task: Option<StatementJob>,
}

impl std::fmt::Debug for LedgerState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LedgerState")
            .field("accounts", &self.accounts.len())
            .field("open", &self.open.as_ref().map(|open| &open.account.reference))
            .finish()
    }
}

/// Move a table selection by `delta` rows, wrapping; selects the first row when none is.
pub fn step(state: &mut TableState, len: usize, delta: i32) {
    if len == 0 {
        state.select(None);
        return;
    }
    let next = match state.selected() {
        Some(index) => (index as i64 + delta as i64).rem_euclid(len as i64) as usize,
        None => 0,
    };
    state.select(Some(next));
}

impl LedgerState {
    /// The account under the cursor in the list.
    pub fn selected_account(&self) -> Option<&Account> {
        self.table.selected().and_then(|index| self.accounts.get(index))
    }

    fn set_accounts(&mut self, accounts: Vec<Account>) {
        let keep = self.selected_account().map(|a| a.reference.clone());
        self.accounts = accounts;
        let index = keep
            .and_then(|reference| self.accounts.iter().position(|a| a.reference == reference))
            .or(if self.accounts.is_empty() { None } else { Some(0) });
        self.table.select(index);
    }

    fn apply_statement(&mut self, reference: &str, append: bool, result: Result<Statement, String>) {
        let Some(open) = self.open.as_mut().filter(|o| o.account.reference == reference) else {
            return;
        };
        open.loading = false;
        match result {
            Ok(statement) => {
                open.error.clear();
                open.chain = statement.chain;
                open.has_more = statement.has_more;
                if !statement.account.reference.is_empty() {
                    open.account = statement.account;
                }
                if append {
                    open.movements.extend(statement.movements);
                } else {
                    open.movements = statement.movements;
                }
                let last = open.movements.len().checked_sub(1);
                let selected = open.table.selected().map(|i| i.min(last.unwrap_or(0)));
                open.table.select(selected.or(last.map(|_| 0)));
            }
            Err(error) => open.error = error,
        }
    }
}

// --- Running the command ---------------------------------------------------

/// Run `nodo <args>` and return its one JSON line. The exit code is not looked at: a
/// broken chain exits 1 and still prints the statement.
pub(crate) async fn run_json(args: Vec<String>, what: &str) -> Result<String, String> {
    let output = tokio::time::timeout(
        READ_TIMEOUT,
        Command::new("nodo").args(&args).kill_on_drop(true).output(),
    )
    .await
    .map_err(|_| format!("{what} timed out after {} seconds", READ_TIMEOUT.as_secs()))?
    .map_err(|error| format!("Unable to run {what}: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    match crate::app::report_line(&stdout) {
        Some(line) => Ok(line.to_string()),
        None => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let line = stderr.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
            Err(nonblank_or(line, &format!("{what} produced no output")))
        }
    }
}

/// The arguments of one page of one account's chain.
pub fn statement_args(reference: &str, from: Option<i64>, kind: Option<&str>) -> Vec<String> {
    let mut args = vec!["statement".to_string(), reference.to_string(), "--json".to_string()];
    if let Some(from) = from {
        args.extend(["--from".to_string(), from.to_string()]);
    }
    args.extend(["--limit".to_string(), PAGE_SIZE.to_string()]);
    if let Some(kind) = kind {
        args.extend(["--kind".to_string(), kind.to_string()]);
    }
    args
}

impl App {
    /// Start reading one page of the open account's chain. `append` adds the page to
    /// the rows held (the next page); otherwise the rows are replaced.
    fn ledger_request(&mut self, append: bool) {
        let kind = kind_arg(self.ledger.kind_index);
        let Some(open) = self.ledger.open.as_mut() else {
            return;
        };
        let from = if append { next_from(&open.movements) } else { None };
        let reference = open.account.reference.clone();
        open.loading = true;
        let args = statement_args(&reference, from, kind);
        let handle = tokio::spawn(async move {
            parse_statement(&run_json(args, "nodo statement").await?)
        });
        self.ledger.statement_task = Some(StatementJob { handle, reference, append });
    }

    /// Enter: open the selected account's movements.
    pub fn ledger_open(&mut self) {
        if self.ledger.open.is_some() {
            return;
        }
        let Some(account) = self.ledger.selected_account().cloned() else {
            return;
        };
        self.ledger.kind_index = 0;
        self.ledger.open = Some(OpenAccount { account, ..OpenAccount::default() });
        self.ledger_request(false);
    }

    /// Open the account of `statement` with its rows already read, for the tests.
    #[cfg(test)]
    pub(crate) fn ledger_open_for_test(&mut self, statement: Statement) {
        let account = statement.account.clone();
        self.ledger.open = Some(OpenAccount { account: account.clone(), ..OpenAccount::default() });
        self.ledger.apply_statement(&account.reference, false, Ok(statement));
    }

    /// Back to the account list. Returns whether an account was open.
    pub fn ledger_close(&mut self) -> bool {
        self.ledger.statement_task = None;
        self.ledger.open.take().is_some()
    }

    /// `m`: read the next page of movements when the last answer said there are more.
    pub fn ledger_more(&mut self) {
        let more = self.ledger.open.as_ref().is_some_and(|o| o.has_more && !o.loading);
        if more {
            self.ledger_request(true);
        }
    }

    /// `f`: the next kind of movement to filter by; reads the account again.
    pub fn ledger_cycle_kind(&mut self) {
        if self.ledger.open.is_none() {
            return;
        }
        self.ledger.kind_index = next_kind(self.ledger.kind_index);
        if let Some(open) = self.ledger.open.as_mut() {
            open.movements.clear();
            open.has_more = false;
            open.table.select(None);
        }
        self.ledger_request(false);
    }

    /// `d`: the selected row's detail, one field per line or in one line.
    pub fn ledger_toggle_detail(&mut self) {
        if let Some(open) = self.ledger.open.as_mut() {
            open.expanded = !open.expanded;
        }
    }

    /// The cursor, in the account list or in the open account's movements.
    pub fn ledger_move(&mut self, delta: i32) {
        match self.ledger.open.as_mut() {
            Some(open) => step(&mut open.table, open.movements.len(), delta),
            None => {
                let len = self.ledger.accounts.len();
                step(&mut self.ledger.table, len, delta)
            }
        }
    }

    /// Collect finished reads and start the ones that are due. Called on every tick;
    /// only reads while the LEDGER page is the one on screen. `force` is the `r` key.
    pub(crate) async fn refresh_ledger(&mut self, force: bool) {
        if self.ledger.accounts_task.as_ref().is_some_and(|t| t.is_finished()) {
            let task = self.ledger.accounts_task.take().unwrap();
            match task.await {
                Ok(Ok(accounts)) => {
                    self.ledger.error.clear();
                    self.ledger.set_accounts(accounts);
                }
                Ok(Err(error)) => self.ledger.error = error,
                Err(error) => self.ledger.error = format!("Statement read failed: {error}"),
            }
            self.ledger.loaded = true;
        }
        if self.ledger.statement_task.as_ref().is_some_and(|t| t.handle.is_finished()) {
            let job = self.ledger.statement_task.take().unwrap();
            let result = job
                .handle
                .await
                .unwrap_or_else(|error| Err(format!("Statement read failed: {error}")));
            self.ledger.apply_statement(&job.reference, job.append, result);
        }
        if self.page() != Page::Ledger {
            return;
        }
        if self.ledger.open.is_some() {
            if force {
                self.ledger_request(false);
            }
            return;
        }
        let due = self.ledger.last_refresh.map_or(true, |at| at.elapsed() >= ACCOUNTS_REFRESH);
        if self.ledger.accounts_task.is_none() && (force || due) {
            self.ledger.last_refresh = Some(Instant::now());
            self.ledger.accounts_task = Some(tokio::spawn(async {
                let args = vec!["statement".to_string(), "--json".to_string()];
                parse_accounts(&run_json(args, "nodo statement").await?)
            }));
        }
    }
}

// --- Drawing ---------------------------------------------------------------

/// The accounts table's columns: what an account is, whether it lives, what it holds.
const ACCOUNT_COLUMNS: [Column; 8] = [
    Column::new("Label", Constraint::Min(12), 8, 0),
    Column::new("Balance MU", Constraint::Length(24), 8, 1),
    Column::new("Kind", Constraint::Length(9), 6, 2),
    Column::new("State", Constraint::Length(5), 5, 3),
    Column::new("Head", Constraint::Length(6), 4, 4),
    Column::new("Rows", Constraint::Length(6), 4, 5),
    Column::new("Service", Constraint::Length(12), 8, 6),
    Column::new("Ref", Constraint::Length(12), 8, 7),
];

/// The movements table's columns.
const MOVEMENT_COLUMNS: [Column; 8] = [
    Column::new("Seq", Constraint::Length(6), 3, 0),
    Column::new("Kind", Constraint::Length(13), 6, 1),
    // Who asked: the operator, the client, the instance, or the node's own rule. It
    // drops after every column but Seq and Kind, as a statement has to say it.
    Column::new("By", Constraint::Length(8), 2, 2),
    Column::new("Amount MU", Constraint::Length(16), 6, 3),
    Column::new("Balance after", Constraint::Min(14), 8, 4),
    Column::new("When (UTC)", Constraint::Length(19), 19, 5),
    Column::new("Receipt", Constraint::Length(12), 8, 6),
    Column::new("Instance", Constraint::Length(12), 8, 7),
];

fn account_row(account: &Account) -> (Vec<TextCell>, Style) {
    let state = if account.alive { "live" } else { "dead" };
    let state_colour = if account.alive { good() } else { muted() };
    let cells = vec![
        crate::ui::nonempty(&account.label, "(no label)").to_string().into(),
        account.balance_mu.clone().into(),
        account.kind.clone().into(),
        TextCell::from(state).style(Style::default().fg(state_colour)),
        account.head_seq.map(|s| s.to_string()).unwrap_or_default().into(),
        account.rows.map(|r| r.to_string()).unwrap_or_default().into(),
        short_hash(&account.service_id).into(),
        short_hash(&account.reference).into(),
    ];
    let style = if account.alive { Style::default() } else { Style::default().fg(muted()) };
    (cells, style)
}

/// One movement as a row. A close anchor is bold in the accent colour and shows the
/// digest of the receipt it holds.
fn movement_row(movement: &Movement) -> (Vec<TextCell>, Style) {
    let anchor = movement.is_close_anchor();
    let kind = if anchor { "tunnel close".to_string() } else { movement.kind.clone() };
    let cells = vec![
        movement.seq.to_string().into(),
        kind.into(),
        TextCell::from(movement.actor().to_string()).style(Style::default().fg(
            if movement.actor() == "operator" { warn() } else { muted() },
        )),
        TextCell::from(movement.amount_mu.clone())
            .style(Style::default().fg(amount_colour(&movement.amount_mu))),
        movement.balance_after_mu.clone().into(),
        format_ts(movement.ts).into(),
        movement.receipt_digest().map(short_hash).unwrap_or_default().into(),
        if movement.instance_ref.is_empty() { String::new() } else { short_hash(&movement.instance_ref) }
            .into(),
    ];
    let style = if anchor { Style::default().fg(accent()).bold() } else { Style::default() };
    (cells, style)
}

/// The LEDGER page: the account list, or the open account's chain status and movements.
/// Shows `nodo statement --json`; see the module doc.
pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    match app.ledger.open.is_some() {
        true => draw_open(frame, &mut app.ledger, area),
        false => draw_accounts(frame, &mut app.ledger, area),
    }
}

fn note(text: impl Into<String>, colour: Color) -> Paragraph<'static> {
    Paragraph::new(Span::styled(text.into(), Style::default().fg(colour))).wrap(Wrap { trim: true })
}

fn draw_accounts(frame: &mut Frame, state: &mut LedgerState, area: Rect) {
    let title = format!(" LEDGER \u{2022} {} accounts ", state.accounts.len());
    let block = section_block(title, accent());
    if state.accounts.is_empty() {
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let (text, colour) = if !state.error.is_empty() {
            (format!("Cannot read the statements: {}", state.error), bad())
        } else if !state.loaded {
            ("Reading the statements\u{2026}".to_string(), muted())
        } else {
            ("No accounts yet: this node has no ledger rows to show.".to_string(), muted())
        };
        frame.render_widget(note(text, colour), inner);
        return;
    }
    let split = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(u16::from(!state.error.is_empty() && area.height > 5)),
    ])
    .split(area);
    let rows = state.accounts.iter().map(account_row).collect();
    let (table, _) = fitted_table(&ACCOUNT_COLUMNS, rows, split[0], state.table.selected().is_some());
    let table = table
        .block(block)
        .highlight_style(selected_style())
        .highlight_symbol("\u{25b8} ");
    frame.render_stateful_widget(table, split[0], &mut state.table);
    if split[1].height > 0 {
        frame.render_widget(note(format!("Last read failed: {}", state.error), bad()), split[1]);
    }
}

/// The two header lines of an open account: the chain verdict first, since a broken
/// chain is what matters most, then who the account is.
fn open_header(open: &OpenAccount, filter: &str) -> Vec<Line<'static>> {
    let (words, broken) = chain_status(open.chain.as_ref());
    let colour = if broken { bad() } else { good() };
    let account = &open.account;
    let who = format!(
        "{} {} \u{2022} {} \u{2022} balance {} MU \u{2022} filter: {}",
        account.kind,
        crate::ui::nonempty(&account.label, "(no label)"),
        short_hash(&account.reference),
        crate::ui::nonempty(&account.balance_mu, "?"),
        filter,
    );
    vec![
        Line::from(Span::styled(words, Style::default().fg(colour).bold())),
        Line::from(Span::styled(who, Style::default().fg(label_colour()))),
    ]
}

/// The selected row's detail: one field per line when `expanded`, otherwise one line.
pub fn detail_lines(movement: &Movement, expanded: bool) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    let mut put = |key: &str, value: String, strong: bool| {
        let style = if strong { Style::default().fg(accent()).bold() } else { Style::default().fg(text_colour()) };
        lines.push(Line::from(vec![
            Span::styled(format!("{key:<16}"), Style::default().fg(label_colour())),
            Span::styled(value, style),
        ]));
    };
    if !expanded {
        let fields: Vec<String> =
            movement.detail.iter().map(|(k, v)| format!("{k}={v}")).collect();
        let text = if fields.is_empty() { "(no detail)".to_string() } else { fields.join("  ") };
        let key = if movement.is_close_anchor() { "close anchor" } else { "detail" };
        put(key, text, movement.is_close_anchor());
        return lines;
    }
    put("seq / kind", format!("{} {}", movement.seq, movement.kind), false);
    put("hash", short_hash(&movement.hash), false);
    put("prev hash", short_hash(&movement.prev_hash), false);
    if movement.is_close_anchor() {
        put("anchors", "a signed execution receipt (see RECEIPTS)".to_string(), true);
    }
    if movement.detail.is_empty() {
        put("detail", "(none)".to_string(), false);
    }
    for (key, value) in &movement.detail {
        let shown = if key.ends_with("digest") || key.ends_with("hash") { short_hash(value) } else { value.clone() };
        put(key, shown, key == "receipt_digest");
    }
    lines
}

fn draw_open(frame: &mut Frame, state: &mut LedgerState, area: Rect) {
    let filter = kind_label(state.kind_index);
    let Some(open) = state.open.as_mut() else {
        return;
    };
    let selected = open.table.selected().and_then(|i| open.movements.get(i)).cloned();
    let detail = selected.as_ref().map(|m| detail_lines(m, open.expanded));
    let detail_want = detail.as_ref().map_or(3, |lines| lines.len() as u16 + 2);
    let table_want = open.movements.len() as u16 + 4;
    let heights = crate::layout_util::allocate_heights(
        area.height,
        &[(1, 3), (4, table_want), (3, detail_want)],
        &[0, 1, 2],
    );
    let parts = crate::layout_util::stack(area, &heights);
    if parts[0].height > 0 {
        frame.render_widget(
            Paragraph::new(open_header(open, filter)).wrap(Wrap { trim: true }),
            parts[0],
        );
    }
    let more = if open.has_more { " \u{2022} m loads more" } else { "" };
    let title = format!(" MOVEMENTS \u{2022} {} held{more} ", open.movements.len());
    if parts[1].height > 0 {
        draw_movements(frame, open, parts[1], title);
    }
    if parts[2].height > 0 {
        let lines = detail.unwrap_or_else(|| vec![Line::from(Span::styled(
            "Select a movement to read its detail.",
            Style::default().fg(muted()),
        ))]);
        let block = section_block(" DETAIL ", muted());
        frame.render_widget(Paragraph::new(lines).block(block).wrap(Wrap { trim: true }), parts[2]);
    }
}

fn draw_movements(frame: &mut Frame, open: &mut OpenAccount, area: Rect, title: String) {
    let block = section_block(title, accent());
    if open.movements.is_empty() {
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let (text, colour) = if !open.error.is_empty() {
            (format!("Cannot read this account: {}", open.error), bad())
        } else if open.loading {
            ("Reading the movements\u{2026}".to_string(), muted())
        } else {
            ("No movements of this kind are held for this account.".to_string(), muted())
        };
        frame.render_widget(note(text, colour), inner);
        return;
    }
    let rows = open.movements.iter().map(movement_row).collect();
    let (table, _) = fitted_table(&MOVEMENT_COLUMNS, rows, area, open.table.selected().is_some());
    let table = table
        .block(block)
        .highlight_style(selected_style())
        .highlight_symbol("\u{25b8} ");
    frame.render_stateful_widget(table, area, &mut open.table);
}

#[cfg(test)]
mod tests {
    use super::*;

    const ACCOUNTS: &str = r#"{"accounts":[{"kind":"client","ref":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","label":"web","alive":true,"balance_mu":"493","service_id":"abababababababababababababababababababababababababababababababab","father_ref":"","peer_id":"","head_seq":3,"head_hash":"deadbeef","rows":3,"last_ts":1794136198},{"kind":"delegated","ref":"bb","label":"","alive":false,"balance_mu":"123456789012345678901234567890","service_id":"","father_ref":null,"peer_id":"cd","head_seq":null,"head_hash":"","rows":null,"last_ts":null}],"read_at":1794136200}"#;

    const STATEMENT: &str = r#"{"account":{"kind":"client","ref":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","label":"web","alive":true,"balance_mu":"493","service_id":"","father_ref":"","peer_id":"","head_seq":3,"head_hash":"x","rows":3,"last_ts":1794136198},"chain":{"ok":true,"problem":"","seq":0,"checked":3,"partial":false},"movements":[{"seq":2,"ts":1794136198,"kind":"tunnel","amount_mu":"-7","balance_after_mu":"493","instance_ref":"","detail":{"execution_name":"ee","open":true,"session":"ab"},"hash":"h2","prev_hash":"h1"},{"seq":3,"ts":1794136199,"kind":"tunnel","amount_mu":"0","balance_after_mu":"493","instance_ref":"","detail":{"close":true,"receipt_digest":"0123456789abcdef0123","mu_charged":"15"},"hash":"h3","prev_hash":"h2"}],"has_more":true,"read_at":1794136200}"#;

    fn movement(seq: i64, kind: &str, detail: &[(&str, &str)]) -> Movement {
        Movement {
            seq,
            kind: kind.to_string(),
            detail: detail.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
            ..Movement::default()
        }
    }

    #[test]
    fn a_movement_says_who_asked_for_it() {
        assert_eq!(movement(1, "credit", &[("actor", "operator")]).actor(), "operator");
        assert_eq!(movement(2, "top_up", &[("actor", "client")]).actor(), "client");
        assert_eq!(movement(3, "deposit", &[("actor", "instance")]).actor(), "instance");
        // The node's own rule, and anything that is not one of the three.
        assert_eq!(movement(4, "maintenance", &[]).actor(), "node");
        assert_eq!(movement(5, "credit", &[("actor", "root")]).actor(), "node");
    }

    #[test]
    fn accounts_parse_with_null_fields_and_huge_mu() {
        let accounts = parse_accounts(ACCOUNTS).unwrap();
        assert_eq!(accounts.len(), 2);
        assert_eq!(accounts[0].label, "web");
        assert!(accounts[0].alive);
        assert_eq!(accounts[0].head_seq, Some(3));
        assert_eq!(accounts[0].rows, Some(3));
        assert_eq!(accounts[1].balance_mu, "123456789012345678901234567890");
        assert_eq!(accounts[1].father_ref, "");
        assert_eq!(accounts[1].head_seq, None);
        assert_eq!(accounts[1].last_ts, None);
        assert!(!accounts[1].alive);
    }

    #[test]
    fn an_empty_account_list_is_not_an_error() {
        assert_eq!(parse_accounts(r#"{"accounts":[],"read_at":1}"#).unwrap(), vec![]);
    }

    #[test]
    fn an_error_object_becomes_the_error_text() {
        assert_eq!(parse_accounts(r#"{"error":"no database"}"#).unwrap_err(), "no database");
        assert_eq!(parse_statement(r#"{"error":"unknown account"}"#).unwrap_err(), "unknown account");
        assert!(parse_accounts("not json").unwrap_err().contains("Unreadable"));
        assert!(parse_accounts("[]").is_err());
        assert!(parse_accounts(r#"{"read_at":1}"#).is_err(), "no list at all is an error");
    }

    #[test]
    fn a_statement_parses_with_its_detail_and_paging() {
        let statement = parse_statement(STATEMENT).unwrap();
        assert!(statement.has_more);
        assert_eq!(statement.movements.len(), 2);
        let chain = statement.chain.unwrap();
        assert!(chain.ok && !chain.partial);
        assert_eq!(chain.checked, 3);
        let open = &statement.movements[0];
        assert_eq!(open.amount_mu, "-7");
        assert_eq!(open.detail_value("open"), Some("true"));
        assert!(!open.is_close_anchor());
        let close = &statement.movements[1];
        assert!(close.is_close_anchor());
        assert_eq!(close.receipt_digest(), Some("0123456789abcdef0123"));
        assert_eq!(close.detail_value("mu_charged"), Some("15"));
        assert_eq!(next_from(&statement.movements), Some(4));
    }

    #[test]
    fn a_broken_chain_is_still_a_statement() {
        let json = r#"{"account":{"kind":"local","ref":"aa"},"chain":{"ok":false,"problem":"hash mismatch","seq":2,"checked":2,"partial":true},"movements":[],"has_more":false}"#;
        let statement = parse_statement(json).unwrap();
        let (words, broken) = chain_status(statement.chain.as_ref());
        assert!(broken);
        assert_eq!(words, "chain BROKEN at seq 2: hash mismatch");
    }

    #[test]
    fn a_statement_with_odd_fields_does_not_fail() {
        let json = r#"{"account":{"ref":"aa","balance_mu":12345678901234567890123},"movements":[{"seq":"5","ts":null,"kind":"stop","detail":null},{"seq":6,"detail":"free text"}]}"#;
        let statement = parse_statement(json).unwrap();
        assert!(statement.chain.is_none());
        assert_eq!(statement.movements[0].seq, 5);
        assert_eq!(statement.movements[0].ts, None);
        assert!(statement.movements[0].detail.is_empty());
        assert_eq!(statement.movements[1].detail_value("detail"), Some("free text"));
        assert_eq!(chain_status(None).0, "chain: not reported");
    }

    #[test]
    fn the_chain_line_says_how_far_it_was_checked() {
        let ok = Chain { ok: true, checked: 3, ..Chain::default() };
        assert_eq!(chain_status(Some(&ok)), ("chain OK (3 rows checked)".to_string(), false));
        let partial = Chain { partial: true, ..ok };
        assert!(chain_status(Some(&partial)).0.contains("OK from the first row held"));
        let broken = Chain { ok: false, ..Chain::default() };
        assert_eq!(chain_status(Some(&broken)).0, "chain BROKEN: no reason given");
    }

    #[test]
    fn hashes_are_cut_to_twelve() {
        assert_eq!(short_hash("0123456789abcdef0123"), "0123456789ab");
        assert_eq!(short_hash("abc"), "abc");
        assert_eq!(short_hash(""), "\u{2014}");
        assert_eq!(short_hash("   "), "\u{2014}");
    }

    #[test]
    fn times_are_written_in_utc() {
        assert_eq!(format_ts(Some(0)), "1970-01-01 00:00:00");
        assert_eq!(format_ts(Some(1794136198)), "2026-11-08 11:09:58");
        assert_eq!(format_ts(Some(951_782_400)), "2000-02-29 00:00:00");
        assert_eq!(format_ts(None), "\u{2014}");
    }

    #[test]
    fn the_kind_filter_cycles_and_wraps_to_all() {
        assert_eq!(kind_arg(0), None);
        assert_eq!(kind_label(0), "all");
        assert_eq!(kind_arg(1), Some("tunnel"));
        let mut index = 0;
        let mut seen = Vec::new();
        for _ in 0..KIND_FILTERS.len() {
            index = next_kind(index);
            seen.push(kind_label(index));
        }
        assert_eq!(seen.last(), Some(&"all"), "a full turn is back on all");
        assert_eq!(seen[0], "tunnel");
        assert_eq!(kind_arg(KIND_FILTERS.len() + 5), None);
    }

    #[test]
    fn the_next_page_starts_after_the_highest_row_held() {
        assert_eq!(next_from(&[]), None);
        let rows = [movement(4, "stop", &[]), movement(9, "stop", &[]), movement(7, "stop", &[])];
        assert_eq!(next_from(&rows), Some(10));
    }

    #[test]
    fn the_command_line_asks_one_page_of_one_kind() {
        assert_eq!(
            statement_args("ab", Some(101), Some("tunnel")),
            ["statement", "ab", "--json", "--from", "101", "--limit", "100", "--kind", "tunnel"]
        );
        assert_eq!(statement_args("ab", None, None), ["statement", "ab", "--json", "--limit", "100"]);
    }

    #[test]
    fn the_cursor_wraps() {
        let mut state = TableState::default();
        step(&mut state, 3, 1);
        assert_eq!(state.selected(), Some(0));
        step(&mut state, 3, -1);
        assert_eq!(state.selected(), Some(2));
        step(&mut state, 3, 1);
        assert_eq!(state.selected(), Some(0));
        step(&mut state, 0, 1);
        assert_eq!(state.selected(), None);
    }

    #[test]
    fn a_page_of_movements_is_appended_or_replaces() {
        let mut state = LedgerState::default();
        let account = Account { reference: "aa".into(), ..Account::default() };
        state.open = Some(OpenAccount { account, loading: true, ..OpenAccount::default() });
        let page = |seqs: &[i64], more| Statement {
            movements: seqs.iter().map(|s| movement(*s, "stop", &[])).collect(),
            has_more: more,
            ..Statement::default()
        };
        state.apply_statement("aa", false, Ok(page(&[1, 2], true)));
        assert_eq!(state.open.as_ref().unwrap().movements.len(), 2);
        assert_eq!(state.open.as_ref().unwrap().table.selected(), Some(0));
        state.apply_statement("aa", true, Ok(page(&[3], false)));
        assert_eq!(state.open.as_ref().unwrap().movements.len(), 3);
        assert!(!state.open.as_ref().unwrap().has_more);
        // An answer for another account is dropped.
        state.apply_statement("zz", false, Ok(page(&[9], false)));
        assert_eq!(state.open.as_ref().unwrap().movements.len(), 3);
        state.apply_statement("aa", false, Err("boom".into()));
        assert_eq!(state.open.as_ref().unwrap().error, "boom");
        assert_eq!(state.open.as_ref().unwrap().movements.len(), 3, "a failure keeps the rows");
    }

    #[test]
    fn detail_is_pretty_printed_and_anchors_say_so() {
        let close = movement(3, "tunnel", &[("close", "true"), ("receipt_digest", "0123456789abcdef0123")]);
        let text: String = detail_lines(&close, true)
            .iter()
            .map(|line| line.to_string())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(text.contains("anchors"));
        assert!(text.contains("0123456789ab") && !text.contains("0123456789abc"));
        let one = detail_lines(&close, false);
        assert_eq!(one.len(), 1);
        assert!(one[0].to_string().starts_with("close anchor"));
    }

    #[test]
    fn the_page_draws_every_state_at_small_and_normal_sizes() {
        let accounts = parse_accounts(ACCOUNTS).unwrap();
        let statement = parse_statement(STATEMENT).unwrap();
        for (width, height) in [(40u16, 11u16), (60, 14), (80, 24), (120, 40)] {
            let mut app = crate::responsive_sweep::on_page(Page::Ledger);
            let shown = |app: &mut App| {
                let buffer = crate::responsive_sweep::draw(app, width, height);
                crate::responsive_sweep::text(&buffer)
            };
            app.ledger.error = "no database".into();
            assert!(shown(&mut app).contains("Cannot read"), "{width}x{height}");
            app.ledger.loaded = true;
            app.ledger.error.clear();
            assert!(shown(&mut app).contains("No accounts yet"), "{width}x{height}");
            app.ledger.set_accounts(accounts.clone());
            assert!(shown(&mut app).contains("web"), "{width}x{height}");
            app.ledger.error = "late failure".into();
            shown(&mut app);
            app.ledger.error.clear();
            app.ledger.open = Some(OpenAccount { account: accounts[0].clone(), ..OpenAccount::default() });
            app.ledger.apply_statement(&accounts[0].reference, false, Ok(statement.clone()));
            let text = shown(&mut app);
            assert!(text.contains("chain OK"), "{width}x{height}\n{text}");
            app.ledger_toggle_detail();
            app.ledger_move(1);
            shown(&mut app);
            let broken = Statement {
                chain: Some(Chain { ok: false, problem: "hash mismatch".into(), seq: Some(2), ..Chain::default() }),
                ..statement.clone()
            };
            app.ledger.apply_statement(&accounts[0].reference, false, Ok(broken));
            let text = shown(&mut app);
            assert!(text.contains("BROKEN at seq 2"), "{width}x{height}\n{text}");
            app.ledger.apply_statement(&accounts[0].reference, false, Err("gone".into()));
            shown(&mut app);
        }
    }
}
