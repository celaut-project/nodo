use ratatui::widgets::{TableState};
use regex::Regex;
use rusqlite::{Connection, Result};
use std::io::{self, BufRead};
use std::process::Stdio;
use std::{error, fs, path::Path, vec};
use sysinfo::System;
use tokio::process::Command;
use tokio::io::AsyncBufReadExt;

/// Application result type.
pub type AppResult<T> = std::result::Result<T, Box<dyn error::Error>>;

const DATABASE_FILE: &str = "../../../storage/database.sqlite";
pub const LOG_FILE: &str = "../../../storage/app.log";
const SERVICES_ROOT: &str = "../../../storage/__registry__";
const METADATA_ROOT: &str = "../../../storage/__metadata__";
pub const RAM_TIMES: usize = 500;
pub const CPU_TIMES: usize = 500;

trait Identifiable {
    fn id(&self) -> &str;
}

#[derive(Debug)]
pub struct Peer {
    pub id: String,
    pub uri: String,
    pub gas: u8,
}

impl Identifiable for Peer {
    fn id(&self) -> &str {
        &self.id
    }
}

#[derive(Debug)]
pub struct Service {
    pub id: String,
}

impl Identifiable for Service {
    fn id(&self) -> &str {
        &self.id
    }
}

#[derive(Debug)]
pub struct Client {
    pub id: String,
}

impl Identifiable for Client {
    fn id(&self) -> &str {
        &self.id
    }
}

#[derive(Debug)]
pub struct Container {
    pub id: String,
}

impl Identifiable for Container {
    fn id(&self) -> &str {
        &self.id
    }
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

fn get_clients() -> Result<Vec<Client>> {
    Ok(Connection::open(DATABASE_FILE)?
        .prepare("SELECT id FROM clients")?
        .query_map([], |row| {
            let id: String = row.get(0)?;
            Ok(Client { id: id })
        })?
        .collect::<Result<Vec<Client>>>()?)
}

fn get_instances() -> Result<Vec<Container>> {
    let conn = Connection::open(DATABASE_FILE)?;

    // Obtener IDs de internal_services
    let internal_instances = conn
        .prepare("SELECT id FROM internal_services")?
        .query_map([], |row| {
            let id: String = row.get(0)?;
            Ok(Container { id })
        })?
        .collect::<Result<Vec<Container>>>()?;

    // Obtener IDs de external_services
    let external_instances = conn
        .prepare("SELECT token FROM external_services")?
        .query_map([], |row| {
            let id: String = row.get(0)?;
            Ok(Container { id })
        })?
        .collect::<Result<Vec<Container>>>()?;

    // Combinar ambos resultados
    let mut instances = Vec::new();
    instances.extend(internal_instances);
    instances.extend(external_instances);

    Ok(instances)
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
pub struct StatefulList<T: Identifiable> {
    pub state: TableState,
    pub state_id: Option<String>,
    pub items: Vec<T>,
}

impl<T: Identifiable> StatefulList<T> {
    pub fn with_items(items: Vec<T>) -> Self {
        Self {
            state: TableState::default(),
            state_id: None,
            items,
        }
    }

    pub fn refresh(&mut self, items: Vec<T>) {
        self.items = items;
        // Reset the state if the list is empty
        if self.items.is_empty() {
            self.state.select(None);
            self.state_id = None;
        }
    }

    pub fn next(&mut self) {
        if self.items.is_empty() {
            self.state.select(None);
            self.state_id = None;
            return;
        }

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
        self.state_id = Some(self.items[i].id().to_string());
    }

    pub fn previous(&mut self) {
        if self.items.is_empty() {
            self.state.select(None);
            self.state_id = None;
            return;
        }

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
        self.state_id = Some(self.items[i].id().to_string());
    }
}

/// Application.
#[derive(Debug)]
pub struct App<'a> {
    pub title: &'a str,
    pub tabs: TabsState<'a>,
    pub running: bool,
    pub logs: Vec<String>,
    pub peers: StatefulList<Peer>,
    pub clients: StatefulList<Client>,
    pub instances: StatefulList<Container>,
    pub services: StatefulList<Service>,
    pub ram_usage: Vec<u64>,
    pub cpu_usage: Vec<u64>,
    pub sys: System,
    pub show_cpu_ram: bool,
    pub connect_popup: bool,
    pub connect_text: String,
}

impl<'a> Default for App<'a> {
    fn default() -> Self {
        Self {
            title: "NODO TUI",
            tabs: TabsState::new(vec!["PEERS", "CLIENTS", "INSTANCES", "SERVICES"]),
            running: true,
            logs: Vec::new(),
            peers: StatefulList::with_items(get_peers().unwrap_or_default()),
            clients: StatefulList::with_items(get_clients().unwrap_or_default()),
            instances: StatefulList::with_items(get_instances().unwrap_or_default()),
            services: StatefulList::with_items(get_services().unwrap_or_default()),
            ram_usage: [0; RAM_TIMES].to_vec(),
            cpu_usage: [0; CPU_TIMES].to_vec(),
            sys: System::new_all(),
            show_cpu_ram: true,
            connect_popup: false,
            connect_text: "".to_string(),
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
            1 => self.clients.previous(),
            2 => self.instances.previous(),
            3 => self.services.previous(),
            _ => {}
        }
    }

    pub fn on_down(&mut self) {
        match self.tabs.index {
            0 => self.peers.next(),
            1 => self.clients.next(),
            2 => self.instances.next(),
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

    pub fn open_popup(&mut self) {
        self.connect_popup = true;
    }

    pub fn close_popup(&mut self) {
        self.connect_popup = false;
    }

    async fn execute_command(&mut self, args: Vec<String>) -> io::Result<()> {
        const COMMAND: &str = "nodo";
        let mut child = Command::new(COMMAND)
            .args(&args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

        let stdout = child.stdout.take().expect("Failed to capture stdout");
        let stderr = child.stderr.take().expect("Failed to capture stderr");

        let mut stdout_reader = tokio::io::BufReader::new(stdout).lines();
        let mut stderr_reader = tokio::io::BufReader::new(stderr).lines();

        while let Some(line) = stdout_reader.next_line().await? {
            self.logs.push(format!("STDOUT: {}", line));
        }

        while let Some(line) = stderr_reader.next_line().await? {
            self.logs.push(format!("STDERR: {}", line));
        }

        let status = child.wait().await?;
        self.logs.push(format!("Command exited with status: {}", status));

        Ok(())
    }

    pub async fn connect(&mut self) {
        if !self.connect_text.is_empty() {
            let re = Regex::new(r"^(\d{1,3}\.){3}\d{1,3}:\d{1,5}$").unwrap();
            if re.is_match(&self.connect_text) {
                let args = vec!["connect".to_string(), self.connect_text.clone()];
                let _ = self.execute_command(args).await;
                self.connect_text.clear();
                self.close_popup();
            } // TODO else show error msg during 3 seconds or any key press.
        }
    }

    pub async fn press_d(&mut self) {
        match self.tabs.index {
            0 => {
                if let Some(id) = &self.peers.state_id {
                    let _ = self.execute_command(vec!["prune:peer".to_string(), id.to_string()]).await;
                }
            }
            1 => {}
            2 => {}
            3 => {}
            _ => {}
        }
    }

    pub async fn press_e(&mut self) {
        match self.tabs.index {
            0 => {}
            1 => {}
            2 => {}
            3 => {
                if let Some(id) = &self.services.state_id {
                    let _ = self.execute_command(vec!["execute".to_string(), id.to_string()]).await;
                }
            }
            _ => {}
        }
    }

    pub async fn refresh(&mut self) {
        self.peers.refresh(get_peers().unwrap_or_default());
        self.clients.refresh(get_clients().unwrap_or_default());
        self.instances.refresh(get_instances().unwrap_or_default());
        self.services.refresh(get_services().unwrap_or_default());
        self.ram_usage.push(get_ram_usage(&mut self.sys));
        self.cpu_usage.push(get_cpu_usage(&mut self.sys));
    }
}
