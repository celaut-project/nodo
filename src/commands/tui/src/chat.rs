//! The CHAT page: free-text conversations with peer operators (issue #431), redesigned
//! as a persistent sidebar + conversation pane (Telegram/WhatsApp-shaped) instead of a
//! table-above-a-card, so the page can actually hold a conversation rather than a
//! one-line-per-message transcript. See the module doc on `peers.rs` for why this
//! lives in its own file.
//!
//! Three things changed from the page this replaces:
//!
//! - **One merged list, not a direction toggle.** `peer_chat_conversations.
//!   opened_by_us` used to pick which half of the table a toggle showed; now both
//!   halves are one list, sorted by last activity, with a glyph per row saying which
//!   half a chat belongs to.
//! - **Topic-less messages are a row.** `peer_chat_messages.conversation_id` can be
//!   NULL -- flat messages that predate conversations, and still the only kind a peer
//!   sends before it names a thread -- and were invisible in the TUI because the old
//!   query only ever read `peer_chat_conversations`. Each peer with any gets a
//!   synthetic "(no topic)" entry here (`ChatEntryKind::Untopiced`).
//! - **Opening a chat is peer → topic → message, not one free-text line.** The old
//!   `<peer_id> <topic...>` line sent `topic` itself as the opening message; now the
//!   three are separate steps, and the message is real prose, not a label.

use crate::app::{
    get_service_command, shorten, App, CommandKind, EditKind, Identifiable, InputMode, Page,
};
use crate::peers::Peer;
use crate::ui::{
    accent, bad, centered_rect, good, header_row, muted, popup_background, section_block,
    selected_style, text_colour, warn, wrapped,
};
use ratatui::prelude::*;
use ratatui::widgets::{Block, BorderType, Cell, Clear, Paragraph, Row, Table, Wrap};
use prost::Message;
use rusqlite::{Connection, Result as SqlResult};
use std::path::Path;

/// One `peer_chat_conversations` row. Mirrors
/// `src/database/sql_connection.py::list_conversations`.
#[derive(Debug, Clone)]
pub struct ConversationSummary {
    pub id: String,
    pub peer_id: String,
    pub topic: String,
    pub opened_by_us: bool,
    pub opened_at: String,
    /// `None` while open. Closing is local bookkeeping only (see
    /// `src/manager/chat.py::close_conversation`) -- nothing here means the peer
    /// agreed, or was even told.
    pub closed_at: Option<String>,
    /// Its most recent message's timestamp, or (if somehow it has none) when it was
    /// opened -- what the sidebar sorts by, so an old thread that just got a reply
    /// reads as current rather than staying buried under its own opening date.
    pub last_ts: i64,
}

/// One `peer_chat_messages` row within a single thread or a topic-less peer history,
/// as the conversation pane shows it. `ts` arrives already formatted -- the message's
/// own clock, not when this node happened to receive it.
#[derive(Debug, Clone)]
pub struct ChatMessageRow {
    pub from_us: bool,
    pub body: String,
    pub ts: String,
    /// A service shared in this message, drawn as a card under its text.
    pub service: Option<SharedService>,
}

/// A service shared in a chat message (issue #438): `ChatMessage.service`, the
/// service's own `Metadata`, as `peer_chat_messages.service_metadata` stores it,
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

/// The service-card columns, or two NULLs on a database the node has not added
/// them to yet -- see `column_exists` for why a missing column must not empty the
/// whole conversation.
fn service_columns(connection: &Connection) -> &'static str {
    if crate::app::column_exists(connection, "peer_chat_messages", "service_metadata") {
        "service_id, service_metadata"
    } else {
        "NULL, NULL"
    }
}

/// A peer's topic-less message history, as one row: `peer_chat_messages` rows with no
/// `conversation_id`, which predate conversations and still arrive whenever a peer
/// chats without naming a thread. Grouped by peer since there is no conversation row
/// to group them under.
#[derive(Debug, Clone)]
pub struct UntopicedSummary {
    pub peer_id: String,
    pub message_count: i64,
    pub last_ts: i64,
}

/// What one sidebar row *is*: a real thread, or a peer's topic-less history.
#[derive(Debug, Clone)]
pub enum ChatEntryKind {
    Conversation {
        conversation_id: String,
        opened_by_us: bool,
        closed_at: Option<String>,
    },
    Untopiced {
        peer_id: String,
    },
}

/// One row in the CHAT sidebar: a conversation and a topic-less peer bucket are the
/// same kind of thing here -- something with a peer, a topic (real or "(no topic)"),
/// and messages -- so both are this one type, told apart by `kind` only where the
/// difference actually matters (closing, replying).
#[derive(Debug, Clone)]
pub struct ChatEntry {
    /// The conversation id, or `untopic:<peer_id>` for a topic-less bucket -- stable
    /// across refreshes either way, which is what lets `StatefulList` keep the
    /// selection when the periodic reload replaces every entry.
    pub key: String,
    pub peer_id: String,
    /// Empty for a topic-less bucket, displayed as "(no topic)".
    pub topic: String,
    pub last_ts: i64,
    pub kind: ChatEntryKind,
}

impl Identifiable for ChatEntry {
    fn id(&self) -> &str {
        &self.key
    }
}

/// What a docked `ComposeChatMessage` box will do with the body once it is sent --
/// which of the three `nodo` subcommands to shell out to, and with what.
#[derive(Debug, Clone)]
pub enum ChatCompose {
    /// Step 3 of the new-chat wizard: `chat_open <peer> <topic> --message <body>`.
    NewConversation { peer_id: String, topic: String },
    /// A reply in an existing, open thread: `chat_reply <conversation_id> <body>`.
    Reply { conversation_id: String, peer_id: String },
    /// A reply to a peer's topic-less history: the flat `chat <peer> <body>`, since
    /// there is no conversation id to reply into.
    ReplyUntopiced { peer_id: String },
}

/// Threads on the CHAT page, both directions in one list -- `opened_by_us` rides
/// along on each row instead of picking which half of the table is shown, and
/// `last_ts` (this thread's most recent message, or its opening if it has none yet)
/// is what the merged sidebar actually sorts by.
pub fn get_conversations(database: &Path) -> SqlResult<Vec<ConversationSummary>> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer_chat_conversations") {
        return Ok(Vec::new());
    }
    let mut statement = connection.prepare(
        "SELECT c.id, c.peer_id, COALESCE(c.topic, ''), c.opened_by_us, c.opened_at, c.closed_at,
                COALESCE(MAX(m.ts), CAST(strftime('%s', c.opened_at) AS INTEGER)) AS last_ts
         FROM peer_chat_conversations c
         LEFT JOIN peer_chat_messages m ON m.conversation_id = c.id
         GROUP BY c.id",
    )?;
    let conversations = statement
        .query_map([], |row| {
            Ok(ConversationSummary {
                id: row.get(0)?,
                peer_id: row.get(1)?,
                topic: row.get(2)?,
                opened_by_us: row.get(3)?,
                opened_at: row.get(4)?,
                closed_at: row.get(5)?,
                last_ts: row.get(6)?,
            })
        })?
        .collect();
    conversations
}

/// One thread's messages, oldest first. Mirrors
/// `src/database/sql_connection.py::get_conversation_messages`.
pub fn get_conversation_messages(database: &Path, conversation_id: &str) -> SqlResult<Vec<ChatMessageRow>> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer_chat_messages") {
        return Ok(Vec::new());
    }
    let mut statement = connection.prepare(&format!(
        "SELECT from_us, body, ts, {}
         FROM peer_chat_messages
         WHERE conversation_id = ?1
         ORDER BY id ASC",
        service_columns(&connection)
    ))?;
    let messages = statement
        .query_map([conversation_id], |row| {
            let ts: i64 = row.get(2)?;
            Ok(ChatMessageRow {
                from_us: row.get(0)?,
                body: row.get(1)?,
                ts: crate::app::format_unix_timestamp(ts),
                service: SharedService::from_row(row.get(3)?, row.get(4)?),
            })
        })?
        .collect();
    messages
}

/// Every peer with topic-less history, one row each, most recent message first --
/// the CHAT page's only view onto `peer_chat_messages` rows with no
/// `conversation_id`, which the old table-of-conversations query could never match.
pub fn get_untopiced_summaries(database: &Path) -> SqlResult<Vec<UntopicedSummary>> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer_chat_messages") {
        return Ok(Vec::new());
    }
    let mut statement = connection.prepare(
        "SELECT peer_id, COUNT(*), MAX(ts)
         FROM peer_chat_messages
         WHERE conversation_id IS NULL
         GROUP BY peer_id",
    )?;
    let summaries = statement
        .query_map([], |row| {
            Ok(UntopicedSummary {
                peer_id: row.get(0)?,
                message_count: row.get(1)?,
                last_ts: row.get(2)?,
            })
        })?
        .collect();
    summaries
}

/// One peer's topic-less messages, oldest first -- the counterpart of
/// `get_conversation_messages` for a bucket with no conversation row of its own.
pub fn get_untopiced_messages(database: &Path, peer_id: &str) -> SqlResult<Vec<ChatMessageRow>> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer_chat_messages") {
        return Ok(Vec::new());
    }
    let mut statement = connection.prepare(&format!(
        "SELECT from_us, body, ts, {}
         FROM peer_chat_messages
         WHERE peer_id = ?1 AND conversation_id IS NULL
         ORDER BY id ASC",
        service_columns(&connection)
    ))?;
    let messages = statement
        .query_map([peer_id], |row| {
            let ts: i64 = row.get(2)?;
            Ok(ChatMessageRow {
                from_us: row.get(0)?,
                body: row.get(1)?,
                ts: crate::app::format_unix_timestamp(ts),
                service: SharedService::from_row(row.get(3)?, row.get(4)?),
            })
        })?
        .collect();
    messages
}

/// Every topic this peer has already used, most recently opened first -- what the
/// "pick a topic" wizard step offers alongside "+ New topic…". Never enforced (a
/// topic is a label, not a key), so this is convenience only: reusing one does not
/// reopen that thread, it just starts a new one with the same label.
pub fn get_conversation_topics(database: &Path, peer_id: &str) -> SqlResult<Vec<String>> {
    if !database.exists() {
        return Ok(Vec::new());
    }
    let connection = Connection::open(database)?;
    if !crate::app::table_exists(&connection, "peer_chat_conversations") {
        return Ok(Vec::new());
    }
    let mut statement = connection.prepare(
        "SELECT topic, MAX(opened_at) AS last_opened
         FROM peer_chat_conversations
         WHERE peer_id = ?1 AND topic <> ''
         GROUP BY topic
         ORDER BY last_opened DESC",
    )?;
    let topics = statement
        .query_map([peer_id], |row| row.get::<_, String>(0))?
        .collect();
    topics
}

/// The sidebar's whole model: every conversation and every topic-less bucket, merged
/// and sorted by `last_ts` descending -- a thread that just got a reply reads as
/// current, same as a peer who just sent a fresh topic-less message.
pub fn load_entries(database: &Path) -> Result<Vec<ChatEntry>, String> {
    let conversations = get_conversations(database).map_err(|error| error.to_string())?;
    let untopiced = get_untopiced_summaries(database).map_err(|error| error.to_string())?;

    let mut entries: Vec<ChatEntry> = conversations
        .into_iter()
        .map(|conversation| ChatEntry {
            key: conversation.id.clone(),
            peer_id: conversation.peer_id,
            topic: conversation.topic,
            last_ts: conversation.last_ts,
            kind: ChatEntryKind::Conversation {
                conversation_id: conversation.id,
                opened_by_us: conversation.opened_by_us,
                closed_at: conversation.closed_at,
            },
        })
        .collect();
    entries.extend(untopiced.into_iter().map(|summary| ChatEntry {
        key: format!("untopic:{}", summary.peer_id),
        peer_id: summary.peer_id.clone(),
        topic: String::new(),
        last_ts: summary.last_ts,
        kind: ChatEntryKind::Untopiced {
            peer_id: summary.peer_id,
        },
    }));
    entries.sort_by(|a, b| b.last_ts.cmp(&a.last_ts));
    Ok(entries)
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

    /// Step 1: start a new chat by picking a peer. `nodo chat_open` still mints the
    /// conversation and needs the peer named up front -- this just replaces the old
    /// one-line `<peer_id> <topic...>` prompt with three focused steps.
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

    pub(crate) fn submit_chat_peer_pick(&mut self) {
        let Some(peer_id) = self
            .filtered_chat_peers()
            .get(self.chat_wizard_peer_index)
            .map(|peer| peer.id.clone())
        else {
            self.status = "No peer matches; type less, or Esc to cancel".to_string();
            return;
        };
        self.chat_wizard_peer_id = Some(peer_id.clone());
        self.chat_wizard_topics =
            get_conversation_topics(&self.paths.database, &peer_id).unwrap_or_default();
        self.chat_wizard_topic_index = 0;
        self.input_mode = InputMode::PickChatTopic;
        self.input_title = format!("New chat with {} — pick a topic", shorten(&peer_id, 18));
        self.status = "↑/↓ choose • Enter next • Esc cancel".to_string();
    }

    /// Step 2: pick a topic already used with this peer, or start a new one. `+1` for
    /// the leading "+ New topic…" row, which is not itself one of `chat_wizard_topics`.
    pub fn move_chat_topic_selection(&mut self, delta: i32) {
        let count = self.chat_wizard_topics.len() + 1;
        let current = self.chat_wizard_topic_index as i32;
        self.chat_wizard_topic_index = (current + delta).rem_euclid(count as i32) as usize;
    }

    pub(crate) fn submit_chat_topic_pick(&mut self) {
        let Some(peer_id) = self.chat_wizard_peer_id.clone() else {
            self.close_input();
            return;
        };
        if self.chat_wizard_topic_index == 0 {
            self.input_mode = InputMode::NewChatTopic;
            self.input.clear();
            self.input_title = format!("New chat with {} — topic", shorten(&peer_id, 18));
            self.edit_kind = EditKind::Text;
            return;
        }
        let Some(topic) = self
            .chat_wizard_topics
            .get(self.chat_wizard_topic_index - 1)
            .cloned()
        else {
            self.close_input();
            return;
        };
        self.open_chat_compose(ChatCompose::NewConversation { peer_id, topic });
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
            ChatCompose::Reply { peer_id, .. } => format!("Reply to {}", shorten(peer_id, 18)),
            ChatCompose::ReplyUntopiced { peer_id } => {
                format!("Message to {} (no topic)", shorten(peer_id, 18))
            }
        };
        self.chat_compose = Some(target);
        self.input.clear();
        self.chat_attachment = None;
        self.edit_kind = EditKind::Text;
        self.back_to_compose();
    }

    /// Reply in the selected entry: an open thread, or a topic-less peer's flat
    /// history. Mirrors what `o` starts from scratch, but with the peer (and, for a
    /// thread, the topic) already settled.
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
        match entry.kind {
            ChatEntryKind::Conversation {
                conversation_id,
                closed_at,
                ..
            } => {
                if closed_at.is_some() {
                    self.status = "Closed; press R to reopen before replying".to_string();
                    return;
                }
                self.open_chat_compose(ChatCompose::Reply {
                    conversation_id,
                    peer_id: entry.peer_id,
                });
            }
            ChatEntryKind::Untopiced { peer_id } => {
                self.open_chat_compose(ChatCompose::ReplyUntopiced { peer_id });
            }
        }
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

    /// Close the selected conversation. Local bookkeeping only -- the peer is
    /// never told, and nothing here waits on a reply, so it is a direct action
    /// rather than a confirmation: reversible with `R`, unlike forgetting a peer.
    /// A no-op, not an error, on a topic-less bucket: there is no conversation row
    /// to close.
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
        let ChatEntryKind::Conversation {
            conversation_id,
            closed_at,
            ..
        } = entry.kind
        else {
            self.status = "Topic-less messages have no conversation to close".to_string();
            return;
        };
        if closed_at.is_some() {
            self.status = format!("{} is already closed", shorten(&conversation_id, 18));
            return;
        }
        self.spawn_command(
            CommandKind::Generic,
            "Close conversation".to_string(),
            vec!["chat_close".to_string(), conversation_id],
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
        let ChatEntryKind::Conversation {
            conversation_id,
            closed_at,
            ..
        } = entry.kind
        else {
            self.status = "Topic-less messages have no conversation to reopen".to_string();
            return;
        };
        if closed_at.is_none() {
            self.status = format!("{} is already open", shorten(&conversation_id, 18));
            return;
        }
        self.spawn_command(
            CommandKind::Generic,
            "Reopen conversation".to_string(),
            vec!["chat_reopen".to_string(), conversation_id],
        );
    }
}

/// The `nodo` invocation a sent compose box turns into: which of the three chat
/// subcommands, the text if there is any, and `--service` for an attached card.
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
        ChatCompose::Reply { conversation_id, .. } => (
            "Reply".to_string(),
            ["chat_reply".to_string(), conversation_id].into_iter().chain(text).collect(),
        ),
        ChatCompose::ReplyUntopiced { peer_id } => (
            "Message".to_string(),
            ["chat".to_string(), peer_id].into_iter().chain(text).collect(),
        ),
    };
    if let Some(service) = attachment {
        args.extend(["--service".to_string(), service.id.clone()]);
    }
    (label, args)
}

/// The clickable id column's `[start, end)` column range on the CHAT sidebar --
/// mirrors `peers::id_column_x`, over the sidebar's own fixed-width peer column.
pub fn id_column_x(area: Rect) -> (u16, u16) {
    let start = area.x + 1 /* left border */ + 2 /* "▸ " highlight gutter */;
    (start, (start + 16).min(area.x + area.width.saturating_sub(1)))
}

const SIDEBAR_WIDTH: u16 = 34;

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let split =
        Layout::horizontal([Constraint::Length(SIDEBAR_WIDTH), Constraint::Min(30)]).split(area);
    draw_sidebar(frame, app, split[0]);
    draw_conversation(frame, app, split[1]);
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
    let rows = app.conversations.items.iter().map(|entry| {
        let (glyph, row_color) = match &entry.kind {
            ChatEntryKind::Conversation {
                opened_by_us,
                closed_at,
                ..
            } => {
                let glyph = if *opened_by_us { "→" } else { "←" };
                let color = if closed_at.is_some() { muted() } else { good() };
                (glyph, color)
            }
            ChatEntryKind::Untopiced { .. } => ("•", muted()),
        };
        Row::new(vec![
            Cell::from(format!("{glyph} {}", short_peer_name(&entry.peer_id))),
            Cell::from(if entry.topic.is_empty() {
                "(no topic)".to_string()
            } else {
                entry.topic.clone()
            }),
        ])
        .style(Style::default().fg(row_color))
    });
    let table = Table::new(rows, [Constraint::Length(16), Constraint::Min(10)])
        .header(header_row(vec!["Chat", "Topic"]))
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
    app.id_column_x = Some(id_column_x(area));
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

    let status_word = match &entry.kind {
        ChatEntryKind::Conversation { closed_at, .. } => {
            if closed_at.is_some() {
                "closed"
            } else {
                "open"
            }
        }
        ChatEntryKind::Untopiced { .. } => "no topic",
    };
    let topic_display = if entry.topic.is_empty() { "(no topic)" } else { &entry.topic };
    let title = format!(
        " {}  •  {}  •  {} ",
        short_peer_name(&entry.peer_id),
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
        entry.peer_id.clone(),
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
    let split = Layout::vertical([
        Constraint::Min(3),
        Constraint::Length(compose_height),
    ])
    .split(inner);

    draw_messages(frame, app, split[0], &entry);
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
        short_peer_name(&entry.peer_id)
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
    let heights: Vec<u16> = segments
        .iter()
        .map(|segment| match segment {
            Segment::Text(lines) => text(lines.clone()).line_count(area.width) as u16,
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
            Segment::Text(lines) => frame.render_widget(text(lines).scroll((skip, 0)), rect),
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
    frame.render_widget(
        Paragraph::new(compose_lines(app))
            .wrap(Wrap { trim: false })
            .block(block)
            .style(Style::default().fg(text_colour())),
        split[0],
    );

    // Buttons to the right of the input, aligned with its first row of text.
    let buttons = split[1];
    let button = Style::default().fg(accent()).add_modifier(Modifier::BOLD);
    app.chat_attach_area = Rect::new(
        buttons.x + 1,
        buttons.y + 1,
        ATTACH_BUTTON.len() as u16,
        1,
    )
    .intersection(buttons);
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

/// Step 2's popup: this peer's existing topics, plus "+ New topic…" leading them.
pub fn draw_topic_picker(frame: &mut Frame, app: &App) {
    let area = centered_rect(60, (app.chat_wizard_topics.len() as u16 + 8).max(9), frame.size());
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

    let mut lines: Vec<Line> = Vec::new();
    let new_topic_selected = app.chat_wizard_topic_index == 0;
    lines.push(Line::from(vec![
        Span::styled(
            if new_topic_selected { "▸ " } else { "  " },
            Style::default().fg(accent()).bold(),
        ),
        Span::styled(
            "+ New topic…",
            if new_topic_selected {
                Style::default().fg(good()).bold()
            } else {
                Style::default().fg(good())
            },
        ),
    ]));
    for (index, topic) in app.chat_wizard_topics.iter().enumerate() {
        let selected = app.chat_wizard_topic_index == index + 1;
        lines.push(Line::from(vec![
            Span::styled(
                if selected { "▸ " } else { "  " },
                Style::default().fg(accent()).bold(),
            ),
            Span::styled(
                topic.clone(),
                if selected {
                    Style::default().fg(text_colour()).bold()
                } else {
                    Style::default().fg(muted())
                },
            ),
        ]));
    }
    if app.chat_wizard_topics.is_empty() {
        lines.push(Line::from(Span::styled(
            "No topic used with this peer yet.",
            Style::default().fg(muted()),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "↑/↓ choose  ·  ⏎ next  ·  Esc cancel",
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
        for table in ["peer", "peer_chat_conversations", "peer_chat_messages"] {
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
        }
    }

    #[test]
    fn conversations_carry_both_directions_against_the_real_schema() {
        let dir = temp_dir("schema");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        connection
            .execute(
                "INSERT INTO peer_chat_conversations (id, peer_id, opened_by_us, topic)
                 VALUES ('conv-ours', 'peer-1', 1, 'ping')",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO peer_chat_conversations (id, peer_id, opened_by_us, topic, closed_at)
                 VALUES ('conv-theirs', 'peer-1', 0, '', '2026-01-01 00:00:00')",
                [],
            )
            .unwrap();

        let conversations = get_conversations(&database).expect("must survive the real schema");
        assert_eq!(conversations.len(), 2, "both directions in one list now");
        let ours = conversations.iter().find(|c| c.id == "conv-ours").unwrap();
        assert!(ours.opened_by_us);
        assert_eq!(ours.topic, "ping");
        assert!(ours.closed_at.is_none());
        let theirs = conversations.iter().find(|c| c.id == "conv-theirs").unwrap();
        assert!(!theirs.opened_by_us);
        assert!(theirs.closed_at.is_some());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn conversation_messages_are_read_oldest_first() {
        let dir = temp_dir("messages");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        connection
            .execute(
                "INSERT INTO peer_chat_conversations (id, peer_id, opened_by_us)
                 VALUES ('conv-1', 'peer-1', 1)",
                [],
            )
            .unwrap();
        for (from_us, body, ts) in [(1, "hi", 1_700_000_000), (0, "hi back", 1_700_000_060)] {
            connection
                .execute(
                    "INSERT INTO peer_chat_messages (peer_id, from_us, body, ts, conversation_id)
                     VALUES ('peer-1', ?1, ?2, ?3, 'conv-1')",
                    rusqlite::params![from_us, body, ts],
                )
                .unwrap();
        }

        let messages = get_conversation_messages(&database, "conv-1").unwrap();
        assert_eq!(messages.len(), 2);
        assert!(messages[0].from_us);
        assert_eq!(messages[0].body, "hi");
        assert!(!messages[1].from_us);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn topicless_messages_are_grouped_by_peer_and_reachable_at_last() {
        // The whole point of `Untopiced`: a message with no `conversation_id` used to
        // be invisible in the TUI, reachable only through `nodo chat`/`show_chat`.
        let dir = temp_dir("untopiced");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        for (body, ts) in [("hello", 1_700_000_000), ("anyone there?", 1_700_000_100)] {
            connection
                .execute(
                    "INSERT INTO peer_chat_messages (peer_id, from_us, body, ts, conversation_id)
                     VALUES ('peer-1', 0, ?1, ?2, NULL)",
                    rusqlite::params![body, ts],
                )
                .unwrap();
        }

        let summaries = get_untopiced_summaries(&database).unwrap();
        assert_eq!(summaries.len(), 1);
        assert_eq!(summaries[0].peer_id, "peer-1");
        assert_eq!(summaries[0].message_count, 2);
        assert_eq!(summaries[0].last_ts, 1_700_000_100);

        let messages = get_untopiced_messages(&database, "peer-1").unwrap();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0].body, "hello");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_entries_merges_conversations_and_untopiced_by_recency() {
        let dir = temp_dir("merge");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        // An old conversation with no messages of its own (falls back to opened_at)...
        connection
            .execute(
                "INSERT INTO peer_chat_conversations (id, peer_id, opened_by_us, opened_at)
                 VALUES ('conv-old', 'peer-1', 1, '2020-01-01 00:00:00')",
                [],
            )
            .unwrap();
        // ...and a topic-less message far more recent than it.
        connection
            .execute(
                "INSERT INTO peer_chat_messages (peer_id, from_us, body, ts, conversation_id)
                 VALUES ('peer-1', 0, 'just now', 2000000000, NULL)",
                [],
            )
            .unwrap();

        let entries = load_entries(&database).unwrap();
        assert_eq!(entries.len(), 2);
        assert!(
            matches!(entries[0].kind, ChatEntryKind::Untopiced { .. }),
            "the more recent topic-less bucket sorts first"
        );
        assert_eq!(entries[0].key, "untopic:peer-1");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn get_conversation_topics_lists_distinct_topics_only() {
        let dir = temp_dir("topics");
        let database = chat_database(&dir);
        let connection = Connection::open(&database).unwrap();
        for (id, topic) in [("c1", "billing"), ("c2", "billing"), ("c3", "uptime")] {
            connection
                .execute(
                    "INSERT INTO peer_chat_conversations (id, peer_id, opened_by_us, topic)
                     VALUES (?1, 'peer-1', 1, ?2)",
                    rusqlite::params![id, topic],
                )
                .unwrap();
        }

        let topics = get_conversation_topics(&database, "peer-1").unwrap();
        assert_eq!(topics.len(), 2);
        assert!(topics.contains(&"billing".to_string()));
        assert!(topics.contains(&"uptime".to_string()));
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

    #[test]
    fn the_wizard_walks_peer_then_topic_then_compose() {
        let mut app = on_chat_page(vec![peer("peer-abc"), peer("peer-xyz")]);
        app.open_new_chat_wizard();
        assert_eq!(app.input_mode, InputMode::PickChatPeer);

        app.chat_wizard_peer_filter = "xyz".to_string();
        app.chat_peer_filter_changed();
        app.submit_chat_peer_pick();
        assert_eq!(app.input_mode, InputMode::PickChatTopic);
        assert_eq!(app.chat_wizard_peer_id.as_deref(), Some("peer-xyz"));

        // No prior topics with this peer, so index 0 is always "+ New topic…".
        app.submit_chat_topic_pick();
        assert_eq!(app.input_mode, InputMode::NewChatTopic);

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
    fn picking_an_existing_topic_skips_the_free_text_step() {
        let mut app = on_chat_page(vec![peer("peer-abc")]);
        app.open_new_chat_wizard();
        app.submit_chat_peer_pick();
        app.chat_wizard_topics = vec!["billing".to_string()];
        app.chat_wizard_topic_index = 1; // "billing", not "+ New topic…"

        app.submit_chat_topic_pick();

        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
        assert!(matches!(
            app.chat_compose,
            Some(ChatCompose::NewConversation { ref topic, .. }) if topic == "billing"
        ));
    }

    #[test]
    fn an_empty_compose_is_rejected_before_anything_is_sent() {
        let mut app = on_chat_page(vec![peer("peer-abc")]);
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "conv-1".to_string(),
            peer_id: "peer-abc".to_string(),
            topic: "ping".to_string(),
            last_ts: 0,
            kind: ChatEntryKind::Conversation {
                conversation_id: "conv-1".to_string(),
                opened_by_us: true,
                closed_at: None,
            },
        }]);
        app.conversations.next();

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
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "conv-1".to_string(),
            peer_id: "peer-1".to_string(),
            topic: "ping".to_string(),
            last_ts: 0,
            kind: ChatEntryKind::Conversation {
                conversation_id: "conv-1".to_string(),
                opened_by_us: true,
                closed_at: Some("2026-01-02 00:00:00".to_string()),
            },
        }]);
        app.conversations.next();

        app.open_reply_prompt();

        assert_eq!(app.input_mode, InputMode::Normal, "no compose opened");
        assert!(app.status.contains("reopen"), "{}", app.status);
    }

    #[test]
    fn replying_to_a_topicless_bucket_sends_the_flat_command() {
        let mut app = on_chat_page(Vec::new());
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "untopic:peer-1".to_string(),
            peer_id: "peer-1".to_string(),
            topic: String::new(),
            last_ts: 0,
            kind: ChatEntryKind::Untopiced { peer_id: "peer-1".to_string() },
        }]);
        app.conversations.next();

        app.open_reply_prompt();
        assert_eq!(app.input_mode, InputMode::ComposeChatMessage);
        assert!(matches!(app.chat_compose, Some(ChatCompose::ReplyUntopiced { .. })));

        // `c`/`R` have nothing to act on here.
        app.close_input();
        app.conversations.next();
        app.close_selected_conversation();
        assert!(app.status.contains("no conversation to close"), "{}", app.status);
    }

    #[test]
    fn closing_an_already_closed_conversation_is_a_no_op_not_a_command() {
        let mut app = on_chat_page(Vec::new());
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "conv-1".to_string(),
            peer_id: "peer-1".to_string(),
            topic: String::new(),
            last_ts: 0,
            kind: ChatEntryKind::Conversation {
                conversation_id: "conv-1".to_string(),
                opened_by_us: true,
                closed_at: Some("2026-01-02 00:00:00".to_string()),
            },
        }]);
        app.conversations.next();

        app.close_selected_conversation();

        assert!(app.command_task.is_none(), "nothing spawned");
        assert!(app.status.contains("already closed"), "{}", app.status);
    }

    #[test]
    fn reopening_an_already_open_conversation_is_a_no_op_not_a_command() {
        let mut app = on_chat_page(Vec::new());
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "conv-1".to_string(),
            peer_id: "peer-1".to_string(),
            topic: String::new(),
            last_ts: 0,
            kind: ChatEntryKind::Conversation {
                conversation_id: "conv-1".to_string(),
                opened_by_us: true,
                closed_at: None,
            },
        }]);
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
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "conv-1".to_string(),
            peer_id: "peer-1".to_string(),
            topic: String::new(),
            last_ts: 0,
            kind: ChatEntryKind::Conversation {
                conversation_id: "conv-1".to_string(),
                opened_by_us: true,
                closed_at: None,
            },
        }]);
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
    /// the topic-less bucket at once, and a conversation pane that wraps a multi-line
    /// message instead of clipping it -- the two concrete complaints this redesign
    /// was for.
    #[test]
    fn the_sidebar_shows_every_kind_of_chat_and_wraps_a_multiline_message() {
        use ratatui::{backend::TestBackend, Terminal};

        let mut app = on_chat_page(Vec::new());
        app.conversations = StatefulList::with_items(vec![
            ChatEntry {
                key: "conv-ours".to_string(),
                peer_id: "peer-ours".to_string(),
                topic: "billing".to_string(),
                last_ts: 3,
                kind: ChatEntryKind::Conversation {
                    conversation_id: "conv-ours".to_string(),
                    opened_by_us: true,
                    closed_at: None,
                },
            },
            ChatEntry {
                key: "conv-theirs".to_string(),
                peer_id: "peer-theirs".to_string(),
                topic: String::new(),
                last_ts: 2,
                kind: ChatEntryKind::Conversation {
                    conversation_id: "conv-theirs".to_string(),
                    opened_by_us: false,
                    closed_at: None,
                },
            },
            ChatEntry {
                key: "untopic:peer-flat".to_string(),
                peer_id: "peer-flat".to_string(),
                topic: String::new(),
                last_ts: 1,
                kind: ChatEntryKind::Untopiced { peer_id: "peer-flat".to_string() },
            },
        ]);
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
        app.conversations = StatefulList::with_items(vec![ChatEntry {
            key: "conv-1".to_string(),
            peer_id: "peer-1".to_string(),
            topic: "ping".to_string(),
            last_ts: 0,
            kind: ChatEntryKind::Conversation {
                conversation_id: "conv-1".to_string(),
                opened_by_us: true,
                closed_at: None,
            },
        }]);
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
        connection
            .execute(
                "INSERT INTO peer_chat_messages
                     (peer_id, from_us, body, ts, conversation_id, service_id, service_metadata)
                 VALUES ('peer-1', 0, 'try this', 1, NULL, ?1, ?2)",
                rusqlite::params![SERVICE_ID, metadata_bytes(&["hello-world", "demo"])],
            )
            .unwrap();

        let messages = get_untopiced_messages(&database, "peer-1").unwrap();

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
        connection
            .execute(
                "INSERT INTO peer_chat_messages
                     (peer_id, from_us, body, ts, conversation_id, service_id, service_metadata)
                 VALUES ('peer-1', 0, 'try this', 1, NULL, NULL, ?1)",
                [metadata_bytes(&["hello-world"])],
            )
            .unwrap();
        let mut app = on_chat_page(Vec::new());
        one_conversation(&mut app);
        app.conversation_messages = get_untopiced_messages(&database, "peer-1").unwrap();
        assert_eq!(app.conversation_messages[0].service.as_ref().unwrap().id, None);

        let screen = render(&mut app, 120, 30);

        assert!(screen.contains("hello-world"), "{screen}");
        assert!(screen.contains("no id of this node's hash type"), "{screen}");
        assert!(!screen.contains("[ Get ]"), "{screen}");
        assert!(app.chat_card_buttons.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    /// A database the node has not added the card columns to yet still reads --
    /// as plain messages -- rather than failing the whole conversation.
    #[test]
    fn a_database_without_the_card_columns_still_reads() {
        let dir = temp_dir("card-old-schema");
        let database = dir.join("database.sqlite");
        let connection = Connection::open(&database).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE peer_chat_messages (id INTEGER PRIMARY KEY, peer_id TEXT,
                     from_us INTEGER, body TEXT, ts INTEGER, conversation_id TEXT);
                 INSERT INTO peer_chat_messages (peer_id, from_us, body, ts, conversation_id)
                 VALUES ('peer-1', 1, 'hi', 1, 'conv-1');",
            )
            .unwrap();

        let messages = get_conversation_messages(&database, "conv-1").unwrap();

        assert_eq!(messages.len(), 1);
        assert!(messages[0].service.is_none());
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
            conversation_id: "conv-1".to_string(),
            peer_id: "peer-1".to_string(),
        };
        let (_, args) = chat_compose_command(target, String::new(), Some(&card()));
        assert_eq!(args, vec!["chat_reply", "conv-1", "--service", SERVICE_ID]);

        let target = ChatCompose::NewConversation {
            peer_id: "peer-1".to_string(),
            topic: "try this".to_string(),
        };
        let (_, args) = chat_compose_command(target, "look".to_string(), Some(&card()));
        assert_eq!(
            args,
            vec!["chat_open", "peer-1", "try this", "--message", "look", "--service", SERVICE_ID]
        );

        let target = ChatCompose::ReplyUntopiced { peer_id: "peer-1".to_string() };
        let (_, args) = chat_compose_command(target, "hi".to_string(), None);
        assert_eq!(args, vec!["chat", "peer-1", "hi"], "no attachment, no flag");
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
