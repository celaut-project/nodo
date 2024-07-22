use ratatui::widgets::{List, ListState, TableState};
use rusqlite::{Connection, Result};
use std::{error, fs, io, path::Path, vec};
use sysinfo::System;

/// Application result type.
pub type AppResult<T> = std::result::Result<T, Box<dyn error::Error>>;

const DATABASE_FILE: &str = "../../../storage/database.sqlite";
const SERVICES_ROOT: &str = "../../../storage/__registry__";
const METADATA_ROOT: &str = "../../../storage/__metadata__";
pub const RAM_TIMES: usize = 500;
pub const CPU_TIMES: usize = 500;

#[derive(Debug)]
pub struct Peer {
    pub id: String,
    pub uri: String,
    pub gas: u8,
}

#[derive(Debug)]
pub struct Service {
    pub id: String,
}

fn get_peers() -> Result<Vec<Peer>> {
    Ok(Connection::open(DATABASE_FILE)?
        .prepare(
            "SELECT p.id, u.ip, u.port
            FROM peer p
            JOIN slot s ON p.id = s.peer_id
            JOIN uri u ON s.id = u.slot_id",
        )?
        .query_map([], |row| {
            let id: String = row.get(0)?;
            let ip: String = row.get(1)?;
            let port: u16 = row.get(2)?;
            Ok(Peer {
                id: id,
                uri: format!("{}:{}", ip, port),
                gas: 0,
            })
        })?
        .collect::<Result<Vec<Peer>>>()?)
}

fn get_clients() -> Result<Vec<String>> {
    Ok(Connection::open(DATABASE_FILE)?
        .prepare("SELECT id FROM clients")?
        .query_map([], |row| {
            let id: String = row.get(0)?;
            Ok(id)
        })?
        .collect::<Result<Vec<String>>>()?)
}

fn get_containers() -> Result<Vec<String>> {
    Ok(Connection::open(DATABASE_FILE)?
        .prepare("SELECT id FROM containers")?
        .query_map([], |row| {
            let id: String = row.get(0)?;
            Ok(id)
        })?
        .collect::<Result<Vec<String>>>()?)
}

fn get_services() -> Result<Vec<Service>, io::Error> {
    let entries = fs::read_dir(Path::new(SERVICES_ROOT))?;
    let mut services = Vec::new();

    for entry in entries {
        let entry = entry?;
        let path = entry.file_name();

        // Convierte el path a String, puede que necesites ajustar esto según tu estructura Service
        let service_id = path.to_string_lossy().into_owned();

        services.push(Service { id: service_id });
    }

    Ok(services)
}

fn get_ram_usage(sys: &mut System) -> u64 {
    // First we update all information of our system struct.
    sys.refresh_memory();

    // Get total and used memory
    let total_memory = sys.total_memory();
    let used_memory = sys.used_memory();

    // Calculate the RAM usage in percentage
    let ram_usage_percentage = (used_memory as f64 / total_memory as f64) * 100.0;
    ram_usage_percentage as u64
}

fn get_cpu_usage(sys: &mut System) -> u64 {
    // Refresh CPU information
    sys.refresh_cpu();

    // Retrieve CPU usage as a percentage for all CPUs combined
    let cpu_usage_percentage = sys.global_cpu_info().cpu_usage();

    cpu_usage_percentage as u64
}

#[derive(Debug)]
pub struct TabsState<'a> {
    pub titles: Vec<&'a str>,
    pub index: usize,
}

impl<'a> TabsState<'a> {
    pub fn new(titles: Vec<&'a str>) -> TabsState {
        TabsState { titles, index: 0 }
    }

    pub fn next(&mut self) {
        self.index = (self.index + 1) % self.titles.len();
    }

    pub fn previous(&mut self) {
        if self.index > 0 {
            self.index -= 1;
        } else {
            self.index = self.titles.len() - 1;
        }
    }
}

#[derive(Debug)]
pub struct StatefulList<T> {
    pub state: TableState,
    pub items: Vec<T>,
}

impl<T> StatefulList<T> {
    pub fn with_items(items: Vec<T>) -> Self {
        Self {
            state: TableState::default(),
            items,
        }
    }

    pub fn next(&mut self) {
        let i = match self.state.selected() {
            Some(i) => {
                if i >= self.items.len() - 1 {
                    0
                } else {
                    i + 1
                }
            }
            None => 0,
        };
        self.state.select(Some(i));
    }

    pub fn previous(&mut self) {
        let i = match self.state.selected() {
            Some(i) => {
                if i == 0 {
                    self.items.len() - 1
                } else {
                    i - 1
                }
            }
            None => 0,
        };
        self.state.select(Some(i));
    }
}

/// Application.
#[derive(Debug)]
pub struct App<'a> {
    pub title: &'a str,
    pub tabs: TabsState<'a>,
    pub running: bool,
    pub peers: StatefulList<Peer>,
    pub clients: StatefulList<String>,
    pub containers: StatefulList<String>,
    pub services: StatefulList<Service>,
    pub ram_usage: Vec<u64>,
    pub cpu_usage: Vec<u64>,
    pub sys: System,
    pub show_cpu_ram: bool, // Nueva variable de estado para controlar la visibilidad de CPU y RAM
}

impl<'a> Default for App<'a> {
    fn default() -> Self {
        Self {
            title: "NODO TUI",
            tabs: TabsState::new(vec!["PEERS", "CLIENTS", "CONTAINERS", "SERVICES"]),
            running: true,
            peers: StatefulList::with_items(get_peers().unwrap_or_default()),
            clients: StatefulList::with_items(get_clients().unwrap_or_default()),
            containers: StatefulList::with_items(get_containers().unwrap_or_default()),
            services: StatefulList::with_items(get_services().unwrap_or_default()),
            ram_usage: [0; RAM_TIMES].to_vec(),
            cpu_usage: [0; CPU_TIMES].to_vec(),
            sys: System::new_all(),
            show_cpu_ram: true, // Inicialmente mostramos CPU y RAM
        }
    }
}

impl<'a> App<'a> {
    /// Constructs a new instance of [`App`].
    pub fn new() -> Self {
        Self::default()
    }

    pub fn on_right(&mut self) {
        self.tabs.next();
    }

    pub fn on_left(&mut self) {
        self.tabs.previous();
    }

    pub fn on_up(&mut self) {
        match self.tabs.index {
            0 => self.peers.previous(),
            3 => self.services.previous(),
            _ => {}
        }
    }

    pub fn on_down(&mut self) {
        match self.tabs.index {
            0 => self.peers.next(),
            3 => self.services.next(),
            _ => {}
        }
    }

    pub fn toggle_cpu_ram_visibility(&mut self) {
        self.show_cpu_ram = !self.show_cpu_ram;
        self.ram_usage.clear();
        self.cpu_usage.clear();
    }

    /// Handles the tick event of the terminal.
    pub fn tick(&self) {}

    /// Set running to false to quit the application.
    pub fn quit(&mut self) {
        self.running = false;
    }

    pub fn connect(&self) {
        if let Some(selected) = self.peers.state.selected() {
            let peer = &self.peers.items[selected];
            println!("Connecting to peer: {}", peer.uri);
        }
    }

    pub fn refresh(&mut self) {
        self.peers = StatefulList::with_items(get_peers().unwrap_or_default());
        self.services = StatefulList::with_items(get_services().unwrap_or_default());
        self.ram_usage.push(get_ram_usage(&mut self.sys));
        self.cpu_usage.push(get_cpu_usage(&mut self.sys));
    }
}
