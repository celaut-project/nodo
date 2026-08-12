use crate::app::{
    format_bytes, format_bytes_compact, format_rate_compact, percent, segment_token, shorten, App,
    ConfigEntry, EditKind, InputMode, Instance, Money, Page, Peer, PriceEntry, HISTORY_POINTS,
};
use ratatui::{prelude::*, widgets::*};
use std::collections::{HashMap, HashSet};
use tui_tree_widget::{Tree, TreeItem};

const ACCENT: Color = Color::Cyan;
const MUTED: Color = Color::DarkGray;
const GOOD: Color = Color::Green;
const WARN: Color = Color::Yellow;

pub fn render(app: &mut App, frame: &mut Frame) {
    let layout = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(8),
        Constraint::Length(2),
    ])
    .split(frame.size());

    draw_tabs(frame, app, layout[0]);
    match app.page() {
        Page::Overview => draw_overview(frame, app, layout[1]),
        Page::Instances => draw_instances(frame, app, layout[1]),
        Page::Services => draw_services(frame, app, layout[1]),
        Page::Network => draw_network(frame, app, layout[1]),
        Page::Pricing => draw_pricing(frame, app, layout[1]),
        Page::Config => draw_config(frame, app, layout[1]),
        Page::Logs => draw_logs(frame, app, layout[1]),
    }
    draw_footer(frame, app, layout[2]);

    match app.input_mode {
        InputMode::Normal => {}
        InputMode::Confirm => draw_confirm_popup(frame, app),
        InputMode::Details => draw_details_popup(frame, app),
        InputMode::Connect | InputMode::EditConfig | InputMode::FilterConfig => {
            draw_input_popup(frame, app)
        }
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
    // 12 = 10 detail lines + the block's two border rows. The card carries the figures
    // the row has no width for: the disk allocation, the vCPU allowance the CPU% is
    // measured against, and the cumulative disk/net totals.
    let layout = Layout::vertical([Constraint::Min(8), Constraint::Length(12)]).split(area);
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

    let mut lines: Vec<Line> = Vec::new();
    let mut printed: HashSet<&str> = HashSet::new();
    for root in &roots {
        build_tree_lines(&app.money, root, 0, &inst_map, &children, &mut printed, &mut lines);
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

fn build_tree_lines<'a>(
    money: &Money,
    node_id: &'a str,
    depth: usize,
    inst_map: &HashMap<&'a str, &'a Instance>,
    children: &HashMap<&'a str, Vec<&'a str>>,
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
    lines.push(Line::from(vec![
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
    ]));

    if let Some(kids) = children.get(node_id) {
        for kid in kids {
            build_tree_lines(money, kid, depth + 1, inst_map, children, printed, lines);
        }
    }
}

fn draw_services(frame: &mut Frame, app: &mut App, area: Rect) {
    let layout = Layout::vertical([Constraint::Min(8), Constraint::Length(5)]).split(area);
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
    frame.render_stateful_widget(table, layout[0], &mut app.services.state);

    let detail = app
        .services
        .selected()
        .map(|service| {
            format!(
                "{}\n{} • {}",
                service.id,
                nonempty(&service.tag, "untagged"),
                format_bytes(service.size_bytes)
            )
        })
        .unwrap_or_else(|| "Select a service, then press e to execute it.".to_string());
    frame.render_widget(
        Paragraph::new(detail)
            .block(section_block(" SELECTED SERVICE ", Color::LightMagenta))
            .style(Style::default().fg(Color::White)),
        layout[1],
    );
}

fn draw_network(frame: &mut Frame, app: &mut App, area: Rect) {
    // The peer detail card sizes itself to the selected peer's contract count,
    // so a peer with several instances stays readable without the table having
    // to carry any of it (issue #231). It yields first when the terminal is
    // short: a card that squeezed the peers table off-screen would leave no way
    // to pick the peer it is describing.
    const MIN_PEERS_HEIGHT: u16 = 7;
    const MIN_CLIENTS_HEIGHT: u16 = 5;
    let available = area
        .height
        .saturating_sub(MIN_PEERS_HEIGHT + MIN_CLIENTS_HEIGHT);
    let selected = app.peers.selected();
    // Prefer the roomy breakdown, but fall back to one line per contract rather
    // than let a short terminal clip the contracts away silently -- an empty
    // card reads as "no contract registered", the exact confusion #231 is about.
    let full = peer_detail_lines(&app.money, selected, false);
    let detail = if full.len() as u16 + 2 <= available {
        full
    } else {
        peer_detail_lines(&app.money, selected, true)
    };
    let detail_height = (detail.len() as u16 + 2).min(available);
    let split = Layout::vertical([
        Constraint::Min(MIN_PEERS_HEIGHT),
        Constraint::Length(detail_height),
        Constraint::Min(MIN_CLIENTS_HEIGHT),
    ])
    .split(area);
    let peer_color = if app.network_focus == 0 {
        ACCENT
    } else {
        MUTED
    };
    let client_color = if app.network_focus == 1 {
        ACCENT
    } else {
        MUTED
    };

    let peers = app.peers.items.iter().map(|peer| {
        Row::new(vec![
            Cell::from(peer.id.clone()),
            Cell::from(peer.uris.clone()),
            Cell::from(app.money.format_raw(&peer.balance)),
            Cell::from(peer.reputation_score.clone()).style(Style::default().fg(GOOD).bold()),
            Cell::from(peer.reputation.clone()),
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
        "Reputation proof",
    ]))
    .block(section_block(
        format!(" PEERS • {} connected ", app.peers.items.len()),
        peer_color,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    frame.render_stateful_widget(peer_table, split[0], &mut app.peers.state);

    draw_card(frame, split[1], "SELECTED PEER", detail, ACCENT);

    let clients = app.clients.items.iter().map(|client| {
        Row::new(vec![
            client.id.clone(),
            app.money.format_raw(&client.balance),
            client.last_usage.clone(),
        ])
    });
    let client_table = Table::new(
        clients,
        [
            Constraint::Min(45),
            Constraint::Length(24),
            Constraint::Length(20),
        ],
    )
    .header(header_row(vec!["Client ID", "Balance", "Last usage"]))
    .block(section_block(
        format!(
            " CLIENTS • {} known • Tab changes focus ",
            app.clients.items.len()
        ),
        client_color,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    frame.render_stateful_widget(client_table, split[2], &mut app.clients.state);
}

/// Full breakdown of the peer highlighted in the peers table: identity, our balance
/// with it, reputation, and every payment contract it has registered — ledger,
/// contract, payout address and per-unit rate per instance. Before this the only
/// way to get at any of it was a raw sqlite query (issue #231).
/// `compact` collapses each contract onto a single line and drops the fields the
/// peers table already shows verbatim, for terminals too short for the full card.
fn peer_detail_lines(money: &Money, peer: Option<&Peer>, compact: bool) -> Vec<Line<'static>> {
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
        lines.push(metric_line(
            "Reputation",
            format!(
                "{}  •  proof {}",
                peer.reputation_score,
                nonempty(&peer.reputation, "none")
            ),
        ));
        lines.push(Line::from(""));
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
    let group: Vec<&PriceEntry> = prices
        .iter()
        .filter(|entry| entry.recurring == recurring)
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
            format!("pricing.{}", entry.key),
            Style::default().fg(Color::White).bold(),
        )));
        lines.push(metric_line("Price", format!("{} MU {}", entry.mu, entry.per)));
        lines.push(metric_line("That is", money.format_mu(entry.mu)));
        lines.push(metric_line(
            "At max load",
            money.format_mu(entry.mu.saturating_mul(app.scarcity.max_multiplier)),
        ));
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
    let rows = app.prices.items.iter().map(|entry| {
        Row::new(vec![
            entry.short.to_string(),
            entry.mu.to_string(),
            money.format_mu(entry.mu),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(8),
            Constraint::Percentage(45),
            Constraint::Percentage(45),
        ],
    )
    .header(header_row(vec!["Price", "MU", money.symbol.as_str()]))
    .block(section_block(" PRICES • +/- adjust, e exact ", Color::Yellow))
    .highlight_style(selected_style())
    .highlight_symbol("> ");
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
        Page::Overview => "←/→ page  •  r refresh  •  q quit",
        Page::Instances => {
            "↑/↓ select  •  g tree/flat  •  k kill  •  ←/→ page  •  r refresh  •  q quit"
        }
        Page::Services => "↑/↓ select  •  e execute  •  i details  •  d delete  •  ←/→ page  •  q quit",
        Page::Network => "↑/↓ select  •  Tab peers/clients  •  +/- reputation  •  c connect  •  q quit",
        Page::Pricing => {
            "↑/↓ select  •  +/- adjust 10%  •  e exact value  •  ←/→ page  •  r refresh  •  q quit"
        }
        Page::Config => {
            "↑/↓ select  •  ⏎ expand/collapse  •  e edit  •  / filter  •  x clear  •  q quit"
        }
        Page::Logs => "←/→ page  •  r refresh  •  q quit",
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
    let area = centered_rect(80, frame.size().height.saturating_sub(4).max(8), frame.size());
    frame.render_widget(Clear, area);
    let (title, text, scroll) = match &app.details {
        Some(details) => (
            details.title.clone(),
            details.lines.join("\n"),
            details.scroll as u16,
        ),
        None => (String::new(), String::new(), 0),
    };
    let popup = Paragraph::new(text)
        .scroll((scroll, 0))
        .wrap(Wrap { trim: false })
        .block(
            Block::bordered()
                .title(Span::styled(
                    format!(" {title} • ↑/↓ scroll • Esc close "),
                    Style::default().fg(ACCENT).bold(),
                ))
                .border_style(Style::default().fg(ACCENT)),
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

    /// The bars are an editor, so what matters is that every price is actually on
    /// screen -- including a free one and one small enough to round to no bar at all,
    /// which is exactly when a chart quietly stops telling the truth.
    mod pricing {
        use super::super::{draw_price_bars, PriceChart, ACCENT};
        use crate::app::{Money, PriceEntry};
        use ratatui::{backend::TestBackend, Terminal};

        fn entry(key: &'static str, short: &'static str, mu: u64, recurring: bool) -> PriceEntry {
            PriceEntry {
                key,
                short,
                per: "per unit",
                recurring,
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
            reputation: String::new(),
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
        let text = rendered(peer_detail_lines(&Money::default(), None, false));
        assert!(text.contains("Select a peer"));
    }

    #[test]
    fn peer_detail_shows_ledger_contract_address_and_price() {
        // The whole point of issue #231: these four facts were only reachable
        // through a raw sqlite query before.
        let text = rendered(peer_detail_lines(&Money::default(), Some(&peer_with(vec![ergo_contract()])), false));
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
        let text = rendered(peer_detail_lines(&Money::default(), Some(&peer_with(vec![])), false));
        assert!(text.contains("No payment contract registered"));
    }

    #[test]
    fn network_page_renders_the_peer_detail_card() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Network).unwrap();
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
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Network).unwrap();
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
        assert!(screen.contains("CLIENTS"));
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
