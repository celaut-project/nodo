use std::fmt::format;

use ratatui::{
    layout::{Alignment, Constraint},
    style::{Color, Style},
    widgets::{Block, BorderType, Cell, Row, Table},
    Frame,
};

use crate::app::App;

use rusqlite::{Connection, Result};

const DATABASE_FILE: &str = "../../../../storage/database.sqlite";

struct Peer {
    id: String,
    uri: String,
    gas: u8
}

fn get_peer() -> Result<Vec<Peer>> {
    Ok(
        Connection::open(DATABASE_FILE)?.prepare(
            "SELECT p.id, u.ip, u.port
            FROM peer p
            JOIN slot s ON p.id = s.peer_id
            JOIN uri u ON s.id = u.slot_id",
        )?.query_map([], |row| {
            let id: String = row.get(0)?;
            let ip: String = row.get(1)?;
            let port: u16 = row.get(2)?;
            Ok(Peer {
                id: id,
                uri: format!("{}:{}", ip, port),
                gas: 0
            })
        })?
        .collect::<Result<Vec<Peer>>>()?
    )
}

/// Renders the user interface widgets.
pub fn render(app: &mut App, frame: &mut Frame) {
    // This is where you add new widgets.
    // See the following resources:
    // - https://docs.rs/ratatui/latest/ratatui/widgets/index.html
    // - https://github.com/ratatui-org/ratatui/tree/master/

    frame.render_widget(
        Table::new(
            match get_peer() {
                Ok(peer_ids) => {
                    peer_ids.iter().map(|peer| {
                        Row::new(vec![peer.id.clone(), peer.uri.clone(), peer.gas.to_string()])
                    }).collect()
                },
                Err(e) => Vec::new(),
            },
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
        frame.size(),
    )
}
