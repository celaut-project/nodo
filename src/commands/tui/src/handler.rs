use crate::app::{App, AppResult};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Handles the key events and updates the state of [`App`].
pub fn handle_key_events(key_event: KeyEvent, app: &mut App) -> AppResult<()> {
    match key_event.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            app.quit();
        }
        KeyCode::Char('c') | KeyCode::Char('C') => {
            if key_event.modifiers == KeyModifiers::CONTROL {
                app.quit();
            }
        }
        KeyCode::Left | KeyCode::Char('h') => app.on_left(),
        KeyCode::Up | KeyCode::Char('k') => app.on_up(),
        KeyCode::Right | KeyCode::Char('l') => app.on_right(),
        KeyCode::Down | KeyCode::Char('j') => app.on_down(),
        KeyCode::Char('i') => app.toggle_cpu_ram_visibility(), // Nueva línea para manejar la tecla 'i'
        _ => {}
    }
    Ok(())
}
