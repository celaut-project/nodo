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

use crate::app::{shorten, App, CommandKind, EditKind, Identifiable, InputMode, Page};
use crate::peers::Peer;
use crate::ui::{
    accent, bad, centered_rect, good, header_row, muted, popup_background, section_block,
    selected_style, text_colour, warn, wrapped,
};
use ratatui::prelude::*;
use ratatui::widgets::{Block, BorderType, Cell, Clear, Paragraph, Row, Table, Wrap};
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
    let mut statement = connection.prepare(
        "SELECT from_us, body, ts
         FROM peer_chat_messages
         WHERE conversation_id = ?1
         ORDER BY id ASC",
    )?;
    let messages = statement
        .query_map([conversation_id], |row| {
            let ts: i64 = row.get(2)?;
            Ok(ChatMessageRow {
                from_us: row.get(0)?,
                body: row.get(1)?,
                ts: crate::app::format_unix_timestamp(ts),
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
    let mut statement = connection.prepare(
        "SELECT from_us, body, ts
         FROM peer_chat_messages
         WHERE peer_id = ?1 AND conversation_id IS NULL
         ORDER BY id ASC",
    )?;
    let messages = statement
        .query_map([peer_id], |row| {
            let ts: i64 = row.get(2)?;
            Ok(ChatMessageRow {
                from_us: row.get(0)?,
                body: row.get(1)?,
                ts: crate::app::format_unix_timestamp(ts),
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
        self.input_mode = InputMode::ComposeChatMessage;
        self.edit_kind = EditKind::Text;
        self.status = "Enter: newline • Ctrl+Enter: send • Esc: cancel".to_string();
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
        if body.trim().is_empty() {
            self.status = "Type a message first".to_string();
            return;
        }
        self.close_input();
        match target {
            ChatCompose::NewConversation { peer_id, topic } => {
                let label = format!("Open conversation with {}", shorten(&peer_id, 18));
                self.spawn_command(
                    CommandKind::Generic,
                    label,
                    vec![
                        "chat_open".to_string(),
                        peer_id,
                        topic,
                        "--message".to_string(),
                        body,
                    ],
                );
            }
            ChatCompose::Reply { conversation_id, .. } => {
                self.spawn_command(
                    CommandKind::Generic,
                    "Reply".to_string(),
                    vec!["chat_reply".to_string(), conversation_id, body],
                );
            }
            ChatCompose::ReplyUntopiced { peer_id } => {
                self.spawn_command(
                    CommandKind::Generic,
                    "Message".to_string(),
                    vec!["chat".to_string(), peer_id, body],
                );
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

    let composing = app.input_mode == InputMode::ComposeChatMessage;
    let compose_height = if composing {
        compose_box_height(&app.input, inner.width)
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

/// The selected chat's messages, oldest first, each split on its own embedded `\n`
/// (the old renderer never did, so a multi-line message was invisible past its first
/// line) and left to `Paragraph::wrap` for the rest -- a read-only transcript, so
/// nothing here is a value an operator is mid-typing (contrast `wrapped`, used for
/// the config editors specifically because reflowing an edited value would change it).
fn message_lines(app: &App, entry: &ChatEntry) -> Vec<Line<'static>> {
    if app.conversation_messages.is_empty() {
        return vec![Line::from(Span::styled(
            "No messages yet",
            Style::default().fg(muted()),
        ))];
    }
    let mut lines = Vec::new();
    for message in &app.conversation_messages {
        let who = if message.from_us {
            "us".to_string()
        } else {
            short_peer_name(&entry.peer_id)
        };
        let mut body_lines = message.body.split('\n');
        let first = body_lines.next().unwrap_or("");
        lines.push(Line::from(vec![
            Span::styled(format!("[{}] ", message.ts), Style::default().fg(muted())),
            Span::styled(format!("{who}: "), Style::default().fg(accent()).bold()),
            Span::raw(first.to_string()),
        ]));
        for continuation in body_lines {
            lines.push(Line::from(Span::raw(continuation.to_string())));
        }
    }
    lines
}

fn draw_messages(frame: &mut Frame, app: &App, area: Rect, entry: &ChatEntry) {
    let lines = message_lines(app, entry);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .style(Style::default().fg(text_colour())),
        area,
    );
}

/// How tall the docked compose box should be: every input line's own wrapped height,
/// clamped so one long paste cannot swallow the whole conversation pane.
fn compose_box_height(input: &str, width: u16) -> u16 {
    let usable = width.saturating_sub(2).max(1) as usize; // block borders
    let wrapped_lines: u16 = input
        .split('\n')
        .map(|line| wrapped(line, usable).len().max(1) as u16)
        .sum();
    (wrapped_lines + 2).clamp(3, 10)
}

fn draw_compose_box(frame: &mut Frame, app: &App, area: Rect) {
    let lines: Vec<Line> = app.input.split('\n').map(|line| Line::from(line.to_string())).collect();
    let block = Block::bordered()
        .border_style(Style::default().fg(accent()))
        .title(Span::styled(
            " COMPOSE · Enter: newline · Ctrl+Enter: send · Esc: cancel ",
            Style::default().fg(accent()).bold(),
        ));
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .block(block)
            .style(Style::default().fg(text_colour())),
        area,
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
}
