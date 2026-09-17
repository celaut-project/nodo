use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use std::io;
use tui::app::{App, AppResult};
use tui::event::{Event, EventHandler};
use tui::handler::{handle_key_events, handle_mouse_events};
use tui::tui::Tui;

/// One line, and exit. This is what makes a cheap liveness check possible at all.
///
/// `nodo tui` will only execute a prebuilt binary it has verified against this
/// host, and the last step of that check is starting it
/// (`src/utils/rust_toolchain.py`). Every other entry point takes over the
/// terminal, so without a flag that prints and exits, the only way to find out
/// whether a shipped binary runs here is to let it try to draw -- which on a
/// mismatch leaves the operator looking at a blank screen instead of a reason.
///
/// It also records the target the binary was built for, so a binary separated
/// from its `tui.host-triple` marker can still say what it is.
fn print_version() {
    println!(
        "{} {} ({})",
        env!("CARGO_PKG_NAME"),
        env!("CARGO_PKG_VERSION"),
        std::env::consts::ARCH
    );
}

#[tokio::main]
async fn main() -> AppResult<()> {
    // Parsed by hand rather than with a CLI crate: this binary takes no options
    // at all beyond the two below, and a dependency to recognise them would be
    // one more thing CI has to build for every target.
    if std::env::args()
        .skip(1)
        .any(|arg| arg == "--version" || arg == "-V")
    {
        print_version();
        return Ok(());
    }

    // Create an application.
    let mut app = App::new();

    // Initialize the terminal user interface.
    let backend = CrosstermBackend::new(io::stderr());
    let terminal = Terminal::new(backend)?;
    let events = EventHandler::new(250);
    let mut tui = Tui::new(terminal, events);
    tui.init()?;

    // Start the main loop.
    while app.running {
        // Render the user interface.
        tui.draw(&mut app)?;
        // Handle events.
        match tui.events.next().await? {
            Event::Tick => app.refresh(false).await,
            Event::Key(key_event) => handle_key_events(key_event, &mut app).await?,
            Event::Mouse(mouse_event) => handle_mouse_events(mouse_event, &mut app),
            Event::Resize(_, _) => {}
        }
    }

    // Exit the user interface.
    tui.exit()?;
    Ok(())
}
