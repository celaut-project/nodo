#[allow(clippy::wildcard_imports)]
use ratatui::{
    prelude::*,
    widgets::{*},
};

use vec_to_array::vec_to_array;
use crate::app::{CPU_TIMES, RAM_TIMES};

use crate::app::App;

/// Renders the user interface widgets.
pub fn render(app: &mut App, frame: &mut Frame) {
    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints(vec![
            Constraint::Percentage(50),
            Constraint::Percentage(23),
            Constraint::Percentage(23),
            Constraint::Percentage(4)
        ])
        .split(frame.size());

    draw_tabs(frame, app, layout[0]);
    draw_ram_usage(frame, app, layout[1]);
    draw_cpu_usage(frame, app, layout[2]);
}

fn draw_tabs(frame: &mut Frame, app: &mut App, area: Rect) {
    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints(vec![
            Constraint::Percentage(15),
            Constraint::Percentage(95),
        ])
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
        2 => draw_container_list(frame, app, layout[1]),
        3 => draw_service_list(frame, app, layout[1]),
        _ => {}
    };
}

fn draw_peer_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.peers.items.iter().map(|peer| {
                Row::new(vec![peer.id.clone(), peer.uri.clone(), peer.gas.to_string()])
            }).collect::<Vec<Row>>(),
            [
                Constraint::Length(30),
                Constraint::Length(30),
                Constraint::Length(30),
            ]
        )
        .header(
            Row::new(vec![
                Cell::from("Id"),
                Cell::from("Main URI"),
                Cell::from("Gas on it")
            ])
        )
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
        &mut app.peers.state
    );
}

fn draw_client_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.clients.items.iter().map(|client| {
                Row::new(vec![client.clone()])
            }).collect::<Vec<Row>>(),
            [
                Constraint::Length(70),
            ]
        )
        .header(
            Row::new(vec![Cell::from("Client Id")])
        )
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
        &mut app.clients.state
    );
}

fn draw_container_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.containers.items.iter().map(|container| {
                Row::new(vec![container.clone()])
            }).collect::<Vec<Row>>(),
            [
                Constraint::Length(70),
            ]
        )
        .header(
            Row::new(vec![Cell::from("Container Id")])
        )
        .block(
            Block::bordered()
                .title("CONTAINERS")
                .title_alignment(Alignment::Left)
                .border_type(BorderType::Thick),
        )
        .highlight_style(Style::default().add_modifier(Modifier::BOLD))
        .highlight_symbol("> ")
        .style(Style::default().fg(Color::LightBlue).bg(Color::Black)),
        area,
        &mut app.containers.state
    );
}

fn draw_service_list(frame: &mut Frame, app: &mut App, area: Rect) {
    frame.render_stateful_widget(
        Table::new(
            app.services.items.iter().map(|peer| {
                Row::new(vec![peer.id.clone()])
            }).collect::<Vec<Row>>(),
            [
                Constraint::Length(70),
            ]
        )
        .header(
            Row::new(vec![
                Cell::from("Id"),
            ])
        )
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
        &mut app.services.state
    );
}

fn draw_ram_usage(frame: &mut Frame, app: &mut App, area: Rect) {
    let ram_usage_arr: [u64; RAM_TIMES]  = {
        if app.ram_usage.len() > RAM_TIMES {
            let ram_usage_vector = app.ram_usage.clone()[(app.ram_usage.len() - RAM_TIMES)..].to_vec();
            let ram_usage_arr: [u64; RAM_TIMES]  = vec_to_array!(ram_usage_vector, u64, RAM_TIMES);
            ram_usage_arr
        }else {
            [0; RAM_TIMES]
        }
    };
    frame.render_widget(
        Sparkline::default()
            .block(Block::default().title("Sparkline").borders(Borders::ALL))
            .data(&ram_usage_arr)
            .max(100)
            .direction(RenderDirection::LeftToRight)
            .style(Style::default().light_yellow().on_white())
        .block(
            Block::bordered()
                .title("Ram usage")
                .title_alignment(Alignment::Center)
                .border_type(BorderType::Thick),
        )
        .style(Style::default().fg(Color::Cyan).bg(Color::Black)),
        area
    );
}

fn draw_cpu_usage(frame: &mut Frame, app: &mut App, area: Rect) {
    let cpu_usage_arr: [u64; CPU_TIMES] = {
        if app.cpu_usage.len() > CPU_TIMES {
            let cpu_usage_vector = app.cpu_usage.clone()[(app.cpu_usage.len() - CPU_TIMES)..].to_vec();
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
                .title_alignment(Alignment::Center)
                .border_type(BorderType::Thick),
            )
            .data(&cpu_usage_arr)
            .max(100)
            .direction(RenderDirection::LeftToRight)
            .style(Style::default().fg(Color::Yellow).bg(Color::Black)),
        area
    );
}
