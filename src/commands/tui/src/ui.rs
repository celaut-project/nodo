use crate::app::{App, CPU_TIMES, LOG_FILE, RAM_TIMES};
#[allow(clippy::wildcard_imports)]
use ratatui::{prelude::*, widgets::*};
use std::cmp;
use std::fs::File;
use std::io::{self, BufRead};
use vec_to_array::vec_to_array;

/// Renders the user interface widgets.
pub fn render(app: &mut App, frame: &mut Frame) {
    let view_constraints =
        get_view_constraints(app.mode_view_index.state_id.as_deref().unwrap_or(""));
    let mut constraints = vec![Constraint::Fill(1)];
    constraints.extend(view_constraints.iter());
    constraints.push(Constraint::Length(1));

    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(frame.size());

    let mut n = 0;
    draw_tabs(frame, app, layout[n]);

    for (i, constraint) in view_constraints.iter().enumerate() {
        n += 1;
        let current_index = (i + app.block_view_index.state.selected().unwrap_or(0))
            % app.block_view_index.items.len();
        match app.block_view_index.items[current_index].0.as_str() {
            "ram-usage" => draw_ram_usage(frame, app, layout[n]),
            "cpu-usage" => draw_cpu_usage(frame, app, layout[n]),
            "tui-logs" => draw_tui_logs(frame, app, layout[n]),
            "logs" => draw_logs(frame, app, layout[n]),
            _ => {}
        }
    }

    let controls_text = get_controls_text(&app);
    let controls_paragraph = Paragraph::new(controls_text)
        .style(Style::default().fg(Color::White).bg(Color::Black))
        .alignment(Alignment::Center);

    frame.render_widget(controls_paragraph, layout[n + 1]);

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

        let min_percent_x: u16 = cmp::min((frame.size().width as f64) as u16, 10);
        let content_width_ratio = cmp::max(app.connect_text.len() as u16, min_percent_x);
        let area = centered_rect(content_width_ratio, 5, frame.size());
        frame.render_widget(popup, area);
    }
}

fn get_view_constraints(mode: &str) -> Vec<Constraint> {
    match mode {
        "" => vec![],
        "10" => vec![Constraint::Percentage(25)],
        "10-10" => vec![Constraint::Percentage(25), Constraint::Percentage(25)],
        "10-10-10" => vec![
            Constraint::Percentage(25),
            Constraint::Percentage(25),
            Constraint::Percentage(25),
        ],
        "20-10" => vec![Constraint::Percentage(50), Constraint::Percentage(25)],
        "30" => vec![Constraint::Percentage(75)],
        _ => vec![],
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
        4 => app.envs.state_id.is_some(),
        _ => false,
    };

    let mut control_text = String::new();

    control_text.push_str("Left/Right for menu  |  Up/Down for table rows");
    control_text.push_str("  |  Press 'o' and 'p' to rotate the block views sections");
    control_text.push_str("  |  Press 'm' to change the block view layout");

    if is_row_selected {
        match app.tabs.index {
            0 => control_text.push_str("  |  Press 'd' to delete the peer."),
            1 => control_text.push_str("  |  Press 'd' to delete the client."),
            2 => control_text.push_str("  |  Press 'd' to delete the instance."),
            3 => control_text.push_str(
                "  |  Press 'e' to execute an instance.  |  Press 'd' to delete the service.",
            ),
            4 => control_text.push_str("  |  Press 'e' to edit."),
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
        4 => draw_env_list(frame, app, layout[1]),
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

fn draw_env_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.envs
                .items
                .iter()
                .map(|env| Row::new(vec![env.id.clone(), env.value.clone(), env.info.clone()]))
                .collect::<Vec<Row>>(),
            [
                Constraint::Length(50),
                Constraint::Length(70),
                Constraint::Length(70),
            ],
        )
        .header(Row::new(vec![
            Cell::from("Id"),
            Cell::from("Value"),
            Cell::from("Info"),
        ]))
        .block(
            Block::bordered()
                .title("ENVS")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .highlight_style(Style::default().add_modifier(Modifier::BOLD))
        .highlight_symbol("> ")
        .style(Style::default().fg(Color::White).bg(Color::Black)),
        area,
        &mut app.envs.state,
    );
}

fn read_last_lines(filename: &str, line_count: usize) -> io::Result<Vec<String>> {
    let file = File::open(filename)?;
    let reader = io::BufReader::new(file);

    let lines: Vec<String> = reader.lines().collect::<Result<_, _>>()?;

    let lines_to_show = if lines.len() > line_count {
        lines.into_iter().rev().take(line_count).rev().collect()
    } else {
        lines
    };

    Ok(lines_to_show)
}

fn draw_logs(frame: &mut Frame, app: &mut App, area: Rect) {
    let log_lines = area.height as usize;
    let logs_text = match read_last_lines(LOG_FILE, log_lines) {
        Ok(lines) => lines.join("\n"),
        Err(_) => "Unable to read log file.".to_string(),
    };

    let logs_paragraph = Paragraph::new(logs_text)
        .block(
            Block::bordered()
                .title("Logs")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .style(Style::default().fg(Color::White).bg(Color::Black));

    frame.render_widget(logs_paragraph, area);
}

fn draw_tui_logs(frame: &mut Frame, app: &mut App, area: Rect) {
    let log_lines = area.height as usize;
    let logs_text: String = app
        .logs
        .iter()
        .rev()
        .take(log_lines)
        .rev()
        .cloned()
        .collect::<Vec<String>>()
        .join("\n");
    let logs_paragraph = Paragraph::new(logs_text)
        .block(
            Block::bordered()
                .title("Tui logs")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .style(Style::default().fg(Color::White).bg(Color::Black));

    frame.render_widget(logs_paragraph, area);
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
