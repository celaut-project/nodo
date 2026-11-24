//! The CHAT page: free-text conversations (issue #431), shown as a persistent sidebar
//! + conversation pane (Telegram/WhatsApp-shaped). See the module doc on `peers.rs`
//! for why this lives in its own file.
//!
//! There is one kind of conversation. A client opens it against a node, and it lives
//! on that node. Its name is `client_id#number`: the "ref". Two sides see the same
//! conversation:
//!
//! - **Served here** (`opened_by_us` is false): one of this node's clients opened it
//!   with this node. The operator of this node answers it.
//! - **A mirror** (`opened_by_us` is true): this node is the client. The rows are a
//!   local copy of a conversation that lives on another node.
//!
//! The page reads both from the view `chat_threads` and shows them in one list. The
//! list is grouped by peer. A served conversation of a client with no peer has no
//! peer: it goes in the last group.
//!
//! `chat_messages.from_operator` is the same on both sides: 1 if the operator of the
//! serving node wrote it, 0 if the client did. So "us" is `from_operator = 1` on a
//! served conversation and `from_operator = 0` on a mirror.
//!
//! Opening a chat is peer, then topic, then message. The topic is always required.
//! Each run of the wizard opens a new conversation.

use crate::app::{
    get_service_command, shorten, table_exists, App, CommandKind, EditKind, Identifiable,
    InputMode, Page,
};
use crate::layout_util::Column;
use crate::peers::Peer;
use crate::ui::{
    accent, bad, centered_rect, fitted_column_x, fitted_table, good, muted, TextCell, popup_background, section_block,
    selected_style, text_colour, warn, wrapped,
};
use ratatui::prelude::*;
use ratatui::widgets::{Block, BorderType, Clear, Paragraph, Wrap};
use prost::Message;
use rusqlite::{Connection, Result as SqlResult};
use std::path::Path;

/// One `chat_messages` row of a single conversation, as the conversation pane shows
/// it. `ts` arrives already formatted -- the message's own clock, not when this node
/// happened to receive it.
#[derive(Debug, Clone)]
pub struct ChatMessageRow {
    pub from_us: bool,
    pub body: String,
    pub ts: String,
    /// A service shared in this message, drawn as a card under its text.
    pub service: Option<SharedService>,
}

/// A service shared in a chat message (issue #438): `ChatMessage.service`, the
/// service's own `Metadata`, as `chat_messages.service_metadata` stores it,
/// with the registry id the node derived from it (`service_id`, see
/// `src/utils/verify.py::registry_service_id`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SharedService {
    /// What `nodo get`/`nodo execute` take here: the Metadata's hash of this node's
    /// registry hash type. `None` when it carries no hash of that type, and then
    /// the card has no buttons -- any other hash would name nothing in this registry.
    pub id: Option<String>,
    pub tags: Vec<String>,
    /// The hash types it carries a digest for, by name (`sha3_256`, ...).
    pub hash_types: Vec<String>,
    /// `Metadata.format`'s tags, when the sender's Metadata has any.
    pub format: Vec<String>,
    pub reputation_proofs: usize,
}

impl SharedService {
    /// Decoded from a stored row. No Metadata, no card; Metadata that no longer
    /// decodes is still a card, just one with nothing but its id on it.
    fn from_row(id: Option<String>, metadata: Option<Vec<u8>>) -> Option<Self> {
        let metadata = metadata?;
        let metadata = crate::app::protos::Metadata::decode(&*metadata).unwrap_or_default();
        let hashtag = metadata.hashtag.unwrap_or_default();
        Some(SharedService {
            id: id.filter(|id| !id.trim().is_empty()),
            tags: hashtag.tag.into_iter().filter(|tag| !tag.trim().is_empty()).collect(),
            hash_types: hashtag.hash.iter().map(|hash| hash_type_name(&hash.r#type)).collect(),
            format: metadata.format.map(|format| format.tags).unwrap_or_default(),
            reputation_proofs: metadata.reputation_proofs.len(),
        })
    }

    /// What its Get/Execute act on, if this node can name it at all.
    pub fn actionable(&self) -> Option<ChatService> {
        Some(ChatService { id: self.id.clone()?, tags: self.tags.clone() })
    }

    /// The card's third line: what the Metadata says beyond tags and id.
    fn details(&self) -> String {
        let mut parts = self.hash_types.clone();
        parts.extend(self.format.iter().cloned());
        match self.reputation_proofs {
            0 => {}
            1 => parts.push("1 reputation proof".to_string()),
            count => parts.push(format!("{count} reputation proofs")),
        }
        parts.join(" · ")
    }
}

/// A `Metadata.HashTag.Hash.type` by the name `hashing.HASH` takes
/// (`src/utils/hashing.py::HASH_SPECS`), or its first bytes in hex if unknown.
fn hash_type_name(hash_type: &[u8]) -> String {
    const KNOWN: [(&str, &str); 4] = [
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "sha2_256"),
        ("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a", "sha3_256"),
        ("46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f", "shake_256"),
        ("0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8", "blake2b_256"),
    ];
    let hex: String = hash_type.iter().map(|byte| format!("{byte:02x}")).collect();
    KNOWN
        .iter()
        .find(|(id, _)| *id == hex)
        .map(|(_, name)| name.to_string())
        .unwrap_or_else(|| shorten(&hex, 8))
}

/// A service a card's buttons act on, or the one attached to a message being
/// composed: an id this node's registry knows it by, and its tags.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatService {
    pub id: String,
    pub tags: Vec<String>,
}

impl ChatService {
    /// What the card and a confirmation call it: its first tag, or its id.
    pub fn label(&self) -> String {
        self.tags
            .iter()
            .find(|tag| !tag.trim().is_empty())
            .cloned()
            .unwrap_or_else(|| shorten(&self.id, 18))
    }
}

/// One of a service card's buttons.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChatCardAction {
    /// `nodo get <id>`, exactly as `g` on SERVICES runs it.
    Get(ChatService),
    /// `nodo execute <id>`, behind the same spend confirmation as `e` on SERVICES.
    Execute(ChatService),
}


/// One row in the CHAT sidebar: one row of the view `chat_threads`.
#[derive(Debug, Clone)]
pub struct ChatEntry {
    /// The ref, `client_id#number`. It is stable across refreshes, which lets
    /// `StatefulList` keep the selection when the periodic reload replaces every entry.
    pub key: String,
    pub client_id: String,
    pub number: i64,
    /// The peer this conversation is grouped under. `None` for a client with no peer.
    pub peer_id: Option<String>,
    /// True if this node is the client (a mirror). False if it is served here.
    pub opened_by_us: bool,
    /// Empty if the conversation has no topic.
    pub topic: String,
    /// `None` while open. Closing is local bookkeeping only: nothing here means the
    /// other side agreed, or was even told.
    pub closed_at: Option<String>,
    /// Its most recent message's timestamp, or (if it has none) when it was opened.
    /// This is what the sidebar sorts by inside a group.
    pub last_ts: i64,
    /// True if the last message is not from the operator of the serving node. On a
    /// served conversation the client wrote last: it waits for us. On a mirror our own
    /// message is last: it waits for the other node.
    pub waiting: bool,
}

impl Identifiable for ChatEntry {
    fn id(&self) -> &str {
        &self.key
    }
}

impl ChatEntry {
    /// The conversation is open and the client wrote last: this node owes an answer.
    pub fn waits_for_us(&self) -> bool {
        self.waiting && !self.opened_by_us && self.closed_at.is_none()
    }

    /// The conversation is open and our message is last: the other node owes an answer.
    pub fn waits_for_peer(&self) -> bool {
        self.waiting && self.opened_by_us && self.closed_at.is_none()
    }

    /// The name of the other side: the peer, or the client if it has no peer.
    pub fn them_name(&self) -> String {
        match &self.peer_id {
            Some(peer_id) => short_peer_name(peer_id),
            None => format!("client {}", short_peer_name(&self.client_id)),
        }
    }

    /// A short form of `them_name` for the sidebar: a client with no peer is `@abc...xyz`.
    fn sidebar_name(&self) -> String {
        match &self.peer_id {
            Some(peer_id) => short_peer_name(peer_id),
            None => format!("@{}", short_peer_name(&self.client_id)),
        }
    }

    /// The full id behind `them_name`, for click-to-copy.
    pub fn them_id(&self) -> &str {
        self.peer_id.as_deref().unwrap_or(&self.client_id)
    }
}

/// What a docked `ComposeChatMessage` box will do with the body once it is sent --
/// which of the two `nodo` subcommands to shell out to, and with what.
#[derive(Debug, Clone)]
pub enum ChatCompose {
    /// Step 3 of the new-chat wizard: `chat_open <peer> <topic> --message <body>`.
    NewConversation { peer_id: String, topic: String },
    /// A reply in an open conversation, of either kind: `chat_reply <ref> <body>`.
    /// `who` is only the name the compose title shows.
    Reply { conversation_ref: String, who: String },
}

/// The conversations on the CHAT page, from the view `chat_threads`. The list is
/// grouped by peer: the group with the newest message is first, the group of clients
/// with no peer is last, and inside a group the newest conversation is first.
pub fn load_entries(database: &Path) -> Result<Vec<ChatEntry>, String> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database).map_err(|error| error.to_string())?;
    if !has_view(&connection, "chat_threads") {
        return Ok(Vec::new());
    }
    let mut statement = connection
        .prepare(
            "SELECT ref, client_id, number, peer_id, opened_by_us, COALESCE(topic, ''),
                    closed_at,
                    CASE WHEN last_ts = 0
                         THEN COALESCE(CAST(strftime('%s', opened_at) AS INTEGER), 0)
                         ELSE last_ts END,
                    messages > 0 AND last_from_operator = 0
             FROM chat_threads",
        )
        .map_err(|error| error.to_string())?;
    let mut entries = statement
        .query_map([], |row| {
            Ok(ChatEntry {
                key: row.get(0)?,
                client_id: row.get(1)?,
                number: row.get(2)?,
                peer_id: row.get(3)?,
                opened_by_us: row.get(4)?,
                topic: row.get(5)?,
                closed_at: row.get(6)?,
                last_ts: row.get(7)?,
                waiting: row.get(8)?,
            })
        })
        .map_err(|error| error.to_string())?
        .collect::<SqlResult<Vec<_>>>()
        .map_err(|error| error.to_string())?;
    group_by_peer(&mut entries);
    Ok(entries)
}

/// Sort the entries so that the conversations of one peer are together.
fn group_by_peer(entries: &mut [ChatEntry]) {
    let mut newest: std::collections::HashMap<Option<String>, i64> = Default::default();
    for entry in entries.iter() {
        let slot = newest.entry(entry.peer_id.clone()).or_insert(i64::MIN);
        *slot = (*slot).max(entry.last_ts);
    }
    entries.sort_by(|a, b| {
        // `false` sorts first: a peer before the group with no peer.
        (a.peer_id.is_none(), std::cmp::Reverse(newest[&a.peer_id]), &a.peer_id)
            .cmp(&(b.peer_id.is_none(), std::cmp::Reverse(newest[&b.peer_id]), &b.peer_id))
            .then(b.last_ts.cmp(&a.last_ts))
            .then(a.key.cmp(&b.key))
    });
}

/// Whether the database has a table or a view with this name.
fn has_view(connection: &Connection, name: &str) -> bool {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?1",
            [name],
            |_| Ok(()),
        )
        .is_ok()
}

/// One conversation's messages, oldest first. `from_us` follows the side: on a
/// served conversation the operator is us, on a mirror the client (this node) is us.
pub fn get_conversation_messages(
    database: &Path,
    client_id: &str,
    number: i64,
    opened_by_us: bool,
) -> SqlResult<Vec<ChatMessageRow>> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !table_exists(&connection, "chat_messages") {
        return Ok(Vec::new());
    }
    let mut statement = connection.prepare(
        "SELECT from_operator, body, ts, service_id, service_metadata
         FROM chat_messages
         WHERE client_id = ?1 AND conversation = ?2
         ORDER BY sequence ASC",
    )?;
    let messages = statement
        .query_map(rusqlite::params![client_id, number], |row| {
            let ts: i64 = row.get(2)?;
            let from_operator: bool = row.get(0)?;
            Ok(ChatMessageRow {
                from_us: from_operator != opened_by_us,
                body: row.get(1)?,
                ts: crate::app::format_unix_timestamp(ts),
                service: SharedService::from_row(row.get(3)?, row.get(4)?),
            })
        })?
        .collect();
    messages
}

impl App {
    /// Reload the CHAT sidebar. The counterpart of `refresh_peers`: a failure leaves
    /// the last good list on screen and says why, rather than rendering as an empty
    /// inbox.
    pub(crate) fn refresh_chat(&mut self) {
        match load_entries(&self.paths.database) {
            Ok(entries) => {
                self.conversations_error = None;
                self.conversations.refresh(entries);
            }
            Err(error) => self.conversations_error = Some(error),
        }
    }

    // --- New chat wizard: peer -> topic -> message --------------------------

    /// Step 1: start a new chat by picking a peer. `nodo chat_open` needs the peer
    /// named up front, and it opens a new conversation each time.
    pub fn open_new_chat_wizard(&mut self) {
        if self.page() != Page::Chat {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        self.chat_wizard_peer_filter.clear();
        self.chat_wizard_peer_index = 0;
        self.input_mode = InputMode::PickChatPeer;
        self.input_title = "New chat — pick a peer".to_string();
        self.status = "Type to filter • ↑/↓ choose • Enter next • Esc cancel".to_string();
    }

    /// Peers matching the wizard's typed filter, in table order -- everything when
    /// nothing has been typed yet.
    pub(crate) fn filtered_chat_peers(&self) -> Vec<&Peer> {
        let filter = self.chat_wizard_peer_filter.trim().to_lowercase();
        self.peers
            .items
            .iter()
            .filter(|peer| filter.is_empty() || peer.id.to_lowercase().contains(&filter))
            .collect()
    }

    pub fn move_chat_peer_selection(&mut self, delta: i32) {
        let count = self.filtered_chat_peers().len();
        if count == 0 {
            self.chat_wizard_peer_index = 0;
            return;
        }
        let current = self.chat_wizard_peer_index as i32;
        self.chat_wizard_peer_index = (current + delta).rem_euclid(count as i32) as usize;
    }

    /// The filter narrowed or widened, so whatever was highlighted may no longer
    /// exist -- back to the top rather than an index that now names someone else.
    pub(crate) fn chat_peer_filter_changed(&mut self) {
        self.chat_wizard_peer_index = 0;
    }

    /// Step 2: the peer is picked. Ask for the topic: it is always required.
    pub(crate) fn submit_chat_peer_pick(&mut self) {
        let Some(peer_id) = self
            .filtered_chat_peers()
            .get(self.chat_wizard_peer_index)
            .map(|peer| peer.id.clone())
        else {
            self.status = "No peer matches; type less, or Esc to cancel".to_string();
            return;
        };
        self.input_mode = InputMode::NewChatTopic;
        self.input.clear();
        self.input_title = format!("New chat with {} — topic", shorten(&peer_id, 18));
        self.chat_wizard_peer_id = Some(peer_id);
        self.edit_kind = EditKind::Text;
    }

    pub(crate) fn submit_new_chat_topic(&mut self) {
        let Some(peer_id) = self.chat_wizard_peer_id.clone() else {
            self.close_input();
            return;
        };
        let topic = self.input.trim().to_string();
        if topic.is_empty() {
            self.status = "Type a topic first".to_string();
            return;
        }
        self.open_chat_compose(ChatCompose::NewConversation { peer_id, topic });
    }

    /// Step 3: a real, possibly multi-line first message -- docked in the
    /// conversation pane rather than a centered popup, per the new chat layout.
    fn open_chat_compose(&mut self, target: ChatCompose) {
        self.input_title = match &target {
            ChatCompose::NewConversation { peer_id, topic } => {
                format!("Message to {} — {}", shorten(peer_id, 18), topic)
            }
            ChatCompose::Reply { who, .. } => format!("Reply to {who}"),
        };
        self.chat_compose = Some(target);
        self.input.clear();
        self.chat_attachment = None;
        self.edit_kind = EditKind::Text;
        self.back_to_compose();
    }

    /// Reply in the selected conversation, of either kind. `nodo chat_reply` decides
    /// what to do: answer as the operator, or send to the peer.
    pub fn open_reply_prompt(&mut self) {
        if self.page() != Page::Chat {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(entry) = self.conversations.selected().cloned() else {
            self.status = "Select a chat first".to_string();
            return;
        };
        if entry.closed_at.is_some() {
            self.status = "Closed; press R to reopen before replying".to_string();
            return;
        }
        self.open_chat_compose(ChatCompose::Reply {
            who: entry.them_name(),
            conversation_ref: entry.key,
        });
    }

    pub(crate) fn submit_chat_compose(&mut self) {
        let Some(target) = self.chat_compose.clone() else {
            self.close_input();
            return;
        };
        // A trailing newline is just where the cursor was, not part of the message;
        // interior ones are the point of a multi-line compose box and stay.
        let body = self.input.trim_end_matches('\n').to_string();
        // An attached service is a message on its own (issue #438).
        let attachment = self.chat_attachment.clone();
        if body.trim().is_empty() && attachment.is_none() {
            self.status = "Type a message first".to_string();
            return;
        }
        self.close_input();
        let (label, args) = chat_compose_command(target, body, attachment.as_ref());
        self.spawn_command(CommandKind::Generic, label, args);
    }

    /// `Ctrl+A` or the Attach button: pick one of this node's services to share.
    pub fn open_chat_service_picker(&mut self) {
        if self.input_mode != InputMode::ComposeChatMessage {
            return;
        }
        // Row 0 is "no attachment"; start on whatever is attached now.
        self.chat_service_index = self
            .chat_attachment
            .as_ref()
            .and_then(|attached| {
                self.services.items.iter().position(|service| service.id == attached.id)
            })
            .map(|index| index + 1)
            .unwrap_or(0);
        self.input_mode = InputMode::PickChatService;
        self.status = "↑/↓ choose • Enter attach • Esc back to the message".to_string();
    }

    pub fn move_chat_service_selection(&mut self, delta: i32) {
        let count = self.services.items.len() as i32 + 1;
        self.chat_service_index =
            (self.chat_service_index as i32 + delta).rem_euclid(count) as usize;
    }

    /// Attach the picked service (or none) and go back to the message, which is
    /// kept exactly as typed.
    pub(crate) fn submit_chat_service_pick(&mut self) {
        self.chat_attachment = self
            .chat_service_index
            .checked_sub(1)
            .and_then(|index| self.services.items.get(index))
            .map(|service| ChatService {
                id: service.id.clone(),
                // `—` is how the services table spells "no tag".
                tags: Some(service.tag.clone())
                    .filter(|tag| !tag.trim().is_empty() && tag != "—")
                    .into_iter()
                    .collect(),
            });
        self.back_to_compose();
    }

    pub fn back_to_compose(&mut self) {
        self.input_mode = InputMode::ComposeChatMessage;
        self.status =
            "Enter: newline • Ctrl+Enter / Alt+Enter / Send: send • Ctrl+A: attach • Esc: cancel"
                .to_string();
    }

    /// A card's Get or Execute button.
    pub fn run_chat_card_action(&mut self, action: ChatCardAction) {
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        match action {
            ChatCardAction::Get(service) => match get_service_command(&service.id) {
                Ok((label, args)) => self.spawn_command(CommandKind::Report, label, args),
                Err(message) => self.status = message,
            },
            ChatCardAction::Execute(service) => {
                let label = service.label();
                self.confirm_execute_service(service.id, label)
            }
        }
    }

    /// Close the selected conversation. Local bookkeeping only -- the other side is
    /// never told, and nothing here waits on a reply, so it is a direct action
    /// rather than a confirmation: reversible with `R`, unlike forgetting a peer.
    pub fn close_selected_conversation(&mut self) {
        if self.page() != Page::Chat {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(entry) = self.conversations.selected().cloned() else {
            self.status = "Select a chat first".to_string();
            return;
        };
        if entry.closed_at.is_some() {
            self.status = format!("{} is already closed", shorten(&entry.key, 18));
            return;
        }
        self.spawn_command(
            CommandKind::Generic,
            "Close conversation".to_string(),
            vec!["chat_close".to_string(), entry.key],
        );
    }

    /// Reopen the selected, closed conversation.
    pub fn reopen_selected_conversation(&mut self) {
        if self.page() != Page::Chat {
            return;
        }
        if self.command_running() {
            self.status = "Busy: a command is already running".to_string();
            return;
        }
        let Some(entry) = self.conversations.selected().cloned() else {
            self.status = "Select a chat first".to_string();
            return;
        };
        if entry.closed_at.is_none() {
            self.status = format!("{} is already open", shorten(&entry.key, 18));
            return;
        }
        self.spawn_command(
            CommandKind::Generic,
            "Reopen conversation".to_string(),
            vec!["chat_reopen".to_string(), entry.key],
        );
    }
}

/// The `nodo` invocation a sent compose box turns into: `chat_open` or `chat_reply`,
/// the text if there is any, and `--service` for an attached card.
pub(crate) fn chat_compose_command(
    target: ChatCompose,
    body: String,
    attachment: Option<&ChatService>,
) -> (String, Vec<String>) {
    let text = (!body.trim().is_empty()).then_some(body);
    let (label, mut args) = match target {
        ChatCompose::NewConversation { peer_id, topic } => {
            let label = format!("Open conversation with {}", shorten(&peer_id, 18));
            let mut args = vec!["chat_open".to_string(), peer_id, topic];
            if let Some(text) = text {
                args.extend(["--message".to_string(), text]);
            }
            (label, args)
        }
        ChatCompose::Reply { conversation_ref, .. } => (
            "Reply".to_string(),
            ["chat_reply".to_string(), conversation_ref].into_iter().chain(text).collect(),
        ),
    };
    if let Some(service) = attachment {
        args.extend(["--service".to_string(), service.id.clone()]);
    }
    (label, args)
}

/// The CHAT sidebar's columns (issue #453). The peer column is wide enough for the
/// `→ abc...xyz` it always holds (a client with no peer reads `← @abc...xyz`) and outlasts the topic, which is in the
/// conversation pane's title as well.
pub(crate) const SIDEBAR_COLUMNS: [Column; 2] = [
    Column::new("Chat", Constraint::Length(16), 11, 0),
    Column::new("Topic", Constraint::Min(10), 6, 1),
];

/// Below this width the sidebar and the conversation are stacked rather than side
/// by side: beside a 34-column sidebar, a conversation narrower than 30 columns
/// wraps every message a few words to a line (issue #453).
const SIDE_BY_SIDE_WIDTH: u16 = SIDEBAR_WIDTH + 30;

const SIDEBAR_WIDTH: u16 = 34;

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let split = if area.width >= SIDE_BY_SIDE_WIDTH {
        Layout::horizontal([Constraint::Length(SIDEBAR_WIDTH), Constraint::Min(30)]).split(area)
    } else {
        // Narrow: the list on top, sized to its rows but never more than half the
        // page, and the conversation under it.
        // The conversation only when it can show a few lines between its borders;
        // otherwise the list has the page.
        let rows = app.conversations.items.len() as u16;
        let mut sidebar = rows.saturating_add(4).clamp(5, (area.height / 2).max(5)).min(area.height);
        if area.height - sidebar < 5 {
            sidebar = area.height;
        }
        // Mid-message, the message is the page: the compose box is docked in the
        // conversation, and a list it cannot be seen under is no use while typing.
        if matches!(app.input_mode, InputMode::ComposeChatMessage | InputMode::PickChatService)
            && area.height < 2 * 5 + 5
        {
            sidebar = 0;
        }
        Layout::vertical([Constraint::Length(sidebar), Constraint::Min(0)]).split(area)
    };
    if split[0].height > 0 {
        draw_sidebar(frame, app, split[0]);
    }
    if split[1].height > 0 {
        draw_conversation(frame, app, split[1]);
    }
}

/// The peer's name as the chat view shows it: its first three characters, then its
/// last three, so two ids that share a long common prefix (the common case for
/// mnemonic-derived ids) still read apart at a glance without spending the sidebar's
/// width on the full id. Short ids (six characters or fewer) are shown whole rather
/// than doubled up on themselves.
fn short_peer_name(peer_id: &str) -> String {
    let chars: Vec<char> = peer_id.chars().collect();
    if chars.len() <= 6 {
        return peer_id.to_string();
    }
    let first: String = chars[..3].iter().collect();
    let last: String = chars[chars.len() - 3..].iter().collect();
    format!("{first}...{last}")
}

fn draw_sidebar(frame: &mut Frame, app: &mut App, area: Rect) {
    let rows = app
        .conversations
        .items
        .iter()
        .map(|entry| {
            // `→`: this node opened it with a peer. `←`: a client of this node opened it.
            let glyph = if entry.opened_by_us { "→" } else { "←" };
            // A conversation that waits for this node stands out. `◇` marks one that
            // waits for the other node.
            let (marker, row_color) = if entry.closed_at.is_some() {
                ("", muted())
            } else if entry.waits_for_us() {
                ("◆ ", warn())
            } else if entry.waits_for_peer() {
                ("◇ ", good())
            } else {
                ("", good())
            };
            let topic = if entry.topic.is_empty() { "(no topic)" } else { entry.topic.as_str() };
            let cells: Vec<TextCell> = vec![
                format!("{glyph} {}", entry.sidebar_name()).into(),
                format!("{marker}{topic}").into(),
            ];
            (cells, Style::default().fg(row_color))
        })
        .collect();
    let has_selection = app.conversations.state.selected().is_some();
    let (table, columns) = fitted_table(&SIDEBAR_COLUMNS, rows, area, has_selection);
    let table = table
        .block(section_block(
            match &app.conversations_error {
                Some(_) => " CHATS • CANNOT BE READ ".to_string(),
                None => format!(" CHATS • {} ", app.conversations.items.len()),
            },
            if app.conversations_error.is_some() { bad() } else { accent() },
        ))
        .highlight_style(selected_style())
        .highlight_symbol("▸ ");
    app.list_area = area;
    app.id_column_x = fitted_column_x(area, &columns, 0, has_selection);
    frame.render_stateful_widget(table, area, &mut app.conversations.state);
}

fn draw_conversation(frame: &mut Frame, app: &mut App, area: Rect) {
    if let Some(error) = &app.conversations_error {
        crate::ui::draw_card(
            frame,
            area,
            "CHAT UNREADABLE",
            vec![Line::from(Span::styled(error.clone(), Style::default().fg(bad())))],
            bad(),
        );
        return;
    }
    let Some(entry) = app.conversations.selected().cloned() else {
        crate::ui::draw_card(
            frame,
            area,
            "CONVERSATION",
            vec![Line::from(Span::styled(
                "Select a chat, or press o to start one",
                Style::default().fg(muted()),
            ))],
            accent(),
        );
        return;
    };

    let status_word = if entry.closed_at.is_some() {
        "closed"
    } else if entry.waits_for_us() {
        "waiting for an answer"
    } else if entry.waits_for_peer() {
        "waiting for the other node"
    } else {
        "open"
    };
    let topic_display = if entry.topic.is_empty() { "(no topic)" } else { &entry.topic };
    let title = format!(
        " {}  •  {}  •  {}  •  {} ",
        entry.them_name(),
        entry.key,
        topic_display,
        status_word
    );
    let block = section_block(title, accent());
    let inner = block.inner(area);
    frame.render_widget(block, area);

    // The full peer id repeats what the title just said, truncated or not by the
    // terminal's width -- clicking the title row copies it either way (issue:
    // click-to-copy full IDs).
    app.id_copy_areas.push((
        entry.them_id().to_string(),
        Rect { x: area.x, y: area.y, width: area.width, height: 1 },
    ));

    // The picker for an attachment is a step *of* composing: the box stays docked
    // behind it, holding the message typed so far.
    let composing = matches!(
        app.input_mode,
        InputMode::ComposeChatMessage | InputMode::PickChatService
    );
    let compose_height = if composing {
        compose_box_height(app, inner.width)
    } else {
        0
    };
    // The box being typed in before the messages above it: squeezed by a short
    // terminal, the messages scroll, but a compose box squeezed to its border hides
    // what is being typed (issue #453).
    let compose_height = compose_height.min(inner.height);
    let split = crate::layout_util::stack(inner, &[inner.height - compose_height, compose_height]);

    if split[0].height > 0 {
        draw_messages(frame, app, split[0], &entry);
    }
    if composing {
        draw_compose_box(frame, app, split[1]);
    }
}

/// One message's text: its header line, then each further line of its body, split
/// on its own embedded `\n` (the old renderer never did, so a multi-line message was
/// invisible past its first line) and left to `Paragraph::wrap` for the rest -- a
/// read-only transcript, so nothing here is a value an operator is mid-typing
/// (contrast `wrapped`, used for the config editors specifically because reflowing
/// an edited value would change it).
fn message_lines(message: &ChatMessageRow, entry: &ChatEntry) -> Vec<Line<'static>> {
    let who = if message.from_us {
        "us".to_string()
    } else {
        entry.them_name()
    };
    let mut body_lines = message.body.split('\n');
    let first = body_lines.next().unwrap_or("");
    let mut lines = vec![Line::from(vec![
        Span::styled(format!("[{}] ", message.ts), Style::default().fg(muted())),
        Span::styled(format!("{who}: "), Style::default().fg(accent()).bold()),
        Span::raw(first.to_string()),
    ])];
    lines.extend(body_lines.map(|continuation| Line::from(Span::raw(continuation.to_string()))));
    lines
}

/// A service card's height: border, tags, id, details, buttons, border.
const CARD_HEIGHT: u16 = 6;
const CARD_MAX_WIDTH: u16 = 72;
const GET_BUTTON: &str = "[ Get ]";
const EXECUTE_BUTTON: &str = "[ Execute ]";

/// What the conversation pane stacks: a message's (wrapped) text, or the card
/// under it.
enum Segment {
    Text(Vec<Line<'static>>),
    Card(SharedService),
}

/// The selected chat, oldest first, pinned to the bottom of the pane like any
/// chat: when it does not fit, what is cut is the oldest, not the newest -- the
/// newest is where a card that was just shared, and its buttons, are.
///
/// Stacked segment by segment rather than one `Paragraph`, because a card is a
/// widget with buttons, and a click can only find a button whose row is known.
fn draw_messages(frame: &mut Frame, app: &mut App, area: Rect, entry: &ChatEntry) {
    if app.conversation_messages.is_empty() {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "No messages yet",
                Style::default().fg(muted()),
            ))),
            area,
        );
        return;
    }
    let mut segments = Vec::new();
    for message in &app.conversation_messages {
        segments.push(Segment::Text(message_lines(message, entry)));
        if let Some(service) = &message.service {
            segments.push(Segment::Card(service.clone()));
        }
    }
    let text = |lines: Vec<Line<'static>>| {
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .style(Style::default().fg(text_colour()))
    };
    // Messages are whatever a peer typed, so a CJK character or an emoji can land in
    // the pane's last column; wrapped one column narrower it stays off the border.
    let wide = segments.iter().any(|segment| match segment {
        Segment::Text(lines) => crate::layout_util::lines_have_wide_glyph(lines),
        Segment::Card(_) => false,
    });
    let text_width = crate::layout_util::wrap_guard(area, wide).width;
    let heights: Vec<u16> = segments
        .iter()
        .map(|segment| match segment {
            Segment::Text(lines) => text(lines.clone()).line_count(text_width) as u16,
            Segment::Card(_) => CARD_HEIGHT,
        })
        .collect();

    let total: u16 = heights.iter().fold(0u16, |sum, height| sum.saturating_add(*height));
    let mut skip = total.saturating_sub(area.height);
    let mut y = area.y;
    for (segment, height) in segments.into_iter().zip(heights) {
        if skip >= height {
            skip -= height;
            continue;
        }
        let visible = height - skip;
        let rect = Rect { x: area.x, y, width: area.width, height: visible };
        match segment {
            Segment::Text(lines) => frame.render_widget(
                text(lines).scroll((skip, 0)),
                Rect { width: text_width, ..rect },
            ),
            // A card is whole or not at all: half a card is a button with no label.
            Segment::Card(service) if skip == 0 => draw_card(frame, app, rect, service),
            Segment::Card(_) => {}
        }
        skip = 0;
        y += visible;
    }
}

/// A shared service as a card, with the buttons that act on it.
fn draw_card(frame: &mut Frame, app: &mut App, area: Rect, service: SharedService) {
    let area = Rect { width: area.width.min(CARD_MAX_WIDTH), ..area };
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(accent()))
        .title(Span::styled(" SERVICE ", Style::default().fg(accent()).bold()));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let tags = if service.tags.is_empty() {
        Span::styled("(untagged)", Style::default().fg(muted()))
    } else {
        Span::styled(service.tags.join(" · "), Style::default().fg(text_colour()).bold())
    };
    let id = match &service.id {
        Some(id) => Span::styled(shorten(id, inner.width as usize), Style::default().fg(muted())),
        None => Span::styled(
            "no id of this node's hash type: get it by hand",
            Style::default().fg(warn()),
        ),
    };
    let button = Style::default().fg(accent()).add_modifier(Modifier::BOLD);
    let actionable = service.actionable();
    let buttons = if actionable.is_some() {
        Line::from(vec![
            Span::styled(GET_BUTTON, button),
            Span::raw("  "),
            Span::styled(EXECUTE_BUTTON, button),
        ])
    } else {
        Line::default()
    };
    let lines = vec![
        Line::from(tags),
        Line::from(id),
        Line::from(Span::styled(service.details(), Style::default().fg(muted()))),
        buttons,
    ];
    frame.render_widget(Paragraph::new(lines), inner);

    // The buttons' own cells, clipped to the card, so a click beside one is not a
    // click on it.
    let row = inner.y + 3;
    if let Some(target) = actionable.filter(|_| row < inner.y + inner.height) {
        let get = Rect::new(inner.x, row, GET_BUTTON.len() as u16, 1).intersection(inner);
        let execute = Rect::new(
            inner.x + GET_BUTTON.len() as u16 + 2,
            row,
            EXECUTE_BUTTON.len() as u16,
            1,
        )
        .intersection(inner);
        app.chat_card_buttons.push((ChatCardAction::Get(target.clone()), get));
        app.chat_card_buttons.push((ChatCardAction::Execute(target), execute));
    }
}

/// Width of the button column right of the compose box.
const COMPOSE_BUTTONS_WIDTH: u16 = 12;
const ATTACH_BUTTON: &str = "[ Attach ]";
const SEND_BUTTON: &str = "[ Send ]";

/// The compose box's own lines: the attached service first, if any, then the text.
fn compose_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if let Some(service) = &app.chat_attachment {
        lines.push(Line::from(vec![
            Span::styled("+ service ", Style::default().fg(muted())),
            Span::styled(service.label(), Style::default().fg(accent()).bold()),
            Span::styled(format!("  {}", shorten(&service.id, 18)), Style::default().fg(muted())),
        ]));
    }
    lines.extend(app.input.split('\n').map(|line| Line::from(line.to_string())));
    lines
}

/// How tall the docked compose box should be: every input line's own wrapped height
/// (and the attachment's line), clamped so one long paste cannot swallow the whole
/// conversation pane.
fn compose_box_height(app: &App, width: u16) -> u16 {
    let usable = width.saturating_sub(2 + COMPOSE_BUTTONS_WIDTH).max(1) as usize; // borders
    let wrapped_lines: u16 = app
        .input
        .split('\n')
        .map(|line| wrapped(line, usable).len().max(1) as u16)
        .sum();
    let attachment = app.chat_attachment.is_some() as u16;
    (wrapped_lines + attachment + 2).clamp(3, 10)
}

fn draw_compose_box(frame: &mut Frame, app: &mut App, area: Rect) {
    let split = Layout::horizontal([
        Constraint::Min(10),
        Constraint::Length(COMPOSE_BUTTONS_WIDTH),
    ])
    .split(area);
    let block = Block::bordered()
        .border_style(Style::default().fg(accent()))
        .title(Span::styled(
            " COMPOSE · Enter: newline · Ctrl+Enter or Alt+Enter: send · Ctrl+A: attach · Esc: cancel ",
            Style::default().fg(accent()).bold(),
        ));
    let lines = compose_lines(app);
    let inner = crate::layout_util::wrap_guard(
        block.inner(split[0]),
        crate::layout_util::lines_have_wide_glyph(&lines),
    );
    frame.render_widget(block.style(Style::default().fg(text_colour())), split[0]);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .style(Style::default().fg(text_colour())),
        inner,
    );

    // Buttons to the right of the input, aligned with its first row of text.
    let buttons = split[1];
    let button = Style::default().fg(accent()).add_modifier(Modifier::BOLD);
    app.chat_attach_area =
        Rect::new(buttons.x + 1, buttons.y + 1, ATTACH_BUTTON.len() as u16, 1).intersection(buttons);
    frame.render_widget(Paragraph::new(Span::styled(ATTACH_BUTTON, button)), app.chat_attach_area);
    // Sending with the mouse (issue #438): the chord that sends is the one key in
    // this interface a terminal may not be able to deliver -- see handler.rs.
    app.chat_send_area =
        Rect::new(buttons.x + 1, buttons.y + 2, SEND_BUTTON.len() as u16, 1).intersection(buttons);
    frame.render_widget(Paragraph::new(Span::styled(SEND_BUTTON, button)), app.chat_send_area);
}

/// The attach picker: this node's own services -- the only ones a card can name --
/// with "no attachment" leading them.
pub fn draw_service_picker(frame: &mut Frame, app: &App) {
    const MAX_VISIBLE: usize = 12;
    let services = &app.services.items;
    let area = centered_rect(
        70,
        (services.len().min(MAX_VISIBLE) as u16 + 6).max(8),
        frame.size(),
    );
    frame.render_widget(Clear, area);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(accent()))
        .style(Style::default().fg(text_colour()).bg(popup_background()))
        .title(Span::styled(
            " ATTACH A SERVICE ",
            Style::default().fg(accent()).bold(),
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let row = |index: usize, text: String, colour: Color| {
        let selected = index == app.chat_service_index;
        Line::from(vec![
            Span::styled(if selected { "▸ " } else { "  " }, Style::default().fg(accent()).bold()),
            Span::styled(
                text,
                if selected {
                    Style::default().fg(colour).bold()
                } else {
                    Style::default().fg(muted())
                },
            ),
        ])
    };
    let mut lines = vec![row(0, "No attachment".to_string(), text_colour())];
    // Scrolled so the pick is always on screen, for a registry longer than the popup.
    let first = app.chat_service_index.saturating_sub(MAX_VISIBLE);
    for (index, service) in services.iter().enumerate().skip(first).take(MAX_VISIBLE) {
        lines.push(row(
            index + 1,
            format!("{:<24} {}", shorten(&service.tag, 24), shorten(&service.id, 32)),
            text_colour(),
        ));
    }
    if services.is_empty() {
        lines.push(Line::from(Span::styled(
            "This node holds no services to share.",
            Style::default().fg(muted()),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "↑/↓ choose  ·  ⏎ attach  ·  Esc back",
        Style::default().fg(warn()),
    )));
    frame.render_widget(
        Paragraph::new(lines).style(Style::default().fg(text_colour()).bg(popup_background())),
        inner,
    );
}

/// Step 1's popup: every peer the typed filter matches, current pick highlighted.
/// Unlike `draw_profile_popup`/`draw_lever_key_popup` (fixed, short lists that just
/// grow to fit), a peer list can run long, so this caps how many rows it draws and
/// says how many more narrowing the filter would reach, rather than growing the
/// popup past the screen.
pub fn draw_peer_picker(frame: &mut Frame, app: &App) {
    const MAX_VISIBLE: usize = 12;
    let filtered = app.filtered_chat_peers();
    let visible = filtered.len().min(MAX_VISIBLE);
    let area = centered_rect(60, (visible as u16 + 7).max(9), frame.size());
    frame.render_widget(Clear, area);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(accent()))
        .style(Style::default().fg(text_colour()).bg(popup_background()))
        .title(Span::styled(
            format!(" {} ", app.input_title.to_uppercase()),
            Style::default().fg(accent()).bold(),
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let mut lines: Vec<Line> = vec![
        Line::from(vec![
            Span::styled("Filter  ", Style::default().fg(muted())),
            Span::styled(
                if app.chat_wizard_peer_filter.is_empty() {
                    "(type to narrow)".to_string()
                } else {
                    app.chat_wizard_peer_filter.clone()
                },
                Style::default().fg(text_colour()).bold(),
            ),
        ]),
        Line::from(""),
    ];
    if filtered.is_empty() {
        lines.push(Line::from(Span::styled(
            "No peer matches",
            Style::default().fg(warn()),
        )));
    }
    for (index, peer) in filtered.iter().take(MAX_VISIBLE).enumerate() {
        let selected = index == app.chat_wizard_peer_index;
        lines.push(Line::from(vec![
            Span::styled(
                if selected { "▸ " } else { "  " },
                Style::default().fg(accent()).bold(),
            ),
            Span::styled(
                peer.id.clone(),
                if selected {
                    Style::default().fg(text_colour()).bold()
                } else {
                    Style::default().fg(muted())
                },
            ),
        ]));
    }
    if filtered.len() > MAX_VISIBLE {
        lines.push(Line::from(Span::styled(
            format!("… and {} more — keep typing to narrow", filtered.len() - MAX_VISIBLE),
            Style::default().fg(muted()),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "type to filter  ·  ↑/↓ choose  ·  ⏎ next  ·  Esc cancel",
        Style::default().fg(warn()),
    )));
    frame.render_widget(
        Paragraph::new(lines).style(Style::default().fg(text_colour()).bg(popup_background())),
        inner,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{InputMode, StatefulList};
    use crate::peers::Peer;
    use rusqlite::Connection;
    use std::fs;
    use std::path::PathBuf;

    /// The same terse table-source reader `peers.rs`'s own schema-drift tests use.
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

    fn chat_database(dir: &Path) -> PathBuf {
        let path = dir.join("database.sqlite");
        let connection = Connection::open(&path).unwrap();
        for table in ["peer", "chat_conversations", "chat_messages", "chat_threads"] {
            connection.execute_batch(&migration_table(table)).unwrap();
        }
        connection
            .execute_batch(
                "INSERT INTO peer (id, advertisement, remote_client_id, balance_mu,
                                   reputation_score)
                 VALUES ('peer-1', NULL, NULL, '0', 0);",
            )
            .unwrap();
        path
    }

    /// A conversation row. `remote` is the peer of a mirror, NULL when served here.
    fn conversation(
        connection: &Connection,
        client_id: &str,
        number: i64,
        remote: Option<&str>,
        topic: &str,
    ) {
        connection
            .execute(
                "INSERT INTO chat_conversations (client_id, number, remote_peer_id, topic,
                                                 opened_at)
                 VALUES (?1, ?2, ?3, ?4, '2020-01-01 00:00:00')",
                rusqlite::params![client_id, number, remote, topic],
            )
            .unwrap();
    }

    fn message(
        connection: &Connection,
        client_id: &str,
        sequence: i64,
        conversation: i64,
        from_operator: i64,
        body: &str,
        ts: i64,
    ) {
        connection
            .execute(
                "INSERT INTO chat_messages (client_id, sequence, conversation, from_operator,
                                            body, ts)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![client_id, sequence, conversation, from_operator, body, ts],
            )
            .unwrap();
    }

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("nodo-tui-chat-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn peer(id: &str) -> Peer {
        Peer {
            id: id.to_string(),
            uris: String::new(),
            balance: "0".to_string(),
            remote_client_id: String::new(),
            local_client_id: String::new(),
            proof_ids: Vec::new(),
            reputation_score: "0".to_string(),
            contracts: Vec::new(),
            resources: Default::default(),
            learned_via_gossip: false,
        }
    }

    #[test]
    fn a_mirror_and_a_served_conversation_of_one_peer_are_grouped_under_it() {
        let dir = temp_dir("grouped");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        // A mirror: this node is the client `ours` of `peer-1`.
        conversation(&connection, "ours", 1, Some("peer-1"), "ping");
        message(&connection, "ours", 1, 1, 0, "hi", 100);
        // A served one: `theirs` is a client of this node, bound to `peer-1`.
        connection.execute_batch("UPDATE peer SET local_client_id = 'theirs'").unwrap();
        conversation(&connection, "theirs", 1, None, "help");
        message(&connection, "theirs", 1, 1, 0, "please", 200);
        // A conversation of another peer, in between by age.
        connection
            .execute_batch(
                "INSERT INTO peer (id, advertisement, remote_client_id, balance_mu,
                                   reputation_score) VALUES ('peer-2', NULL, NULL, '0', 0)",
            )
            .unwrap();
        conversation(&connection, "ours2", 1, Some("peer-2"), "other");
        message(&connection, "ours2", 1, 1, 0, "x", 150);

        let entries = load_entries(&database).unwrap();

        let order: Vec<(&str, Option<&str>, bool)> = entries
            .iter()
            .map(|e| (e.key.as_str(), e.peer_id.as_deref(), e.opened_by_us))
            .collect();
        assert_eq!(
            order,
            [
                ("theirs#1", Some("peer-1"), false),
                ("ours#1", Some("peer-1"), true),
                ("ours2#1", Some("peer-2"), true),
            ],
            "peer-1 holds both kinds and comes first: its newest message is the newest"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_served_conversation_of_a_client_with_no_peer_is_listed_under_no_peer() {
        let dir = temp_dir("nopeer");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        conversation(&connection, "plain", 1, None, "support");
        message(&connection, "plain", 1, 1, 0, "hola", 5_000);
        conversation(&connection, "ours", 1, Some("peer-1"), "ping");
        message(&connection, "ours", 1, 1, 0, "hi", 100);

        let entries = load_entries(&database).unwrap();

        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].key, "ours#1", "the no-peer group is last, even if newer");
        assert_eq!(entries[1].key, "plain#1");
        assert_eq!(entries[1].peer_id, None);
        assert!(!entries[1].opened_by_us);
        assert_eq!(entries[1].them_id(), "plain");
        assert!(entries[1].them_name().starts_with("client "));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_empty_conversation_sorts_by_its_opening_and_keeps_its_closing() {
        let dir = temp_dir("opening");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        conversation(&connection, "ours", 1, Some("peer-1"), "");
        connection
            .execute_batch("UPDATE chat_conversations SET closed_at = '2026-01-01 00:00:00'")
            .unwrap();

        let entries = load_entries(&database).unwrap();

        assert_eq!(entries[0].last_ts, 1_577_836_800, "opened_at as unix seconds");
        assert!(entries[0].closed_at.is_some());
        assert!(!entries[0].waiting, "no message, nothing to wait for");
        assert!(entries[0].topic.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_waiting_marker_follows_who_wrote_last_on_each_side() {
        let dir = temp_dir("waiting");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        // Served, the client wrote last: it waits for us.
        conversation(&connection, "c1", 1, None, "a");
        message(&connection, "c1", 1, 1, 0, "q", 10);
        // Served, the operator wrote last: answered.
        conversation(&connection, "c2", 1, None, "b");
        message(&connection, "c2", 1, 1, 0, "q", 10);
        message(&connection, "c2", 2, 1, 1, "a", 11);
        // Mirror, our message is last: it waits for the other node.
        conversation(&connection, "m1", 1, Some("peer-1"), "c");
        message(&connection, "m1", 1, 1, 0, "q", 10);
        // Mirror, the operator of the other node wrote last: answered.
        conversation(&connection, "m2", 1, Some("peer-1"), "d");
        message(&connection, "m2", 1, 1, 0, "q", 10);
        message(&connection, "m2", 2, 1, 1, "a", 11);

        let entries = load_entries(&database).unwrap();
        let find = |key: &str| entries.iter().find(|e| e.key == key).unwrap();

        assert!(find("c1#1").waits_for_us() && !find("c1#1").waits_for_peer());
        assert!(!find("c2#1").waits_for_us() && !find("c2#1").waits_for_peer());
        assert!(find("m1#1").waits_for_peer() && !find("m1#1").waits_for_us());
        assert!(!find("m2#1").waits_for_us() && !find("m2#1").waits_for_peer());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_closed_conversation_does_not_wait() {
        let mut entry = entry_of("c#1", false);
        entry.waiting = true;
        assert!(entry.waits_for_us());
        entry.closed_at = Some("2026-01-01 00:00:00".to_string());
        assert!(!entry.waits_for_us() && !entry.waits_for_peer());
    }

    #[test]
    fn us_and_them_are_mapped_per_side() {
        let dir = temp_dir("sides");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        for (client, remote) in [("served", None), ("mirror", Some("peer-1"))] {
            conversation(&connection, client, 1, remote, "t");
            message(&connection, client, 1, 1, 0, "from the client", 1_700_000_000);
            message(&connection, client, 2, 1, 1, "from the operator", 1_700_000_060);
            // Another conversation of the same client must not leak in.
            conversation(&connection, client, 2, remote, "other");
            message(&connection, client, 3, 2, 1, "elsewhere", 1_700_000_090);
        }

        let served = get_conversation_messages(&database, "served", 1, false).unwrap();
        let read: Vec<(bool, &str)> = served.iter().map(|m| (m.from_us, m.body.as_str())).collect();
        assert_eq!(read, [(false, "from the client"), (true, "from the operator")]);

        let mirror = get_conversation_messages(&database, "mirror", 1, true).unwrap();
        let read: Vec<(bool, &str)> = mirror.iter().map(|m| (m.from_us, m.body.as_str())).collect();
        assert_eq!(read, [(true, "from the client"), (false, "from the operator")]);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_database_without_the_chat_tables_reads_as_empty() {
        let dir = temp_dir("missing");
        let database = dir.join("database.sqlite");
        Connection::open(&database).unwrap();
        assert!(load_entries(&database).unwrap().is_empty());
        assert!(get_conversation_messages(&database, "c", 1, false).unwrap().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    fn on_chat_page(peers: Vec<Peer>) -> App {
        let mut app = App::default();
        app.tabs.index = crate::app::Page::ALL
            .iter()
            .position(|page| *page == Page::Chat)
            .unwrap();
        app.peers = StatefulList::with_items(peers);
        app
    }

    fn entry_of(key: &str, opened_by_us: bool) -> ChatEntry {
        let (client_id, number) = key.split_once('#').unwrap();
        ChatEntry {
            key: key.to_string(),
            client_id: client_id.to_string(),
            number: number.parse().unwrap(),
            peer_id: Some("peer-1".to_string()),
            opened_by_us,
            topic: "ping".to_string(),
            closed_at: None,
            last_ts: 0,
            waiting: false,
        }
    }

    #[test]
    fn the_wizard_walks_peer_then_topic_then_compose() {
        let mut app = on_chat_page(vec![peer("peer-abc"), peer("peer-xyz")]);
        app.open_new_chat_wizard();
        assert_eq!(app.input_mode, InputMode::PickChatPeer);

        app.chat_wizard_peer_filter = "xyz".to_string();
        app.chat_peer_filter_changed();
        app.submit_chat_peer_pick();
        assert_eq!(app.input_mode, InputMode::NewChatTopic, "the topic is always typed");
        assert_eq!(app.chat_wizard_peer_id.as_deref(), Some("peer-xyz"));

        app.input = "   ".to_string();
        app.submit_new_chat_topic();
        assert_eq!(app.input_mode, InputMode::NewChatTopic, "an empty topic is refused");

        app.input = "status".to_string();
        app.submit_new_chat_topic();
        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
        assert!(matches!(
            app.chat_compose,
            Some(ChatCompose::NewConversation { ref peer_id, ref topic })
                if peer_id == "peer-xyz" && topic == "status"
        ));
    }

    #[test]
    fn an_empty_compose_is_rejected_before_anything_is_sent() {
        let mut app = on_chat_page(vec![peer("peer-abc")]);
        one_conversation(&mut app);

        app.open_reply_prompt();
        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
        app.input = "   ".to_string();
        app.submit_chat_compose();

        assert_eq!(app.input_mode, InputMode::ComposeChatMessage, "still open: empty body");
        assert!(app.status.contains("Type a message"), "{}", app.status);
    }

    #[test]
    fn replying_to_a_closed_conversation_is_refused() {
        let mut app = on_chat_page(Vec::new());
        let mut entry = entry_of("c#1", true);
        entry.closed_at = Some("2026-01-02 00:00:00".to_string());
        app.conversations = StatefulList::with_items(vec![entry]);
        app.conversations.next();

        app.open_reply_prompt();

        assert_eq!(app.input_mode, InputMode::Normal, "no compose opened");
        assert!(app.status.contains("reopen"), "{}", app.status);
    }

    #[test]
    fn a_reply_sends_chat_reply_with_the_ref_for_both_kinds() {
        for (key, opened_by_us) in [("served#3", false), ("mirror#7", true)] {
            let mut app = on_chat_page(Vec::new());
            app.conversations = StatefulList::with_items(vec![entry_of(key, opened_by_us)]);
            app.conversations.next();

            app.open_reply_prompt();
            assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
            let Some(target) = app.chat_compose.clone() else { panic!("no compose") };
            assert!(
                matches!(&target, ChatCompose::Reply { conversation_ref, .. } if conversation_ref == key)
            );
            let (_, args) = chat_compose_command(target, "hola".to_string(), None);
            assert_eq!(args, ["chat_reply", key, "hola"]);
        }
    }

    #[test]
    fn a_reply_to_a_client_can_carry_a_service() {
        let mut app = on_chat_page(Vec::new());
        let mut entry = entry_of("plain#1", false);
        entry.peer_id = None;
        app.conversations = StatefulList::with_items(vec![entry]);
        app.conversations.next();
        app.open_reply_prompt();
        app.open_chat_service_picker();
        assert_eq!(app.input_mode, InputMode::PickChatService, "no text-only rule any more");
    }

    #[test]
    fn close_and_reopen_use_the_ref() {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        rt.block_on(async {
            let mut app = on_chat_page(Vec::new());
            app.conversations = StatefulList::with_items(vec![entry_of("served#3", false)]);
            app.conversations.next();
            app.close_selected_conversation();
            assert!(app.command_task.is_some(), "a command was spawned");
            assert_eq!(app.status, "Running nodo chat_close served#3…");

            let mut app = on_chat_page(Vec::new());
            let mut entry = entry_of("mirror#7", true);
            entry.closed_at = Some("2026-01-02 00:00:00".to_string());
            app.conversations = StatefulList::with_items(vec![entry]);
            app.conversations.next();
            app.reopen_selected_conversation();
            assert!(app.command_task.is_some(), "a command was spawned");
            assert_eq!(app.status, "Running nodo chat_reopen mirror#7…");
        });
    }

    #[test]
    fn the_sidebar_marks_what_waits_and_groups_by_peer() {
        let mut app = on_chat_page(Vec::new());
        let mut waiting = entry_of("plain#1", false);
        waiting.peer_id = None;
        waiting.topic = "support".to_string();
        waiting.waiting = true;
        let mut mirror = entry_of("ours#1", true);
        mirror.waiting = true;
        app.conversations = StatefulList::with_items(vec![mirror, waiting]);
        app.conversations.next();
        app.conversation_messages = vec![ChatMessageRow {
            from_us: false,
            body: "ayuda".to_string(),
            ts: "2026-11-08 18:29:44 UTC".to_string(),
            service: None,
        }];
        let screen = render(&mut app, 120, 20);
        assert!(screen.contains("→ peer-1"), "{screen}");
        assert!(screen.contains("← @plain"), "{screen}");
        assert!(screen.contains("◇ ping"), "waits for the other node: {screen}");
        assert!(screen.contains("◆ support"), "waits for us: {screen}");
        assert!(screen.contains("ayuda"), "{screen}");
    }

    #[test]
    fn closing_an_already_closed_conversation_is_a_no_op_not_a_command() {
        let mut app = on_chat_page(Vec::new());
        let mut entry = entry_of("c#1", true);
        entry.closed_at = Some("2026-01-02 00:00:00".to_string());
        app.conversations = StatefulList::with_items(vec![entry]);
        app.conversations.next();

        app.close_selected_conversation();

        assert!(app.command_task.is_none(), "nothing spawned");
        assert!(app.status.contains("already closed"), "{}", app.status);
    }

    #[test]
    fn reopening_an_already_open_conversation_is_a_no_op_not_a_command() {
        let mut app = on_chat_page(Vec::new());
        app.conversations = StatefulList::with_items(vec![entry_of("c#1", true)]);
        app.conversations.next();

        app.reopen_selected_conversation();

        assert!(app.command_task.is_none(), "nothing spawned");
        assert!(app.status.contains("already open"), "{}", app.status);
    }

    /// Enter inserts a newline while composing; Ctrl+Enter sends -- the inversion of
    /// every other text entry in this interface, and the one that makes a real
    /// multi-line message possible to type at all.
    #[test]
    fn composing_enter_is_a_newline_and_ctrl_enter_sends() {
        use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyEventState, KeyModifiers};

        fn key(modifiers: KeyModifiers, code: KeyCode) -> KeyEvent {
            KeyEvent {
                code,
                modifiers,
                kind: KeyEventKind::Press,
                state: KeyEventState::NONE,
            }
        }

        let mut app = on_chat_page(Vec::new());
        app.conversations = StatefulList::with_items(vec![entry_of("conv-1#1", true)]);
        app.conversations.next();
        app.open_reply_prompt();
        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);

        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(async {
            crate::handler::handle_key_events(key(KeyModifiers::NONE, KeyCode::Char('h')), &mut app)
                .await
                .unwrap();
            crate::handler::handle_key_events(key(KeyModifiers::NONE, KeyCode::Enter), &mut app)
                .await
                .unwrap();
            crate::handler::handle_key_events(key(KeyModifiers::NONE, KeyCode::Char('i')), &mut app)
                .await
                .unwrap();
            assert_eq!(app.input, "h\ni");
            assert_eq!(app.input_mode, InputMode::ComposeChatMessage, "Enter must not send");

            crate::handler::handle_key_events(key(KeyModifiers::CONTROL, KeyCode::Enter), &mut app)
                .await
                .unwrap();
        });
        assert_eq!(app.input_mode, InputMode::Normal, "Ctrl+Enter sends and closes");
    }

    /// The redesigned page end to end: a sidebar wide enough for both directions and
    /// a client with no peer at once, and a conversation pane that wraps a multi-line
    /// message instead of clipping it -- the two concrete complaints this redesign
    /// was for.
    #[test]
    fn the_sidebar_shows_every_kind_of_chat_and_wraps_a_multiline_message() {
        use ratatui::{backend::TestBackend, Terminal};

        let mut app = on_chat_page(Vec::new());
        let mut billing = entry_of("conv-ours#1", true);
        billing.peer_id = Some("peer-ours".to_string());
        billing.topic = "billing".to_string();
        let mut theirs = entry_of("conv-theirs#1", false);
        theirs.peer_id = Some("peer-theirs".to_string());
        theirs.topic = String::new();
        let mut flat = entry_of("flat#1", false);
        flat.peer_id = None;
        app.conversations = StatefulList::with_items(vec![billing, theirs, flat]);
        app.conversations.next();
        app.conversation_messages = vec![ChatMessageRow {
            from_us: true,
            body: "line one\nline two, which is long enough on its own to need wrapping across more than one terminal column".to_string(),
            ts: "2026-01-01 00:00:00".to_string(),
            service: None,
        }];
        app.tabs.index = crate::app::Page::ALL
            .iter()
            .position(|page| *page == Page::Chat)
            .unwrap();

        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| crate::ui::render(&mut app, frame)).unwrap();
        let screen: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();

        assert!(screen.contains("billing"), "{screen}");
        assert!(screen.contains("(no topic)"), "{screen}");
        // The peer's full id is truncated to its first and last three characters
        // (short_peer_name), so "peer-ours" reads as "pee...urs" on screen.
        assert!(screen.contains("pee...urs"), "{screen}");
        assert!(screen.contains("line one"), "{screen}");
        assert!(screen.contains("line two"), "{screen}");
        // Wrapped, not clipped: the tail of the long line reached the screen too.
        assert!(screen.contains("wrapping"), "long message was clipped: {screen}");
    }

    /// Issue #438: a terminal that sends LF for Ctrl+Enter is read in raw mode as
    /// Ctrl+J, which used to type a "j" into the message instead of sending it.
    #[test]
    fn ctrl_j_is_ctrl_enter_from_a_terminal_that_sends_lf() {
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.open_reply_prompt();
        app.input = "hi".to_string();
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        rt.block_on(async {
            crate::handler::handle_key_events(
                KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL),
                &mut app,
            )
            .await
            .unwrap();
        });

        assert_eq!(app.input_mode, InputMode::Normal, "sent, not typed");
        assert!(app.input.is_empty(), "no stray j: {:?}", app.input);
    }

    #[test]
    fn alt_enter_still_sends() {
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.open_reply_prompt();
        app.input = "hi".to_string();
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        rt.block_on(async {
            crate::handler::handle_key_events(
                KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT),
                &mut app,
            )
            .await
            .unwrap();
        });

        assert_eq!(app.input_mode, InputMode::Normal);
    }

    /// The Send button, right of the input next to Attach, sends with the mouse.
    #[test]
    fn the_send_button_sends_the_message() {
        use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};

        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.open_reply_prompt();
        app.input = "sent by mouse".to_string();
        let screen = render(&mut app, 120, 30);
        assert!(screen.contains("[ Send ]"), "{screen}");
        let send = app.chat_send_area;
        assert_eq!(send.x, app.chat_attach_area.x, "stacked under Attach");

        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        rt.block_on(async {
            crate::handler::handle_mouse_events(
                MouseEvent {
                    kind: MouseEventKind::Down(MouseButton::Left),
                    column: send.x + 1,
                    row: send.y,
                    modifiers: crossterm::event::KeyModifiers::NONE,
                },
                &mut app,
            )
            .await
            .unwrap();
        });

        assert_eq!(app.input_mode, InputMode::Normal, "sent and closed");
        assert!(app.command_task.is_some(), "the send was spawned");
    }

    /// An empty box is not sent by the button either.
    #[test]
    fn the_send_button_refuses_an_empty_message() {
        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.open_reply_prompt();
        render(&mut app, 120, 30);
        let send = app.chat_send_area;

        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(crate::handler::handle_mouse_events(
            crossterm::event::MouseEvent {
                kind: crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left),
                column: send.x,
                row: send.y,
                modifiers: crossterm::event::KeyModifiers::NONE,
            },
            &mut app,
        ))
        .unwrap();

        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
        assert!(app.status.contains("Type a message"), "{}", app.status);
    }

    // --- Service cards (issue #438) ------------------------------------------

    const SERVICE_ID: &str = "abababababababababababababababababababababababababababababababab";

    fn card() -> ChatService {
        ChatService { id: SERVICE_ID.to_string(), tags: vec!["hello-world".to_string()] }
    }

    /// A received card as the database gives it back: Metadata with this node's
    /// hash of the service, so it has an id and buttons.
    fn shared() -> SharedService {
        SharedService {
            id: Some(SERVICE_ID.to_string()),
            tags: vec!["hello-world".to_string()],
            hash_types: vec!["sha3_256".to_string(), "shake_256".to_string()],
            format: Vec::new(),
            reputation_proofs: 0,
        }
    }

    fn metadata_bytes(tags: &[&str]) -> Vec<u8> {
        use crate::app::protos::{metadata::hash_tag::Hash, metadata::HashTag, DataFormat, Metadata};
        let hash = |id: &str, value: &str| Hash {
            r#type: (0..id.len()).step_by(2).map(|i| u8::from_str_radix(&id[i..i + 2], 16).unwrap()).collect(),
            value: (0..value.len()).step_by(2).map(|i| u8::from_str_radix(&value[i..i + 2], 16).unwrap()).collect(),
        };
        Metadata {
            hashtag: Some(HashTag {
                hash: vec![
                    hash("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a", SERVICE_ID),
                    hash("46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f", &"cd".repeat(32)),
                ],
                tag: tags.iter().map(|tag| tag.to_string()).collect(),
                attr_hashtag: Vec::new(),
            }),
            format: Some(DataFormat { tags: vec!["linux/amd64".to_string()], ..Default::default() }),
            reputation_proofs: vec![Default::default(); 2],
        }
        .encode_to_vec()
    }

    fn one_conversation(app: &mut App) {
        app.conversations = StatefulList::with_items(vec![entry_of("conv-1#1", true)]);
        app.conversations.next();
    }

    fn render(app: &mut App, width: u16, height: u16) -> String {
        use ratatui::{backend::TestBackend, Terminal};
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| crate::ui::render(app, frame)).unwrap();
        terminal.backend().buffer().content().iter().map(|cell| cell.symbol()).collect()
    }

    #[test]
    fn a_stored_card_is_decoded_from_its_metadata() {
        let dir = temp_dir("card");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        conversation(&connection, "ours", 1, Some("peer-1"), "t");
        connection
            .execute(
                "INSERT INTO chat_messages
                     (client_id, sequence, conversation, from_operator, body, ts, service_id,
                      service_metadata)
                 VALUES ('ours', 1, 1, 1, 'try this', 1, ?1, ?2)",
                rusqlite::params![SERVICE_ID, metadata_bytes(&["hello-world", "demo"])],
            )
            .unwrap();

        let messages = get_conversation_messages(&database, "ours", 1, true).unwrap();

        let service = messages[0].service.clone().expect("a card");
        assert_eq!(service.id.as_deref(), Some(SERVICE_ID));
        assert_eq!(service.tags, vec!["hello-world".to_string(), "demo".to_string()]);
        assert_eq!(service.hash_types, vec!["sha3_256".to_string(), "shake_256".to_string()]);
        assert_eq!(service.format, vec!["linux/amd64".to_string()]);
        assert_eq!(service.reputation_proofs, 2);
        let _ = fs::remove_dir_all(&dir);
    }

    /// Metadata with no hash of this node's type is stored with no id: the card
    /// still shows, without buttons that would have to guess one.
    #[test]
    fn a_card_without_an_id_of_this_nodes_hash_type_has_no_buttons() {
        let dir = temp_dir("card-no-id");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        conversation(&connection, "ours", 1, Some("peer-1"), "t");
        connection
            .execute(
                "INSERT INTO chat_messages
                     (client_id, sequence, conversation, from_operator, body, ts, service_id,
                      service_metadata)
                 VALUES ('ours', 1, 1, 1, 'try this', 1, NULL, ?1)",
                [metadata_bytes(&["hello-world"])],
            )
            .unwrap();
        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.conversation_messages = get_conversation_messages(&database, "ours", 1, true).unwrap();
        assert_eq!(app.conversation_messages[0].service.as_ref().unwrap().id, None);

        let screen = render(&mut app, 120, 30);

        assert!(screen.contains("hello-world"), "{screen}");
        assert!(screen.contains("no id of this node's hash type"), "{screen}");
        assert!(!screen.contains("[ Get ]"), "{screen}");
        assert!(app.chat_card_buttons.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_card_is_drawn_with_get_and_execute_buttons() {
        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.conversation_messages = vec![ChatMessageRow {
            from_us: false,
            body: "try this".to_string(),
            ts: "2026-01-01 00:00:00".to_string(),
            service: Some(shared()),
        }];

        let screen = render(&mut app, 120, 30);

        assert!(screen.contains("try this"), "{screen}");
        assert!(screen.contains("SERVICE"), "{screen}");
        assert!(screen.contains("hello-world"), "{screen}");
        assert!(screen.contains("sha3_256 · shake_256"), "{screen}");
        assert!(screen.contains("[ Get ]"), "{screen}");
        assert!(screen.contains("[ Execute ]"), "{screen}");
        let actions: Vec<_> = app.chat_card_buttons.iter().map(|(action, _)| action.clone()).collect();
        assert_eq!(actions, vec![ChatCardAction::Get(card()), ChatCardAction::Execute(card())]);
    }

    /// Execute spends, so the button asks exactly as `e` on SERVICES does.
    #[test]
    fn clicking_execute_asks_before_it_spends() {
        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.conversation_messages = vec![ChatMessageRow {
            from_us: false,
            body: String::new(),
            ts: "2026-01-01 00:00:00".to_string(),
            service: Some(shared()),
        }];
        render(&mut app, 120, 30);
        let (_, execute) = app
            .chat_card_buttons
            .iter()
            .find(|(action, _)| matches!(action, ChatCardAction::Execute(_)))
            .cloned()
            .unwrap();

        app.click_at(execute.x, execute.y);

        assert_eq!(app.input_mode, InputMode::Confirm);
        assert!(matches!(
            app.pending_action,
            Some(crate::app::PendingAction::ExecuteService { ref id, ref label })
                if id == SERVICE_ID && label == "hello-world"
        ));
        assert!(app.command_task.is_none(), "nothing may run before the answer");
    }

    /// The newest message is the one kept when the pane overflows, since that is
    /// where a card someone just shared -- and its buttons -- are.
    #[test]
    fn an_overflowing_conversation_keeps_its_newest_messages_on_screen() {
        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.conversation_messages = (0..60)
            .map(|index| ChatMessageRow {
                from_us: true,
                body: format!("message number {index}"),
                ts: "2026-01-01 00:00:00".to_string(),
                service: (index == 59).then(shared),
            })
            .collect();

        let screen = render(&mut app, 120, 30);

        assert!(screen.contains("message number 59"), "{screen}");
        assert!(!screen.contains("message number 0 "), "{screen}");
        assert_eq!(app.chat_card_buttons.len(), 2, "the newest card is drawn whole");
    }

    #[test]
    fn a_message_with_only_an_attachment_sends_the_service_alone() {
        let target = ChatCompose::Reply {
            conversation_ref: "conv-1#1".to_string(),
            who: "peer-1".to_string(),
        };
        let (_, args) = chat_compose_command(target, String::new(), Some(&card()));
        assert_eq!(args, vec!["chat_reply", "conv-1#1", "--service", SERVICE_ID]);

        let target = ChatCompose::NewConversation {
            peer_id: "peer-1".to_string(),
            topic: "try this".to_string(),
        };
        let (_, args) = chat_compose_command(target, "look".to_string(), Some(&card()));
        assert_eq!(
            args,
            vec!["chat_open", "peer-1", "try this", "--message", "look", "--service", SERVICE_ID]
        );

        let target = ChatCompose::Reply {
            conversation_ref: "conv-1#1".to_string(),
            who: "peer-1".to_string(),
        };
        let (_, args) = chat_compose_command(target, "hi".to_string(), None);
        assert_eq!(args, vec!["chat_reply", "conv-1#1", "hi"], "no attachment, no flag");
    }

    /// Ctrl+A picks one of this node's services; Esc or Enter both return to the
    /// message exactly as it was typed.
    #[test]
    fn attaching_keeps_the_message_being_typed() {
        use crate::app::Service;
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.services = StatefulList::with_items(vec![Service {
            id: SERVICE_ID.to_string(),
            tag: "hello-world".to_string(),
            size_bytes: 0,
            total_size_bytes: None,
        }]);
        app.open_reply_prompt();
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(async {
            for key in [
                KeyEvent::new(KeyCode::Char('h'), KeyModifiers::NONE),
                KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL),
            ] {
                crate::handler::handle_key_events(key, &mut app).await.unwrap();
            }
            assert_eq!(app.input_mode, InputMode::PickChatService);
            for key in [
                KeyEvent::new(KeyCode::Down, KeyModifiers::NONE),
                KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
            ] {
                crate::handler::handle_key_events(key, &mut app).await.unwrap();
            }
        });

        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
        assert_eq!(app.input, "h");
        assert_eq!(app.chat_attachment, Some(card()));
        let screen = render(&mut app, 120, 30);
        assert!(screen.contains("+ service hello-world"), "{screen}");
    }

    #[test]
    fn the_attach_button_opens_the_picker() {
        use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};

        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.open_reply_prompt();
        let screen = render(&mut app, 120, 30);
        assert!(screen.contains("[ Attach ]"), "{screen}");
        let area = app.chat_attach_area;

        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(crate::handler::handle_mouse_events(
            MouseEvent {
                kind: MouseEventKind::Down(MouseButton::Left),
                column: area.x,
                row: area.y,
                modifiers: crossterm::event::KeyModifiers::NONE,
            },
            &mut app,
        ))
        .unwrap();

        assert_eq!(app.input_mode, InputMode::PickChatService);
    }
}
