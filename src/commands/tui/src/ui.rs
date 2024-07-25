use crate::app::{App, CPU_TIMES, RAM_TIMES};
#[allow(clippy::wildcard_imports)]
use ratatui::{prelude::*, widgets::*};
use vec_to_array::vec_to_array;

/// Renders the user interface widgets.
pub fn render(app: &mut App, frame: &mut Frame) {
    let constraints = if app.show_cpu_ram {
        vec![
            Constraint::Fill(1),
            Constraint::Percentage(20),
            Constraint::Percentage(20),
            Constraint::Length(1),
        ]
    } else {
        vec![
            Constraint::Fill(1),
            Constraint::Percentage(40),
            Constraint::Length(1),
        ]
    };

    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(frame.size());

    draw_tabs(frame, app, layout[0]);

    if app.show_cpu_ram {
        draw_ram_usage(frame, app, layout[1]);
        draw_cpu_usage(frame, app, layout[2]);
    } else {
        let logs_text = app.logs.join("\n");
        let logs_paragraph = Paragraph::new(logs_text)
            .block(
                Block::bordered()
                    .title("Logs")
                    .title_alignment(Alignment::Left)
                    .border_type(BorderType::Thick),
            )
            .style(Style::default().fg(Color::White).bg(Color::Black));

        frame.render_widget(logs_paragraph, layout[1]);
    }

    let controls_text = get_controls_text(&app);
    let controls_paragraph = Paragraph::new(controls_text)
        .style(Style::default().fg(Color::White).bg(Color::Black))
        .alignment(Alignment::Center);

    frame.render_widget(
        controls_paragraph,
        if app.show_cpu_ram {
            layout[3]
        } else {
            layout[2]
        },
    );

    if app.connect_popup {
        let popup = Paragraph::new(app.connect_text.to_string())
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(Color::Yellow))
                    .title("Connect new peer")
                    .title_style(
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD),
                    )
                    .style(Style::default().bg(Color::Black).fg(Color::White)),
            )
            .alignment(Alignment::Left)
            .wrap(Wrap { trim: true });

        let area = centered_rect(10, 5, frame.size());
        frame.render_widget(popup, area);
    }
}

fn centered_rect(percent_x: u16, percent_y: u16, r: Rect) -> Rect {
    let popup_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints(
            [
                Constraint::Percentage((100 - percent_y) / 2),
                Constraint::Percentage(percent_y),
                Constraint::Percentage((100 - percent_y) / 2),
            ]
            .as_ref(),
        )
        .split(r);

    Layout::default()
        .direction(Direction::Horizontal)
        .constraints(
            [
                Constraint::Percentage((100 - percent_x) / 2),
                Constraint::Percentage(percent_x),
                Constraint::Percentage((100 - percent_x) / 2),
            ]
            .as_ref(),
        )
        .split(popup_layout[1])[1]
}

fn get_controls_text(app: &App) -> String {
    let is_row_selected = match app.tabs.index {
        0 => app.peers.state_id.is_some(),
        1 => app.clients.state_id.is_some(),
        2 => app.instances.state_id.is_some(),
        3 => app.services.state_id.is_some(),
        _ => false,
    };

    let mut control_text = String::new();

    control_text.push_str("Left/Right for menu  |  Up/Down for table rows");

    if app.show_cpu_ram {
        control_text.push_str("  |  Press 'i' to hide CPU and RAM sections");
    } else {
        control_text.push_str("  |  Press 'i' to show CPU and RAM sections");
    }

    if is_row_selected {
        match app.tabs.index {
            0 => control_text.push_str("  |  Press 'd' to delete the peer."),
            1 => control_text.push_str("  |  Press 'd' to delete the client."),
            2 => control_text.push_str("  |  Press 'd' to delete the instance."),
            3 => control_text.push_str(
                "  |  Press 'e' to execute an instance.  |  Press 'd' to delete the service.",
            ),
            _ => (),
        }
    }

    match app.tabs.index {
        0 => {
            if app.connect_popup {
                control_text.push_str("  |  Press 'esc' to close");
            } else {
                control_text.push_str("  |  Press 'c' to connect to a new peer");
            }
        }
        _ => (),
    }

    control_text
}

fn draw_tabs(frame: &mut Frame, app: &mut App, area: Rect) {
    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints(vec![Constraint::Length(3), Constraint::Fill(1)])
        .split(area);

    let tabs = app
        .tabs
        .titles
        .iter()
        .map(|t| text::Line::from(Span::styled(*t, Style::default().fg(Color::Green))))
        .collect::<Tabs>()
        .block(Block::default().borders(Borders::ALL).title(app.title))
        .highlight_style(Style::default().fg(Color::Yellow))
        .select(app.tabs.index);

    frame.render_widget(tabs, layout[0]);
    match app.tabs.index {
        0 => draw_peer_list(frame, app, layout[1]),
        1 => draw_client_list(frame, app, layout[1]),
        2 => draw_instance_list(frame, app, layout[1]),
        3 => draw_service_list(frame, app, layout[1]),
        _ => {}
    };
}

fn draw_peer_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.peers
                .items
                .iter()
                .map(|peer| {
                    Row::new(vec![
                        peer.id.clone(),
                        peer.uri.clone(),
                        peer.gas.to_string(),
                    ])
                })
                .collect::<Vec<Row>>(),
            [
                Constraint::Length(30),
                Constraint::Length(30),
                Constraint::Length(30),
            ],
        )
        .header(Row::new(vec![
            Cell::from("Id"),
            Cell::from("Main URI"),
            Cell::from("Gas on it"),
        ]))
        .block(
            Block::bordered()
                .title("PEERS")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .highlight_style(Style::default().add_modifier(Modifier::BOLD))
        .highlight_symbol("> ")
        .style(Style::default().fg(Color::Cyan).bg(Color::Black)),
        area,
        &mut app.peers.state,
    );
}

fn draw_client_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.clients
                .items
                .iter()
                .map(|client| Row::new(vec![client.id.clone()]))
                .collect::<Vec<Row>>(),
            [Constraint::Length(70)],
        )
        .header(Row::new(vec![Cell::from("Client Id")]))
        .block(
            Block::bordered()
                .title("CLIENTS")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .highlight_style(Style::default().add_modifier(Modifier::BOLD))
        .highlight_symbol("> ")
        .style(Style::default().fg(Color::LightGreen).bg(Color::Black)),
        area,
        &mut app.clients.state,
    );
}

fn draw_instance_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.instances
                .items
                .iter()
                .map(|instance| Row::new(vec![instance.id.clone()]))
                .collect::<Vec<Row>>(),
            [Constraint::Length(70)],
        )
        .header(Row::new(vec![Cell::from("Instance Id")]))
        .block(
            Block::bordered()
                .title("INSTANCES")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .highlight_style(Style::default().add_modifier(Modifier::BOLD))
        .highlight_symbol("> ")
        .style(Style::default().fg(Color::LightBlue).bg(Color::Black)),
        area,
        &mut app.instances.state,
    );
}

fn draw_service_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.services
                .items
                .iter()
                .map(|peer| Row::new(vec![peer.id.clone()]))
                .collect::<Vec<Row>>(),
            [Constraint::Length(70)],
        )
        .header(Row::new(vec![Cell::from("Id")]))
        .block(
            Block::bordered()
                .title("SERVICES")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .highlight_style(Style::default().add_modifier(Modifier::BOLD))
        .highlight_symbol("> ")
        .style(Style::default().fg(Color::LightMagenta).bg(Color::Black)),
        area,
        &mut app.services.state,
    );
}

fn draw_ram_usage(frame: &mut Frame, app: &mut App, area: Rect) {
    let ram_usage_arr: [u64; RAM_TIMES] = {
        if app.ram_usage.len() > RAM_TIMES {
            let ram_usage_vector =
                app.ram_usage.clone()[(app.ram_usage.len() - RAM_TIMES)..].to_vec();
            let ram_usage_arr: [u64; RAM_TIMES] = vec_to_array!(ram_usage_vector, u64, RAM_TIMES);
            ram_usage_arr
        } else {
            [0; RAM_TIMES]
        }
    };
    frame.render_widget(
        Sparkline::default()
            .data(&ram_usage_arr)
            .max(100)
            .direction(RenderDirection::LeftToRight)
            .style(Style::default().light_yellow().on_white())
            .block(
                Block::bordered()
                    .title("Ram usage")
                    .title_alignment(Alignment::Left)
                    .border_type(BorderType::Thick),
            )
            .style(Style::default().fg(Color::Cyan).bg(Color::Black)),
        area,
    );
}

fn draw_cpu_usage(frame: &mut Frame, app: &mut App, area: Rect) {
    let cpu_usage_arr: [u64; CPU_TIMES] = {
        if app.cpu_usage.len() > CPU_TIMES {
            let cpu_usage_vector =
                app.cpu_usage.clone()[(app.cpu_usage.len() - CPU_TIMES)..].to_vec();
            let cpu_usage_arr: [u64; CPU_TIMES] = vec_to_array!(cpu_usage_vector, u64, CPU_TIMES);
            cpu_usage_arr
        } else {
            [0; CPU_TIMES]
        }
    };
    frame.render_widget(
        Sparkline::default()
            .block(
                Block::bordered()
                    .title("CPU usage")
                    .title_alignment(Alignment::Left)
                    .border_type(BorderType::Thick),
            )
            .data(&cpu_usage_arr)
            .max(100)
            .direction(RenderDirection::LeftToRight)
            .style(Style::default().fg(Color::Yellow).bg(Color::Black)),
        area,
    );
}
