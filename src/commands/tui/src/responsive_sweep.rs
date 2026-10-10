//! Every page, and every popup that can sit on top of one, drawn at a grid of
//! terminal sizes from 1×1 up (issue #453).
//!
//! The pages are written for the 80×24 they were designed at, and nothing short of
//! drawing them small finds the subtraction that underflows at 9 columns. One test
//! per question rather than per page, so a regression names the size it broke at.

use crate::app::{
    App, DetailsView, InputMode, Instance, InstanceClient,
    InstanceUsage, Page, PendingAction, Service, StatefulList,
};
use crate::chat::{ChatCompose, ChatEntry, ChatEntryKind, ChatMessageRow, ChatService, SharedService};
use crate::clients::Client;
use crate::peer_resources::{Announced, ArchOffer};
use crate::peers::Peer;
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::Terminal;
use std::path::Path;

pub(crate) const WIDTHS: [u16; 9] = [1, 2, 10, 20, 40, 60, 80, 120, 200];
pub(crate) const HEIGHTS: [u16; 8] = [1, 2, 3, 5, 10, 24, 40, 60];

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../../.."))
}

fn instance(id: &str, father: &str) -> Instance {
    Instance {
        id: id.to_string(),
        name: format!("worker-{id}"),
        ip: "10.0.0.7:4040".to_string(),
        service: "builder-service-with-a-long-name".to_string(),
        balance: "1000".to_string(),
        virtualizer: "ch".to_string(),
        memory_limit: 1 << 30,
        disk_limit: 10 << 30,
        vcpus: Some(2.0),
        usage: InstanceUsage {
            cpu_percent: Some(42.0),
            memory_current: Some(1 << 28),
            ..InstanceUsage::default()
        },
        location: "local".to_string(),
        father_id: father.to_string(),
        mu_per_minute: Some(3.0),
        mu_per_hour: Some(180.0),
        consumption_samples: None,
        consumption_age_secs: None,
        energy_watts: Some(12.0),
        energy_share: Some(0.25),
        age_secs: Some(3600.0),
        client: InstanceClient::Client("client-🦀-東京".to_string()),
    }
}

fn peer(id: &str) -> Peer {
    Peer {
        id: id.to_string(),
        uris: "10.0.0.1:8080, 192.168.100.200:9090".to_string(),
        balance: "123456".to_string(),
        remote_client_id: format!("remote-{id}"),
        local_client_id: String::new(),
        proof_ids: vec!["proof-a".to_string()],
        reputation_score: "7".to_string(),
        contracts: Vec::new(),
        resources: Default::default(),
    }
}

/// What peer `n` announced (issue #455): the first, which is the one selected, both
/// architectures with every benchmark key; then a mix of the silent and the
/// unreadable among peers that announced one architecture, some of it unstated.
fn announced(n: usize) -> Announced {
    const GIB: u64 = 1 << 30;
    let bench = |pairs: &[(&str, u64)]| pairs.iter().map(|(key, value)| (key.to_string(), *value)).collect();
    match n % 5 {
        0 if n == 0 => Announced::Declared(vec![
            ArchOffer {
                arch: "linux/amd64".to_string(),
                millicores: Some(16_000),
                mem_bytes: Some(64 * GIB),
                disk_bytes: Some(2048 * GIB),
                benchmark: bench(&[
                    ("int_ops_per_sec", 1_840_000_000),
                    ("flt_ops_per_sec", 920_000_000),
                    ("mem_bandwidth_64mib_bytes_per_sec", 21 * GIB),
                    ("mem_bandwidth_256mib_bytes_per_sec", 14 * GIB),
                    ("mem_bandwidth_1gib_bytes_per_sec", 9 * GIB),
                    ("sha256_hashes_per_sec", 3_100_000),
                ]),
            },
            ArchOffer {
                arch: "linux/arm64".to_string(),
                millicores: Some(16_000),
                mem_bytes: Some(64 * GIB),
                disk_bytes: Some(2048 * GIB),
                benchmark: bench(&[("int_ops_per_sec", 96_000_000)]),
            },
        ]),
        1 => Announced::Undeclared,
        2 => Announced::Unreadable,
        3 => Announced::Declared(vec![ArchOffer {
            arch: "linux/arm64".to_string(),
            millicores: Some(4_000),
            mem_bytes: Some(8 * GIB),
            disk_bytes: None,
            benchmark: Vec::new(),
        }]),
        _ => Announced::Declared(vec![ArchOffer {
            arch: "linux/amd64".to_string(),
            millicores: Some(8_000),
            mem_bytes: Some(32 * GIB),
            disk_bytes: Some(500 * GIB),
            benchmark: bench(&[("int_ops_per_sec", 1_500_000_000)]),
        }]),
    }
}

/// What this node announces, as `nodo resources --json` would report it: both
/// architectures, arm64 unbenchmarked.
fn own_announcement() -> Announced {
    const GIB: u64 = 1 << 30;
    Announced::Declared(vec![
        ArchOffer {
            arch: "linux/amd64".to_string(),
            millicores: Some(12_000),
            mem_bytes: Some(48 * GIB),
            disk_bytes: Some(1024 * GIB),
            benchmark: vec![("int_ops_per_sec".to_string(), 1_700_000_000)],
        },
        ArchOffer {
            arch: "linux/arm64".to_string(),
            millicores: Some(8_000),
            mem_bytes: Some(16 * GIB),
            disk_bytes: Some(512 * GIB),
            benchmark: Vec::new(),
        },
    ])
}

fn client(id: &str) -> Client {
    Client {
        id: id.to_string(),
        balance: "5000".to_string(),
        last_usage: "2026-09-30 12:00".to_string(),
        unmetered: false,
    }
}

fn service(id: &str) -> Service {
    Service {
        id: id.to_string(),
        tag: format!("tag-{id}"),
        size_bytes: 4096,
        total_size_bytes: Some(1 << 20),
    }
}

/// An `App` with something on every page: a table drawn empty takes a different
/// path through the layout from one with rows in it.
pub(crate) fn populated_app() -> App {
    let mut app = App::default();
    let ids: Vec<String> = (0..30).map(|n| format!("{n:02}f4e2c9a1b7d3e5f60718293a4b5c6d7e8f9")).collect();
    app.instances = StatefulList::with_items(
        ids.iter()
            .enumerate()
            .map(|(n, id)| instance(id, if n % 3 == 1 { &ids[n - 1] } else { "" }))
            .collect(),
    );
    app.services = StatefulList::with_items(ids.iter().map(|id| service(id)).collect());
    app.peers = StatefulList::with_items(
        ids.iter()
            .enumerate()
            .map(|(n, id)| Peer { resources: announced(n), ..peer(id) })
            .collect(),
    );
    app.clients = StatefulList::with_items(ids.iter().map(|id| client(id)).collect());
    app.own_resources.announced = Some(own_announcement());
    app.conversations = StatefulList::with_items(
        ids.iter()
            .take(8)
            .enumerate()
            .map(|(n, id)| ChatEntry {
                key: format!("conv-{n}"),
                peer_id: id.clone(),
                topic: format!("topic {n} — a fairly long subject line 🦀"),
                last_ts: 1_700_000_000 + n as i64,
                kind: ChatEntryKind::Conversation {
                    conversation_id: format!("conv-{n}"),
                    opened_by_us: n % 2 == 0,
                    closed_at: None,
                },
            })
            .collect(),
    );
    app.conversation_messages = (0..12)
        .map(|n| ChatMessageRow {
            from_us: n % 2 == 0,
            body: format!("message {n}: a body long enough to wrap at most widths, with 東京 and 🦀 in it"),
            ts: "2026-09-30 12:00".to_string(),
            service: (n == 3).then(|| SharedService {
                id: Some(ids[0].clone()),
                tags: vec!["shared-tag".to_string()],
                hash_types: vec!["sha3_256".to_string()],
                format: Vec::new(),
                reputation_proofs: 1,
            }),
        })
        .collect();
    let config = repo_root().join("config.example.yaml");
    app.config_document = std::fs::read_to_string(&config)
        .ok()
        .and_then(|text| serde_yaml::from_str(&text).ok());
    app.config_all = crate::app::get_config_entries(&config).unwrap_or_default();
    let (prices, scarcity) = crate::app::get_prices(&config);
    app.prices = StatefulList::with_items(prices);
    app.scarcity = scarcity;
    app.app_logs = (0..50).map(|n| format!("app log line {n} with some text")).collect();
    app.node_logs = (0..50).map(|n| format!("node log line {n} with some text")).collect();
    app.docs = crate::docs::DocsState::at(&repo_root().join("docs"));
    app.node_info.service_status = "running".to_string();
    app
}

/// The page, with its first row selected the way a keypress would.
pub(crate) fn on_page(page: Page) -> App {
    let mut app = populated_app();
    app.tabs.index = Page::ALL.iter().position(|candidate| *candidate == page).unwrap();
    app.instances.next();
    app.services.next();
    app.peers.next();
    app.clients.next();
    app.conversations.next();
    app.prices.next();
    if page == Page::Docs {
        app.docs_enter();
    }
    app
}

/// Every popup and docked editor, as `(name, page it opens on, setup)`.
#[allow(clippy::type_complexity)]
pub(crate) fn overlays() -> Vec<(&'static str, Page, Box<dyn Fn(&mut App)>)> {
    vec![
        ("confirm", Page::Instances, Box::new(|app: &mut App| {
            app.pending_action = Some(PendingAction::KillInstance {
                id: "8f4e2c".to_string(),
                label: "worker with a very long label that will not fit 🦀".to_string(),
            });
            app.input_mode = InputMode::Confirm;
        })),
        ("details", Page::Services, Box::new(|app: &mut App| {
            app.details = Some(DetailsView {
                title: "nodo inspect".to_string(),
                lines: (0..80).map(|n| format!("detail line {n} — 東京 🦀 long enough to need truncating somewhere")).collect(),
                scroll: 3,
            });
            app.input_mode = InputMode::Details;
        })),
        ("kya", Page::Overview, Box::new(|app: &mut App| {
            app.input_mode = InputMode::AcceptKya;
        })),
        ("edit-config", Page::Config, Box::new(|app: &mut App| {
            app.input_title = "Edit network.DELEGATION_TUNNEL_POLICY".to_string();
            app.input = "auto".to_string();
            app.input_mode = InputMode::EditConfig;
        })),
        ("filter-config", Page::Config, Box::new(|app: &mut App| {
            app.input = "gateway".to_string();
            app.input_mode = InputMode::FilterConfig;
        })),
        ("connect", Page::Peers, Box::new(|app: &mut App| {
            app.input = "10.0.0.1:8080".to_string();
            app.input_mode = InputMode::Connect;
        })),
        ("credit", Page::Clients, Box::new(|app: &mut App| {
            app.credit_client_id = Some("client-1".to_string());
            app.input = "100".to_string();
            app.input_mode = InputMode::CreditClient;
        })),
        ("get-service", Page::Services, Box::new(|app: &mut App| {
            app.input_mode = InputMode::GetService;
        })),
        ("search-docs", Page::Docs, Box::new(|app: &mut App| {
            app.input = "install".to_string();
            app.input_mode = InputMode::SearchDocs;
        })),
        ("profile", Page::Cell, Box::new(|app: &mut App| {
            app.input_mode = InputMode::PickProfile;
        })),
        ("lever-key", Page::Cell, Box::new(|app: &mut App| {
            app.lever_keys = vec!["network.DELEGATION_TUNNEL_POLICY", "hashing.HASH", "ui.THEME"];
            app.input_mode = InputMode::PickLeverKey;
        })),
        ("assets", Page::Cell, Box::new(|app: &mut App| {
            app.input_mode = InputMode::EditAssets;
        })),
        ("asset-form", Page::Cell, Box::new(|app: &mut App| {
            app.asset_form.error = Some("the rate must be a positive number".to_string());
            app.input_mode = InputMode::AddAsset;
        })),
        ("execute-envs", Page::Services, Box::new(|app: &mut App| {
            let spec = |name: &str, required: bool| crate::env_form::EnvSpec {
                name: name.to_string(),
                tags: vec!["text".to_string()],
                prose: "a fairly long explanation of what the variable is for 🦀 東京".to_string(),
                required,
                networks: if required { vec!["pow:ergo".to_string()] } else { Vec::new() },
            };
            let mut specs = vec![spec("A_VERY_LONG_VARIABLE_NAME_INDEED", true)];
            specs.extend((0..10).map(|n| spec(&format!("VAR_{n}"), false)));
            app.env_form = crate::env_form::EnvForm::new("svc".to_string(), "svc".to_string(), specs);
            app.env_form.values[0] = "x".repeat(200);
            app.env_form.revealed = true;
            app.env_form.error = Some("A_VERY_LONG_VARIABLE_NAME_INDEED is required".to_string());
            app.input_mode = InputMode::ExecuteEnvs;
        })),
        ("chat-peer", Page::Chat, Box::new(|app: &mut App| {
            app.input_mode = InputMode::PickChatPeer;
        })),
        ("chat-topic", Page::Chat, Box::new(|app: &mut App| {
            app.chat_wizard_peer_id = Some("peer-1".to_string());
            app.chat_wizard_topics = (0..12).map(|n| format!("topic {n}")).collect();
            app.input_mode = InputMode::PickChatTopic;
        })),
        ("chat-new-topic", Page::Chat, Box::new(|app: &mut App| {
            app.input_mode = InputMode::NewChatTopic;
        })),
        ("chat-compose", Page::Chat, Box::new(|app: &mut App| {
            app.chat_compose = Some(ChatCompose::Reply {
                conversation_id: "conv-0".to_string(),
                peer_id: "peer-1".to_string(),
            });
            app.input = "line one\nline two that is fairly long 🦀 東京\nthree".to_string();
            app.chat_attachment = Some(ChatService {
                id: "svc".to_string(),
                tags: vec!["tag".to_string()],
            });
            app.input_mode = InputMode::ComposeChatMessage;
        })),
        ("chat-service", Page::Chat, Box::new(|app: &mut App| {
            app.chat_compose = Some(ChatCompose::ReplyUntopiced { peer_id: "peer-1".to_string() });
            app.input_mode = InputMode::PickChatService;
        })),
        ("instance-tree", Page::Instances, Box::new(|app: &mut App| {
            app.instances_grouped = true;
        })),
        ("confirm-writes", Page::Cell, Box::new(|app: &mut App| {
            app.details = Some(DetailsView {
                title: "apply OPEN RENTER".to_string(),
                lines: (0..14).map(|n| format!("network.KEY_{n}: old → new 東京")).collect(),
                scroll: 0,
            });
            app.input_mode = InputMode::ConfirmWrites;
        })),
        ("add-config-item", Page::Config, Box::new(|app: &mut App| {
            app.input_title = "Add to network.ALLOWED_HOSTS".to_string();
            app.input_mode = InputMode::AddConfigItem;
        })),
        ("add-custom-unit", Page::Config, Box::new(|app: &mut App| {
            app.input_mode = InputMode::AddCustomUnit;
        })),
        ("docs-index", Page::Docs, Box::new(|app: &mut App| {
            app.docs.focus = crate::docs::Focus::Index;
        })),
        ("context-menu", Page::Services, Box::new(|app: &mut App| {
            app.context_menu = Some(crate::context_menu::ContextMenu {
                items: crate::context_menu::page_actions(Page::Services),
                selected: 0,
                anchor: ratatui::layout::Position::new(5, 5),
                item_areas: Vec::new(),
            });
            app.input_mode = InputMode::ContextMenu;
        })),
    ]
}

pub(crate) fn draw(app: &mut App, width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| crate::ui::render(app, frame)).unwrap();
    terminal.backend().buffer().clone()
}

pub(crate) fn text(buffer: &Buffer) -> String {
    (0..buffer.area.height)
        .map(|row| {
            (0..buffer.area.width)
                .map(|column| buffer.get(column, row).symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The text of one screen row.
fn row_text(buffer: &Buffer, row: u16) -> String {
    (0..buffer.area.width).map(|column| buffer.get(column, row).symbol()).collect()
}

/// The payload of a caught panic, as text.
fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|text| text.to_string()))
        .unwrap_or_default()
}

/// Draw `app` at every size in the grid, largest first and then back up, as a live
/// resize would: one `App` across the whole sweep, so state a frame leaves behind
/// (scroll offsets, hit areas) is carried into the next size the way it is in a
/// real terminal. `check` sees each frame; every failure is collected rather than
/// the first, so one sweep names every size a state breaks at.
fn sweep(name: &str, build: &dyn Fn() -> App, check: &dyn Fn(&App, &Buffer) -> Result<(), String>) -> Vec<String> {
    let mut sizes: Vec<(u16, u16)> = Vec::new();
    for width in WIDTHS.iter().rev() {
        for height in HEIGHTS.iter().rev() {
            sizes.push((*width, *height));
        }
    }
    let mut failures = Vec::new();
    let mut app = build();
    for (width, height) in sizes {
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| draw(&mut app, width, height)));
        match outcome {
            Ok(buffer) => {
                if let Err(reason) = check(&app, &buffer) {
                    failures.push(format!("{name} at {width}x{height}: {reason}\n{}", text(&buffer)));
                }
            }
            Err(payload) => {
                failures.push(format!("{name} at {width}x{height}: panicked: {}", panic_message(payload)));
                app = build();
            }
        }
    }
    failures
}

/// Rows the chrome takes above a page: the bordered group row, and the page row when
/// the group has more than one page.
fn chrome_top(app: &App) -> u16 {
    3 + u16::from(app.tabs.group().pages().len() > 1)
}

/// What every frame must show, whatever the page: below the minimum, the notice and
/// nothing else; at or above it, the open group's title on the group row, the open
/// page's on the page row, and a footer that is not blank.
fn chrome_is_intact(app: &App, buffer: &Buffer) -> Result<(), String> {
    let area = buffer.area;
    let screen = text(buffer);
    if crate::layout_util::too_small(area) {
        if !app.too_small {
            return Err("too small, but not flagged as such".into());
        }
        if screen.contains("NODO") {
            return Err("the page was drawn under the notice".into());
        }
        if area.width >= 18 && !screen.contains("Terminal too small") {
            return Err("no notice".into());
        }
        if area.width >= 30 && area.height >= 3 && !screen.contains(&format!("needs {}×{}", crate::layout_util::MIN_WIDTH, crate::layout_util::MIN_HEIGHT)) {
            return Err("the notice does not say what size is needed".into());
        }
        return Ok(());
    }
    if app.too_small {
        return Err("flagged too small at a usable size".into());
    }
    let group = app.tabs.group();
    let groups = row_text(buffer, 1);
    if !groups.contains(group.title()) && !groups.contains(group.short_title()) {
        return Err(format!("group row does not name {group:?}"));
    }
    if group.pages().len() > 1 {
        let page = app.page();
        let pages = row_text(buffer, 3);
        if !pages.contains(page.title()) && !pages.contains(page.short_title()) {
            return Err(format!("page row does not name {page:?}"));
        }
    }
    for row in [area.height - 2, area.height - 1] {
        if row_text(buffer, row).trim().is_empty() {
            return Err(format!("footer row {row} is blank"));
        }
    }
    Ok(())
}

/// Every area a click is resolved against lies on the page itself -- below the tab
/// rows, above the footer, inside the screen -- so a click on the chrome can never
/// land on something the page recorded for a row it did not draw.
fn hit_areas_are_on_the_page(app: &App, buffer: &Buffer) -> Result<(), String> {
    if app.too_small {
        return Ok(());
    }
    let area = buffer.area;
    let top = chrome_top(app);
    let page = ratatui::layout::Rect::new(0, top, area.width, area.height.saturating_sub(top + 2));
    let mut areas: Vec<(&str, ratatui::layout::Rect)> = vec![("list", app.list_area)];
    areas.extend(app.id_copy_areas.iter().map(|(_, rect)| ("id copy", *rect)));
    areas.extend(app.chat_card_buttons.iter().map(|(_, rect)| ("card button", *rect)));
    areas.push(("attach", app.chat_attach_area));
    areas.push(("send", app.chat_send_area));
    areas.push(("peers refresh", app.peers_refresh_area));
    areas.extend(app.energy_row_areas.iter().map(|(_, rect)| ("energy row", *rect)));
    areas.extend(app.price_bar_areas.iter().map(|(_, rect)| ("price bar", *rect)));
    areas.extend(app.payment_rate_areas.iter().map(|(_, rect)| ("payment rate", *rect)));
    areas.extend(app.schedule_edge_areas.iter().map(|(_, _, rect)| ("schedule edge", *rect)));
    areas.extend(app.schedule_remove_areas.iter().map(|(_, rect)| ("schedule remove", *rect)));
    areas.push(("schedule add", app.schedule_add_area));
    areas.push(("schedule enabled", app.schedule_enabled_area));
    areas.push(("schedule closing", app.schedule_on_close_area));
    areas.extend(app.cell.organelle_areas.iter().map(|(_, rect)| ("organelle", *rect)));
    areas.extend(app.cell.lever_areas.iter().map(|(_, _, rect)| ("lever", *rect)));
    if app.page() == Page::Docs {
        areas.push(("docs index", app.docs.index_area));
        areas.push(("docs page", app.docs.page_area));
    }
    for (name, rect) in areas {
        if rect.is_empty() {
            continue;
        }
        if rect.intersection(page) != rect {
            return Err(format!("{name} area {rect:?} is not inside the page {page:?}"));
        }
    }
    if let Some((start, end)) = app.id_column_x {
        let list = app.list_area;
        if start < list.x || end > list.right() || start >= end {
            return Err(format!("id column {start}..{end} is not inside the table {list:?}"));
        }
    }
    Ok(())
}

/// The selected row is on screen: the table drew its highlight symbol inside the
/// area it recorded for clicks.
fn selection_is_visible(app: &App, buffer: &Buffer) -> Result<(), String> {
    if app.too_small {
        return Ok(());
    }
    let list = app.list_area;
    if list.is_empty() {
        return Err("no table recorded".into());
    }
    let symbol = if app.page() == Page::Pricing { ">" } else { "▸" };
    let found = (list.y..list.bottom())
        .any(|row| (list.x..list.right()).any(|column| buffer.get(column, row).symbol() == symbol));
    if found {
        Ok(())
    } else {
        Err(format!("no selected row inside the table {list:?}"))
    }
}

#[test]
fn every_page_draws_at_every_size() {
    let mut failures = Vec::new();
    for page in Page::ALL {
        failures.extend(sweep(&format!("{page:?}"), &|| on_page(page), &|app, buffer| {
            chrome_is_intact(app, buffer)?;
            hit_areas_are_on_the_page(app, buffer)
        }));
    }
    assert!(failures.is_empty(), "{} failures:\n{}", failures.len(), failures.join("\n\n"));
}

/// Every table page keeps its selection on screen at every usable size, with a row
/// selected far down the list -- the case a short terminal hides.
#[test]
fn the_selected_row_stays_visible_at_every_size() {
    let mut failures = Vec::new();
    for page in [Page::Instances, Page::Services, Page::Peers, Page::Clients, Page::Chat, Page::Pricing] {
        let build = move || {
            let mut app = on_page(page);
            for _ in 0..6 {
                app.instances.next();
                app.services.next();
                app.peers.next();
                app.clients.next();
                app.conversations.next();
                app.prices.next();
            }
            app
        };
        failures.extend(sweep(&format!("{page:?}"), &build, &selection_is_visible));
    }
    // ENERGY is three blocks rather than a table: its last key must still be on
    // screen (it was clipped off the bottom of an 80×24 terminal before #453).
    let last = crate::energy::entries().len() - 1;
    let build = move || {
        let mut app = on_page(Page::Energy);
        app.energy_selected = last;
        app
    };
    failures.extend(sweep("Energy", &build, &|app, _| {
        if app.too_small || app.energy_row_areas.iter().any(|(index, _)| *index == last) {
            Ok(())
        } else {
            Err("the selected energy key is not on screen".into())
        }
    }));
    assert!(failures.is_empty(), "{} failures:\n{}", failures.len(), failures.join("\n\n"));
}

#[test]
fn every_overlay_draws_at_every_size() {
    let mut failures = Vec::new();
    for (name, page, setup) in overlays() {
        let build = move || {
            let mut app = on_page(page);
            setup(&mut app);
            app
        };
        failures.extend(sweep(name, &build, &|app, buffer| {
            if app.too_small {
                return chrome_is_intact(app, buffer);
            }
            if let Some(menu) = &app.context_menu {
                for item in &menu.item_areas {
                    if item.intersection(buffer.area) != *item {
                        return Err(format!("menu entry {item:?} is off screen"));
                    }
                }
            }
            Ok(())
        }));
    }
    assert!(failures.is_empty(), "{} failures:\n{}", failures.len(), failures.join("\n\n"));
}

/// The notice at a few sizes below the minimum, and the page again once there is
/// room: a resize needs no restart, because every frame is laid out from scratch.
#[test]
fn a_resize_reflows_the_page_and_keeps_the_selection() {
    let mut app = on_page(Page::Instances);
    for _ in 0..25 {
        app.instances.next();
    }
    let selected = app.instances.selected().unwrap().id[..3].to_string();
    let selected_row = |buffer: &Buffer| {
        (0..buffer.area.height)
            .map(|row| row_text(buffer, row))
            .find(|line| line.contains('▸'))
            .unwrap_or_default()
    };

    let wide = draw(&mut app, 160, 40);
    assert!(row_text(&wide, 1).contains("OVERVIEW"), "{}", text(&wide));
    assert!(row_text(&wide, 5).contains("Net ↓/↑ per s"), "{}", text(&wide));
    assert!(selected_row(&wide).contains(&selected), "{}", text(&wide));

    // Narrower and shorter: the short tab titles, the low-priority columns gone, the
    // same row still selected and on screen.
    let narrow = draw(&mut app, 48, 14);
    assert!(row_text(&narrow, 1).contains("WORK"), "{}", text(&narrow));
    assert!(!row_text(&narrow, 1).contains("WORKLOAD"), "{}", text(&narrow));
    assert!(!text(&narrow).contains("Net ↓/↑"), "{}", text(&narrow));
    assert!(selected_row(&narrow).contains(&selected), "{}", text(&narrow));
    assert_eq!(app.instances.state.selected(), Some(25));

    // Too small: the notice, and clicks are ignored.
    let tiny = draw(&mut app, 30, 8);
    assert!(text(&tiny).contains("Terminal too small"), "{}", text(&tiny));
    assert!(app.too_small);

    // And back: the full layout again, the same row selected. (Scrolled where the
    // short terminal left it, as any table is after a resize -- not redrawn from the
    // top.)
    let again = draw(&mut app, 160, 40);
    assert!(!app.too_small);
    assert!(row_text(&again, 1).contains("OVERVIEW"), "{}", text(&again));
    assert!(row_text(&again, 5).contains("Net ↓/↑ per s"), "{}", text(&again));
    assert!(selected_row(&again).contains(&selected), "{}", text(&again));
}

/// Each table gives way in its own declared order: at any width the columns kept are
/// exactly the most important ones that fit, never a less important column in place
/// of a more important one.
#[test]
fn columns_give_way_by_priority() {
    let tables: Vec<(&str, Vec<crate::layout_util::Column>)> = vec![
        ("instances", crate::ui::INSTANCE_COLUMNS.to_vec()),
        ("services", crate::ui::SERVICE_COLUMNS.to_vec()),
        ("peers", crate::peers::PEER_COLUMNS.to_vec()),
        ("clients", crate::clients::CLIENT_COLUMNS.to_vec()),
        ("chat", crate::chat::SIDEBAR_COLUMNS.to_vec()),
    ];
    for (name, columns) in tables {
        let mut priorities: Vec<u8> = columns.iter().map(|column| column.priority).collect();
        priorities.sort();
        priorities.dedup();
        assert_eq!(priorities.len(), columns.len(), "{name}: two columns share a priority");
        let mut previous = columns.len();
        for available in (0..=220).rev() {
            let fitted = crate::layout_util::fit_columns(&columns, available);
            let worst_kept = fitted.keep.iter().map(|index| columns[*index].priority).max().unwrap();
            for (index, column) in columns.iter().enumerate() {
                assert_eq!(
                    fitted.keep.contains(&index),
                    column.priority <= worst_kept,
                    "{name} at {available}: kept {:?}",
                    fitted.keep
                );
            }
            assert!(fitted.keep.len() <= previous, "{name}: a column came back at {available}");
            previous = fitted.keep.len();
            // The most important column is never the one that goes.
            let first = columns.iter().position(|column| column.priority == priorities[0]).unwrap();
            assert!(fitted.keep.contains(&first), "{name} at {available}");
            // Kept columns fit, at no less than their minimum, unless only one is left.
            if fitted.keep.len() > 1 {
                let used: u16 = fitted.widths.iter().sum::<u16>() + fitted.widths.len() as u16 - 1;
                assert!(used <= available, "{name} at {available}: {fitted:?}");
                for (position, index) in fitted.keep.iter().enumerate() {
                    assert!(fitted.widths[position] >= columns[*index].min, "{name} at {available}: {fitted:?}");
                }
            }
        }
    }
}

/// What each table shows at 40 columns -- the narrowest the console draws -- and at
/// 80, read off a real render.
#[test]
fn each_table_keeps_its_most_important_columns() {
    let header = |page: Page, width: u16| {
        let mut app = on_page(page);
        let buffer = draw(&mut app, width, 30);
        row_text(&buffer, app.list_area.y + 1)
    };
    let cases: [(Page, u16, &[&str], &[&str]); 8] = [
        (Page::Instances, 40, &["Name", "Instance", "Service", "CPU%"], &["VM", "Burn", "Net", "Up"]),
        (Page::Instances, 80, &["Name", "Loca", "Instance", "Service", "CPU%", "Left", "RAM now"], &["VM", "Burn/h", "Net"]),
        (Page::Peers, 40, &["Peer ID", "Rep"], &["Reputation"]),
        (Page::Peers, 80, &["Peer ID", "Endpoints", "Our balance", "Rep", "Reputation proofs"], &[]),
        (Page::Services, 40, &["Tag", "Content ID", "With b"], &["Stored"]),
        (Page::Clients, 40, &["Client ID", "Balance"], &["Metering"]),
        (Page::Clients, 80, &["Client ID", "Balance", "Last usage", "Metering"], &[]),
        (Page::Chat, 40, &["Chat", "Topic"], &[]),
    ];
    for (page, width, shown, hidden) in cases {
        let header = header(page, width);
        for column in shown {
            assert!(header.contains(column), "{page:?} at {width} hides {column}: {header}");
        }
        for column in hidden {
            assert!(!header.contains(column), "{page:?} at {width} shows {column}: {header}");
        }
    }
}

/// A click on a table's id column copies the id; a click on the column drawn next to
/// it does not -- wherever truncation and hidden columns put the boundary.
#[test]
fn the_id_column_is_hit_where_it_was_drawn() {
    for page in [Page::Peers, Page::Clients, Page::Chat] {
        for width in [40u16, 60, 80, 120, 200] {
            let mut app = on_page(page);
            draw(&mut app, width, 30);
            let (start, end) = app.id_column_x.unwrap_or_else(|| panic!("{page:?} at {width}: no id column"));
            let list = app.list_area;
            let first_row = list.y + 3;
            let header = {
                let buffer = draw(&mut app, width, 30);
                row_text(&buffer, list.y + 1)
            };
            // The column's header starts where the hit range does.
            let header_start = header.chars().position(|glyph| !matches!(glyph, ' ' | '│')).unwrap() as u16;
            assert_eq!(header_start, start, "{page:?} at {width}: {header}");

            app.status.clear();
            app.click_at(start, first_row);
            assert!(app.status.starts_with("Copied"), "{page:?} at {width}: {}", app.status);

            // One past the id column: the spacing, then the next column.
            app.status.clear();
            app.click_at(end, first_row);
            assert!(!app.status.starts_with("Copied"), "{page:?} at {width}: copied from outside the id column");
        }
    }
}

/// The group and page rows are hit where their titles were drawn, in either form.
#[test]
fn a_tab_is_hit_where_its_title_was_drawn() {
    use crate::app::{group_at, page_at, PageGroup};
    for width in [40u16, 48, 60, 80, 120] {
        let mut app = on_page(Page::Instances);
        let buffer = draw(&mut app, width, 24);
        let groups = row_text(&buffer, 1);
        for group in PageGroup::ALL {
            let title = if groups.contains(group.title()) { group.title() } else { group.short_title() };
            let column = groups.find(title).unwrap_or_else(|| panic!("{group:?} not drawn at {width}: {groups}"));
            let column = groups[..column].chars().count() as u16;
            assert_eq!(group_at(column, app.tabs_area), Some(group), "{group:?} at {width}: {groups}");
        }
        let pages = row_text(&buffer, 3);
        for page in PageGroup::Activity.pages() {
            let title = if pages.contains(page.title()) { page.title() } else { page.short_title() };
            let column = pages.find(title).unwrap_or_else(|| panic!("{page:?} not drawn at {width}: {pages}"));
            let column = pages[..column].chars().count() as u16;
            assert_eq!(page_at(column, app.page_tabs_area, PageGroup::Activity), Some(page), "{page:?} at {width}: {pages}");
        }
    }
}

/// While the notice is up the mouse does nothing: what the page recorded for clicks
/// is no longer on screen.
#[tokio::test]
async fn clicks_are_ignored_while_the_terminal_is_too_small() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    let mut app = on_page(Page::Instances);
    draw(&mut app, 120, 30);
    let row = app.list_area.y + 4;
    draw(&mut app, 30, 8);
    let before = app.instances.state.selected();
    let click = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 5,
        row,
        modifiers: KeyModifiers::NONE,
    };
    crate::handler::handle_mouse_events(click, &mut app).await.unwrap();
    assert_eq!(app.instances.state.selected(), before);
    assert_eq!(app.page(), Page::Instances);
}

/// A page scrolled to its end on a wide terminal is still a page of text on a narrow
/// one: the rendering is cached per width, and the scroll is clamped to the new
/// length rather than left pointing past it.
#[test]
fn a_docs_page_reflows_on_resize() {
    let mut app = on_page(Page::Docs);
    draw(&mut app, 200, 60);
    app.docs_jump(true);
    draw(&mut app, 200, 60);
    for (width, height) in [(40u16, 12u16), (70, 20), (200, 60), (45, 11)] {
        let buffer = draw(&mut app, width, height);
        let page = app.docs.page_area;
        assert!(!page.is_empty(), "no page at {width}x{height}");
        let blank = (page.y..page.bottom()).all(|row| {
            (page.x..page.right()).all(|column| buffer.get(column, row).symbol() == " ")
        });
        assert!(!blank, "an empty page at {width}x{height}:\n{}", text(&buffer));
        assert!(app.docs.open.as_ref().unwrap().scroll <= app.docs.max_scroll());
    }
}

/// A peer's CJK and emoji stay inside the conversation pane's border.
#[test]
fn wide_glyphs_stay_inside_the_chat_pane() {
    for width in [64u16, 70, 80, 81, 100, 120] {
        let mut app = on_page(Page::Chat);
        let buffer = draw(&mut app, width, 24);
        let right = width - 1;
        for row in 5..21 {
            assert_eq!(buffer.get(right, row).symbol(), "│", "row {row} at {width}:\n{}", text(&buffer));
        }
    }
}

/// The Overview's estimate of what this node can reach (issue #455) is drawn on
/// every terminal the grid is used at, and says what it is: an upper bound, per
/// architecture, with the peers that said nothing counted.
#[test]
fn the_overview_shows_what_peers_can_reach() {
    for (width, height) in [(80, 24), (120, 40), (200, 60)] {
        let mut app = on_page(Page::Overview);
        let screen = text(&draw(&mut app, width, height));
        for needle in ["TOTAL · UPPER BOUND", "amd64", "arm64", "6 undeclared"] {
            assert!(screen.contains(needle), "{needle:?} missing at {width}x{height}:\n{screen}");
        }
    }
    // 30 peers: 6 undeclared (n % 5 == 1), 6 unreadable (n % 5 == 2); plus this
    // node's own 12c/48G/1T amd64 and 8c/16G/512G arm64.
    let mut app = on_page(Page::Overview);
    let screen = text(&draw(&mut app, 200, 60));
    for needle in ["amd64 116c 464G 8.4T ×13", "arm64 48c 128G 2.5T ×8*", "6 undeclared, 6 unreadable", "not free capacity"] {
        assert!(screen.contains(needle), "{needle:?} missing:\n{screen}");
    }
    assert!(!screen.contains("This node not included"), "{screen}");

    // Until `nodo resources` answers, the total is the peers' alone, and says so.
    let mut app = on_page(Page::Overview);
    app.own_resources = Default::default();
    let screen = text(&draw(&mut app, 200, 60));
    for needle in ["amd64 104c 416G 7.4T ×12", "arm64 40c 112G 2.0T ×7*", "This node not included"] {
        assert!(screen.contains(needle), "{needle:?} missing:\n{screen}");
    }
}

/// This node's own announcement has a card of its own beside the total, at every
/// size the grid is drawn at, and a failed read says why.
#[test]
fn the_overview_shows_what_this_node_announces() {
    for (width, height) in [(80, 24), (120, 40), (200, 60)] {
        let mut app = on_page(Page::Overview);
        let screen = text(&draw(&mut app, width, height));
        for needle in ["NODE · ANNOUNCED", "amd64 12c 48G 1.0T", "arm64 8c 16G 512G", "No scores: arm64"] {
            assert!(screen.contains(needle), "{needle:?} missing at {width}x{height}:\n{screen}");
        }
    }
    let mut app = on_page(Page::Overview);
    app.own_resources = crate::peer_resources::OwnResources {
        announced: None,
        error: "nodo resources timed out".to_string(),
    };
    let screen = text(&draw(&mut app, 200, 60));
    assert!(screen.contains("nodo resources timed out"), "{screen}");
    assert!(!screen.contains("reading…"), "{screen}");
}

/// The selected peer's card spells out what it announced, per architecture, with
/// its benchmark scores in their own units.
#[test]
fn the_peer_card_shows_announced_resources() {
    let mut app = on_page(Page::Peers);
    let screen = text(&draw(&mut app, 120, 60));
    for needle in [
        "Announced resources (2 architectures)",
        "linux/amd64  16 cores • 64.0 GiB RAM • 2.0 TiB disk",
        "int ops/s",
        "mem bw @ 64 MiB",
        "21.0 GiB/s per core",
        "linux/arm64",
    ] {
        assert!(screen.contains(needle), "{needle:?} missing:\n{screen}");
    }
    // Too short for the full card: the compact one still names the resources.
    let mut app = on_page(Page::Peers);
    let screen = text(&draw(&mut app, 120, 24));
    assert!(screen.contains("amd64 16c/64G  arm64 16c/64G"), "{screen}");
}

/// The Overview card and a peer's resources, for the pull request: `cargo test
/// print_peer_resource_renders -- --ignored --nocapture`.
#[test]
#[ignore]
fn print_peer_resource_renders() {
    for (page, width, height) in [(Page::Overview, 80, 24), (Page::Overview, 120, 30), (Page::Peers, 100, 60), (Page::Overview, 60, 40)] {
        let mut app = on_page(page);
        let buffer = draw(&mut app, width, height);
        println!("{page:?} {width}x{height}\n{}\n", text(&buffer));
    }
}

/// Small renders for the pull request, not a check: `cargo test print_small_renders
/// -- --ignored --nocapture`.
#[test]
#[ignore]
fn print_small_renders() {
    for (page, width, height) in [(Page::Instances, 40, 12), (Page::Peers, 60, 16), (Page::Overview, 40, 12), (Page::Instances, 20, 6)] {
        let mut app = on_page(page);
        let buffer = draw(&mut app, width, height);
        println!("{page:?} {width}x{height}\n{}\n", text(&buffer));
    }
}
