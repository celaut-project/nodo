use std::error;
use rusqlite::{Connection, Result};
use sysinfo::System;

/// Application result type.
pub type AppResult<T> = std::result::Result<T, Box<dyn error::Error>>;

const DATABASE_FILE: &str = "../../../../storage/database.sqlite";
pub const RAM_TIMES: usize = 500;

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

fn get_ram_usage() -> u64 {
    // Create a System object
    let mut sys = System::new_all();

    // First we update all information of our system struct.
    sys.refresh_all();

    // Get total and used memory
    let total_memory = sys.total_memory();
    let used_memory = sys.used_memory();

    // Calculate the RAM usage in percentage
    let ram_usage_percentage = (used_memory as f64 / total_memory as f64) * 100.0;
    ram_usage_percentage as u64 
}

#[derive(Debug)]
pub enum Tabs {
    Peers,
    Containers
}

/// Application.
#[derive(Debug)]
pub struct App {
    /// Is the application running?
    pub running: bool,
    pub tab: Tabs,
    pub peers: Vec<Peer>,
    pub ram_usage: Vec<u64>
}

impl Default for App {
    fn default() -> Self {
        Self {
            running: true,
            tab: Tabs::Peers,
            peers: get_peers().unwrap_or_default(),
            ram_usage: [0; RAM_TIMES].to_vec()
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
        self.peers = get_peers().unwrap_or_default();
        self.ram_usage.push(get_ram_usage());
    }
}
