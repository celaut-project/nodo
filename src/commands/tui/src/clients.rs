//! The CLIENTS page: who this node's own gateway has issued a client_id to.
//!
//! Split out of `app.rs`/`ui.rs` alongside `peers.rs` and `chat.rs` -- see the module
//! doc on `peers.rs` for why.

use crate::app::{shorten, App, Identifiable, Money, PaymentRow};
use crate::layout_util::Column;
use crate::ui::{fitted_column_x, fitted_table, TextCell, accent, good, metric_line, muted, nonempty, payment_lines, section_block, selected_style, status_color, text_colour};
use ratatui::prelude::*;
use rusqlite::{Connection, Result as SqlResult};
use std::path::Path;

#[derive(Debug, Clone)]
pub struct Client {
    pub id: String,
    pub balance: String,
    pub last_usage: String,
    /// A client this node keeps funded: its balance is topped up to a target whenever
    /// it holds less, so its own dev clients never run dry. An operator wondering why
    /// a balance keeps coming back is owed this word.
    pub unlimited: bool,
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
    // The daemon adds `unlimited` when it starts after an upgrade, so a database this
    // page opens first reads as nobody being unlimited rather than failing the page.
    let mut statement = connection
        .prepare("SELECT id, balance_mu, last_usage, unlimited FROM clients")
        .or_else(|_| connection.prepare("SELECT id, balance_mu, last_usage, 0 FROM clients"))?;
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
                unlimited: row.get::<_, Option<i64>>(3)?.unwrap_or(0) != 0,
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
        payments: crate::app::get_payments(&connection, "account_id", client_id)?,
        bound_peer_id: crate::peers::get_peer_id_for_client(database, client_id)?,
    })
}

fn get_deposit_tokens(connection: &Connection, client_id: &str) -> SqlResult<Vec<DepositToken>> {
    let mut statement = connection.prepare(
        "SELECT id, status, created_at FROM deposit_tokens
         WHERE account_id = ?1 ORDER BY created_at DESC LIMIT ?2",
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
    // Only the live ones; a dead instance is on the DEAD page. The column is missing
    // from a database the node has not migrated yet.
    let alive = if crate::app::column_exists(connection, "local_instances", "killed_at") {
        " AND killed_at IS NULL"
    } else {
        ""
    };
    let mut statement = connection.prepare(&format!(
        "SELECT id, COALESCE(name, '') FROM local_instances
         WHERE father_id = ?1{alive} ORDER BY name LIMIT ?2"
    ))?;
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
        self.credit_is_instance = false;
        self.edit_kind = crate::app::EditKind::Text;
    }

    /// Open the same amount modal for the selected instance: `+` adds to its deposit and
    /// `-` takes from it (`nodo increase_deposit` / `decrease_deposit`). The MU come from
    /// the father of the instance, or go back to it, and the rows of the statement say
    /// that the operator asked.
    pub fn open_credit_instance(&mut self, decrement: bool) {
        if self.page() != crate::app::Page::Instances {
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
        let name = if instance.name.trim().is_empty() {
            shorten(&instance.id, 18)
        } else {
            instance.name.clone()
        };
        self.input_mode = crate::app::InputMode::CreditClient;
        self.input.clear();
        self.input_title = format!(
            "{} balance of instance {} (amount, {})",
            if decrement { "Decrease" } else { "Increase" },
            name,
            self.money.symbol
        );
        self.credit_client_id = Some(instance.id);
        self.credit_client_decrement = decrement;
        self.credit_is_instance = true;
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
        let instance = self.credit_is_instance;
        self.close_input();
        let (verb, subject, command) = match (instance, decrement) {
            (false, false) => ("Credit", "client", "credit_client"),
            (false, true) => ("Debit", "client", "debit_client"),
            (true, false) => ("Increase", "instance", "increase_deposit"),
            (true, true) => ("Decrease", "instance", "decrease_deposit"),
        };
        let label = format!("{verb} {subject} {}", shorten(&client_id, 18));
        self.spawn_command(
            crate::app::CommandKind::Generic,
            label,
            vec![command.to_string(), client_id, amount],
        );
    }
}

/// The CLIENTS table's columns (issue #453): the id and what it holds here outlast
/// when it last spent and whether it is metered, both of which the card repeats.
pub(crate) const CLIENT_COLUMNS: [Column; 4] = [
    Column::new("Client ID", Constraint::Min(38), 12, 0),
    Column::new("Balance", Constraint::Length(24), 8, 1),
    Column::new("Last usage", Constraint::Length(20), 10, 2),
    Column::new("Metering", Constraint::Length(14), 8, 3),
];

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

    let clients = app
        .clients
        .items
        .iter()
        .map(|client| {
            let cells: Vec<TextCell> = vec![
                client.id.clone().into(),
                app.money.format_raw(&client.balance).into(),
                client.last_usage.clone().into(),
                // A balance that keeps coming back is the flag doing its job, not a bug.
                TextCell::from(if client.unlimited { "kept funded" } else { "" })
                    .style(Style::default().fg(muted())),
            ];
            (cells, Style::default())
        })
        .collect();
    let has_selection = app.clients.state.selected().is_some();
    let (client_table, columns) = fitted_table(&CLIENT_COLUMNS, clients, split[0], has_selection);
    let client_table = client_table
        .block(section_block(
            format!(" CLIENTS • {} known ", app.clients.items.len()),
            accent(),
        ))
        .highlight_style(selected_style())
        .highlight_symbol("▸ ");
    let selected_id = selected.map(|client| client.id.clone());
    app.list_area = split[0];
    // Where the id column was actually drawn: it is the flexible one, so its width
    // is whatever the others leave, and it must not claim columns they were drawn in.
    app.id_column_x = fitted_column_x(split[0], &columns, 0, has_selection);
    frame.render_stateful_widget(client_table, split[0], &mut app.clients.state);

    if let Some(id) = selected_id.filter(|_| split[1].height > 2) {
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
        if client.unlimited {
            lines.push(Line::from(Span::styled(
                "Unlimited: the node tops this client up to a target (the node's own dev clients are).",
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
                                    last_usage FLOAT, unlimited INTEGER NOT NULL DEFAULT 0);
                 CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT,
                                    direction TEXT, status TEXT, peer_id TEXT, account_id TEXT,
                                    deposit_token TEXT, ledger TEXT, contract_hash TEXT,
                                    address TEXT, amount_mu TEXT NOT NULL,
                                    created_at DATETIME);
                 CREATE TABLE deposit_tokens (id TEXT PRIMARY KEY, account_id TEXT,
                                    status TEXT, created_at DATETIME);
                 CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT, father_id TEXT);
                 INSERT INTO clients VALUES ('client-1', '500', NULL, 1);
                 INSERT INTO payments (direction, status, account_id, deposit_token, amount_mu, created_at)
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
    fn the_unlimited_flag_reaches_the_table() {
        let dir = temp_dir("unlimited");
        let database = history_database(&dir);

        let clients = get_clients(&database).unwrap();

        assert_eq!(clients.len(), 1);
        assert!(clients[0].unlimited, "a dev client is kept funded");

        let _ = fs::remove_dir_all(&dir);
    }

    fn app_on_instances(location: &str, suspended: Option<&str>) -> crate::app::App {
        use crate::app::{App, Instance, InstanceRevive, Page, StatefulList};
        let mut app = App::default();
        app.tabs.index = Page::ALL.iter().position(|page| *page == Page::Instances).unwrap();
        let instance = Instance {
            id: "inst-1".to_string(),
            name: "demo".to_string(),
            ip: String::new(),
            service: String::new(),
            balance: "0".to_string(),
            virtualizer: "ch".to_string(),
            memory_limit: 0,
            disk_limit: 0,
            vcpus: None,
            usage: crate::app::InstanceUsage::default(),
            location: location.to_string(),
            father_id: String::new(),
            mu_per_minute: None,
            mu_per_hour: None,
            consumption_samples: None,
            consumption_age_secs: None,
            energy_watts: None,
            energy_share: None,
            revive: InstanceRevive { suspended: suspended.map(str::to_string), ..Default::default() },
            age_secs: None,
            client: crate::app::InstanceClient::None,
        };
        app.instances = StatefulList::with_items(vec![instance]);
        app.instances.next();
        app
    }

    #[test]
    fn the_balance_of_an_instance_goes_through_increase_and_decrease_deposit() {
        let mut app = app_on_instances("local", None);
        app.open_credit_instance(false);
        assert_eq!(app.input_mode, crate::app::InputMode::CreditClient);
        assert!(app.credit_is_instance);
        assert!(app.input_title.starts_with("Increase balance of instance demo"));

        app.open_credit_instance(true);
        assert!(app.input_title.starts_with("Decrease balance of instance demo"));
        assert!(app.credit_client_decrement);
    }

    #[test]
    fn a_client_modal_after_an_instance_one_is_a_client_modal_again() {
        let mut app = app_on_instances("local", None);
        app.open_credit_instance(false);
        app.close_input();
        app.tabs.index = crate::app::Page::ALL
            .iter()
            .position(|page| *page == crate::app::Page::Clients)
            .unwrap();
        app.clients = crate::app::StatefulList::with_items(vec![Client {
            id: "c1".to_string(),
            balance: "0".to_string(),
            last_usage: String::new(),
            unlimited: false,
        }]);
        app.clients.next();
        app.open_credit_client(false);
        assert!(!app.credit_is_instance);
    }

    #[test]
    fn hibernating_asks_first_and_names_the_nodo_command() {
        let mut app = app_on_instances("local", None);
        app.open_hibernate_instance_confirm();
        assert_eq!(app.input_mode, crate::app::InputMode::Confirm);
        let action = app.pending_action.clone().unwrap();
        let (label, args) = crate::app::pending_command(action).unwrap();
        assert_eq!(label, "Hibernate instance demo");
        assert_eq!(args, vec!["hibernate".to_string(), "inst-1".to_string()]);
    }

    #[test]
    fn a_remote_or_suspended_instance_is_not_offered_for_hibernation() {
        let mut remote = app_on_instances("some-peer", None);
        remote.open_hibernate_instance_confirm();
        assert_eq!(remote.input_mode, crate::app::InputMode::Normal);
        assert!(remote.pending_action.is_none());

        let mut down = app_on_instances("local", Some("hibernated"));
        down.open_hibernate_instance_confirm();
        assert_eq!(down.input_mode, crate::app::InputMode::Normal);
        assert!(down.status.contains("already"));
    }
}
