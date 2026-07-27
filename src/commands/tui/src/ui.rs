use crate::app::{format_bytes, percent, shorten, App, InputMode, Page, HISTORY_POINTS};
use ratatui::{prelude::*, widgets::*};

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

    if app.input_mode != InputMode::Normal {
        draw_input_popup(frame, app);
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
            metric_line(
                "Proof",
                shorten(nonempty(&app.node_info.reputation_proof, "—"), 18),
            ),
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
    let sender = nonempty(&app.node_info.sender_address, "not configured");
    let receiver = nonempty(&app.node_info.receiver_address, "not configured");
    let lines = vec![
        Line::from(vec![
            Span::styled("Total  ", Style::default().fg(MUTED)),
            Span::styled(
                format_balance(app.node_info.total_balance),
                Style::default().fg(Color::LightGreen).bold(),
            ),
        ]),
        Line::from(format!(
            "Send   {}  {}",
            shorten(sender, 28),
            format_balance(app.node_info.sender_balance)
        )),
        Line::from(format!(
            "Recv   {}  {}",
            shorten(receiver, 28),
            format_balance(app.node_info.receiver_balance)
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
            Cell::from(peer.client_gas.clone()),
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
            Constraint::Length(13),
            Constraint::Length(7),
            Constraint::Min(20),
        ],
    )
    .header(header_row(vec![
        "Peer ID",
        "Endpoints",
        "Peer Gas",
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
        Page::Instances => "↑/↓ select  •  ←/→ page  •  r refresh  •  q quit",
        Page::Services => "↑/↓ select  •  e execute  •  ←/→ page  •  q quit",
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

fn draw_input_popup(frame: &mut Frame, app: &App) {
    let area = centered_rect(72, 7, frame.size());
    frame.render_widget(Clear, area);
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
    let content = vec![
        Line::from(display),
        Line::from(Span::styled(
            format!("Enter saves • Esc cancels • Ctrl+U clears{secret_hint}"),
            Style::default().fg(MUTED),
        )),
    ];
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
}
