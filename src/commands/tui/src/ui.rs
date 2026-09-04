use crate::app::{
    format_bytes, format_bytes_compact, format_rate_compact, percent, segment_token, shorten, App,
    Client, ClientDetail, ConfigEntry, EditKind, InputMode, Instance, Money, Page, PaymentRow, Peer,
    PeerDetail, PriceEntry, ReputationEvent, Service, ServiceDetail, HISTORY_POINTS,
};
use crate::cell::{self, Lever, LeverStatus, Organelle};
use ratatui::{prelude::*, widgets::*};
use std::collections::{HashMap, HashSet};
use tui_tree_widget::{Tree, TreeItem};

const ACCENT: Color = Color::Cyan;
const MUTED: Color = Color::DarkGray;
const GOOD: Color = Color::Green;
const WARN: Color = Color::Yellow;
/// For the things that cost the operator something: a payment nobody acknowledged,
/// a deposit that was refused, a penalty. Yellow already means "look at this later".
const BAD: Color = Color::Red;

pub fn render(app: &mut App, frame: &mut Frame) {
    let layout = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(8),
        Constraint::Length(2),
    ])
    .split(frame.size());

    // Remembered for the mouse: a click has only the coordinates, so the hit test needs
    // where things ended up this frame. Cleared here so a page without a table (or the
    // instances tree) cannot inherit the previous page's rows.
    app.tabs_area = layout[0];
    app.list_area = Rect::ZERO;

    draw_tabs(frame, app, layout[0]);
    match app.page() {
        Page::Overview => draw_overview(frame, app, layout[1]),
        Page::Instances => draw_instances(frame, app, layout[1]),
        Page::Services => draw_services(frame, app, layout[1]),
        Page::Peers => draw_peers(frame, app, layout[1]),
        Page::Clients => draw_clients(frame, app, layout[1]),
        Page::Cell => draw_cell(frame, app, layout[1]),
        Page::Pricing => draw_pricing(frame, app, layout[1]),
        Page::Config => draw_config(frame, app, layout[1]),
        Page::Logs => draw_logs(frame, app, layout[1]),
    }
    draw_footer(frame, app, layout[2]);

    match app.input_mode {
        InputMode::Normal => {}
        InputMode::Confirm => draw_confirm_popup(frame, app),
        InputMode::Details | InputMode::ConfirmWrites => draw_details_popup(frame, app),
        InputMode::PickProfile => draw_profile_popup(frame, app),
        InputMode::Connect
        | InputMode::EditConfig
        | InputMode::AddConfigItem
        | InputMode::FilterConfig
        | InputMode::CreditClient => draw_input_popup(frame, app),
    }
}

fn draw_tabs(frame: &mut Frame, app: &App, area: Rect) {
    let titles = Page::ALL
        .iter()
        .map(|page| Line::from(page.title()))
        .collect::<Vec<_>>();
    let status_color = if app.node_info.service_status == "running" {
        GOOD
    } else {
        WARN
    };
    let title = Line::from(vec![
        Span::styled(
            " NODO ",
            Style::default().fg(Color::Black).bg(ACCENT).bold(),
        ),
        Span::raw("  operations console  "),
        Span::styled(
            if app.node_info.service_status.is_empty() {
                "unknown"
            } else {
                &app.node_info.service_status
            },
            Style::default().fg(status_color),
        ),
    ]);
    let tabs = Tabs::new(titles)
        .block(Block::bordered().title(title))
        .select(app.tabs.index)
        .style(Style::default().fg(MUTED))
        .highlight_style(Style::default().fg(ACCENT).bold())
        .divider(" │ ");
    frame.render_widget(tabs, area);
}

fn draw_overview(frame: &mut Frame, app: &App, area: Rect) {
    let rows = Layout::vertical([
        Constraint::Length(7),
        Constraint::Length(7),
        Constraint::Min(6),
    ])
    .split(area);
    let top = Layout::horizontal([
        Constraint::Percentage(25),
        Constraint::Percentage(25),
        Constraint::Percentage(25),
        Constraint::Percentage(25),
    ])
    .split(rows[0]);

    draw_card(
        frame,
        top[0],
        "NODE",
        vec![
            metric_line(
                "Status",
                nonempty(&app.node_info.service_status, "checking…"),
            ),
            metric_line("Address", nonempty(&app.node_info.address, "—")),
            metric_line("Version", shorten(&app.node_info.version, 18)),
        ],
        ACCENT,
    );
    draw_card(
        frame,
        top[1],
        "WORKLOAD",
        vec![
            metric_line("Instances", app.instances.items.len().to_string()),
            metric_line(
                "Memory now",
                format_bytes(app.stats.instance_memory_current),
            ),
            metric_line(
                "Reserved",
                format!(
                    "{} RAM / {} disk",
                    format_bytes(app.stats.instance_memory_reserved),
                    format_bytes(app.stats.instance_disk_reserved)
                ),
            ),
        ],
        Color::LightBlue,
    );
    draw_card(
        frame,
        top[2],
        "STORAGE",
        vec![
            metric_line(
                "Host disk",
                format!(
                    "{} / {} ({}%)",
                    format_bytes(app.stats.disk_used),
                    format_bytes(app.stats.disk_total),
                    percent(app.stats.disk_used, app.stats.disk_total)
                ),
            ),
            metric_line("Nodo data", format_bytes(app.stats.storage_bytes)),
            metric_line("Services", app.services.items.len().to_string()),
        ],
        Color::LightMagenta,
    );
    draw_card(
        frame,
        top[3],
        "NETWORK",
        vec![
            metric_line("Peers", app.peers.items.len().to_string()),
            metric_line("Clients", app.clients.items.len().to_string()),
        ],
        Color::LightGreen,
    );

    let middle =
        Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).split(rows[1]);
    draw_ergo(frame, app, middle[0]);
    draw_health(frame, app, middle[1]);

    let charts =
        Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).split(rows[2]);
    draw_sparkline(
        frame,
        charts[0],
        "CPU HISTORY",
        app.cpu_history.iter().copied().collect(),
        app.stats.cpu_percent,
        Color::Yellow,
    );
    draw_sparkline(
        frame,
        charts[1],
        "MEMORY HISTORY",
        app.ram_history.iter().copied().collect(),
        percent(app.stats.memory_used, app.stats.memory_total),
        ACCENT,
    );
}

fn draw_card<'a>(frame: &mut Frame, area: Rect, title: &str, lines: Vec<Line<'a>>, color: Color) {
    let block = Block::bordered()
        .title(Span::styled(
            format!(" {title} "),
            Style::default().fg(color).bold(),
        ))
        .border_style(Style::default().fg(MUTED));
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn metric_line(label: &str, value: impl Into<String>) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<12}"), Style::default().fg(MUTED)),
        Span::styled(value.into(), Style::default().fg(Color::White).bold()),
    ])
}

fn draw_ergo(frame: &mut Frame, app: &App, area: Rect) {
    let wallet = nonempty(&app.node_info.wallet_address, "not configured");
    let cold = nonempty(&app.node_info.cold_wallet_address, "not configured");
    let lines = vec![
        Line::from(vec![
            Span::styled("Balance  ", Style::default().fg(MUTED)),
            Span::styled(
                format_balance(app.node_info.wallet_balance),
                Style::default().fg(Color::LightGreen).bold(),
            ),
        ]),
        Line::from(format!(
            "Wallet {}  {}",
            shorten(wallet, 28),
            format_balance(app.node_info.wallet_balance)
        )),
        Line::from(format!(
            "Cold   {}",
            shorten(cold, 28)
        )),
        Line::from(format!(
            "Proof  {}",
            shorten(nonempty(&app.node_info.reputation_proof, "not registered"), 28)
        )),
        Line::from(Span::styled(
            nonempty(
                &app.node_info.error,
                "On-chain ERG, not a node balance • refreshes every 60s",
            ),
            Style::default().fg(if app.node_info.error.is_empty() {
                MUTED
            } else {
                WARN
            }),
        )),
    ];
    draw_card(frame, area, "ERGO WALLET", lines, Color::LightGreen);
}

fn draw_health(frame: &mut Frame, app: &App, area: Rect) {
    let block = Block::bordered()
        .title(Span::styled(
            " HOST CAPACITY ",
            Style::default().fg(Color::Yellow).bold(),
        ))
        .border_style(Style::default().fg(MUTED));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let rows = Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(2),
        Constraint::Length(1),
    ])
    .split(inner);
    draw_gauge(frame, rows[0], "CPU", app.stats.cpu_percent, Color::Yellow);
    draw_gauge(
        frame,
        rows[1],
        "RAM",
        percent(app.stats.memory_used, app.stats.memory_total),
        ACCENT,
    );
    frame.render_widget(
        Paragraph::new(format!(
            "{} used of {}",
            format_bytes(app.stats.memory_used),
            format_bytes(app.stats.memory_total)
        ))
        .style(Style::default().fg(MUTED)),
        rows[2],
    );
}

fn draw_gauge(frame: &mut Frame, area: Rect, label: &str, value: u64, color: Color) {
    let gauge = Gauge::default()
        .block(Block::default().title(label))
        .gauge_style(Style::default().fg(color).bg(Color::Black))
        .percent(value.min(100) as u16)
        .label(format!("{value}%"));
    frame.render_widget(gauge, area);
}

fn draw_sparkline(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    data: Vec<u64>,
    current: u64,
    color: Color,
) {
    let title = format!(" {title} • {current}% • last {} samples ", HISTORY_POINTS);
    frame.render_widget(
        Sparkline::default()
            .block(
                Block::bordered()
                    .title(Span::styled(title, Style::default().fg(color).bold()))
                    .border_style(Style::default().fg(MUTED)),
            )
            .data(&data)
            .max(100)
            .style(Style::default().fg(color)),
        area,
    );
}

fn draw_instances(frame: &mut Frame, app: &mut App, area: Rect) {
    if app.instances_grouped {
        draw_instances_tree(frame, app, area);
        return;
    }
    // 13 = 11 detail lines + the block's two border rows. The card carries the figures
    // the row has no width for: the disk allocation, the vCPU allowance the CPU% is
    // measured against, the cumulative disk/net totals, and the burn rate broken out
    // per minute and per hour with the age and sample count of the average.
    let layout = Layout::vertical([Constraint::Min(8), Constraint::Length(13)]).split(area);
    let rows = app.instances.items.iter().map(|instance| {
        let location = if instance.is_local() {
            "local".to_string()
        } else {
            shorten(&instance.location, 14)
        };
        let location_style = if instance.is_local() {
            Style::default().fg(GOOD)
        } else {
            Style::default().fg(WARN)
        };
        Row::new(vec![
            Cell::from(instance.name.clone()),
            Cell::from(location).style(location_style),
            Cell::from(shorten(&instance.id, 18)),
            Cell::from(instance.service.clone()),
            Cell::from(instance.ip.clone()),
            Cell::from(instance.virtualizer.clone()),
            Cell::from(format_cpu_percent(instance.usage.cpu_percent))
                .style(Style::default().fg(cpu_load_color(instance))),
            // Used against allocated in one cell: two columns made the operator do the
            // division, which is the whole question being asked of this page.
            Cell::from(format!(
                "{} / {}",
                instance
                    .usage
                    .memory_current
                    .map(format_bytes_compact)
                    .unwrap_or_else(|| "—".to_string()),
                format_bytes_compact(instance.memory_limit)
            )),
            Cell::from(format!(
                "{} / {}",
                format_rate_compact(instance.usage.net_rx_rate),
                format_rate_compact(instance.usage.net_tx_rate)
            )),
            Cell::from(app.money.format_raw(&instance.balance)),
            Cell::from(format_burn_rate(instance.mu_per_hour, &app.money)),
        ])
    });
    let local_count = app.instances.items.iter().filter(|i| i.is_local()).count();
    let remote_count = app.instances.items.len() - local_count;
    let table = Table::new(
        rows,
        [
            Constraint::Length(16),
            Constraint::Length(14),
            Constraint::Length(19),
            Constraint::Length(18),
            Constraint::Length(15),
            Constraint::Length(7),
            Constraint::Length(7),
            Constraint::Length(14),
            Constraint::Length(14),
            Constraint::Length(14),
            Constraint::Min(12),
        ],
    )
    .header(header_row(vec![
        "Name",
        "Location",
        "Instance",
        "Service",
        "IP",
        "VM",
        "CPU%",
        "RAM now/max",
        "Net ↓/↑ per s",
        "Balance",
        "Burn/h",
    ]))
    .block(section_block(
        format!(
            " INSTANCES • {} local • {} remote ",
            local_count, remote_count
        ),
        Color::LightBlue,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = layout[0];
    frame.render_stateful_widget(table, layout[0], &mut app.instances.state);

    let money = &app.money;
    let detail = if let Some(instance) = app.instances.selected() {
        let mut lines = vec![
            metric_line("Instance", instance.id.clone()),
            metric_line(
                "Location",
                if instance.is_local() {
                    "local".to_string()
                } else {
                    format!("remote • peer {}", instance.location)
                },
            ),
            metric_line("Service", instance.service.clone()),
            metric_line("Endpoint", nonempty(&instance.ip, "—")),
            metric_line("CPU", cpu_detail(instance)),
            metric_line(
                "RAM",
                format!(
                    "{} / {}",
                    instance
                        .usage
                        .memory_current
                        .map(format_bytes)
                        .unwrap_or_else(|| "—".to_string()),
                    format_bytes(instance.memory_limit)
                ),
            ),
            metric_line(
                "Disk",
                format!(
                    "read {} • wrote {} • {} allocated",
                    optional_bytes(instance.usage.disk_read_bytes),
                    optional_bytes(instance.usage.disk_write_bytes),
                    format_bytes(instance.disk_limit)
                ),
            ),
            metric_line("Net", net_detail(instance)),
            metric_line("Balance", money.format_raw(&instance.balance)),
            metric_line("Burn", burn_detail(instance, money)),
        ];
        // `observe` attaches to a local process, so it is only offered for local
        // instances. Full id, so the line can be copied as-is.
        if instance.is_local() {
            lines.push(metric_line(
                "Live view",
                format!("nodo observe {}", instance.id),
            ));
        }
        lines
    } else {
        vec![Line::from(Span::styled(
            "Select an instance to inspect its complete identity and allocation.",
            Style::default().fg(MUTED),
        ))]
    };
    draw_card(
        frame,
        layout[1],
        "SELECTED INSTANCE",
        detail,
        Color::LightBlue,
    );
}

/// A CPU reading for a table cell. `—` covers both "no cgroup to read" (delegated or
/// stopped) and "only one sample so far", which are equally not-a-measurement; a `0%`
/// there would claim the instance is idle.
fn format_cpu_percent(cpu_percent: Option<f64>) -> String {
    match cpu_percent {
        Some(value) if value.is_finite() => format!("{value:.0}%"),
        _ => "—".to_string(),
    }
}

/// Colour for the CPU cell: muted when there is no reading, and a warning once the
/// instance is within a tenth of its whole vCPU allowance — the point at which the
/// figure stops being informational and starts meaning "this one is throttling".
fn cpu_load_color(instance: &Instance) -> Color {
    match (instance.usage.cpu_percent, instance.cpu_allowance_percent()) {
        (None, _) => MUTED,
        (Some(used), Some(allowance)) if allowance > 0.0 && used >= allowance * 0.9 => WARN,
        _ => GOOD,
    }
}

/// The CPU line for the detail card: what the instance is using, next to the allowance
/// that makes the number legible. `observe` reports cumulative core time, so `180%` is
/// unremarkable on a 2-vCPU guest and impossible on a 1-vCPU one — without the
/// allowance beside it the percentage cannot be judged.
fn cpu_detail(instance: &Instance) -> String {
    let used = format_cpu_percent(instance.usage.cpu_percent);
    match instance.cpu_allowance_percent() {
        Some(allowance) => format!(
            "{used} of {allowance:.0}% allowance ({:.2} vCPU)",
            instance.vcpus.unwrap_or(0.0)
        ),
        None => format!("{used} • no vCPU quota recorded"),
    }
}

/// The network line for the detail card: current rates plus the totals they accumulate
/// into. Orientation is the host tap's (see `InstanceUsage`), so `↓` is traffic the
/// host took *from* the VM.
fn net_detail(instance: &Instance) -> String {
    let rate = |value: Option<f64>| match value {
        Some(value) if value.is_finite() && value >= 0.0 => {
            format!("{}/s", format_bytes(value.round() as u64))
        }
        _ => "—".to_string(),
    };
    format!(
        "↓ {} ↑ {} • total {} / {}",
        rate(instance.usage.net_rx_rate),
        rate(instance.usage.net_tx_rate),
        optional_bytes(instance.usage.net_rx_bytes),
        optional_bytes(instance.usage.net_tx_bytes)
    )
}

fn optional_bytes(bytes: Option<u64>) -> String {
    bytes.map(format_bytes).unwrap_or_else(|| "—".to_string())
}

/// The `Burn/h` table cell: an hourly spend rate in the display unit, or `—` when the
/// instance has never been charged (a `0` there would claim it is free, not unknown).
/// A rate only needs formatting, not a second unit system, so it reuses `format_mu`.
fn format_burn_rate(mu_per_hour: Option<f64>, money: &Money) -> String {
    match mu_per_hour {
        Some(rate) if rate.is_finite() && rate >= 0.0 => money.format_mu(rate.round() as u64),
        _ => "—".to_string(),
    }
}

/// The burn line for the detail card: both the per-minute and per-hour figures, plus
/// how many samples the average is built from and how long ago it was last updated — a
/// rate from two stale samples must not read like a fresh one. It prices *reserved*
/// resources at current scarcity, so it is the cost of keeping the instance running at
/// present prices, not measured resource usage (#245); the label says so.
fn burn_detail(instance: &Instance, money: &Money) -> String {
    match (instance.mu_per_minute, instance.mu_per_hour) {
        (Some(per_minute), Some(per_hour)) => {
            let samples = instance.consumption_samples.unwrap_or(0);
            let age = instance
                .consumption_age_secs
                .map(format_age_secs)
                .unwrap_or_else(|| "—".to_string());
            format!(
                "{} /min • {} /h • {} sample{} averaged, updated {} ago • reserved-resource cost at current prices",
                money.format_mu(per_minute.round().max(0.0) as u64),
                money.format_mu(per_hour.round().max(0.0) as u64),
                samples,
                if samples == 1 { "" } else { "s" },
                age,
            )
        }
        // No maintenance tick has charged this instance yet (or it is delegated).
        _ => "— • no samples yet".to_string(),
    }
}

/// A compact "how long ago" for the burn-rate average: seconds under a minute and a
/// half, then minutes, then hours, so an operator can tell a fresh figure from a stale one.
fn format_age_secs(secs: f64) -> String {
    let secs = secs.max(0.0);
    if secs < 90.0 {
        format!("{:.0}s", secs)
    } else if secs < 5400.0 {
        format!("{:.0}m", secs / 60.0)
    } else {
        format!("{:.1}h", secs / 3600.0)
    }
}

/// Render instances as a dependency tree grouped by `father_id`, porting the
/// Python `list_instances(groupable=True)` builder from commands/instances.py.
fn draw_instances_tree(frame: &mut Frame, app: &App, area: Rect) {
    let items = &app.instances.items;
    let inst_map: HashMap<&str, &Instance> = items
        .iter()
        .filter(|instance| !instance.id.is_empty())
        .map(|instance| (instance.id.as_str(), instance))
        .collect();

    // Group children under their parent; a father_id that is empty, "None", or
    // not present locally makes the node a root (mirrors the Python logic).
    let mut children: HashMap<&str, Vec<&str>> = HashMap::new();
    let mut has_parent: HashSet<&str> = HashSet::new();
    for instance in items.iter().filter(|instance| !instance.id.is_empty()) {
        let father = instance.father_id.as_str();
        if !father.is_empty() && father != "None" && inst_map.contains_key(father) {
            children.entry(father).or_default().push(instance.id.as_str());
            has_parent.insert(instance.id.as_str());
        }
    }
    let roots: Vec<&str> = items
        .iter()
        .filter(|instance| !instance.id.is_empty())
        .map(|instance| instance.id.as_str())
        .filter(|id| !has_parent.contains(id))
        .collect();

    // A root is only a root because its father is not another instance here. When that
    // father is one of our clients, say so: without it an instance a client started is
    // indistinguishable from one with no parent at all. Reuses the already-loaded
    // clients list (refreshed every sweep), so this costs no query.
    let client_ids: HashSet<&str> = app
        .clients
        .items
        .iter()
        .map(|client| client.id.as_str())
        .collect();

    let mut lines: Vec<Line> = Vec::new();
    let mut printed: HashSet<&str> = HashSet::new();
    for root in &roots {
        build_tree_lines(
            &app.money,
            root,
            0,
            &inst_map,
            &children,
            &client_ids,
            &mut printed,
            &mut lines,
        );
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "No instances to display.",
            Style::default().fg(MUTED),
        )));
    }

    frame.render_widget(
        Paragraph::new(lines)
            .block(section_block(
                format!(
                    " INSTANCE DEPENDENCY TREE • {} nodes • g toggles flat view ",
                    inst_map.len()
                ),
                Color::LightBlue,
            ))
            .wrap(Wrap { trim: false }),
        area,
    );
}

// Eight, counting the recursion's own bookkeeping (`printed`, `lines`). A context
// struct to carry them would be more code than the function it serves.
#[allow(clippy::too_many_arguments)]
fn build_tree_lines<'a>(
    money: &Money,
    node_id: &'a str,
    depth: usize,
    inst_map: &HashMap<&'a str, &'a Instance>,
    children: &HashMap<&'a str, Vec<&'a str>>,
    client_ids: &HashSet<&str>,
    printed: &mut HashSet<&'a str>,
    lines: &mut Vec<Line<'static>>,
) {
    if printed.contains(node_id) {
        return;
    }
    printed.insert(node_id);
    let Some(instance) = inst_map.get(node_id) else {
        return;
    };

    let indent = "    ".repeat(depth);
    let marker = if depth == 0 { "● " } else { "└─ " };
    let label = if instance.name.is_empty() {
        shorten(&instance.id, 20)
    } else {
        instance.name.clone()
    };
    let location = if instance.is_local() {
        "local".to_string()
    } else {
        format!("peer {}", shorten(&instance.location, 12))
    };
    let mut spans = vec![
        Span::raw(format!("{indent}{marker}")),
        Span::styled(label, Style::default().fg(Color::White).bold()),
        Span::styled(format!("  [{}]", instance.service), Style::default().fg(MUTED)),
        Span::styled(
            format!("  {location}"),
            Style::default().fg(if instance.is_local() { GOOD } else { WARN }),
        ),
        Span::styled(
            format!("  balance {}", money.format_raw(&instance.balance)),
            Style::default().fg(ACCENT),
        ),
    ];
    // Only roots can have a father this tree does not already show: a child is nested
    // here precisely because its father is another instance above it.
    if depth == 0 {
        if let Some(parent) = external_parent_label(instance, client_ids) {
            spans.push(parent);
        }
    }
    lines.push(Line::from(spans));

    if let Some(kids) = children.get(node_id) {
        for kid in kids {
            build_tree_lines(
                money,
                kid,
                depth + 1,
                inst_map,
                children,
                client_ids,
                printed,
                lines,
            );
        }
    }
}

/// Who started an instance whose father is not another instance on this node: one of
/// our clients, or a father we cannot resolve at all. `None` when the instance has no
/// father, or when it has one that this node runs (the tree already nests those).
///
/// Mirrors the `internal_service` / `client` / `unknown` split `list_instances` does in
/// `src/commands/instances.py`.
fn external_parent_label(instance: &Instance, client_ids: &HashSet<&str>) -> Option<Span<'static>> {
    let father = instance.father_id.as_str();
    if father.is_empty() || father == "None" {
        return None;
    }
    Some(if client_ids.contains(father) {
        Span::styled(
            format!("  ← client {}", shorten(father, 20)),
            Style::default().fg(Color::Magenta),
        )
    } else {
        // A father that is neither a local instance nor a known client: the row still
        // says where it came from, rather than reading as "started by nobody".
        Span::styled(
            format!("  ← {} (unknown)", shorten(father, 20)),
            Style::default().fg(MUTED),
        )
    })
}

fn draw_services(frame: &mut Frame, app: &mut App, area: Rect) {
    // The card grows with the selected service's reputation history, and yields to the
    // table when the terminal is short, like the peer and client cards.
    const MIN_TABLE_HEIGHT: u16 = 8;
    let card = service_detail_lines(app.services.selected(), app.service_detail.as_ref());
    let available = area.height.saturating_sub(MIN_TABLE_HEIGHT);
    let card_height = (card.len() as u16 + 2).clamp(5, available.max(5));
    let layout =
        Layout::vertical([Constraint::Min(MIN_TABLE_HEIGHT), Constraint::Length(card_height)])
            .split(area);
    let rows = app.services.items.iter().map(|service| {
        Row::new(vec![
            service.tag.clone(),
            service.id.clone(),
            format_bytes(service.size_bytes),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(28),
            Constraint::Min(42),
            Constraint::Length(14),
        ],
    )
    .header(header_row(vec!["Tag", "Content ID", "Stored size"]))
    .block(section_block(
        format!(" SERVICES • {} available ", app.services.items.len()),
        Color::LightMagenta,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = layout[0];
    frame.render_stateful_widget(table, layout[0], &mut app.services.state);

    frame.render_widget(
        Paragraph::new(card)
            .block(section_block(" SELECTED SERVICE ", Color::LightMagenta))
            .style(Style::default().fg(Color::White)),
        layout[1],
    );
}

/// The selected service: what it is, and how it has behaved here.
///
/// The reputation is the service's own, accumulated over every instance of it that
/// ever ran on this node — an instance is gone minutes after it misbehaves, so a score
/// tied to one would answer nothing when the service is started again.
fn service_detail_lines(
    service: Option<&Service>,
    detail: Option<&ServiceDetail>,
) -> Vec<Line<'static>> {
    let Some(service) = service else {
        return vec![Line::from(Span::styled(
            "Select a service, then press e to execute it.",
            Style::default().fg(MUTED),
        ))];
    };

    let mut lines = vec![
        Line::from(Span::styled(
            service.id.clone(),
            Style::default().fg(Color::White),
        )),
        Line::from(Span::styled(
            format!(
                "{} • {}",
                nonempty(&service.tag, "untagged"),
                format_bytes(service.size_bytes)
            ),
            Style::default().fg(Color::White),
        )),
    ];

    let Some(detail) = detail.filter(|detail| detail.service_id == service.id) else {
        return lines;
    };

    lines.push(metric_line(
        "Reputation",
        detail
            .score
            .map(|score| score.to_string())
            // Never scored is not the same as scored to zero, and an operator choosing
            // a service should be able to tell the two apart.
            .unwrap_or_else(|| "not scored yet".to_string()),
    ));
    if !detail.events.is_empty() {
        lines.extend(reputation_event_lines(&detail.events));
    }
    lines
}

/// The peers page: who we talk to, and everything we have paid them.
///
/// Peers and clients used to share one page, which is why the peer detail card had to
/// fight two tables for height. They are separate concerns -- a peer is someone we pay,
/// a client is someone who pays us -- and each now has the room to say so.
fn draw_peers(frame: &mut Frame, app: &mut App, area: Rect) {
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
    let full = peer_detail_lines(&app.money, selected, detail_source, false);
    let detail = if full.len() as u16 + 2 <= available {
        full
    } else {
        peer_detail_lines(&app.money, selected, detail_source, true)
    };
    let detail_height = (detail.len() as u16 + 2).min(available);
    let split = Layout::vertical([
        Constraint::Min(MIN_TABLE_HEIGHT),
        Constraint::Length(detail_height),
    ])
    .split(area);

    let peers = app.peers.items.iter().map(|peer| {
        Row::new(vec![
            Cell::from(peer.id.clone()),
            Cell::from(peer.uris.clone()),
            Cell::from(app.money.format_raw(&peer.balance)),
            Cell::from(peer.reputation_score.clone()).style(Style::default().fg(GOOD).bold()),
            Cell::from(match peer.proof_ids.len() {
                0 => "none".to_string(),
                1 => shorten(&peer.proof_ids[0], 18),
                n => format!("{n} announced"),
            }),
        ])
    });
    let peer_table = Table::new(
        peers,
        [
            Constraint::Length(30),
            Constraint::Length(24),
            Constraint::Length(13),
            Constraint::Length(7),
            Constraint::Min(20),
        ],
    )
    .header(header_row(vec![
        "Peer ID",
        "Endpoints",
        "Our balance",
        "Rep",
        "Reputation proofs",
    ]))
    .block(section_block(
        format!(" PEERS • {} connected ", app.peers.items.len()),
        ACCENT,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = split[0];
    frame.render_stateful_widget(peer_table, split[0], &mut app.peers.state);

    draw_card(frame, split[1], "SELECTED PEER", detail, ACCENT);
}

/// The clients page: who pays us, and what they are running here.
fn draw_clients(frame: &mut Frame, app: &mut App, area: Rect) {
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
                .style(Style::default().fg(MUTED)),
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
        ACCENT,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = split[0];
    frame.render_stateful_widget(client_table, split[0], &mut app.clients.state);

    draw_card(frame, split[1], "SELECTED CLIENT", detail, ACCENT);
}

/// Everything the Clients page knows about the selected client.
///
/// Deliberately says nothing about *who* the client is: a client id has no link back
/// to a peer (`peer.remote_client_id` is our id inside a remote peer, not a key into
/// our `clients` table -- issue #178), so the card shows what this client did here and
/// nothing inferred.
fn client_detail_lines(
    money: &Money,
    client: Option<&Client>,
    detail: Option<&ClientDetail>,
    compact: bool,
) -> Vec<Line<'static>> {
    let Some(client) = client else {
        return vec![Line::from(Span::styled(
            "Select a client to inspect its deposits, instances and payments.",
            Style::default().fg(MUTED),
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
                Style::default().fg(MUTED),
            )));
        }
    }

    // A stale card is worse than none: the selection can move between the load and
    // the frame, and a payment shown under the wrong client is a lie about money.
    let Some(detail) = detail.filter(|detail| detail.client_id == client.id) else {
        return lines;
    };

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
            Style::default().fg(ACCENT).bold(),
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
                    Style::default().fg(Color::White),
                ),
            ]));
        }
    }

    if !detail.instances.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("Instances started here ({})", detail.instances.len()),
            Style::default().fg(ACCENT).bold(),
        )));
        for instance in &detail.instances {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(GOOD)),
                Span::styled(
                    nonempty(&instance.name, "unnamed").to_string(),
                    Style::default().fg(Color::White).bold(),
                ),
                Span::styled(
                    format!("  {}", shorten(&instance.id, 24)),
                    Style::default().fg(MUTED),
                ),
            ]));
        }
    }

    lines
}

/// A payment history as card lines, shared by the peer and client cards.
fn payment_lines(
    money: &Money,
    payments: &[PaymentRow],
    title: &str,
    empty: &str,
) -> Vec<Line<'static>> {
    if payments.is_empty() {
        return vec![Line::from(Span::styled(
            empty.to_string(),
            Style::default().fg(MUTED),
        ))];
    }

    let mut lines = vec![Line::from(Span::styled(
        format!("{title} ({})", payments.len()),
        Style::default().fg(ACCENT).bold(),
    ))];
    for payment in payments {
        // The transaction id when there is one -- a simulated contract settles nothing
        // on a chain -- otherwise the deposit token, which is what identifies the
        // payment on the receiving side.
        let reference = if payment.tx_id.is_empty() {
            format!("token {}", shorten(nonempty(&payment.deposit_token, "—"), 16))
        } else {
            format!("tx {}", shorten(&payment.tx_id, 16))
        };
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(status_color(&payment.status))),
            Span::styled(
                format!("{:<20}", payment.created_at.clone()),
                Style::default().fg(MUTED),
            ),
            Span::styled(
                format!("{:>14}  ", money.format_raw(&payment.amount)),
                Style::default().fg(Color::White).bold(),
            ),
            Span::styled(
                format!("{:<14}", payment.status.clone()),
                Style::default().fg(status_color(&payment.status)),
            ),
            Span::styled(reference, Style::default().fg(MUTED)),
        ]));
    }
    lines
}

/// Reputation history as card lines: what moved the score, and why.
fn reputation_event_lines(events: &[ReputationEvent]) -> Vec<Line<'static>> {
    if events.is_empty() {
        return vec![Line::from(Span::styled(
            "No reputation event recorded yet.".to_string(),
            Style::default().fg(MUTED),
        ))];
    }

    let mut lines = vec![Line::from(Span::styled(
        format!("Reputation history ({})", events.len()),
        Style::default().fg(ACCENT).bold(),
    ))];
    for event in events {
        let color = if event.amount < 0 { BAD } else { GOOD };
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(color)),
            Span::styled(
                format!("{:<20}", event.created_at.clone()),
                Style::default().fg(MUTED),
            ),
            Span::styled(
                format!("{:>+6}  ", event.amount),
                Style::default().fg(color).bold(),
            ),
            // Stored as `payment_unacknowledged`; read as "payment unacknowledged".
            Span::styled(
                format!("{:<26}", event.reason.replace('_', " ")),
                Style::default().fg(Color::White),
            ),
            Span::styled(
                event
                    .score_after
                    .map(|score| format!("→ {score}"))
                    .unwrap_or_default(),
                Style::default().fg(MUTED),
            ),
        ]));
    }
    lines
}

/// Colour for a payment or deposit status: the ones that mean "money moved and
/// nothing came of it" have to stand out from the ones that worked.
fn status_color(status: &str) -> Color {
    match status {
        "communicated" | "accepted" | "payed" => GOOD,
        "unacknowledged" | "rejected" => BAD,
        _ => WARN,
    }
}

/// Full breakdown of the peer highlighted in the peers table: identity, our balance
/// with it, reputation, and every payment contract it has registered — ledger,
/// contract, payout address and per-unit rate per instance. Before this the only
/// way to get at any of it was a raw sqlite query (issue #231).
/// `compact` collapses each contract onto a single line and drops the fields the
/// peers table already shows verbatim, for terminals too short for the full card.
fn peer_detail_lines(
    money: &Money,
    peer: Option<&Peer>,
    detail: Option<&PeerDetail>,
    compact: bool,
) -> Vec<Line<'static>> {
    let Some(peer) = peer else {
        return vec![Line::from(Span::styled(
            "Select a peer to inspect its endpoints, reputation and payment contracts.",
            Style::default().fg(MUTED),
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
                Span::styled("      proof  ", Style::default().fg(MUTED)),
                Span::styled(shorten(proof_id, 46), Style::default().fg(Color::White)),
            ]));
        }
        lines.push(Line::from(""));
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
            "No payment contract registered for this peer.",
            Style::default().fg(WARN),
        )));
        return lines;
    }

    lines.push(Line::from(Span::styled(
        format!("Payment contracts ({})", peer.contracts.len()),
        Style::default().fg(ACCENT).bold(),
    )));
    for contract in &peer.contracts {
        if compact {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(GOOD)),
                Span::styled(contract.ledger.clone(), Style::default().fg(GOOD).bold()),
                Span::styled(
                    format!(
                        "  {}  {}  1 {} = {} MU",
                        shorten(&contract.contract_hash, 14),
                        shorten(nonempty(&contract.address, "—"), 14),
                        contract.ledger.to_uppercase(),
                        nonempty(&contract.mu_per_unit, "—")
                    ),
                    Style::default().fg(Color::White),
                ),
            ]));
            continue;
        }
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(GOOD)),
            Span::styled(contract.ledger.clone(), Style::default().fg(GOOD).bold()),
            Span::styled(
                format!("  contract {}", shorten(&contract.contract_hash, 24)),
                Style::default().fg(Color::White),
            ),
        ]));
        lines.push(Line::from(vec![
            Span::styled("      address  ", Style::default().fg(MUTED)),
            Span::styled(
                shorten(nonempty(&contract.address, "—"), 46),
                Style::default().fg(Color::White),
            ),
        ]));
        lines.push(Line::from(vec![
            Span::styled("      rate     ", Style::default().fg(MUTED)),
            Span::styled(
                // What this peer says one unit of its ledger buys in ITS MU. This is
                // what makes a price it quotes convertible into money we understand,
                // so it is stated as an equation rather than as a bare number.
                format!(
                    "1 {} = {} MU",
                    contract.ledger.to_uppercase(),
                    nonempty(&contract.mu_per_unit, "—")
                ),
                Style::default().fg(Color::White),
            ),
        ]));
    }
    lines
}

/// The pricing page: what this node charges, as bars you can nudge.
///
/// Recurring and one-off prices get their own chart because their magnitudes are
/// unrelated -- a build price three orders of magnitude above a tunnel-open price would
/// flatten the whole one-off group to nothing on a shared axis. Exact figures live in
/// the table below, which is also where the selection lives; the bars are for judging
/// proportion at a glance while editing.
/// The CELL page: the node drawn as a cell, and its policies as levers inside it.
///
/// The membrane is the page's own frame, and the organelles are boxes inside it: the
/// three that face outward above the nucleus, the three that keep the node alive
/// below. The anatomy is load-bearing, not decoration -- what an operator is looking
/// for ("can anyone reach me?", "what do I let a stranger's service do?") is found by
/// asking which part of a cell would be responsible for it.
///
/// Two layouts, one cursor: wide terminals get the grid, narrow ones an accordion
/// where only the focused organelle opens. The keys behave identically in both, so
/// there is one interaction to learn and one to keep correct.
fn draw_cell(frame: &mut Frame, app: &mut App, area: Rect) {
    let document = app.config_document.clone();
    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(6)]).split(area);
    draw_profile_bar(frame, app, rows[0]);

    app.cell.organelle_areas.clear();
    app.cell.lever_areas.clear();

    // The membrane: everything inside it is this node, everything outside is not.
    let membrane = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(MUTED))
        .title(Span::styled(
            " MEMBRANE · inside vs outside ",
            Style::default().fg(MUTED),
        ))
        .title_alignment(Alignment::Center);
    let inside = membrane.inner(rows[1]);
    frame.render_widget(membrane, rows[1]);

    // 3 columns of organelles need room for a label, a value and a gap; below that
    // the boxes are narrower than their own titles and the grid stops being readable.
    if inside.width >= 84 && inside.height >= 16 {
        draw_cell_grid(frame, app, inside, document.as_ref());
    } else {
        draw_cell_accordion(frame, app, inside, document.as_ref());
    }
}

/// Which posture this node is closest to, and how to see where it differs.
fn draw_profile_bar(frame: &mut Frame, app: &App, area: Rect) {
    let report = app.cell_profile();
    let colour = if report.deviations.is_empty() { GOOD } else { WARN };
    let line = Line::from(vec![
        Span::styled(" closest profile ", Style::default().fg(MUTED)),
        Span::styled(report.summary(), Style::default().fg(colour).bold()),
        Span::styled(
            format!(" ({}/{} keys)", report.matched(), report.total),
            Style::default().fg(MUTED),
        ),
        Span::styled(
            if area.width >= 92 {
                "   p apply a profile · d what differs"
            } else {
                ""
            },
            Style::default().fg(MUTED),
        ),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

/// Two rows of three organelles with the nucleus banded across the middle.
///
/// The nucleus is horizontal and central because it is what everything else depends
/// on and the only part whose loss is permanent: a mnemonic is not recoverable, and
/// a row of it beside "keep failures for 7 days" would read as equally routine.
fn draw_cell_grid(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let nucleus_height = (Organelle::Nucleus.levers().len() as u16 + 3).min(area.height / 3);
    let bands = Layout::vertical([
        Constraint::Min(5),
        Constraint::Length(nucleus_height),
        Constraint::Min(5),
    ])
    .split(area);
    let top = Layout::horizontal([
        Constraint::Ratio(1, 3),
        Constraint::Ratio(1, 3),
        Constraint::Ratio(1, 3),
    ])
    .split(bands[0]);
    let bottom = Layout::horizontal([
        Constraint::Ratio(1, 3),
        Constraint::Ratio(1, 3),
        Constraint::Ratio(1, 3),
    ])
    .split(bands[2]);

    let placement = [
        (Organelle::Channels, top[0]),
        (Organelle::Ribosomes, top[1]),
        (Organelle::Vesicles, top[2]),
        (Organelle::Nucleus, bands[1]),
        (Organelle::Immune, bottom[0]),
        (Organelle::Mitochondria, bottom[1]),
        (Organelle::Vacuole, bottom[2]),
    ];
    for (organelle, box_area) in placement {
        draw_organelle(frame, app, organelle, box_area, document);
    }
}

/// One column, with only the focused organelle open.
///
/// A narrow terminal cannot show six boxes of rows at once, and a grid squeezed into
/// one would clip the values -- which on this page are the whole content. So the rest
/// collapse to a single summary line each and stay one keypress away.
fn draw_cell_accordion(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let focused = app.cell.organelle();
    let constraints: Vec<Constraint> = Organelle::ALL
        .iter()
        .map(|organelle| {
            if *organelle == focused {
                // Whatever the collapsed rows leave: a box shorter than its levers
                // would hide the ones at the bottom with no way to reach them.
                Constraint::Min(focused.levers().len() as u16 + 2)
            } else {
                Constraint::Length(1)
            }
        })
        .collect();
    let rows = Layout::vertical(constraints).split(area);
    for (organelle, row) in Organelle::ALL.iter().zip(rows.iter()) {
        if *organelle == focused {
            draw_organelle(frame, app, *organelle, *row, document);
        } else {
            draw_collapsed_organelle(frame, app, *organelle, *row, document);
        }
    }
}

/// One organelle's box: its levers, each with the position it is currently in.
fn draw_organelle(
    frame: &mut Frame,
    app: &mut App,
    organelle: Organelle,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let index = Organelle::ALL
        .iter()
        .position(|candidate| *candidate == organelle)
        .unwrap_or(0);
    let focused = app.cell.organelle == index;
    let colour = organelle_colour(organelle);
    app.cell.organelle_areas.push((index, area));

    let block = Block::bordered()
        .border_type(if organelle == Organelle::Nucleus {
            // The nucleus is the one part of the cell whose loss cannot be undone.
            BorderType::Double
        } else {
            BorderType::Rounded
        })
        .border_style(Style::default().fg(if focused { colour } else { MUTED }))
        .title(Line::from(vec![
            Span::styled(
                format!(" {} ", organelle.title()),
                Style::default().fg(colour).bold(),
            ),
            Span::styled(
                format!("· {} ", organelle.subtitle()),
                Style::default().fg(MUTED),
            ),
        ]));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height == 0 {
        return;
    }

    let levers = organelle.levers();
    let rows = Layout::vertical(
        (0..inner.height)
            .map(|_| Constraint::Length(1))
            .collect::<Vec<_>>(),
    )
    .split(inner);
    for (lever_index, (row, lever)) in rows.iter().zip(levers.iter()).enumerate() {
        let selected = focused && app.cell.lever == lever_index;
        app.cell.lever_areas.push((index, lever_index, *row));
        frame.render_widget(
            Paragraph::new(lever_line(lever, document, selected, row.width)),
            *row,
        );
    }
}

/// A collapsed organelle: its name, and enough of its state to know whether to open
/// it. A row that said only "IMMUNE" would make the operator open all six to find
/// the one they want.
fn draw_collapsed_organelle(
    frame: &mut Frame,
    app: &mut App,
    organelle: Organelle,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let index = Organelle::ALL
        .iter()
        .position(|candidate| *candidate == organelle)
        .unwrap_or(0);
    app.cell.organelle_areas.push((index, area));
    let summary = organelle
        .levers()
        .iter()
        .take(2)
        .map(|lever| cell::status(lever, document).label(lever))
        .collect::<Vec<_>>()
        .join(" · ");
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("  ", Style::default()),
            Span::styled(
                format!("{:<14}", organelle.title()),
                Style::default().fg(organelle_colour(organelle)),
            ),
            Span::styled(summary, Style::default().fg(MUTED)),
        ])),
        area,
    );
}

/// One lever row: what it decides on the left, where it is set on the right.
fn lever_line(
    lever: &Lever,
    document: Option<&serde_yaml::Value>,
    selected: bool,
    width: u16,
) -> Line<'static> {
    let status = cell::status(lever, document);
    let marker = match &status {
        // A filled marker is a position this page named; a hollow one is a value it
        // is only reporting. `custom` and `not set` are called out because both mean
        // "this page cannot describe what your node is doing here".
        LeverStatus::State(_) => "●",
        LeverStatus::Value(_) => "·",
        LeverStatus::Custom | LeverStatus::Unset => "⁓",
        LeverStatus::Link => "→",
    };
    let value = status.label(lever);
    let value_colour = match &status {
        LeverStatus::Custom | LeverStatus::Unset => WARN,
        LeverStatus::Link => ACCENT,
        _ if lever.warning.is_some() => BAD,
        _ => Color::White,
    };
    // The label is padded to a fixed column so the values line up down the box: a
    // ragged right edge on eight rows is what makes a panel hard to scan.
    let label_width = (width.saturating_sub(14)).clamp(8, 18) as usize;
    let label = shorten(lever.label, label_width);
    Line::from(vec![
        Span::styled(
            if selected { "▸" } else { " " },
            Style::default().fg(ACCENT).bold(),
        ),
        Span::styled(
            format!("{label:<label_width$} "),
            if selected {
                Style::default().fg(Color::White).bold()
            } else {
                Style::default().fg(Color::Gray)
            },
        ),
        Span::styled(format!("{marker} "), Style::default().fg(value_colour)),
        Span::styled(value, Style::default().fg(value_colour)),
    ])
}

fn organelle_colour(organelle: Organelle) -> Color {
    match organelle {
        Organelle::Channels => ACCENT,
        Organelle::Ribosomes => Color::LightBlue,
        Organelle::Vesicles => Color::LightMagenta,
        Organelle::Nucleus => WARN,
        Organelle::Immune => Color::Red,
        Organelle::Mitochondria => GOOD,
        Organelle::Vacuole => MUTED,
    }
}

/// The profile picker: the postures, ordered from the most closed to the most open,
/// with how far this node already is from each.
fn draw_profile_popup(frame: &mut Frame, app: &App) {
    let profiles = cell::profiles();
    let area = centered_rect(70, profiles.len() as u16 * 2 + 6, frame.size());
    frame.render_widget(Clear, area);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(ACCENT))
        .title(Span::styled(
            " APPLY A PROFILE ",
            Style::default().fg(ACCENT).bold(),
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let mut lines: Vec<Line> = Vec::new();
    for (index, profile) in profiles.iter().enumerate() {
        let report = cell::report(profile, app.config_document.as_ref());
        let selected = index == app.cell.profile;
        let distance = if report.deviations.is_empty() {
            "you are here".to_string()
        } else {
            format!("{} keys would change", report.deviations.len())
        };
        lines.push(Line::from(vec![
            Span::styled(
                if selected { "▸ " } else { "  " },
                Style::default().fg(ACCENT).bold(),
            ),
            Span::styled(
                format!("{:<18}", profile.label),
                if selected {
                    Style::default().fg(Color::White).bold()
                } else {
                    Style::default().fg(Color::Gray)
                },
            ),
            Span::styled(
                distance,
                Style::default().fg(if report.deviations.is_empty() { GOOD } else { MUTED }),
            ),
        ]));
        lines.push(Line::from(Span::styled(
            format!("    {}", profile.blurb),
            Style::default().fg(MUTED),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "A profile sets policy only — never an identity, a wallet or a path.",
        Style::default().fg(MUTED),
    )));
    lines.push(Line::from(Span::styled(
        "⏎ see exactly what changes  ·  Esc cancel",
        Style::default().fg(WARN),
    )));
    frame.render_widget(Paragraph::new(lines), inner);
}

fn draw_pricing(frame: &mut Frame, app: &mut App, area: Rect) {
    let columns = Layout::horizontal([Constraint::Percentage(62), Constraint::Percentage(38)])
        .split(area);
    let left = Layout::vertical([
        Constraint::Percentage(50),
        Constraint::Percentage(50),
    ])
    .split(columns[0]);

    let selected = app.prices.state_id.clone();
    draw_price_bars(
        frame,
        left[0],
        PriceChart {
            title: " RECURRING • charged while held ",
            recurring: true,
            color: ACCENT,
        },
        &app.prices.items,
        selected.as_deref(),
        &app.money,
    );
    draw_price_bars(
        frame,
        left[1],
        PriceChart {
            title: " ONE-OFF • charged per event ",
            recurring: false,
            color: Color::Magenta,
        },
        &app.prices.items,
        selected.as_deref(),
        &app.money,
    );

    // 13 rows fit the card's tallest state (a price selected, plus the worked
    // example on the last line); anything shorter silently clips the example.
    let right = Layout::vertical([Constraint::Length(13), Constraint::Min(4)]).split(columns[1]);
    draw_money_card(frame, app, right[0]);
    draw_price_table(frame, app, right[1]);
}

/// Which half of the price vector a chart draws, and how it looks.
struct PriceChart<'a> {
    title: &'a str,
    /// Recurring prices are charged for as long as a resource is held; one-off ones
    /// price an event.
    recurring: bool,
    color: Color,
}

/// One vertical bar per price in the requested group.
fn draw_price_bars(
    frame: &mut Frame,
    area: Rect,
    chart: PriceChart,
    prices: &[PriceEntry],
    selected: Option<&str>,
    money: &Money,
) {
    let PriceChart {
        title,
        recurring,
        color,
    } = chart;
    // Node-wide prices only. A per-arch override is a variation on the price above it,
    // not a fourth resource, and charting both would show "RAM" three times and read as
    // three times the memory revenue. The table below carries every per-arch figure.
    let group: Vec<&PriceEntry> = prices
        .iter()
        .filter(|entry| entry.recurring == recurring && entry.arch.is_none())
        .collect();
    if group.is_empty() {
        return;
    }

    // A zero-height bar prints no value -- BarChart draws the amount inside the bar --
    // so "free" has to ride in the label, which is always rendered. Without it a price
    // deliberately set to nothing looks identical to a missing feature. Amounts small
    // but non-zero legitimately render as a short bar; the table beside the chart is
    // what carries every exact figure.
    let bars: Vec<Bar> = group
        .iter()
        .map(|entry| {
            let highlighted = selected == Some(entry.key);
            Bar::default()
                .value(entry.mu)
                .text_value(money.format_raw(&entry.mu.to_string()))
                .label(Line::from(if entry.mu == 0 {
                    format!("{} free", entry.short)
                } else {
                    entry.short.to_string()
                }))
                .style(Style::default().fg(if highlighted { Color::White } else { color }))
                .value_style(if highlighted {
                    Style::default().fg(Color::Black).bg(Color::White).bold()
                } else {
                    Style::default().fg(Color::Black).bg(color)
                })
        })
        .collect();

    let width = ((area.width.saturating_sub(4)) / group.len().max(1) as u16).clamp(3, 14);
    let chart = BarChart::default()
        .block(section_block(title.to_string(), color))
        .data(BarGroup::default().bars(&bars))
        .bar_width(width.saturating_sub(1).max(1))
        .bar_gap(1)
        .label_style(Style::default().fg(MUTED));
    frame.render_widget(chart, area);
}

/// What the numbers on the bars actually mean: the display unit, the ledger rate, the
/// scarcity ceiling that bounds every price, and a worked example.
fn draw_money_card(frame: &mut Frame, app: &App, area: Rect) {
    let money = &app.money;
    let hourly = app.reference_hourly_mu();
    let selected = app.prices.selected();

    let mut lines = vec![
        metric_line("Unit", format!("{} ({})", money.symbol, money.unit_name)),
        metric_line("1 MU", {
            // What one MU is worth on the ledger. This is the piece that makes a price
            // in MU mean anything to a peer, so it is stated rather than implied.
            let nanoerg = 1.0 / money.mu_per_nanoerg;
            format!("{nanoerg} nanoERG")
        }),
        metric_line(
            "Scarcity",
            format!(
                "x1 .. x{} (curve {})",
                app.scarcity.max_multiplier, app.scarcity.curve
            ),
        ),
        Line::from(""),
    ];

    if let Some(entry) = selected {
        lines.push(Line::from(Span::styled(
            entry.config_label(),
            Style::default().fg(Color::White).bold(),
        )));
        lines.push(metric_line("Price", format!("{} MU {}", entry.mu, entry.per)));
        lines.push(metric_line("That is", money.format_mu(entry.mu)));
        lines.push(metric_line(
            "At max load",
            money.format_mu(entry.mu.saturating_mul(app.scarcity.max_multiplier)),
        ));

        // The whole reason this page has to mention the virtualizer at all: a memory
        // price is not what the node earns per GiB it commits. The guest kernel's own
        // footprint comes out of the VM's RAM before the service sees any of it, so
        // the node boots the VM larger than the service declared -- and absorbs the
        // difference, so a client never pays for the kernel underneath it. Set a
        // memory price without that in view and it under-recovers, silently, by an
        // amount that differs per architecture.
        if let (Some(arch), Some((effective, multiplier))) =
            (entry.arch, app.effective_memory_mu(entry))
        {
            if let Some(reserve) = app.reserve_for(arch) {
                lines.push(Line::from(""));
                lines.push(metric_line(
                    "Guest kernel",
                    format!("+{} MiB +{:.0}%", reserve.fixed_mib, reserve.ratio * 100.0),
                ));
                // "You set X, you keep Y" -- said in the same unit as the price above
                // it, because the operator is choosing X and cares about Y.
                lines.push(metric_line(
                    "Node earns",
                    format!("{} MU /GiB committed", effective.round() as u64),
                ));
                lines.push(metric_line(
                    "…on 1GiB",
                    format!("{:.0}% of the price set", 100.0 / multiplier),
                ));
            }
        }
    }

    lines.push(Line::from(""));
    // Approximate on purpose: the node charges per manager tick and truncates each one
    // to whole MU, so an hour of ticks comes to a hair less than the hourly price.
    lines.push(metric_line("1h example", "256MiB+1vCPU+10GiB"));
    lines.push(Line::from(Span::styled(
        format!("{:<12}~ {}", "", money.format_mu(hourly)),
        Style::default().fg(GOOD),
    )));

    draw_card(frame, area, "MONEY", lines, GOOD);
}

fn draw_price_table(frame: &mut Frame, app: &mut App, area: Rect) {
    let money = app.money.clone();
    let rows: Vec<Row> = app
        .prices
        .items
        .iter()
        .map(|entry| {
            // A per-arch row that config.yaml does not set is showing the scalar price
            // it inherits, not a price of its own. Saying so is the difference between
            // "arm64 costs this" and "arm64 has been given its own rate" -- editing it
            // is what turns the first into the second, and the operator should be able
            // to tell which they are looking at before they nudge it.
            let amount = if entry.inherited {
                format!("{} (inherited)", money.format_mu(entry.mu))
            } else {
                money.format_mu(entry.mu)
            };
            let row = Row::new(vec![entry.short.clone(), entry.mu.to_string(), amount]);
            if entry.arch.is_some() {
                row.style(Style::default().fg(MUTED))
            } else {
                row
            }
        })
        .collect();
    let table = Table::new(
        rows,
        [
            Constraint::Length(12),
            Constraint::Percentage(40),
            Constraint::Percentage(48),
        ],
    )
    .header(header_row(vec!["Price", "MU", money.symbol.as_str()]))
    .block(section_block(
        " PRICES • +/- adjust, e exact ".to_string(),
        Color::Yellow,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("> ");
    app.list_area = area;
    frame.render_stateful_widget(table, area, &mut app.prices.state);
}

fn draw_config(frame: &mut Frame, app: &mut App, area: Rect) {
    let needle = app.config_filter.to_lowercase();
    // Owns its Strings (`'static`), so it doesn't borrow `app` and the tree state
    // can be mutated (pre-selection, render) alongside it.
    let items = build_config_tree(&app.config_all, &needle);

    // On first entry nothing is selected yet; land on the first top-level section
    // so the selection highlight (and later Enter/e) has a target.
    if app.config_tree_state.selected().is_empty() {
        if let Some(first) = items.first() {
            app.config_tree_state
                .select(vec![first.identifier().clone()]);
        }
    }

    let title = if app.config_filter.is_empty() {
        format!(
            " CONFIGURATION • {} values • all sections ",
            app.config_all.len()
        )
    } else {
        let matches = app
            .config_all
            .iter()
            .filter(|entry| {
                entry.path.to_lowercase().contains(&needle)
                    || (!entry.secret && entry.value.to_lowercase().contains(&needle))
            })
            .count();
        format!(
            " CONFIGURATION • {} values • filter \"{}\" • {} match ",
            app.config_all.len(),
            app.config_filter,
            matches
        )
    };

    let tree = Tree::new(&items)
        .expect("config tree identifiers are unique within each section")
        .block(section_block(title, Color::Yellow))
        .highlight_style(selected_style())
        .node_closed_symbol("▸ ")
        .node_open_symbol("▾ ")
        .node_no_children_symbol("· ");
    frame.render_stateful_widget(tree, area, &mut app.config_tree_state);
}

/// Build the collapsible configuration tree from the flat, document-ordered
/// [`ConfigEntry`] list. Each mapping/sequence becomes a branch and every scalar
/// a leaf; the branch structure comes straight from each entry's
/// `path_segments`, so the data model is unchanged — this only shapes how it is
/// drawn. `needle` (already lowercased; empty means no filter) highlights the
/// nodes that match, without removing any of the others.
fn build_config_tree(entries: &[ConfigEntry], needle: &str) -> Vec<TreeItem<'static, String>> {
    // An ordered intermediate tree: children stay in document order, and a node
    // either carries a scalar (`leaf`) or has children, never both.
    #[derive(Default)]
    struct Node {
        children: Vec<(String, Node)>,
        leaf: Option<usize>,
    }
    fn child_mut<'a>(node: &'a mut Node, token: &str) -> &'a mut Node {
        if let Some(pos) = node.children.iter().position(|(t, _)| t == token) {
            &mut node.children[pos].1
        } else {
            node.children.push((token.to_string(), Node::default()));
            &mut node.children.last_mut().unwrap().1
        }
    }

    let mut root = Node::default();
    for (index, entry) in entries.iter().enumerate() {
        let mut cursor = &mut root;
        for segment in &entry.path_segments {
            let token = segment_token(segment);
            cursor = child_mut(cursor, &token);
        }
        cursor.leaf = Some(index);
    }

    fn convert(
        token: &str,
        node: Node,
        parent_path: &str,
        entries: &[ConfigEntry],
        needle: &str,
    ) -> TreeItem<'static, String> {
        let display_path = join_config_path(parent_path, token);
        match node.leaf {
            Some(index) => {
                let entry = &entries[index];
                let highlighted = !needle.is_empty()
                    && (entry.path.to_lowercase().contains(needle)
                        || (!entry.secret && entry.value.to_lowercase().contains(needle)));
                TreeItem::new_leaf(token.to_string(), config_leaf_line(entry, highlighted))
            }
            None => {
                let count = node.children.len();
                let highlighted = !needle.is_empty() && display_path.to_lowercase().contains(needle);
                let children = node
                    .children
                    .into_iter()
                    .map(|(child_token, child)| {
                        convert(&child_token, child, &display_path, entries, needle)
                    })
                    .collect::<Vec<_>>();
                TreeItem::new(
                    token.to_string(),
                    config_branch_line(token, count, highlighted),
                    children,
                )
                .expect("config tree identifiers are unique within each section")
            }
        }
    }

    root.children
        .into_iter()
        .map(|(token, node)| convert(&token, node, "", entries, needle))
        .collect()
}

/// Reconstruct the dotted display path (`a.b[1].c`) from a parent path and one
/// more token, matching `config_path_display` so highlight tests hit the same
/// strings the flat table used to show.
fn join_config_path(parent: &str, token: &str) -> String {
    if parent.is_empty() {
        token.to_string()
    } else if token.starts_with('[') {
        format!("{parent}{token}")
    } else {
        format!("{parent}.{token}")
    }
}

/// A scalar leaf: `key: value  [type]`, with the value masked exactly as the
/// table did (via [`ConfigEntry::display_value`]). The key is reverse-video when
/// it matches the active filter.
fn config_leaf_line(entry: &ConfigEntry, highlighted: bool) -> Line<'static> {
    let key = entry
        .path_segments
        .last()
        .map(segment_token)
        .unwrap_or_default();
    let key_style = if highlighted {
        Style::default().fg(Color::Black).bg(WARN).bold()
    } else {
        Style::default().fg(Color::White)
    };
    Line::from(vec![
        Span::styled(key, key_style),
        Span::styled(": ", Style::default().fg(MUTED)),
        Span::styled(entry.display_value(), Style::default().fg(ACCENT)),
        Span::styled(
            format!("  [{}]", entry.value_type),
            Style::default().fg(MUTED),
        ),
    ])
}

/// A section branch: its name plus the number of direct children, highlighted
/// (reverse-video) when the section's path matches the active filter.
fn config_branch_line(token: &str, count: usize, highlighted: bool) -> Line<'static> {
    let name_style = if highlighted {
        Style::default().fg(Color::Black).bg(WARN).bold()
    } else {
        Style::default().fg(WARN).bold()
    };
    Line::from(vec![
        Span::styled(token.to_string(), name_style),
        Span::styled(format!("  ({count})"), Style::default().fg(MUTED)),
    ])
}

fn draw_logs(frame: &mut Frame, app: &App, area: Rect) {
    let split =
        Layout::horizontal([Constraint::Percentage(68), Constraint::Percentage(32)]).split(area);
    let node_text = visible_tail(&app.node_logs, split[0].height.saturating_sub(2) as usize);
    frame.render_widget(
        Paragraph::new(node_text)
            .block(section_block(" NODE LOG • app.log ", Color::White))
            .style(Style::default().fg(Color::Gray))
            .wrap(Wrap { trim: false }),
        split[0],
    );
    let action_text = visible_tail(&app.app_logs, split[1].height.saturating_sub(2) as usize);
    frame.render_widget(
        Paragraph::new(action_text)
            .block(section_block(" TUI ACTIONS ", ACCENT))
            .style(Style::default().fg(Color::Gray))
            .wrap(Wrap { trim: false }),
        split[1],
    );
}

fn draw_footer(frame: &mut Frame, app: &App, area: Rect) {
    let controls = match app.page() {
        Page::Overview => "tab/shift+tab cycle  •  r refresh  •  q quit",
        Page::Instances => {
            "tab/shift+tab cycle  •  ↑/↓ select  •  g tree/flat  •  k kill  •  r refresh  •  q quit"
        }
        Page::Services => {
            "tab/shift+tab cycle  •  ↑/↓ select  •  e execute  •  i details  •  d delete  •  q quit"
        }
        Page::Peers => {
            "tab/shift+tab cycle  •  ↑/↓ select  •  +/- reputation  •  c connect  •  d forget  •  q quit"
        }
        Page::Clients => {
            "tab/shift+tab cycle  •  ↑/↓ select  •  + credit  •  - debit  •  r refresh  •  q quit"
        }
        Page::Cell => {
            "→/← organelle  •  ↑/↓ lever  •  ⏎ change  •  e keys behind it  •  p profiles  •  d deviations  •  n router guide"
        }
        Page::Pricing => {
            "tab/shift+tab cycle  •  ↑/↓ select  •  +/- adjust 10%  •  e exact value  •  r refresh  •  q quit"
        }
        Page::Config => {
            "tab/shift+tab cycle  •  ↑/↓ select  •  →/← branch  •  ⏎ toggle  •  e edit  •  a add to list  •  d remove element  •  / filter  •  q quit"
        }
        Page::Logs => "tab/shift+tab cycle  •  r refresh  •  q quit",
    };
    let lines = vec![
        Line::from(Span::styled(&app.status, Style::default().fg(WARN))),
        Line::from(Span::styled(controls, Style::default().fg(MUTED))),
    ];
    frame.render_widget(Paragraph::new(lines).alignment(Alignment::Center), area);
}

/// Body lines and hint text for the `EditConfig` popup, one variant per
/// [`EditKind`]: a checkbox, a steppable number, a cyclable enum picker, or the
/// original freeform text field (also used outside config editing, e.g. Connect
/// and the filter box).
fn edit_popup_body(app: &App) -> (Vec<Line<'static>>, String) {
    if app.input_mode == InputMode::AddConfigItem {
        // The quoting note is not decoration: a leading `*` is YAML's alias
        // indicator, so `*.example.com` unquoted is rejected as invalid YAML rather
        // than stored. A `*` anywhere else needs no quoting.
        return (
            vec![Line::from(app.input.clone())],
            "Enter appends • Esc cancels • a YAML literal, so quote a leading *: \"*.example.com\""
                .to_string(),
        );
    }
    if app.input_mode != InputMode::EditConfig {
        return (
            vec![Line::from(app.input.clone())],
            "Enter saves • Esc cancels • Ctrl+U clears".to_string(),
        );
    }

    match &app.edit_kind {
        EditKind::Bool => {
            let checked = app.input.trim() == "true";
            let label = if checked { "[x] true" } else { "[ ] false" };
            (
                vec![Line::from(Span::styled(
                    label,
                    Style::default().fg(Color::White).bold(),
                ))],
                "Space / ←/→ toggles • Enter saves • Esc cancels".to_string(),
            )
        }
        EditKind::Number => (
            vec![Line::from(app.input.clone())],
            "↑/↓ adjust by 1 • type to overwrite • Enter saves • Esc cancels".to_string(),
        ),
        EditKind::Enum(options) => {
            let current = app.input.trim();
            let lines = options
                .iter()
                .map(|option| {
                    let selected = option == current;
                    let marker = if selected { "▸ " } else { "  " };
                    Line::from(Span::styled(
                        format!("{marker}{option}"),
                        if selected {
                            Style::default().fg(ACCENT).bold()
                        } else {
                            Style::default().fg(Color::White)
                        },
                    ))
                })
                .collect();
            (lines, "↑/↓ cycle • Enter saves • Esc cancels".to_string())
        }
        EditKind::Text => {
            let secret_hint = if app.edit_config_secret {
                " • existing secret hidden; blank keeps it, type \"\" to clear"
            } else {
                ""
            };
            let display = if app.edit_config_secret {
                "•".repeat(app.input.chars().count())
            } else {
                app.input.clone()
            };
            (
                vec![Line::from(display)],
                format!("Enter saves • Esc cancels • Ctrl+U clears{secret_hint}"),
            )
        }
    }
}

fn draw_input_popup(frame: &mut Frame, app: &App) {
    let (mut content, hint) = edit_popup_body(app);
    let height = (content.len() as u16 + 2).max(3) + 2;
    let area = centered_rect(72, height, frame.size());
    frame.render_widget(Clear, area);
    content.push(Line::from(Span::styled(hint, Style::default().fg(MUTED))));
    let popup = Paragraph::new(content)
        .block(
            Block::bordered()
                .title(Span::styled(
                    format!(" {} ", app.input_title),
                    Style::default().fg(ACCENT).bold(),
                ))
                .border_style(Style::default().fg(ACCENT)),
        )
        .style(Style::default().fg(Color::White).bg(Color::Black));
    frame.render_widget(popup, area);
}

fn draw_confirm_popup(frame: &mut Frame, app: &App) {
    let area = centered_rect(60, 6, frame.size());
    frame.render_widget(Clear, area);
    let content = vec![
        Line::from(Span::styled(
            app.input_title.clone(),
            Style::default().fg(Color::White).bold(),
        )),
        Line::from(""),
        Line::from(Span::styled(
            "y confirms • n / Esc cancels",
            Style::default().fg(MUTED),
        )),
    ];
    let popup = Paragraph::new(content)
        .alignment(Alignment::Center)
        .block(
            Block::bordered()
                .title(Span::styled(" CONFIRM ", Style::default().fg(WARN).bold()))
                .border_style(Style::default().fg(WARN)),
        )
        .style(Style::default().fg(Color::White).bg(Color::Black));
    frame.render_widget(popup, area);
}

fn draw_details_popup(frame: &mut Frame, app: &App) {
    let (title, text, scroll, lines) = match &app.details {
        Some(details) => (
            details.title.clone(),
            details.lines.join("\n"),
            details.scroll as u16,
            details.lines.len() as u16,
        ),
        None => (String::new(), String::new(), 0, 0),
    };
    // A diff of five keys in a box of thirty rows reads as if something is missing.
    // The overlay is sized to what it holds, up to the room there is.
    let height = (lines + 2).clamp(8, frame.size().height.saturating_sub(4).max(8));
    let area = centered_rect(80, height, frame.size());
    frame.render_widget(Clear, area);
    let confirming = app.input_mode == InputMode::ConfirmWrites;
    let keys = if confirming {
        "y apply • n cancel • ↑/↓ scroll"
    } else {
        "↑/↓ scroll • Esc close"
    };
    let colour = if confirming { WARN } else { ACCENT };
    let popup = Paragraph::new(text)
        .scroll((scroll, 0))
        .wrap(Wrap { trim: false })
        .block(
            Block::bordered()
                .title(Span::styled(
                    format!(" {title} • {keys} "),
                    Style::default().fg(colour).bold(),
                ))
                .border_style(Style::default().fg(colour)),
        )
        .style(Style::default().fg(Color::White).bg(Color::Black));
    frame.render_widget(popup, area);
}

fn centered_rect(percent_x: u16, height: u16, area: Rect) -> Rect {
    let vertical = Layout::vertical([
        Constraint::Fill(1),
        Constraint::Length(height.min(area.height)),
        Constraint::Fill(1),
    ])
    .split(area);
    Layout::horizontal([
        Constraint::Percentage((100 - percent_x) / 2),
        Constraint::Percentage(percent_x),
        Constraint::Percentage((100 - percent_x) / 2),
    ])
    .split(vertical[1])[1]
}

fn header_row(labels: Vec<&str>) -> Row<'static> {
    Row::new(
        labels
            .into_iter()
            .map(|label| Cell::from(label.to_string()))
            .collect::<Vec<_>>(),
    )
    .style(Style::default().fg(ACCENT).bold())
    .bottom_margin(1)
}

fn section_block(title: impl Into<String>, color: Color) -> Block<'static> {
    Block::bordered()
        .title(Span::styled(
            title.into(),
            Style::default().fg(color).bold(),
        ))
        .border_style(Style::default().fg(color))
}

fn selected_style() -> Style {
    Style::default().fg(Color::Black).bg(ACCENT).bold()
}

fn format_balance(balance: Option<f64>) -> String {
    balance
        .map(|amount| format!("{amount:.6} ERG"))
        .unwrap_or_else(|| "—".to_string())
}

fn nonempty<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.trim().is_empty() {
        fallback
    } else {
        value
    }
}

fn visible_tail(lines: &[String], count: usize) -> String {
    let start = lines.len().saturating_sub(count);
    lines[start..].join("\n")
}

#[cfg(test)]
mod tests {

    /// The cell is a panel an operator reads to decide something, so what matters is
    /// that the decision and where it is currently set are both legibly on screen --
    /// at both layouts, and without a secret leaking into either.
    mod cell_page {
        use super::super::{draw_cell, lever_line};
        use crate::app::App;
        use crate::cell::{self, LeverKind};
        use ratatui::{backend::TestBackend, Terminal};

        fn screen(width: u16, height: u16, config: &str) -> String {
            let mut app = App::new();
            // The page derives every lever from the cached document, the same one
            // the refresh sweep reloads, so the fixture goes there.
            app.config_document = Some(serde_yaml::from_str(config).unwrap());
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_cell(frame, &mut app, frame.size()))
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..buffer.area.height)
                .map(|row| {
                    (0..buffer.area.width)
                        .map(|column| buffer.get(column, row).symbol())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n")
        }

        const RENTING: &str = "client:\n  ACCEPT_NEW_DEPOSITS: true\nnetwork:\n  GATEWAY_PORT: 58443\n  DISABLE_EXPOSE_OUTSIDE: true\ncosts:\n  ALLOW_DEBT: false\nidentity:\n  MNEMONIC: \"abandon abandon ability\"\n";

        /// Wide enough for the grid: every organelle is drawn, so the operator can
        /// see the whole cell without hunting through collapsed sections.
        #[test]
        fn the_grid_shows_every_organelle() {
            let screen = screen(140, 40, RENTING);
            for organelle in cell::Organelle::ALL {
                assert!(
                    screen.contains(organelle.title()),
                    "{} is missing from the grid:\n{screen}",
                    organelle.title()
                );
            }
            assert!(screen.contains("MEMBRANE"), "the membrane frames the page");
        }

        /// A lever is only useful if its current position is visible: "outside work"
        /// with no "open" beside it is a question with no answer.
        #[test]
        fn a_levers_current_position_is_on_screen_beside_it() {
            let screen = screen(140, 40, RENTING);
            assert!(screen.contains("outside work"));
            assert!(screen.contains("open"), "the position is shown:\n{screen}");
            assert!(screen.contains("58443"), "a scalar shows its value:\n{screen}");
        }

        /// The one thing this page must never do. The mnemonic is in the file it
        /// reads, and the nucleus row that reports it says only whether it is set.
        #[test]
        fn a_secret_never_reaches_the_screen() {
            for (width, height) in [(140, 40), (80, 24)] {
                let screen = screen(width, height, RENTING);
                assert!(
                    !screen.contains("abandon"),
                    "the identity mnemonic leaked at {width}x{height}:\n{screen}"
                );
            }
        }

        /// A narrow terminal cannot show six boxes of rows, so it collapses to one
        /// column -- and still names every organelle, so nothing becomes unreachable.
        #[test]
        fn a_narrow_terminal_collapses_to_one_column_without_losing_an_organelle() {
            let screen = screen(80, 24, RENTING);
            for organelle in cell::Organelle::ALL {
                assert!(
                    screen.contains(organelle.title()),
                    "{} is unreachable at 80x24:\n{screen}",
                    organelle.title()
                );
            }
        }

        /// A combination the catalogue cannot name is called out rather than shown
        /// as one of the named positions. Showing the nearest one would have the
        /// page misreport the policy the node is running.
        #[test]
        fn an_unnamed_combination_is_marked_rather_than_rounded() {
            let lever = cell::lever("descendants").unwrap();
            let document: serde_yaml::Value = serde_yaml::from_str(
                "workload_admission:\n  POLICY: full\n  ON_UNSATISFIABLE: reject\n",
            )
            .unwrap();
            let line = lever_line(lever, Some(&document), false, 30);
            let text: String = line
                .spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect();
            assert!(text.contains("custom"), "unnamed state reads as: {text}");
            assert!(!text.contains("strict") && !text.contains("lenient"));
        }

        /// Every lever the page can draw has a label short enough to survive the
        /// narrowest box the grid produces, or its value is pushed off the row.
        #[test]
        fn every_lever_label_fits_the_narrowest_box() {
            for lever in cell::levers() {
                assert!(
                    lever.label.chars().count() <= 18,
                    "{} has a label too long for a box",
                    lever.id
                );
            }
        }

        /// Every lever says what it decides and what happens if you change it. The
        /// questions and consequences are the page's whole reason to exist over the
        /// raw keys, so an empty one is a bug rather than a style slip.
        #[test]
        fn every_lever_explains_itself() {
            for lever in cell::levers() {
                assert!(!lever.question.is_empty(), "{} asks nothing", lever.id);
                assert!(
                    lever.consequence.len() > 30,
                    "{} does not say what changing it does",
                    lever.id
                );
                if let LeverKind::Link(_) = lever.kind {
                    continue;
                }
                assert!(!lever.paths().is_empty(), "{} writes nothing", lever.id);
            }
        }
    }

    /// The bars are an editor, so what matters is that every price is actually on
    /// screen -- including a free one and one small enough to round to no bar at all,
    /// which is exactly when a chart quietly stops telling the truth.
    mod pricing {
        use super::super::{draw_price_bars, PriceChart, ACCENT};
        use crate::app::{Money, PriceEntry};
        use ratatui::{backend::TestBackend, Terminal};

        fn entry(key: &'static str, short: &'static str, mu: u64, recurring: bool) -> PriceEntry {
            PriceEntry {
                id: key.to_string(),
                key,
                short: short.to_string(),
                per: "per unit",
                recurring,
                arch: None,
                inherited: false,
                mu,
            }
        }

        /// A per-architecture override of `key`, as `get_prices` builds one.
        fn arch_entry(
            key: &'static str,
            short: &str,
            arch: &'static str,
            mu: u64,
            inherited: bool,
        ) -> PriceEntry {
            PriceEntry {
                id: format!("{arch}/{key}"),
                key,
                short: short.to_string(),
                per: "per unit",
                recurring: true,
                arch: Some(arch),
                inherited,
                mu,
            }
        }

        fn render(prices: &[PriceEntry], selected: Option<&str>) -> String {
            let mut terminal = Terminal::new(TestBackend::new(60, 14)).unwrap();
            terminal
                .draw(|frame| {
                    draw_price_bars(
                        frame,
                        frame.size(),
                        PriceChart {
                            title: " RECURRING ",
                            recurring: true,
                            color: ACCENT,
                        },
                        prices,
                        selected,
                        &Money::default(),
                    );
                })
                .unwrap();
            terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect()
        }

        #[test]
        fn every_price_gets_a_labelled_bar() {
            let prices = vec![
                entry("RAM_MU_PER_GIB_HOUR", "RAM", 1_000_000, true),
                entry("CPU_MU_PER_VCPU_HOUR", "CPU", 4_000_000, true),
                entry("DISK_MU_PER_GIB_HOUR", "DISK", 100_000, true),
            ];
            let text = render(&prices, Some("CPU"));
            for label in ["RAM", "CPU", "DISK"] {
                assert!(text.contains(label), "missing {label} in:\n{text}");
            }
        }

        #[test]
        fn a_free_resource_is_labelled_rather_than_missing() {
            // A bar of height zero is indistinguishable from an absent feature, so a
            // price of zero says so in words.
            let prices = vec![entry("NET_MU_PER_GIB", "NET", 0, true)];
            let text = render(&prices, None);
            assert!(text.contains("NET"));
            assert!(text.contains("free"), "expected 'free' in:\n{text}");
        }

        #[test]
        fn a_price_dwarfed_by_its_neighbour_still_shows_its_amount() {
            // At 1/1000 of the tallest bar there is no bar left to draw, and that is
            // the honest picture. BarChart still prints the amount for any non-zero
            // value, so the price does not vanish -- which is precisely why a price of
            // exactly zero needs the `free` label instead.
            let prices = vec![
                entry("BUILD_MU", "BUILD", 10_000_000, true),
                entry("TUNNEL_OPEN_MU", "TUNNEL", 10_000, true),
            ];
            let text = render(&prices, None);
            assert!(text.contains("TUNNEL"), "missing label in:\n{text}");
            assert!(text.contains("0.00001"), "missing amount in:\n{text}");
        }

        #[test]
        fn the_table_carries_every_exact_figure() {
            // The chart is for proportion; this is the part that must never lose a
            // number, however small, and it shows both the raw MU and the display unit.
            let mut app = crate::app::App::default();
            app.money = Money::default();
            app.prices = crate::app::StatefulList::with_items(vec![
                entry("BUILD_MU", "BUILD", 10_000_000, false),
                entry("TUNNEL_OPEN_MU", "TUNNEL", 10_000, false),
            ]);

            let mut terminal = Terminal::new(TestBackend::new(70, 8)).unwrap();
            terminal
                .draw(|frame| super::super::draw_price_table(frame, &mut app, frame.size()))
                .unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect();

            assert!(text.contains("10000000"), "raw MU missing in:\n{text}");
            assert!(text.contains("0.01 ERG"), "display unit missing in:\n{text}");
            assert!(text.contains("10000"), "the small price is missing in:\n{text}");
            assert!(text.contains("0.00001 ERG"), "its amount is missing in:\n{text}");
        }

        #[test]
        fn the_other_group_is_not_drawn() {
            let prices = vec![
                entry("RAM_MU_PER_GIB_HOUR", "RAM", 1_000_000, true),
                entry("BUILD_MU", "BUILD", 10_000_000, false),
            ];
            let text = render(&prices, None);
            assert!(text.contains("RAM"));
            assert!(!text.contains("BUILD"));
        }

        #[test]
        fn per_arch_overrides_do_not_get_their_own_bar() {
            // A per-arch memory price is a variation on the memory price, not a fourth
            // resource. Charting both would draw "RAM" three times and read as three
            // times the memory revenue. The table is what carries the per-arch figures.
            let prices = vec![
                entry("RAM_MU_PER_GIB_HOUR", "RAM", 1_000_000, true),
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·amd64", "linux/amd64", 1_000_000, true),
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·arm64", "linux/arm64", 1_400_000, false),
            ];
            let text = render(&prices, None);
            assert!(text.contains("RAM"), "the node-wide bar is missing in:\n{text}");
            assert!(
                !text.contains("amd64") && !text.contains("arm64"),
                "a per-arch override was charted in:\n{text}"
            );
        }

        #[test]
        fn a_price_an_arch_only_inherits_says_so() {
            // "arm64 costs this" and "arm64 has been given its own rate" are different
            // facts, and the second is the one an edit creates. An operator has to be
            // able to tell which they are looking at before they nudge it -- otherwise
            // the page shows a per-arch pricing policy the node does not have.
            let mut app = crate::app::App::default();
            app.money = Money::default();
            app.prices = crate::app::StatefulList::with_items(vec![
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·amd64", "linux/amd64", 1_000_000, true),
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·arm64", "linux/arm64", 1_400_000, false),
            ]);

            let mut terminal = Terminal::new(TestBackend::new(80, 8)).unwrap();
            terminal
                .draw(|frame| super::super::draw_price_table(frame, &mut app, frame.size()))
                .unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect();

            assert!(text.contains("amd64"), "the arch row is missing in:\n{text}");
            assert!(
                text.contains("inherited"),
                "an inherited price is not marked in:\n{text}"
            );
            // The configured one must NOT be, or the marker means nothing.
            assert_eq!(
                text.matches("inherited").count(),
                1,
                "a configured per-arch price was marked inherited in:\n{text}"
            );
        }

        /// What the operator is actually deciding when they set a memory price.
        ///
        /// The node boots a guest larger than the service declared, so the guest
        /// kernel's footprint does not come out of the service's share -- and absorbs
        /// the difference deliberately, so a client never pays for the kernel underneath
        /// it. A memory price therefore earns less per GiB of host RAM than it says,
        /// by a different amount per architecture. These pin that the page says so.
        mod overhead_guidance {
            use crate::app::{App, GuestKernelReserve, Money, PriceEntry, StatefulList};
            use ratatui::{backend::TestBackend, Terminal};

            const GIB: u64 = 1024 * 1024 * 1024;

            fn app_with(arch: &'static str, mu: u64, reserve: GuestKernelReserve) -> App {
                let mut app = App::default();
                app.money = Money::default();
                app.guest_kernel_reserves = vec![(arch, reserve)];
                app.prices = StatefulList::with_items(vec![PriceEntry {
                    id: format!("{arch}/RAM_MU_PER_GIB_HOUR"),
                    key: "RAM_MU_PER_GIB_HOUR",
                    short: "RAM".to_string(),
                    per: "per GiB-hour",
                    recurring: true,
                    arch: Some(arch),
                    inherited: false,
                    mu,
                }]);
                app.prices.state.select(Some(0));
                app.prices.state_id = Some(format!("{arch}/RAM_MU_PER_GIB_HOUR"));
                app
            }

            #[test]
            fn the_reserve_matches_the_nodes_own_model() {
                // The same arithmetic as `limits.guest_kernel_reserve_bytes`: a fixed
                // part plus a share of the guest. If these drift, the page advises the
                // operator about an overhead the node does not actually apply.
                let reserve = GuestKernelReserve {
                    fixed_mib: 40,
                    ratio: 0.05,
                };
                // Rounded UP, like the node's `math.ceil`: a reserve short by a page is
                // still a guest that can be OOM-killed below its declared ceiling, and
                // rounding is the one place that is free to get right.
                assert_eq!(
                    reserve.bytes_for(GIB),
                    40 * 1024 * 1024 + (GIB as f64 * 0.05).ceil() as u64,
                    "the fixed and proportional parts must both apply"
                );
                assert!(
                    reserve.bytes_for(GIB) > 40 * 1024 * 1024 + GIB / 20,
                    "the proportional part must round up, not truncate"
                );
                assert_eq!(reserve.bytes_for(0), 0, "nothing is reserved for nothing");
            }

            #[test]
            fn a_costlier_arch_earns_the_node_less_at_the_same_price() {
                // This is the whole argument for per-arch pricing in one assertion: the
                // same number in config.yaml is not the same amount of money, because
                // the RAM the node has to commit to honour it differs per arch.
                let amd64 = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                let arm64 = app_with(
                    "linux/arm64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 32,
                        ratio: 0.05,
                    },
                );
                let (amd_effective, amd_multiplier) = amd64
                    .effective_memory_mu(amd64.prices.selected().unwrap())
                    .unwrap();
                let (arm_effective, _) = arm64
                    .effective_memory_mu(arm64.prices.selected().unwrap())
                    .unwrap();

                assert!(
                    amd_effective < arm_effective,
                    "amd64 reserves more, so the same price must earn less: \
                     amd64={amd_effective} arm64={arm_effective}"
                );
                // And never more than the price itself -- the node cannot earn more
                // than it charges by committing extra RAM.
                assert!(amd_effective < 1_000_000.0);
                assert!(amd_multiplier > 1.0);
            }

            #[test]
            fn the_suggested_price_recovers_the_overhead_exactly() {
                // The guidance has to be actionable, not just descriptive: a price set
                // to the suggestion must earn the target per GiB of host RAM committed.
                let reserve = GuestKernelReserve {
                    fixed_mib: 40,
                    ratio: 0.05,
                };
                let app = app_with("linux/amd64", 1_000_000, reserve);
                let suggested = app.suggested_memory_mu("linux/amd64", 1_000_000).unwrap();
                assert!(
                    suggested > 1_000_000,
                    "covering an overhead cannot cost less than not covering it"
                );

                let mut priced = app_with("linux/amd64", suggested, reserve);
                priced.prices.state.select(Some(0));
                let (effective, _) = priced
                    .effective_memory_mu(priced.prices.selected().unwrap())
                    .unwrap();
                assert!(
                    (effective - 1_000_000.0).abs() <= 1.0,
                    "a price set to the suggestion should earn the target, got {effective}"
                );
            }

            #[test]
            fn an_unpriced_arch_is_not_guessed_at() {
                // No measurement, no advice. Inventing an overhead figure for an
                // architecture nodo has never characterised would have the operator
                // price against a number nobody measured.
                let app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                assert!(app.reserve_for("linux/riscv64").is_none());
                assert!(app.suggested_memory_mu("linux/riscv64", 1_000_000).is_none());
            }

            #[test]
            fn the_node_wide_price_is_not_given_a_per_arch_figure() {
                // The scalar price applies to every arch, so there is no single
                // overhead to quote against it. Picking one arch's would be a
                // guess presented as a fact.
                let mut app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                app.prices = StatefulList::with_items(vec![PriceEntry {
                    id: "RAM_MU_PER_GIB_HOUR".to_string(),
                    key: "RAM_MU_PER_GIB_HOUR",
                    short: "RAM".to_string(),
                    per: "per GiB-hour",
                    recurring: true,
                    arch: None,
                    inherited: false,
                    mu: 1_000_000,
                }]);
                assert!(app
                    .effective_memory_mu(app.prices.items.first().unwrap())
                    .is_none());
            }

            #[test]
            fn a_non_memory_price_carries_no_overhead() {
                // Only memory has a per-arch cost to recover: the node hands a guest
                // the vCPUs and the image it asked for whatever arch it is.
                let mut app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                app.prices = StatefulList::with_items(vec![PriceEntry {
                    id: "linux/amd64/CPU_MU_PER_VCPU_HOUR".to_string(),
                    key: "CPU_MU_PER_VCPU_HOUR",
                    short: "CPU".to_string(),
                    per: "per vCPU-hour",
                    recurring: true,
                    arch: Some("linux/amd64"),
                    inherited: false,
                    mu: 4_000_000,
                }]);
                assert!(app
                    .effective_memory_mu(app.prices.items.first().unwrap())
                    .is_none());
            }

            #[test]
            fn the_money_card_states_what_the_node_keeps() {
                // The operator has to be able to read the consequence off the screen,
                // not derive it. A page that shows only the price teaches nothing about
                // the overhead it has to cover.
                let mut app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                let mut terminal = Terminal::new(TestBackend::new(46, 16)).unwrap();
                terminal
                    .draw(|frame| super::super::super::draw_money_card(frame, &app, frame.size()))
                    .unwrap();
                let text: String = terminal
                    .backend()
                    .buffer()
                    .content()
                    .iter()
                    .map(|cell| cell.symbol())
                    .collect();

                assert!(
                    text.contains("Guest kernel"),
                    "the overhead is not named in:\n{text}"
                );
                assert!(
                    text.contains("40"),
                    "the arch's own reserve is not shown in:\n{text}"
                );
                assert!(
                    text.contains("Node earns"),
                    "what the price actually earns is not shown in:\n{text}"
                );
                let _ = &mut app;
            }
        }
    }

    /// The instances page has to answer "is this instance using what it was given?".
    /// These tests pin the two halves of that: a live figure appears next to its
    /// allocation, and an instance we cannot see into says so instead of reading idle.
    mod instances {
        use super::super::{cpu_detail, cpu_load_color, net_detail, GOOD, MUTED, WARN};
        use crate::app::{Instance, InstanceUsage};

        fn instance(vcpus: Option<f64>, usage: InstanceUsage) -> Instance {
            Instance {
                id: "8f4e2c".to_string(),
                name: "worker".to_string(),
                ip: "10.0.0.7:4040".to_string(),
                service: "builder".to_string(),
                balance: "1000".to_string(),
                virtualizer: "ch".to_string(),
                memory_limit: 1 << 30,
                disk_limit: 10 << 30,
                vcpus,
                usage,
                location: "local".to_string(),
                father_id: String::new(),
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
            }
        }

        /// A raw percentage is ambiguous on its own: 180% is nearly idle on 4 vCPUs and
        /// impossible on 1. The card carries the allowance so the figure can be judged.
        #[test]
        fn the_cpu_line_states_the_allowance_the_percentage_is_measured_against() {
            let usage = InstanceUsage {
                cpu_percent: Some(182.4),
                ..InstanceUsage::default()
            };
            let detail = cpu_detail(&instance(Some(2.0), usage.clone()));
            assert!(detail.contains("182%"), "{detail}");
            assert!(detail.contains("200%"), "{detail}");
            assert!(detail.contains("2.00 vCPU"), "{detail}");

            // No quota recorded: say so rather than inventing a denominator.
            let unbounded = cpu_detail(&instance(None, usage));
            assert!(unbounded.contains("182%"), "{unbounded}");
            assert!(unbounded.contains("no vCPU quota"), "{unbounded}");
        }

        #[test]
        fn an_unreadable_cpu_reads_as_unknown_in_both_the_text_and_the_colour() {
            let blind = instance(Some(2.0), InstanceUsage::default());
            assert!(cpu_detail(&blind).contains('—'), "{}", cpu_detail(&blind));
            assert_eq!(cpu_load_color(&blind), MUTED);
        }

        /// The colour is the at-a-glance signal for oversubscription, so it has to turn
        /// on the allowance rather than on a flat percentage.
        #[test]
        fn the_cpu_colour_warns_only_near_the_instances_own_allowance() {
            let at = |percent: f64, vcpus: f64| {
                cpu_load_color(&instance(
                    Some(vcpus),
                    InstanceUsage {
                        cpu_percent: Some(percent),
                        ..InstanceUsage::default()
                    },
                ))
            };
            // 95% of one core is nearly saturated; the same figure on four cores is not.
            assert_eq!(at(95.0, 1.0), WARN);
            assert_eq!(at(95.0, 4.0), GOOD);
            assert_eq!(at(390.0, 4.0), WARN);
        }

        #[test]
        fn the_net_line_shows_rates_and_the_totals_they_accumulate() {
            let detail = net_detail(&instance(
                Some(1.0),
                InstanceUsage {
                    net_rx_bytes: Some(3 << 30),
                    net_tx_bytes: Some(512 << 20),
                    net_rx_rate: Some(1024.0 * 1024.0),
                    net_tx_rate: Some(2048.0),
                    ..InstanceUsage::default()
                },
            ));
            assert!(detail.contains("1.0 MiB/s"), "{detail}");
            assert!(detail.contains("2.0 KiB/s"), "{detail}");
            assert!(detail.contains("3.0 GiB"), "{detail}");
            assert!(detail.contains("512.0 MiB"), "{detail}");

            let blind = net_detail(&instance(Some(1.0), InstanceUsage::default()));
            assert!(!blind.contains('0'), "a missing tap is not silence: {blind}");
        }
    }

    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    /// The whole point of the page is the live column, so it has to survive the draw at
    /// the sizes an operator actually uses — including 80 columns, where the row is far
    /// wider than the terminal and ratatui has to truncate it.
    #[test]
    fn the_instances_table_shows_live_usage_beside_the_allocation() {
        let usage = crate::app::InstanceUsage {
            memory_current: Some(412 << 20),
            cpu_percent: Some(143.0),
            net_rx_rate: Some(1024.0 * 1024.0),
            net_tx_rate: Some(2048.0),
            ..crate::app::InstanceUsage::default()
        };
        let mut app = App::new();
        app.instances_grouped = false;
        app.instances.refresh(vec![Instance {
            id: "8f4e2c".to_string(),
            name: "worker".to_string(),
            ip: "10.0.0.7:4040".to_string(),
            service: "builder".to_string(),
            balance: "1000".to_string(),
            virtualizer: "ch".to_string(),
            memory_limit: 1 << 30,
            disk_limit: 10 << 30,
            vcpus: Some(2.0),
            usage,
            location: "local".to_string(),
            father_id: String::new(),
            // 1e6 MU/s → 6e7 MU/min, 3.6e9 MU/h; in the default ERG unit (1e9 MU) the
            // Burn/h column reads "3.6 ERG", exercising the rate through format_mu.
            mu_per_minute: Some(60_000_000.0),
            mu_per_hour: Some(3_600_000_000.0),
            consumption_samples: Some(12),
            consumption_age_secs: Some(45.0),
        }]);
        app.instances.state.select(Some(0));
        app.instances.state_id = Some("8f4e2c".to_string());

        let mut terminal = Terminal::new(TestBackend::new(160, 40)).unwrap();
        terminal
            .draw(|frame| draw_instances(frame, &mut app, frame.size()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        let text: String = buffer.content().iter().map(|cell| cell.symbol()).collect();

        assert!(text.contains("CPU%"), "missing the CPU column header");
        assert!(text.contains("143%"), "missing the live CPU reading");
        // Used and allocated in the same cell, so no division is left to the operator.
        assert!(text.contains("412M / 1.0G"), "missing RAM used/allocated");
        assert!(text.contains("1.0M / 2.0K"), "missing the net rates");
        // And the detail card explains what the 143% is a fraction of.
        assert!(text.contains("2.00 vCPU"), "missing the vCPU allowance");
        // The burn rate is a column of its own and a detail line: 3.6e9 MU/h renders
        // as "3.6 ERG" in the default unit, and the card states what it is built from.
        assert!(text.contains("Burn/h"), "missing the burn-rate column header");
        assert!(text.contains("3.6 ERG"), "missing the per-hour burn rate");
        assert!(text.contains("12 samples"), "missing the burn-rate sample count");
    }

    #[test]
    fn every_page_renders_at_common_terminal_sizes() {
        for (width, height) in [(80, 24), (140, 40)] {
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            let mut app = App::new();
            for index in 0..Page::ALL.len() {
                app.tabs.index = index;
                terminal.draw(|frame| render(&mut app, frame)).unwrap();
            }
        }
    }

    fn peer_with(contracts: Vec<crate::app::PeerContract>) -> Peer {
        Peer {
            id: "f3b61c2e-aaaa-bbbb-cccc-ddddeeeeffff".to_string(),
            uris: "10.0.0.4:8080".to_string(),
            // Raw MU, as the catalogue stores it; formatting happens at draw time.
            balance: "1000".to_string(),
            remote_client_id: "cli-9f2a".to_string(),
            proof_ids: Vec::new(),
            reputation_score: "7".to_string(),
            contracts,
        }
    }

    fn ergo_contract() -> crate::app::PeerContract {
        crate::app::PeerContract {
            ledger: "ergo".to_string(),
            contract_hash: "1c691f72deadbeef".to_string(),
            address: "0008cd0392aabbcc".to_string(),
            mu_per_unit: "1000000000".to_string(),
        }
    }

    fn rendered(lines: Vec<Line<'static>>) -> String {
        lines
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn peer_detail_prompts_when_nothing_is_selected() {
        let text = rendered(peer_detail_lines(&Money::default(), None, None, false));
        assert!(text.contains("Select a peer"));
    }

    #[test]
    fn peer_detail_shows_ledger_contract_address_and_price() {
        // The whole point of issue #231: these four facts were only reachable
        // through a raw sqlite query before.
        let text = rendered(peer_detail_lines(&Money::default(), Some(&peer_with(vec![ergo_contract()])), None, false));
        assert!(text.contains("Payment contracts (1)"));
        assert!(text.contains("ergo"));
        assert!(text.contains("1c691f72deadbeef"));
        assert!(text.contains("0008cd0392aabbcc"));
        // The rate reads as an equation: what one unit of that ledger buys in MU.
        assert!(text.contains("1 ERGO = 1000000000 MU"));
    }

    #[test]
    fn peer_detail_lists_every_contract_instance() {
        // A peer with several instances used to get silently truncated to one.
        let second = crate::app::PeerContract {
            ledger: "simulator".to_string(),
            contract_hash: "abc123".to_string(),
            address: "sim-address".to_string(),
            mu_per_unit: "500".to_string(),
        };
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer_with(vec![ergo_contract(), second])),
            None,
            false,
        ));
        assert!(text.contains("Payment contracts (2)"));
        assert!(text.contains("ergo"));
        assert!(text.contains("simulator"));
        assert!(text.contains("sim-address"));
    }

    #[test]
    fn peer_detail_says_so_when_no_contract_is_registered() {
        // Must stay distinguishable from "peer charges through something we
        // don't render", which is exactly what the old hardcoded lookup did.
        let text = rendered(peer_detail_lines(&Money::default(), Some(&peer_with(vec![])), None, false));
        assert!(text.contains("No payment contract registered"));
    }

    fn payment(status: &str, tx_id: &str, amount: &str) -> PaymentRow {
        PaymentRow {
            created_at: "2026-01-02 10:00:00".to_string(),
            amount: amount.to_string(),
            status: status.to_string(),
            tx_id: tx_id.to_string(),
            deposit_token: "token-1".to_string(),
        }
    }

    fn peer_history(peer_id: &str) -> PeerDetail {
        PeerDetail {
            peer_id: peer_id.to_string(),
            payments: vec![payment("unacknowledged", "abcdef0123456789", "2000")],
            events: vec![ReputationEvent {
                created_at: "2026-01-02 10:00:01".to_string(),
                amount: -100,
                reason: "payment_unacknowledged".to_string(),
                score_after: Some(-93),
            }],
        }
    }

    fn a_client() -> Client {
        Client {
            id: "client-1".to_string(),
            balance: "500".to_string(),
            last_usage: "1700000000".to_string(),
            unmetered: true,
        }
    }

    #[test]
    fn service_detail_shows_its_reputation_and_what_moved_it() {
        let service = Service {
            id: "service-1".to_string(),
            tag: "demo".to_string(),
            size_bytes: 1024,
        };
        let detail = ServiceDetail {
            service_id: service.id.clone(),
            score: Some(-90),
            events: vec![ReputationEvent {
                created_at: "2026-01-02 10:00:00".to_string(),
                amount: -100,
                reason: "instance_lost".to_string(),
                score_after: Some(-90),
            }],
        };

        let text = rendered(service_detail_lines(Some(&service), Some(&detail)));

        assert!(text.contains("Reputation"), "{text}");
        assert!(text.contains("-90"), "{text}");
        assert!(text.contains("instance lost"), "{text}");
    }

    #[test]
    fn a_service_never_scored_says_so_rather_than_reading_as_zero() {
        let service = Service {
            id: "service-1".to_string(),
            tag: "demo".to_string(),
            size_bytes: 1024,
        };
        let detail = ServiceDetail {
            service_id: service.id.clone(),
            score: None,
            events: Vec::new(),
        };

        let text = rendered(service_detail_lines(Some(&service), Some(&detail)));

        assert!(text.contains("not scored yet"), "{text}");
    }

    #[test]
    fn peer_detail_shows_what_we_paid_and_why_the_score_moved() {
        let peer = peer_with(vec![ergo_contract()]);
        let history = peer_history(&peer.id);
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer),
            Some(&history),
            false,
        ));

        assert!(text.contains("Payments made to this peer (1)"), "{text}");
        assert!(text.contains("unacknowledged"), "{text}");
        // The reason is stored with underscores and read as words.
        assert!(text.contains("payment unacknowledged"), "{text}");
        assert!(text.contains("-100"), "{text}");
        assert!(text.contains("→ -93"), "{text}");
    }

    #[test]
    fn a_peer_with_no_history_says_so_rather_than_showing_an_empty_card() {
        let peer = peer_with(vec![ergo_contract()]);
        let history = PeerDetail {
            peer_id: peer.id.clone(),
            payments: Vec::new(),
            events: Vec::new(),
        };
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer),
            Some(&history),
            false,
        ));

        assert!(text.contains("Nothing paid to this peer yet."), "{text}");
        assert!(text.contains("No reputation event recorded yet."), "{text}");
    }

    #[test]
    fn history_loaded_for_another_peer_is_never_shown_under_this_one() {
        // The selection can move between the load and the frame. A payment rendered
        // under the wrong peer is a lie about money, so the id has to match.
        let peer = peer_with(vec![ergo_contract()]);
        let history = peer_history("some-other-peer");
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer),
            Some(&history),
            false,
        ));

        assert!(!text.contains("Payments made to this peer"), "{text}");
        assert!(!text.contains("payment unacknowledged"), "{text}");
    }

    #[test]
    fn client_detail_shows_deposits_instances_and_payments() {
        let client = a_client();
        let detail = ClientDetail {
            client_id: client.id.clone(),
            deposits: vec![crate::app::DepositToken {
                id: "token-1".to_string(),
                status: "payed".to_string(),
                created_at: "2026-01-03 09:59:00".to_string(),
            }],
            instances: vec![crate::app::ClientInstance {
                id: "instance-1".to_string(),
                name: "demo".to_string(),
            }],
            payments: vec![payment("accepted", "", "750")],
        };

        let text = rendered(client_detail_lines(
            &Money::default(),
            Some(&client),
            Some(&detail),
            false,
        ));

        assert!(text.contains("Payments received (1)"), "{text}");
        assert!(text.contains("Deposit tokens (1)"), "{text}");
        assert!(text.contains("Instances started here (1)"), "{text}");
        assert!(text.contains("demo"), "{text}");
        // An incoming payment has no transaction id; the token identifies it.
        assert!(text.contains("token token-1"), "{text}");
        // And the reason its balance never moves.
        assert!(text.contains("Never charged"), "{text}");
    }

    #[test]
    fn clients_page_renders_the_client_detail_card() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Clients).unwrap();
        app.clients.items = vec![a_client()];
        app.clients.state.select(Some(0));
        app.client_detail = Some(ClientDetail {
            client_id: "client-1".to_string(),
            deposits: Vec::new(),
            instances: Vec::new(),
            payments: vec![payment("accepted", "", "750")],
        });
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();

        assert!(screen.contains("CLIENTS • 1 known"), "{screen}");
        assert!(screen.contains("SELECTED CLIENT"), "{screen}");
        assert!(screen.contains("Payments received (1)"), "{screen}");
        // Peers are a page of their own now, not a pane on this one.
        assert!(!screen.contains("Reputation proof"), "{screen}");
    }

    #[test]
    fn peers_page_renders_the_peer_detail_card() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Peers).unwrap();
        app.peers.items = vec![peer_with(vec![ergo_contract()])];
        app.peers.state.select(Some(0));
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("SELECTED PEER"));
        assert!(screen.contains("Payment contracts (1)"));
        // The table itself stays lean -- no contract columns were added to it.
        assert!(screen.contains("Reputation proof"));
        assert!(!screen.contains("Ledger  "));
    }

    #[test]
    fn a_short_terminal_keeps_both_the_peers_table_and_the_contracts() {
        // Regression: a fixed-height detail card pushed the peers table off an
        // 80x24 screen entirely, and clipped the contracts out of the card --
        // leaving it looking exactly like a peer with nothing registered.
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Peers).unwrap();
        let second = crate::app::PeerContract {
            ledger: "simulator".to_string(),
            contract_hash: "abc123def456".to_string(),
            address: "sim-address".to_string(),
            mu_per_unit: "500".to_string(),
        };
        app.peers.items = vec![peer_with(vec![ergo_contract(), second])];
        app.peers.state.select(Some(0));
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("PEERS • 1 connected"));
        assert!(screen.contains("Payment contracts (2)"));
        assert!(screen.contains("ergo"));
        assert!(screen.contains("simulator"));
    }

    #[test]
    fn grouped_instance_tree_renders() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Instances).unwrap();
        app.instances_grouped = true;
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("DEPENDENCY TREE"));
    }

    #[test]
    fn confirm_popup_shows_prompt_and_choices() {
        let backend = TestBackend::new(100, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.input_mode = InputMode::Confirm;
        app.input_title = "Delete service demo? (y/N)".to_string();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("Delete service demo?"));
        assert!(screen.contains("y confirms"));
    }

    #[test]
    fn details_popup_renders_inspect_output() {
        use crate::app::DetailsView;
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.input_mode = InputMode::Details;
        app.details = Some(DetailsView {
            title: "Service abcdef".to_string(),
            lines: vec!["tag: demo".to_string(), "size: 42 bytes".to_string()],
            scroll: 0,
        });
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("Service abcdef"));
        assert!(screen.contains("tag: demo"));
    }

    #[test]
    fn secret_editor_never_renders_plaintext() {
        let backend = TestBackend::new(100, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.input_mode = InputMode::EditConfig;
        app.edit_config_secret = true;
        app.input_title = "Edit wallet mnemonic".to_string();
        app.input = "these words must stay hidden".to_string();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(!screen.contains("these words"));
        assert!(screen.contains("••••"));
    }

    #[test]
    fn ergo_wallet_card_shows_reputation_proof() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.node_info.reputation_proof = "rep-proof-xyz".to_string();
        app.node_info.wallet_address = "9walletaddr".to_string();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Overview).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        // Proof now lives in the ERGO WALLET card, not the NETWORK summary card.
        assert!(screen.contains("ERGO WALLET"));
        assert!(screen.contains("rep-proof-xyz"));
        assert_eq!(screen.matches("rep-proof-xyz").count(), 1);
    }

    /// Who started an instance whose parent this node does not run itself. The tree
    /// nests instance-under-instance already; a client parent had nowhere to show, so
    /// an instance a client started read as one with no parent at all (issue #277).
    mod external_parents {
        use crate::app::{App, Client, Instance, InstanceUsage};
        use ratatui::{backend::TestBackend, Terminal};

        fn instance(id: &str, father: &str) -> Instance {
            Instance {
                id: id.to_string(),
                name: format!("inst-{id}"),
                ip: "10.0.0.7:4040".to_string(),
                service: "builder".to_string(),
                balance: "1000".to_string(),
                virtualizer: "ch".to_string(),
                memory_limit: 1 << 30,
                disk_limit: 10 << 30,
                vcpus: Some(1.0),
                usage: InstanceUsage::default(),
                location: "local".to_string(),
                father_id: father.to_string(),
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
            }
        }

        fn tree_text(instances: Vec<Instance>, clients: Vec<&str>) -> String {
            let mut app = App::new();
            app.instances_grouped = true;
            app.instances.refresh(instances);
            app.clients.refresh(
                clients
                    .into_iter()
                    .map(|id| Client {
                        id: id.to_string(),
                        balance: "0".to_string(),
                        last_usage: String::new(),
                        unmetered: false,
                    })
                    .collect(),
            );
            let mut terminal = Terminal::new(TestBackend::new(120, 12)).unwrap();
            terminal
                .draw(|frame| super::super::draw_instances_tree(frame, &app, frame.size()))
                .unwrap();
            terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect()
        }

        #[test]
        fn a_root_started_by_a_client_names_the_client() {
            let text = tree_text(vec![instance("aaa", "client-42")], vec!["client-42"]);
            assert!(text.contains("client client-42"), "{text}");
        }

        #[test]
        fn a_father_this_node_cannot_resolve_is_flagged_rather_than_hidden() {
            let text = tree_text(vec![instance("aaa", "ghost-7")], vec!["client-42"]);
            assert!(text.contains("ghost-7"), "{text}");
            assert!(text.contains("unknown"), "{text}");
        }

        /// The nesting already says who the parent is, so repeating it on the child
        /// would both duplicate it and mislabel a perfectly ordinary parent "unknown".
        #[test]
        fn a_child_nested_under_its_parent_carries_no_parent_label() {
            let text = tree_text(
                vec![instance("aaa", ""), instance("bbb", "aaa")],
                vec!["client-42"],
            );
            assert!(text.contains("inst-bbb"), "{text}");
            assert!(!text.contains("unknown"), "{text}");
        }
    }

    /// Our id inside a remote peer, which is what the other side's logs call us. The
    /// CLI has always printed it; the TUI never carried the column at all (issue #277).
    #[test]
    fn the_peer_card_shows_our_client_id_on_that_peer() {
        let peer = peer_with(vec![]);
        let lines = peer_detail_lines(&Money::default(), Some(&peer), None, false);
        let text: String = lines
            .iter()
            .flat_map(|line| line.spans.iter().map(|span| span.content.to_string()))
            .collect();
        assert!(text.contains("cli-9f2a"), "{text}");

        let unregistered = Peer {
            remote_client_id: String::new(),
            ..peer
        };
        let text: String = peer_detail_lines(&Money::default(), Some(&unregistered), None, false)
            .iter()
            .flat_map(|line| line.spans.iter().map(|span| span.content.to_string()))
            .collect();
        assert!(text.contains("not registered"), "{text}");
    }

    /// The mouse hit tests read off a *real* frame: the row arithmetic in `app.rs`
    /// retraces widget internals (border, header, the header's bottom margin, the tab
    /// padding and dividers), and nothing but a render can confirm it still matches.
    mod mouse_clicks {
        use super::*;

        fn app_with_peers() -> App {
            let mut app = App::new();
            app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Peers).unwrap();
            app.peers.refresh(
                ["peer-aaa", "peer-bbb", "peer-ccc"]
                    .into_iter()
                    .map(|id| Peer {
                        id: id.to_string(),
                        uris: "10.0.0.4:8080".to_string(),
                        balance: "1000".to_string(),
                        remote_client_id: String::new(),
                        proof_ids: Vec::new(),
                        reputation_score: "0".to_string(),
                        contracts: Vec::new(),
                    })
                    .collect(),
            );
            app
        }

        /// Renders a frame and returns the screen as one string per terminal row.
        fn draw(app: &mut App) -> Vec<String> {
            let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
            terminal.draw(|frame| render(app, frame)).unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..buffer.area.height)
                .map(|y| {
                    (0..buffer.area.width)
                        .map(|x| buffer.get(x, y).symbol())
                        .collect()
                })
                .collect()
        }

        #[test]
        fn a_click_lands_on_the_row_under_the_pointer() {
            let mut app = app_with_peers();
            let screen = draw(&mut app);
            // Whatever row the third peer actually printed on -- not where the
            // arithmetic thinks it should be.
            let y = screen
                .iter()
                .position(|row| row.contains("peer-ccc"))
                .expect("the peers table should have rendered every peer") as u16;

            app.click_at(4, y);
            assert_eq!(
                app.peers.selected().map(|peer| peer.id.as_str()),
                Some("peer-ccc"),
                "clicked row {y} of:\n{}",
                screen.join("\n")
            );
        }

        #[test]
        fn a_click_on_the_header_or_the_border_selects_nothing() {
            let mut app = app_with_peers();
            let screen = draw(&mut app);
            let header = screen
                .iter()
                .position(|row| row.contains("Peer ID"))
                .expect("header") as u16;
            app.click_at(4, header);
            assert!(app.peers.selected().is_none(), "header click selected a row");
        }

        #[test]
        fn a_click_on_a_tab_opens_that_page() {
            let mut app = app_with_peers();
            let screen = draw(&mut app);
            // The tab bar is the first rows of the frame; find CLIENTS in it.
            let (y, x) = screen
                .iter()
                .enumerate()
                .find_map(|(y, row)| {
                    // Byte offset -> terminal column: the dividers between tabs are
                    // multi-byte boxdrawing characters, so the two are not the same.
                    row.find("CLIENTS")
                        .map(|byte| (y as u16, row[..byte].chars().count() as u16))
                })
                .expect("the tab bar should list every page");

            app.click_at(x, y);
            assert_eq!(app.page(), Page::Clients);
        }
    }
}

#[cfg(test)]
mod pricing_preview {
    //! Prints the pricing page once so a change to the layout is visible in the test
    //! output. `cargo test -- --nocapture pricing_preview` renders it.
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    fn preview() {
        let mut app = App::new();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Pricing)
            .unwrap();
        app.prices.state.select(Some(1));
        app.prices.state_id = Some("CPU_MU_PER_VCPU_HOUR".to_string());

        let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer();
        for y in 0..buffer.area.height {
            let row: String = (0..buffer.area.width)
                .map(|x| buffer.get(x, y).symbol())
                .collect();
            println!("{row}");
        }
    }
}

#[cfg(test)]
mod config_tree {
    //! The Config page is a collapsible tree, so the properties that matter are
    //! that sections start collapsed, expanding reveals the nested (indented)
    //! scalars, and the `/` filter *expands and highlights* matches instead of
    //! hiding everything else.
    use super::*;
    use crate::app::ConfigPathSegment;
    use ratatui::buffer::Buffer;
    use ratatui::{backend::TestBackend, Terminal};

    fn entry(path: &str, value: &str, value_type: &str, secret: bool) -> ConfigEntry {
        ConfigEntry {
            path: path.to_string(),
            path_segments: path
                .split('.')
                .map(|key| ConfigPathSegment::Key(key.to_string()))
                .collect(),
            value: value.to_string(),
            edit_value: value.to_string(),
            value_type: value_type.to_string(),
            secret,
        }
    }

    fn render_buffer(app: &mut App) -> Buffer {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal
            .draw(|frame| draw_config(frame, app, frame.size()))
            .unwrap();
        terminal.backend().buffer().clone()
    }

    fn rows_of(buffer: &Buffer) -> Vec<String> {
        (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer.get(x, y).symbol())
                    .collect::<String>()
            })
            .collect()
    }

    /// Leading spaces of the row containing `needle`, after the left border — the
    /// tree indents each level by two, so a deeper node has a larger value.
    fn indent_of(rows: &[String], needle: &str) -> usize {
        let row = rows
            .iter()
            .find(|row| row.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle} in:\n{}", rows.join("\n")));
        let body = row.trim_start_matches('│');
        body.len() - body.trim_start().len()
    }

    #[test]
    fn sections_start_collapsed_and_expand_to_reveal_indented_nested_values() {
        let mut app = App::default();
        app.config_all = vec![
            entry("virtualizers.ch.MIN_MEM_MIB", "512", "number", false),
            entry("virtualizers.ch.MAX_MEM_MIB", "2048", "number", false),
            entry("network.GATEWAY_PORT", "5000", "number", false),
        ];

        // Collapsed: the top-level sections show, their nested scalars do not.
        let screen = rows_of(&render_buffer(&mut app)).join("\n");
        assert!(screen.contains("virtualizers"), "{screen}");
        assert!(screen.contains("network"), "{screen}");
        assert!(
            !screen.contains("MIN_MEM_MIB"),
            "a collapsed tree must hide nested leaves:\n{screen}"
        );

        // Expand the branch and its child mapping: the nested scalar now renders,
        // carrying its value, and is indented deeper than its parent section.
        app.config_tree_state.open(vec!["virtualizers".to_string()]);
        app.config_tree_state
            .open(vec!["virtualizers".to_string(), "ch".to_string()]);
        let rows = rows_of(&render_buffer(&mut app));
        let screen = rows.join("\n");
        assert!(screen.contains("MIN_MEM_MIB"), "{screen}");
        assert!(screen.contains("512"), "{screen}");
        assert!(
            indent_of(&rows, "MIN_MEM_MIB") > indent_of(&rows, "virtualizers"),
            "nested leaf should be indented deeper than its section:\n{screen}"
        );
    }

    #[test]
    fn filter_expands_and_highlights_the_match_without_hiding_context() {
        let mut app = App::default();
        app.config_all = vec![
            entry("virtualizers.ch.MIN_MEM_MIB", "512", "number", false),
            entry("virtualizers.ch.MAX_MEM_MIB", "2048", "number", false),
            entry("network.GATEWAY_PORT", "5000", "number", false),
        ];

        app.config_filter = "mem".to_string();
        app.apply_config_filter();

        let buffer = render_buffer(&mut app);
        let screen = rows_of(&buffer).join("\n");

        // Both matches' ancestors were opened, so the nested leaves are revealed...
        assert!(screen.contains("MIN_MEM_MIB"), "{screen}");
        assert!(screen.contains("MAX_MEM_MIB"), "{screen}");
        // ...and the unrelated section is still on screen (filter expands, not hides).
        assert!(
            screen.contains("network"),
            "filter must keep non-matching sections visible for context:\n{screen}"
        );
        // The title reports a match count, not a shrunken row count.
        assert!(screen.contains("2 match"), "{screen}");

        // A match that isn't the (selected) first one is highlighted with the
        // filter colour — the selected row carries the selection style instead,
        // which is why the assertion looks at a second, non-selected match.
        let has_highlight = (0..buffer.area.height).any(|y| {
            (0..buffer.area.width).any(|x| {
                let cell = buffer.get(x, y);
                cell.symbol() != " " && cell.style().bg == Some(WARN)
            })
        });
        assert!(has_highlight, "expected a non-selected filter match to be highlighted");
    }

    /// Prints the Config page once so a layout change is visible in the test
    /// output. `cargo test -- --nocapture config_tree::preview` renders it.
    #[test]
    fn preview() {
        let mut app = App::default();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Config)
            .unwrap();
        app.config_all = vec![
            entry("main.MAIN_DIR", "/var/lib/nodo", "string", false),
            entry("virtualizers.ch.MIN_MEM_MIB", "512", "number", false),
            entry("virtualizers.ch.MAX_MEM_MIB", "2048", "number", false),
            entry("ledgers.ergo.NODE_URL", "http://localhost:9053", "string", false),
            entry("ledgers.ergo.WALLET_MNEMONIC", "word word word", "string", true),
            entry("network.GATEWAY_PORT", "5000", "number", false),
            entry("core_services.packer", "Qm…packer", "string", false),
        ];
        app.config_tree_state.open(vec!["virtualizers".to_string()]);
        app.config_tree_state
            .open(vec!["virtualizers".to_string(), "ch".to_string()]);
        app.config_tree_state.open(vec!["ledgers".to_string()]);
        app.config_tree_state
            .open(vec!["ledgers".to_string(), "ergo".to_string()]);
        app.config_tree_state.select(vec![
            "virtualizers".to_string(),
            "ch".to_string(),
            "MIN_MEM_MIB".to_string(),
        ]);

        for row in rows_of(&render_buffer(&mut app)) {
            println!("{row}");
        }
    }

}

#[cfg(test)]
mod cell_preview {
    use super::{draw_cell, render};
    use crate::app::{App, InputMode, Page};
    use ratatui::{backend::TestBackend, Terminal};

    fn with_example_config() -> App {
        let mut app = App::new();
        let example = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../config.example.yaml");
        app.config_document =
            serde_yaml::from_str(&std::fs::read_to_string(example).unwrap()).ok();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Cell)
            .unwrap();
        app
    }

    fn dump(app: &mut App, width: u16, height: u16, title: &str) {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        println!("--- {title} ---");
        for row in 0..buffer.area.height {
            let line: String = (0..buffer.area.width)
                .map(|column| buffer.get(column, row).symbol())
                .collect();
            println!("{line}");
        }
    }

    /// Prints the two overlays for eyeballing:
    /// `cargo test cell_overlays -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn cell_overlays() {
        let mut app = with_example_config();
        app.open_profile_picker();
        dump(&mut app, 120, 32, "profile picker");

        let mut app = with_example_config();
        app.cell.profile = 1;
        app.submit_profile_selection();
        dump(&mut app, 120, 32, "profile diff");

        let mut app = with_example_config();
        app.cell.organelle = 4;
        app.cell.lever = 0;
        app.toggle_selected_lever();
        dump(&mut app, 120, 32, "one lever, several keys");
        assert_eq!(app.input_mode, InputMode::ConfirmWrites);
    }

    /// Prints the CELL page for eyeballing during development:
    /// `cargo test cell_preview -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn preview() {
        for (width, height) in [(120, 30), (80, 24)] {
            let mut app = App::new();
            // The shipped defaults, so the preview shows real values rather than a
            // page of "not set".
            let example = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../../config.example.yaml");
            app.config_document =
                serde_yaml::from_str(&std::fs::read_to_string(example).unwrap()).ok();
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_cell(frame, &mut app, frame.size()))
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            println!("--- {width}x{height} ---");
            for row in 0..buffer.area.height {
                let line: String = (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect();
                println!("{line}");
            }
        }
    }
}
