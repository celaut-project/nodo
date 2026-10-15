//! The PEERS page: other nodes this one has introduced itself to or heard from.
//!
//! Split out of `app.rs`/`ui.rs` (issue: TUI chat/peers/clients redesign) alongside
//! `clients.rs` and `chat.rs`, since all three pages were being rewritten together
//! anyway. Holds the page's data, its SQL, its rendering and its `App` methods; only
//! what genuinely spans several pages (selection-detail loading, the generic mouse
//! router, the shared `PaymentRow`/`ReputationEvent` history helpers) stays behind in
//! `app.rs`/`ui.rs`.

use crate::app::{shorten, App, CommandKind, Identifiable, Money, PaymentRow, PendingAction};
use crate::app::{format_bytes, format_bytes_compact};
use crate::layout_util::Column;
use crate::peer_resources::{format_benchmark, format_cores, short_arch, Announced};
use crate::ui::{fitted_column_x, fitted_table, TextCell,
    accent, bad, good, metric_line, muted, nonempty, payment_lines,
    reputation_event_lines, section_block, selected_style, text_colour, warn,
};
use prost::Message;
use ratatui::prelude::*;
use ratatui::widgets::Paragraph;
use rusqlite::{Connection, OptionalExtension, Result as SqlResult};
use std::path::Path;

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
    /// The client id THIS node handed *this peer*, when it registered as one of our
    /// own clients (`GenerateClient`, asserting this peer's identity over it) — the
    /// other direction from `remote_client_id`, and the one that *is* a real key
    /// into the local `clients` table (`clients::Client::id`). Empty when this peer
    /// has never asserted itself to this node's gateway.
    pub local_client_id: String,
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
    /// What this peer announced it can run, per architecture (`Peer.resources`,
    /// #459), read from the same stored advertisement as `proof_ids`. Summed on the
    /// Overview and spelled out in the detail card (issue #455).
    pub resources: crate::peer_resources::Announced,
}

/// One `contract_instance` row: the ledger a peer settles on, the contract it
/// charges through, the address it gets paid at, and what one of its units is worth.
#[derive(Debug, Clone, Default)]
pub struct PeerContract {
    /// Ledger tag (e.g. "ergo"), falling back to the raw stored hash when the
    /// ledger row can't be resolved or carries no tag.
    pub ledger: String,
    pub contract_hash: String,
    /// The asset this method settles in: a reserved native symbol ("ERG", "BTC") or a
    /// token's 64-hex id. Part of the identity, not decoration -- on Ergo one contract
    /// is paid in ERG and in every token at the same address, so two rows can differ in
    /// nothing else, and each carries its own `mu_per_unit`.
    pub asset: String,
    pub address: String,
    pub mu_per_unit: String,
}

impl Identifiable for Peer {
    fn id(&self) -> &str {
        &self.id
    }
}

/// Everything the Peers page shows about the selected peer beyond its table row.
///
/// Loaded for the selection rather than for every peer: this is three queries, and
/// the list refreshes every couple of seconds whether or not anyone is reading it.
#[derive(Debug, Clone, Default)]
pub struct PeerDetail {
    pub peer_id: String,
    pub payments: Vec<PaymentRow>,
    pub events: Vec<crate::app::ReputationEvent>,
}

pub fn get_peers(database: &Path) -> SqlResult<Vec<Peer>> {
    // A node that has never been migrated has no database, or one with no `peer`
    // table in it, and neither is a failure to report -- it is a node with no peers
    // yet, which is exactly what an empty list says. `list_peers()` draws the same
    // line ("the 'peer' table does not exist"). Anything past this point is a query
    // that disagrees with a schema that *is* there, which is the case worth raising.
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer") {
        return Ok(Vec::new());
    }
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
                COALESCE(p.remote_client_id, ''),
                COALESCE(p.local_client_id, '')
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
            // own could only ever keep the last one announced (issue #281). Decoded
            // once here for both the proofs and the resources it announced (#455).
            let advertisement = row.get::<_, Option<Vec<u8>>>(3)?;
            let decoded = advertisement
                .as_deref()
                .map(crate::app::protos::Peer::decode);
            let resources = crate::peer_resources::from_decoded(decoded.as_ref());
            let proof_ids = decoded
                .and_then(Result::ok)
                .map(|announced| {
                    announced
                        .reputation_proofs
                        .into_iter()
                        .filter_map(|contract| {
                            // An entry list, not a map: a key resolves to the LAST
                            // entry that carries it (what a map did on a repeat).
                            contract
                                .xattrs
                                .iter()
                                .rev()
                                .find(|entry| entry.key == "token_id")
                                .and_then(|entry| entry.value.clone())
                                .and_then(|value| String::from_utf8(value).ok())
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
                resources,
                reputation_score,
                remote_client_id: row.get(5)?,
                local_client_id: row.get(6)?,
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

/// Every payment *method* a peer has registered. A peer's `contract_instance` rows
/// aren't reachable from the uri join `get_peers` already runs, and before this the TUI
/// surfaced none of it at all (issue #231). A method is ledger + contract + asset, so
/// `token_id` comes back with the rest: without it two methods of one Ergo contract
/// render as the same row twice, at two different rates.
///
/// `contract_instance.ledger` **is** the chain's tag. It used to be `ledger_hash`, a
/// sha3 of a serialized description joined against `ledger(hash, content)` to get the
/// tag back; `refactor(db): a ledger is its tag, so stop storing one` dropped both the
/// column and the table's content, and this query kept asking for them. SQLite rejects
/// the statement at `prepare`, so every peer on the page disappeared rather than every
/// peer's contracts -- see `get_peers`.
fn get_peer_contracts(connection: &Connection, peer_id: &str) -> SqlResult<Vec<PeerContract>> {
    let mut statement = connection.prepare(
        "SELECT ci.contract_hash, ci.ledger, ci.address, ci.mu_per_unit, ci.token_id
         FROM contract_instance ci
         WHERE ci.peer_id = ?1",
    )?;
    let contracts = statement
        .query_map([peer_id], |row| {
            Ok(PeerContract {
                ledger: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                contract_hash: row.get(0)?,
                asset: row.get::<_, Option<String>>(4)?.unwrap_or_default(),
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
pub fn adjust_peer_reputation(database: &Path, peer_id: &str, delta: i64) -> SqlResult<()> {
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

/// What we paid a peer and why its score is where it is.
pub fn get_peer_detail(database: &Path, peer_id: &str) -> SqlResult<PeerDetail> {
    let connection = Connection::open(database)?;
    Ok(PeerDetail {
        peer_id: peer_id.to_string(),
        payments: crate::app::get_payments(&connection, "peer_id", peer_id)?,
        events: crate::app::get_reputation_events(&connection, "peer", peer_id)?,
    })
}

/// Which peer, if any, holds `client_id` as its `local_client_id` -- the reverse of
/// reading `Peer::local_client_id` off a peer row, for the Clients page's own detail
/// card (issue: client↔peer cross-reference). Mirrors
/// `sql_connection.get_peer_id_by_local_client`.
pub fn get_peer_id_for_client(database: &Path, client_id: &str) -> SqlResult<Option<String>> {
    if !database.exists() {
        return Ok(None);
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer") {
        return Ok(None);
    }
    connection
        .query_row(
            "SELECT id FROM peer WHERE local_client_id = ?1",
            [client_id],
            |row| row.get(0),
        )
        .optional()
}

impl App {
    /// Reload the peer list, keeping the reason when there is nothing to show.
    ///
    /// The one place `get_peers` reaches the page, so a query that fails cannot be
    /// swallowed at one call site and reported at another. A failure leaves the last
    /// good list on screen rather than blanking it: a peer this node knew a second
    /// ago is still a peer, and replacing the table with nothing would hide the very
    /// rows the error is about.
    pub(crate) fn refresh_peers(&mut self) {
        match get_peers(&self.paths.database) {
            Ok(peers) => {
                self.peers_error = None;
                self.peers.refresh(peers);
            }
            Err(error) => self.peers_error = Some(error.to_string()),
        }
    }

    /// Ask before re-fetching every peer's announcement and our balance there
    /// (`nodo refresh_peers`): it opens a connection to each peer, which is a lot of
    /// network and disk I/O. Reached by `r`, and by clicking the ⟳ button.
    pub fn open_refresh_peers_confirm(&mut self) {
        if self.page() != crate::app::Page::Peers {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let count = self.peers.items.len();
        self.input_mode = crate::app::InputMode::Confirm;
        self.input_title = format!("Refresh all {count} peers from the network? Heavy on I/O (y/N)");
        self.pending_action = Some(PendingAction::RefreshPeers);
    }

    /// Increase or decrease the selected peer's local reputation score.
    pub fn adjust_selected_peer_reputation(&mut self, delta: i64) {
        if self.page() != crate::app::Page::Peers {
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
                self.refresh_peers();
                // The adjustment is an event like any other; show it without waiting
                // for the next refresh.
                self.load_selection_details();
            }
            Err(error) => self.status = format!("Reputation update failed: {error}"),
        }
    }

    pub fn open_connect(&mut self) {
        self.input_mode = crate::app::InputMode::Connect;
        self.input.clear();
        self.input_title = "Connect peer (host:port)".to_string();
        self.edit_kind = crate::app::EditKind::Text;
    }

    pub(crate) fn connect(&mut self) {
        let target = self.input.trim().to_string();
        let valid_shape = regex::Regex::new(r"^(\[[0-9a-fA-F:]+\]|[^:\s]+):\d{1,5}$")
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

    /// Ask before forgetting a peer: its addresses and contract instances — the same
    /// thing the operator would type. The peer is *forgotten*, not banned: it can
    /// re-introduce itself, or be reconnected with `c`. That is exactly what makes
    /// this useful — a peer whose addresses went stale (say another node claimed one,
    /// see `claim_uri`) is cleared out here.
    pub fn open_disconnect_peer_confirm(&mut self) {
        // Peers only. A client is not forgotten by hand -- it is ours, and expires on
        // its own -- and since the two now have a page each, `d` on Clients is simply
        // not bound rather than answered with an explanation.
        if self.page() != crate::app::Page::Peers {
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
        self.input_mode = crate::app::InputMode::Confirm;
        self.input_title = format!("Forget peer {label}? (y/N)");
        self.pending_action = Some(PendingAction::DisconnectPeer {
            id: peer.id.clone(),
            label,
        });
    }
}

/// The refresh button: a clockwise open-circle arrow.
const REFRESH_BUTTON: &str = "[ \u{27f3} ]";

/// The PEERS table's columns (issue #453): the id, then what this node holds there
/// and the peer's standing, outlast where it is reached and what it announced --
/// both of which the card below spells out.
pub(crate) const PEER_COLUMNS: [Column; 5] = [
    Column::new("Peer ID", Constraint::Length(30), 10, 0),
    Column::new("Endpoints", Constraint::Length(24), 9, 3),
    Column::new("Our balance", Constraint::Length(13), 8, 1),
    Column::new("Rep", Constraint::Length(7), 3, 2),
    Column::new("Reputation proofs", Constraint::Min(20), 8, 4),
];

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    // The card sizes itself to what the selected peer actually has: contracts, the
    // payments made to it, the events behind its score. It yields first when the
    // terminal is short -- a card that squeezed the table off-screen would leave no
    // way to pick the peer it is describing.
    const MIN_TABLE_HEIGHT: u16 = 7;
    let available = area.height.saturating_sub(MIN_TABLE_HEIGHT);
    let selected = app.peers.selected();
    let detail_source = app.peer_detail.as_ref();
    // Prefer the roomy breakdown, but fall back to one line per contract rather
    // than let a short terminal clip the contracts away silently -- an empty
    // card reads as "no contract registered", the exact confusion #231 is about.
    let donation = selected.and_then(|peer| app.donations.for_peer(&peer.id));
    // A failed query replaces the card outright, so it also has to be what the card
    // is *sized* from: sizing to the peer detail and then drawing the error into it
    // clips the one line that says what went wrong.
    let detail = match &app.peers_error {
        Some(error) => peers_unreadable_lines(error),
        None => {
            let full = peer_detail_lines(&app.money, selected, detail_source, donation, false);
            if full.len() as u16 + 2 <= available {
                full
            } else {
                peer_detail_lines(&app.money, selected, detail_source, donation, true)
            }
        }
    };
    let detail_height = (detail.len() as u16 + 2).min(available);
    let split = Layout::vertical([
        Constraint::Min(MIN_TABLE_HEIGHT),
        Constraint::Length(detail_height),
    ])
    .split(area);

    let peers = app
        .peers
        .items
        .iter()
        .map(|peer| {
            let cells: Vec<TextCell> = vec![
                peer.id.clone().into(),
                peer.uris.clone().into(),
                app.money.format_raw(&peer.balance).into(),
                TextCell::from(peer.reputation_score.clone())
                    .style(Style::default().fg(good()).bold()),
                match peer.proof_ids.len() {
                    0 => "none".to_string(),
                    1 => shorten(&peer.proof_ids[0], 18),
                    n => format!("{n} announced"),
                }
                .into(),
            ];
            (cells, Style::default())
        })
        .collect();
    let has_selection = app.peers.state.selected().is_some();
    let (peer_table, columns) = fitted_table(&PEER_COLUMNS, peers, split[0], has_selection);
    let peer_table = peer_table
        .block(section_block(
            match &app.peers_error {
                // Consequence first: an operator reading this row has to learn that the
                // page is not answering before learning what SQLite said about it.
                Some(_) => " PEERS • CANNOT BE READ ".to_string(),
                None => format!(" PEERS • {} connected ", app.peers.items.len()),
            },
            if app.peers_error.is_some() { bad() } else { accent() },
        ))
        .highlight_style(selected_style())
        .highlight_symbol("▸ ");
    let selected_id = selected.map(|peer| peer.id.clone());
    let has_error = app.peers_error.is_some();
    app.list_area = split[0];
    // Where the id column was actually drawn, after any narrower columns gave way.
    app.id_column_x = fitted_column_x(split[0], &columns, 0, has_selection);
    frame.render_stateful_widget(peer_table, split[0], &mut app.peers.state);
    // The ⟳ button sits on the table's top border, right-aligned. Clicking it asks the
    // same confirmation as `r`.
    let button_width = REFRESH_BUTTON.chars().count() as u16;
    if split[0].width > button_width + 4 {
        let area = Rect::new(split[0].right() - button_width - 2, split[0].y, button_width, 1);
        app.peers_refresh_area = area;
        frame.render_widget(
            Paragraph::new(Span::styled(
                REFRESH_BUTTON,
                Style::default().fg(accent()).add_modifier(Modifier::BOLD),
            )),
            area,
        );
    }

    // A query that failed takes the card, not a corner of it. `0 connected` is the
    // screen a new node draws, so an unreadable table that merely looked empty was
    // indistinguishable from a healthy one -- which is how a stale query survived a
    // schema change unnoticed.
    if has_error {
        crate::ui::draw_card(frame, split[1], "PEERS UNREADABLE", detail, bad());
    } else {
        // The full id repeats the table's own first line, so it is worth a precise
        // click target: clicking it copies the id even when the table truncated it
        // (issue: click-to-copy full IDs).
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
        crate::ui::draw_card(frame, split[1], "SELECTED PEER", detail, accent());
    }
}

/// What the card says instead of a peer, when the peer list could not be read.
///
/// Consequence first: the rows on screen are stale and the count cannot be trusted.
/// The database's own words come second -- they are what an operator pastes into an
/// issue, and useless without knowing they matter.
fn peers_unreadable_lines(error: &str) -> Vec<Line<'static>> {
    vec![
        Line::from(Span::styled(
            "This node's peers cannot be listed, so the table above is stale and its \
             count cannot be trusted.",
            Style::default().fg(bad()).bold(),
        )),
        Line::from(Span::styled(
            error.to_string(),
            Style::default().fg(text_colour()),
        )),
        Line::from(Span::styled(
            "`nodo peers` reads the same database, and reports the same failure in full.",
            Style::default().fg(muted()),
        )),
    ]
}

/// What the peer announced it can run, one block per architecture (issue #455): the
/// most one instance there could be granted, and its measured per-core scores. As
/// announced and signed by the peer -- a ceiling it claims, not what is free on it.
fn announced_resource_lines(resources: &Announced) -> Vec<Line<'static>> {
    let offers = match resources {
        Announced::Declared(offers) => offers,
        Announced::Undeclared => {
            return vec![metric_line("Resources", "none announced (adds nothing to the total)")];
        }
        Announced::Unreadable => {
            return vec![Line::from(vec![
                Span::styled(format!("{:<12}", "Resources"), Style::default().fg(muted())),
                Span::styled(
                    "announcement could not be decoded",
                    Style::default().fg(warn()).bold(),
                ),
            ])];
        }
    };
    let unstated = || "not stated".to_string();
    let mut lines = vec![Line::from(Span::styled(
        format!("Announced resources ({} architecture{})", offers.len(), if offers.len() == 1 { "" } else { "s" }),
        Style::default().fg(accent()).bold(),
    ))];
    for offer in offers {
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(good())),
            Span::styled(offer.arch.clone(), Style::default().fg(good()).bold()),
            Span::styled(
                format!(
                    "  {} cores • {} RAM • {} disk",
                    offer.millicores.map(format_cores).unwrap_or_else(unstated),
                    offer.mem_bytes.map(format_bytes).unwrap_or_else(unstated),
                    offer.disk_bytes.map(format_bytes).unwrap_or_else(unstated),
                ),
                Style::default().fg(text_colour()),
            ),
        ]));
        if offer.benchmark.is_empty() {
            lines.push(Line::from(Span::styled(
                "      no benchmark scores announced",
                Style::default().fg(muted()),
            )));
        }
        for (key, value) in &offer.benchmark {
            let (label, value) = format_benchmark(key, *value);
            lines.push(Line::from(vec![
                Span::styled(format!("      {label:<18}"), Style::default().fg(muted())),
                Span::styled(format!("{value} per core"), Style::default().fg(text_colour())),
            ]));
        }
    }
    lines
}

/// [`announced_resource_lines`] in one line, for the compact card.
fn announced_resources_summary(resources: &Announced) -> String {
    match resources {
        Announced::Undeclared => "none announced".to_string(),
        Announced::Unreadable => "announcement unreadable".to_string(),
        Announced::Declared(offers) => offers
            .iter()
            .map(|offer| {
                format!(
                    "{} {}c/{}",
                    short_arch(&offer.arch),
                    offer.millicores.map(format_cores).unwrap_or_else(|| "?".to_string()),
                    offer.mem_bytes.map(format_bytes_compact).unwrap_or_else(|| "?".to_string()),
                )
            })
            .collect::<Vec<_>>()
            .join("  "),
    }
}

/// Full breakdown of the peer highlighted in the peers table: identity, balance,
/// reputation, and every payment contract it has registered. Previously reachable
/// only through a raw sqlite query (issue #231).
///
/// `compact` collapses each contract onto one line for terminals too short for the
/// full card.
/// A peer's advertised rate, as a person reads it.
///
/// On the wire (`ContractRate.mu_per_unit`) the rate is MU per BASE unit -- per nanoERG,
/// per satoshi, per smallest unit of a token -- because between nodes nothing else is
/// needed. A person thinks in whole ERG or BTC, so a native asset's rate is shown per
/// whole unit: the base rate followed by as many zeros as the asset has decimals, which
/// is exact for any size of number. A token's decimals are not on the wire, so its rate
/// stays per base unit and says so.
fn rate_for_a_person(contract: &PeerContract) -> String {
    let rate = contract.mu_per_unit.trim();
    if rate.is_empty() {
        return "rate —".to_string();
    }
    let native = match (contract.asset.as_str(), contract.ledger.as_str()) {
        ("ERG", _) | ("", "ergo") => Some(("ERG", 9)),
        ("BTC", _) | ("", "bitcoin") => Some(("BTC", 8)),
        _ => None,
    };
    match native {
        Some((symbol, decimals)) if rate.chars().all(|c| c.is_ascii_digit()) && rate != "0" => {
            format!("1 {} = {}{} MU", symbol, rate, "0".repeat(decimals))
        }
        _ => format!("1 base unit = {} MU", rate),
    }
}

fn peer_detail_lines(
    money: &Money,
    peer: Option<&Peer>,
    detail: Option<&PeerDetail>,
    donation: Option<(f64, f64)>,
    compact: bool,
) -> Vec<Line<'static>> {
    let Some(peer) = peer else {
        return vec![Line::from(Span::styled(
            "Select a peer to inspect its endpoints, reputation and payment contracts.",
            Style::default().fg(muted()),
        ))];
    };

    // The full id is worth repeating even in compact mode: the table truncates it.
    let mut lines = vec![metric_line("Peer", peer.id.clone())];
    if !compact {
        lines.push(metric_line(
            "Endpoints",
            nonempty(&peer.uris, "—").to_string(),
        ));
        lines.push(metric_line("Our balance", money.format_raw(&peer.balance)));
        // Our id inside *their* node, which is what an operator needs when reading
        // the other side's logs. Not a client of ours -- see the doc on the field.
        lines.push(metric_line(
            "Our client id there",
            nonempty(&peer.remote_client_id, "not registered").to_string(),
        ));
        // The other direction: this node's own `clients` row for whoever this peer
        // registered as (issue: client↔peer cross-reference). Real, unlike the line
        // above -- see `Peer::local_client_id`'s doc.
        lines.push(metric_line(
            "Client relationship",
            if peer.local_client_id.is_empty() {
                "no client relationship".to_string()
            } else {
                format!(
                    "client {} (see CLIENTS)",
                    shorten(&peer.local_client_id, 24)
                )
            },
        ));
        // The score is ours, first-hand, keyed by this peer's public key. The proofs
        // below are the peer's own published opinions about other nodes, as it
        // announced them -- unverified here (issue #281).
        lines.push(metric_line(
            "Reputation",
            format!(
                "{}  •  {}",
                peer.reputation_score,
                match peer.proof_ids.len() {
                    0 => "no proof announced".to_string(),
                    n => format!("{n} proof(s) announced"),
                }
            ),
        ));
        for proof_id in &peer.proof_ids {
            lines.push(Line::from(vec![
                Span::styled("      proof  ", Style::default().fg(muted())),
                Span::styled(shorten(proof_id, 46), Style::default().fg(text_colour())),
            ]));
        }
        // What this peer's on-chain donations earn it here, and only here: the credit
        // is computed with *this* node's list of whose contributions it recognises, so
        // it is this node's opinion and not a property of the peer. A peer with none
        // is not being penalised -- the term is a bonus only.
        lines.push(metric_line(
            "Donation credit",
            match donation {
                Some((bonus, term)) => format!(
                    "{bonus:.4}  •  +{term:.4} to its score (beats a price up to {:.0}% higher)",
                    (term.exp() - 1.0) * 100.0
                ),
                None => "none counted by this node".to_string(),
            },
        ));
        lines.push(Line::from(""));
        lines.extend(announced_resource_lines(&peer.resources));
        lines.push(Line::from(""));
    }

    if compact {
        lines.push(metric_line("Resources", announced_resources_summary(&peer.resources)));
    }

    // Same guard as the client card: the selection can move between the load and the
    // frame, and payments shown under the wrong peer would be a lie about money.
    let history = detail.filter(|detail| detail.peer_id == peer.id);
    if let Some(history) = history {
        if compact {
            lines.push(metric_line(
                "History",
                format!(
                    "{} payment(s) • {} reputation event(s)",
                    history.payments.len(),
                    history.events.len()
                ),
            ));
        } else {
            lines.extend(payment_lines(
                money,
                &history.payments,
                "Payments made to this peer",
                "Nothing paid to this peer yet.",
            ));
            lines.push(Line::from(""));
            lines.extend(reputation_event_lines(&history.events));
            lines.push(Line::from(""));
        }
    }

    if peer.contracts.is_empty() {
        lines.push(Line::from(Span::styled(
            "No payment method registered for this peer.",
            Style::default().fg(warn()),
        )));
        return lines;
    }

    lines.push(Line::from(Span::styled(
        format!("Payment methods ({})", peer.contracts.len()),
        Style::default().fg(accent()).bold(),
    )));
    for contract in &peer.contracts {
        // The asset names the money: on Ergo one contract is paid in ERG and in every
        // token at the same address, so the ledger alone would label two different
        // rates identically. A 64-hex token id is shortened; a symbol is not.
        let asset = shorten(nonempty(&contract.asset, &contract.ledger.to_uppercase()), 12);
        if compact {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(good())),
                Span::styled(contract.ledger.clone(), Style::default().fg(good()).bold()),
                Span::styled(
                    format!(
                        "  {}  {}  {}  {}",
                        asset,
                        shorten(&contract.contract_hash, 14),
                        shorten(nonempty(&contract.address, "—"), 14),
                        rate_for_a_person(contract)
                    ),
                    Style::default().fg(text_colour()),
                ),
            ]));
            continue;
        }
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(good())),
            Span::styled(contract.ledger.clone(), Style::default().fg(good()).bold()),
            Span::styled(
                format!("  {}  contract {}", asset, shorten(&contract.contract_hash, 24)),
                Style::default().fg(text_colour()),
            ),
        ]));
        lines.push(Line::from(vec![
            Span::styled("      address  ", Style::default().fg(muted())),
            Span::styled(
                shorten(nonempty(&contract.address, "—"), 46),
                Style::default().fg(text_colour()),
            ),
        ]));
        lines.push(Line::from(vec![
            Span::styled("      rate     ", Style::default().fg(muted())),
            Span::styled(
                // What this peer says its money buys in ITS MU. This is what makes a
                // price it quotes convertible into money we understand, so it is
                // stated as an equation rather than as a bare number.
                rate_for_a_person(contract),
                Style::default().fg(text_colour()),
            ),
        ]));
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{pending_command, App, InputMode, Page, StatefulList};
    use rusqlite::Connection;
    use std::fs;
    use std::path::PathBuf;

    /// The same terse table-source reader `app.rs`'s own schema-drift tests use, so a
    /// fixture here is checked against the exact SQL the node's own migration runs
    /// rather than a hand-written approximation of it.
    fn migration_table(name: &str) -> String {
        let source = include_str!("../../../database/migrate.py");
        let needle = format!("\"{name}\": '''");
        let start = source
            .find(&needle)
            .unwrap_or_else(|| panic!("no '{name}' table in migrate.py"))
            + needle.len();
        let rest = &source[start..];
        let end = rest
            .find("'''")
            .unwrap_or_else(|| panic!("the '{name}' table's SQL is unterminated"));
        rest[..end].to_string()
    }

    /// A database built from the node's *own* schema, holding one peer and whatever
    /// contract instances the caller asks for.
    fn peer_database(dir: &Path, instances: &[(&str, &str, &str)]) -> PathBuf {
        let path = dir.join("database.sqlite");
        let connection = Connection::open(&path).unwrap();
        for table in ["peer", "uri", "ledger", "contract_instance"] {
            connection.execute_batch(&migration_table(table)).unwrap();
        }
        connection
            .execute_batch(
                "INSERT INTO peer (id, advertisement, remote_client_id, balance_mu,
                                   reputation_score)
                 VALUES ('peer-1', NULL, 'cli-7f3a', '1000', 7);",
            )
            .unwrap();
        for (contract_hash, ledger, address) in instances {
            connection
                .execute(
                    "INSERT INTO contract_instance (address, ledger, contract_hash,
                                                    token_id, peer_id, mu_per_unit)
                     VALUES (?1, ?2, ?3, 'ERG', 'peer-1', '500')",
                    rusqlite::params![address, ledger, contract_hash],
                )
                .unwrap();
        }
        path
    }

    /// The regression: against the schema the node actually creates, the page lists
    /// the peers that are in it.
    ///
    /// `get_peers` was reaching for `contract_instance.ledger_hash` and a
    /// `ledger(hash, content)` join, both of which `refactor(db): a ledger is its tag`
    /// removed. SQLite rejects that at `prepare`, the whole call returns `Err`, and
    /// `unwrap_or_default()` turned it into an empty list -- so the page said "0
    /// connected" about a node with peers, which is what `nodo peers` was listing all
    /// along (issue #414).
    #[test]
    fn peers_are_listed_against_the_schema_the_node_actually_creates() {
        let dir = std::env::temp_dir().join("nodo-tui-test-real-schema");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let database = peer_database(&dir, &[("contract-hash-1", "ergo", "addr-1")]);

        let peers = get_peers(&database).expect("the query must survive the real schema");

        assert_eq!(peers.len(), 1, "a peer in the database is a peer on the page");
        assert_eq!(peers[0].id, "peer-1");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn peer_contracts_name_the_ledger_by_its_tag() {
        // Peers only ever name a ledger by tag, and the column now *is* the tag --
        // there is no hash left to resolve it from.
        let dir = std::env::temp_dir().join("nodo-tui-test-ledger-tag");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let database = peer_database(&dir, &[("contract-hash-1", "ergo", "addr-1")]);

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
    fn a_native_rate_is_shown_per_whole_unit_and_a_token_rate_per_base_unit() {
        // The wire carries MU per base unit; a person reads whole ERG / BTC.
        let contract = |ledger: &str, asset: &str, rate: &str| PeerContract {
            ledger: ledger.to_string(),
            asset: asset.to_string(),
            mu_per_unit: rate.to_string(),
            ..Default::default()
        };
        assert_eq!(rate_for_a_person(&contract("ergo", "ERG", "1")), "1 ERG = 1000000000 MU");
        assert_eq!(rate_for_a_person(&contract("bitcoin", "BTC", "1400000")), "1 BTC = 140000000000000 MU");
        assert_eq!(rate_for_a_person(&contract("ergo", "", "2")), "1 ERG = 2000000000 MU");
        assert_eq!(
            rate_for_a_person(&contract("ergo", &"ab".repeat(32), "20000000")),
            "1 base unit = 20000000 MU"
        );
        assert_eq!(rate_for_a_person(&contract("ergo", "ERG", "")), "rate —");
    }

    #[test]
    fn every_contract_instance_of_a_peer_is_returned() {
        // The pre-#231 lookup could only ever surface a single instance.
        let dir = std::env::temp_dir().join("nodo-tui-test-multi-contract");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let database = peer_database(
            &dir,
            &[
                ("contract-a", "ergo", "addr-a"),
                ("contract-b", "bitcoin", "addr-b"),
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
        assert_eq!(peers[0].local_client_id, "", "never asserted itself here");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_peer_bound_as_one_of_our_clients_reports_it() {
        let dir = std::env::temp_dir().join("nodo-tui-test-local-client");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let database = peer_database(&dir, &[]);
        Connection::open(&database)
            .unwrap()
            .execute(
                "UPDATE peer SET local_client_id = 'client-9' WHERE id = 'peer-1'",
                [],
            )
            .unwrap();

        let peers = get_peers(&database).unwrap();
        assert_eq!(peers[0].local_client_id, "client-9");
        assert_eq!(
            get_peer_id_for_client(&database, "client-9").unwrap(),
            Some("peer-1".to_string())
        );
        assert_eq!(get_peer_id_for_client(&database, "nobody").unwrap(), None);
        let _ = fs::remove_dir_all(&dir);
    }

    /// A failure must leave a reason behind, or the page is back to claiming a node
    /// with peers has none.
    #[test]
    fn a_query_that_fails_is_recorded_rather_than_rendered_as_an_empty_network() {
        let dir = std::env::temp_dir().join("nodo-tui-test-peers-error");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        // A `peer` table with none of the columns the query reads: the same shape of
        // failure a schema change produces.
        let database = dir.join("database.sqlite");
        Connection::open(&database)
            .unwrap()
            .execute_batch("CREATE TABLE peer (id TEXT PRIMARY KEY);")
            .unwrap();

        let mut app = App::default();
        app.peers_error = None;
        app.paths.database = database;
        app.refresh_peers();

        assert!(
            app.peers_error.is_some(),
            "a failed peer query must say so rather than render as zero peers"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    mod forgetting_a_peer {
        use super::*;

        fn peer(id: &str) -> Peer {
            Peer {
                id: id.to_string(),
                uris: "10.0.0.1:8080".to_string(),
                balance: "0".to_string(),
                remote_client_id: String::new(),
                local_client_id: String::new(),
                proof_ids: Vec::new(),
                reputation_score: "0".to_string(),
                contracts: Vec::new(),
                resources: Default::default(),
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

    /// The history behind a peer, read straight out of SQLite.
    ///
    /// A card that silently renders nothing looks exactly like a peer with no
    /// history -- the confusion issue #231 was about -- so this is exercised against
    /// a real database rather than through the widgets.
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
                     CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT,
                                        direction TEXT, status TEXT, peer_id TEXT, client_id TEXT,
                                        deposit_token TEXT, ledger TEXT, contract_hash TEXT,
                                        address TEXT, amount_mu TEXT NOT NULL,
                                        created_at DATETIME);
                     CREATE TABLE reputation_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                        subject_kind TEXT, subject_id TEXT, amount INTEGER,
                                        reason TEXT, score_after INTEGER, created_at DATETIME);
                     INSERT INTO peer VALUES ('peer-1', '1000', NULL, 7, 3);
                     INSERT INTO payments (tx_id, direction, status, peer_id, amount_mu, created_at)
                        VALUES ('tx-old', 'out', 'communicated', 'peer-1', '1000', '2026-01-01 10:00:00');
                     INSERT INTO payments (tx_id, direction, status, peer_id, amount_mu, created_at)
                        VALUES ('tx-new', 'out', 'unacknowledged', 'peer-1', '2000', '2026-01-02 10:00:00');
                     INSERT INTO reputation_events (subject_kind, subject_id, amount, reason, score_after, created_at)
                        VALUES ('peer', 'peer-1', -100, 'payment_unacknowledged', -93, '2026-01-02 10:00:01');
                     INSERT INTO reputation_events (subject_kind, subject_id, amount, reason, score_after, created_at)
                        VALUES ('service', 'peer-1', -100, 'instance_lost', -100, '2026-01-02 10:00:02');",
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
