use ratatui::{
    layout::{Alignment, Constraint, Rect},
    style::{Color, Style, Stylize},
    widgets::{Block, BorderType, Borders, Cell, RenderDirection, Row, Sparkline, Table, TableState},
    Frame,
};
use vec_to_array::vec_to_array;
use crate::app::RAM_TIMES;

use crate::app::App;

/// Renders the user interface widgets.
pub fn render(app: &mut App, frame: &mut Frame) {
    // This is where you add new widgets.
    // See the following resources:
    // - https://docs.rs/ratatui/latest/ratatui/widgets/index.html
    // - https://github.com/ratatui-org/ratatui/tree/master/

    let mut table_state = TableState::default();
    frame.render_stateful_widget(
        Table::new(
            app.peers.iter().map(|peer| {
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
                .title("Available peer list")
                .title_alignment(Alignment::Center)
                .border_type(BorderType::Thick),
        )
        .style(Style::default().fg(Color::Cyan).bg(Color::Black)),
        Rect::new(0, 0, frame.size().width, frame.size().height*3/4),
        &mut table_state
    );

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
        Rect::new(0, frame.size().height*3/4, frame.size().width, frame.size().height/4)
    );
}
