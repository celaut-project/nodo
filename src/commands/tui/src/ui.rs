use crate::app::{
    format_bytes, percent, shorten, App, EditKind, Instance, InputMode, Page, HISTORY_POINTS,
};
use ratatui::{prelude::*, widgets::*};
use std::collections::{HashMap, HashSet};

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
            nonempty(&app.node_info.error, "Balances refresh every 60 seconds"),
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
    let layout = Layout::vertical([Constraint::Min(8), Constraint::Length(6)]).split(area);
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
            Cell::from(
                instance
                    .memory_current
                    .map(format_bytes)
                    .unwrap_or_else(|| "—".to_string()),
            ),
            Cell::from(format_bytes(instance.memory_limit)),
            Cell::from(format_bytes(instance.disk_limit)),
            Cell::from(instance.gas.clone()),
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
            Constraint::Length(9),
            Constraint::Length(9),
            Constraint::Length(9),
            Constraint::Min(12),
        ],
    )
    .header(header_row(vec![
        "Name", "Location", "Instance", "Service", "IP", "VM", "RAM now", "RAM max", "Disk max",
        "Gas",
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

    let detail = if let Some(instance) = app.instances.selected() {
        vec![
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
            metric_line("Gas", instance.gas.clone()),
        ]
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
        build_tree_lines(root, 0, &inst_map, &children, &mut printed, &mut lines);
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
            format!("  gas {}", instance.gas),
            Style::default().fg(ACCENT),
        ),
    ]));

    if let Some(kids) = children.get(node_id) {
        for kid in kids {
            build_tree_lines(kid, depth + 1, inst_map, children, printed, lines);
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
    let split =
        Layout::vertical([Constraint::Percentage(62), Constraint::Percentage(38)]).split(area);
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
            Cell::from(peer.gas.clone()),
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
        "Our Gas",
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

    let clients = app.clients.items.iter().map(|client| {
        Row::new(vec![
            client.id.clone(),
            client.gas.clone(),
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
    .header(header_row(vec!["Client ID", "Gas", "Last usage"]))
    .block(section_block(
        format!(
            " CLIENTS • {} known • Tab changes focus ",
            app.clients.items.len()
        ),
        client_color,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    frame.render_stateful_widget(client_table, split[1], &mut app.clients.state);
}

fn draw_config(frame: &mut Frame, app: &mut App, area: Rect) {
    let filter = if app.config_filter.is_empty() {
        "all values".to_string()
    } else {
        format!("filter: {}", app.config_filter)
    };
    let rows = app.config.items.iter().map(|entry| {
        Row::new(vec![
            entry.path.clone(),
            entry.display_value(),
            entry.value_type.clone(),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Percentage(44),
            Constraint::Percentage(46),
            Constraint::Length(10),
        ],
    )
    .header(header_row(vec!["Configuration path", "Value", "Type"]))
    .block(section_block(
        format!(
            " CONFIGURATION • {} of {} values • {} ",
            app.config.items.len(),
            app.config_all.len(),
            filter
        ),
        Color::Yellow,
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    frame.render_stateful_widget(table, area, &mut app.config.state);
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
        Page::Config => "↑/↓ select  •  e edit  •  / filter  •  x clear filter  •  q quit",
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
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

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
