//! The CLIENTS page: who this node's own gateway has issued a client_id to.
//!
//! Split out of `app.rs`/`ui.rs` alongside `peers.rs` and `chat.rs` -- see the module
//! doc on `peers.rs` for why.

use crate::app::{shorten, App, Identifiable, Money, PaymentRow};
use crate::ui::{accent, good, header_row, metric_line, muted, nonempty, payment_lines, section_block, selected_style, status_color, text_colour};
use ratatui::prelude::*;
use ratatui::widgets::{Cell, Row, Table};
use rusqlite::{Connection, Result as SqlResult};
use std::path::Path;

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

/// Everything the Clients page shows about the selected client beyond its table row.
///
/// A client is not a peer and cannot be resolved to one from `peer.remote_client_id`
/// -- that is our client id *inside* a remote peer, not a key into our `clients`
/// table (#178). `bound_peer_id` is the *other*, real direction: the peer, if any,
/// that this client_id was minted for when it registered itself here
/// (`peer.local_client_id`, issue: client↔peer cross-reference).
#[derive(Debug, Clone, Default)]
pub struct ClientDetail {
    pub client_id: String,
    pub deposits: Vec<DepositToken>,
    pub instances: Vec<ClientInstance>,
    pub payments: Vec<PaymentRow>,
    pub bound_peer_id: Option<String>,
}

pub fn get_clients(database: &Path) -> SqlResult<Vec<Client>> {
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

/// What a client paid us, what it was given a token for, what it is running here, and
/// which peer (if any) it is.
pub fn get_client_detail(database: &Path, client_id: &str) -> SqlResult<ClientDetail> {
    let connection = Connection::open(database)?;
    Ok(ClientDetail {
        client_id: client_id.to_string(),
        deposits: get_deposit_tokens(&connection, client_id)?,
        instances: get_client_instances(&connection, client_id)?,
        payments: crate::app::get_payments(&connection, "client_id", client_id)?,
        bound_peer_id: crate::peers::get_peer_id_for_client(database, client_id)?,
    })
}

fn get_deposit_tokens(connection: &Connection, client_id: &str) -> SqlResult<Vec<DepositToken>> {
    let mut statement = connection.prepare(
        "SELECT id, status, created_at FROM deposit_tokens
         WHERE client_id = ?1 ORDER BY created_at DESC LIMIT ?2",
    )?;
    let tokens = statement
        .query_map(
            rusqlite::params![client_id, crate::app::DETAIL_ROWS as i64],
            |row| {
                Ok(DepositToken {
                    id: row.get(0)?,
                    status: row.get(1)?,
                    created_at: row.get(2)?,
                })
            },
        )?
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
        .query_map(
            rusqlite::params![client_id, crate::app::DETAIL_ROWS as i64],
            |row| {
                Ok(ClientInstance {
                    id: row.get(0)?,
                    name: row.get(1)?,
                })
            },
        )?
        .collect();
    instances
}

impl App {
    /// Open an amount-entry modal to credit or debit the selected client's balance.
    ///
    /// The amount is typed in `ui.DISPLAY_UNIT` -- the same unit the balance column
    /// already shows -- and, on submit, handed to `nodo credit_client`/`debit_client`
    /// (see `src/commands/credit_client.py`). Delegating to the CLI rather than
    /// writing `balance_mu` directly means the same MU conversion and client-existence
    /// check the operator gets from a shell apply here too, with one code path to keep
    /// correct instead of two.
    pub fn open_credit_client(&mut self, decrement: bool) {
        if self.page() != crate::app::Page::Clients {
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
        self.input_mode = crate::app::InputMode::CreditClient;
        self.input.clear();
        self.input_title = format!(
            "{} client {} (amount, {})",
            if decrement { "Debit" } else { "Credit" },
            shorten(&client.id, 18),
            self.money.symbol
        );
        self.credit_client_id = Some(client.id);
        self.credit_client_decrement = decrement;
        self.edit_kind = crate::app::EditKind::Text;
    }

    /// Validate the typed amount and run the credit/debit as a background `nodo`
    /// command, the same way a confirmed `PendingAction` does.
    pub(crate) fn submit_credit_client(&mut self) {
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
            crate::app::CommandKind::Generic,
            label,
            vec![command.to_string(), client_id, amount],
        );
    }
}

/// The clickable id column's `[start, end)` column range on the Clients table. Unlike
/// Peers' fixed-width id column, this one is `Constraint::Min(38)` -- the flexible
/// slot that absorbs whatever width the fixed columns after it (`Balance`, `Last
/// usage`, `Metering`) leave behind, so it has to be derived from the area rather
/// than hardcoded (kept next to `draw` so the two cannot drift apart).
pub fn id_column_x(area: Rect) -> (u16, u16) {
    const OTHER_COLUMNS: u16 = 24 + 20 + 14;
    let start = area.x + 1 /* left border */ + 2 /* "▸ " highlight gutter */;
    let inner_width = area.width.saturating_sub(2); // both borders
    let id_width = inner_width.saturating_sub(2 + OTHER_COLUMNS).max(38);
    (start, start + id_width)
}

/// The clients page: who pays us, and what they are running here.
pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    const MIN_TABLE_HEIGHT: u16 = 6;
    let available = area.height.saturating_sub(MIN_TABLE_HEIGHT);
    let selected = app.clients.selected();
    let detail_source = app.client_detail.as_ref();
    let full = client_detail_lines(&app.money, selected, detail_source, false);
    let detail = if full.len() as u16 + 2 <= available {
        full
    } else {
        client_detail_lines(&app.money, selected, detail_source, true)
    };
    let detail_height = (detail.len() as u16 + 2).min(available);
    let split = Layout::vertical([
        Constraint::Min(MIN_TABLE_HEIGHT),
        Constraint::Length(detail_height),
    ])
    .split(area);

    let clients = app.clients.items.iter().map(|client| {
        Row::new(vec![
            Cell::from(client.id.clone()),
            Cell::from(app.money.format_raw(&client.balance)),
            Cell::from(client.last_usage.clone()),
            // A balance that never moves is the flag doing its job, not a bug.
            Cell::from(if client.unmetered { "never charged" } else { "" })
                .style(Style::default().fg(muted())),
        ])
    });
    let client_table = Table::new(
        clients,
        [
            Constraint::Min(38),
            Constraint::Length(24),
            Constraint::Length(20),
            Constraint::Length(14),
        ],
    )
    .header(header_row(vec![
        "Client ID",
        "Balance",
        "Last usage",
        "Metering",
    ]))
    .block(section_block(
        format!(" CLIENTS • {} known ", app.clients.items.len()),
        accent(),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    let selected_id = selected.map(|client| client.id.clone());
    app.list_area = split[0];
    app.id_column_x = Some(id_column_x(split[0]));
    frame.render_stateful_widget(client_table, split[0], &mut app.clients.state);

    if let Some(id) = selected_id {
        app.id_copy_areas.push((
            id,
            Rect {
                x: split[1].x + 1,
                y: split[1].y + 1,
                width: split[1].width.saturating_sub(2),
                height: 1,
            },
        ));
    }
    crate::ui::draw_card(frame, split[1], "SELECTED CLIENT", detail, accent());
}

fn client_detail_lines(
    money: &Money,
    client: Option<&Client>,
    detail: Option<&ClientDetail>,
    compact: bool,
) -> Vec<Line<'static>> {
    let Some(client) = client else {
        return vec![Line::from(Span::styled(
            "Select a client to inspect its deposits, instances and payments.",
            Style::default().fg(muted()),
        ))];
    };

    let mut lines = vec![metric_line("Client", client.id.clone())];
    if !compact {
        lines.push(metric_line("Balance", money.format_raw(&client.balance)));
        lines.push(metric_line(
            "Last usage",
            nonempty(&client.last_usage, "—").to_string(),
        ));
        if client.unmetered {
            lines.push(Line::from(Span::styled(
                "Never charged (unmetered): one of this node's own dev clients.",
                Style::default().fg(muted()),
            )));
        }
    }

    // A stale card is worse than none: the selection can move between the load and
    // the frame, and a payment shown under the wrong client is a lie about money.
    let Some(detail) = detail.filter(|detail| detail.client_id == client.id) else {
        return lines;
    };

    // The other half of `Peer::local_client_id`: which peer, if any, this client_id
    // was minted for (issue: client↔peer cross-reference).
    lines.push(metric_line(
        "Peer",
        match &detail.bound_peer_id {
            Some(peer_id) => format!("{} (see PEERS)", shorten(peer_id, 24)),
            None => "not bound to a peer".to_string(),
        },
    ));

    if compact {
        lines.push(metric_line(
            "History",
            format!(
                "{} deposit token(s) • {} instance(s) • {} payment(s)",
                detail.deposits.len(),
                detail.instances.len(),
                detail.payments.len()
            ),
        ));
        return lines;
    }

    lines.push(Line::from(""));
    lines.extend(payment_lines(
        money,
        &detail.payments,
        "Payments received",
        "Nothing received from this client yet.",
    ));

    if !detail.deposits.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("Deposit tokens ({})", detail.deposits.len()),
            Style::default().fg(accent()).bold(),
        )));
        for deposit in &detail.deposits {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(status_color(&deposit.status))),
                Span::styled(
                    format!("{:<10}", deposit.status.clone()),
                    Style::default().fg(status_color(&deposit.status)),
                ),
                Span::styled(
                    format!("{}  {}", deposit.created_at.clone(), shorten(&deposit.id, 20)),
                    Style::default().fg(text_colour()),
                ),
            ]));
        }
    }

    if !detail.instances.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("Instances started here ({})", detail.instances.len()),
            Style::default().fg(accent()).bold(),
        )));
        for instance in &detail.instances {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(good())),
                Span::styled(
                    nonempty(&instance.name, "unnamed").to_string(),
                    Style::default().fg(text_colour()).bold(),
                ),
                Span::styled(
                    format!("  {}", shorten(&instance.id, 24)),
                    Style::default().fg(muted()),
                ),
            ]));
        }
    }

    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::fs;
    use std::path::PathBuf;

    fn history_database(dir: &Path) -> PathBuf {
        let path = dir.join("history.sqlite");
        let connection = Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE peer (id TEXT PRIMARY KEY, local_client_id TEXT);
                 CREATE TABLE clients (id TEXT PRIMARY KEY, balance_mu TEXT,
                                    last_usage FLOAT, unmetered INTEGER NOT NULL DEFAULT 0);
                 CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT,
                                    direction TEXT, status TEXT, peer_id TEXT, client_id TEXT,
                                    deposit_token TEXT, ledger TEXT, contract_hash TEXT,
                                    address TEXT, amount_mu TEXT NOT NULL,
                                    created_at DATETIME);
                 CREATE TABLE deposit_tokens (id TEXT PRIMARY KEY, client_id TEXT,
                                    status TEXT, created_at DATETIME);
                 CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT, father_id TEXT);
                 INSERT INTO clients VALUES ('client-1', '500', NULL, 1);
                 INSERT INTO payments (direction, status, client_id, deposit_token, amount_mu, created_at)
                    VALUES ('in', 'accepted', 'client-1', 'token-1', '750', '2026-01-03 10:00:00');
                 INSERT INTO deposit_tokens VALUES ('token-1', 'client-1', 'payed', '2026-01-03 09:59:00');
                 INSERT INTO local_instances VALUES ('instance-1', 'demo', 'client-1');
                 INSERT INTO local_instances VALUES ('instance-2', 'other', 'someone-else');",
            )
            .unwrap();
        path
    }

    fn temp_dir(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("nodo-tui-clients-{name}-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        dir
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
        assert_eq!(detail.bound_peer_id, None, "no peer claims this client yet");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_client_bound_to_a_peer_reports_it() {
        let dir = temp_dir("bound");
        let database = history_database(&dir);
        Connection::open(&database)
            .unwrap()
            .execute(
                "INSERT INTO peer (id, local_client_id) VALUES ('peer-9', 'client-1')",
                [],
            )
            .unwrap();

        let detail = get_client_detail(&database, "client-1").unwrap();

        assert_eq!(detail.bound_peer_id, Some("peer-9".to_string()));

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
}
