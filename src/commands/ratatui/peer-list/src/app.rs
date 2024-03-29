use std::error;
use rusqlite::{Connection, Result};

/// Application result type.
pub type AppResult<T> = std::result::Result<T, Box<dyn error::Error>>;

const DATABASE_FILE: &str = "../../../../storage/database.sqlite";

#[derive(Debug)]
pub struct Peer {
    pub id: String,
    pub uri: String,
    pub gas: u8
}

fn get_peers() -> Result<Vec<Peer>> {
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

/// Application.
#[derive(Debug)]
pub struct App {
    /// Is the application running?
    pub running: bool,
    pub peers: Vec<Peer>
}

impl Default for App {
    fn default() -> Self {
        Self {
            running: true,
            peers: get_peers().unwrap_or_default()
        }
    }
}

impl App {
    /// Constructs a new instance of [`App`].
    pub fn new() -> Self {
        Self::default()
    }

    /// Handles the tick event of the terminal.
    pub fn tick(&self) {}

    /// Set running to false to quit the application.
    pub fn quit(&mut self) {
        self.running = false;
    }

    pub fn refresh(&mut self) {
        self.peers = get_peers().unwrap_or_default()
    }
}
