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
    // Parsed by hand rather than with a CLI crate: this binary takes three options
    // in total (`--version`, `-V`, `--theme`), and a dependency to recognise them
    // would be one more thing CI has to build for every shipped target.
    if std::env::args()
        .skip(1)
        .any(|arg| arg == "--version" || arg == "-V")
    {
        print_version();
        return Ok(());
    }

    // The colour scheme, before anything is drawn (issue #395).
    //
    // Process-wide rather than carried on `App`: it is a property of the run, and the
    // alternative is a `&Theme` parameter on forty draw functions.
    //
    // Resolved here rather than in `App::new()`, like the KyA gate, so building an
    // `App` does not consult the environment and rendering tests draw in a known
    // theme.
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let config = tui::app::Paths::discover().config;
    let document = std::fs::read_to_string(&config)
        .ok()
        .and_then(|text| serde_yaml::from_str::<serde_yaml::Value>(&text).ok());
    tui::theme::set_current(tui::theme::Theme::resolve(document.as_ref(), &argv));

    // Create an application, behind the KyA gate.
    //
    // The question the CLI has always asked on first run (`src/commands/onboarding.py`)
    // and this console did not, which meant an operator whose first command is
    // `nodo tui` was never asked it at all (issue #395). Applied here rather than
    // inside `App::new()` so it is one visible line in the entry point, and so that
    // constructing an `App` stays a pure thing that does not consult the filesystem
    // for a marker.
    let mut app = App::new().with_operator_alerts().with_kya_gate();

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
