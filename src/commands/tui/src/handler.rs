use crate::app::{App, AppResult};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Handles the key events and updates the state of [`App`].
pub fn handle_key_events(key_event: KeyEvent, app: &mut App) -> AppResult<()> {
    if app.connect_popup {
        match key_event.code {
            KeyCode::Enter => app.connect(),
            KeyCode::Esc => {
                app.connect_text.clear();
                app.close_popup();
            }
            KeyCode::Char(c) => {
                app.connect_text.push(c);
            }
            KeyCode::Backspace => {
                app.connect_text.pop();
            }
            _ => {}
        }
    } else {
        match key_event.code {
            KeyCode::Esc | KeyCode::Char('q') => {
                app.quit();
            }
            KeyCode::Char('c') | KeyCode::Char('C') => {
                if key_event.modifiers == KeyModifiers::CONTROL {
                    app.quit();
                } else {
                    app.open_popup();
                }
            }
            KeyCode::Left | KeyCode::Char('h') => app.on_left(),
            KeyCode::Up | KeyCode::Char('k') => app.on_up(),
            KeyCode::Right | KeyCode::Char('l') => app.on_right(),
            KeyCode::Down | KeyCode::Char('j') => app.on_down(),
            KeyCode::Char('i') => app.toggle_cpu_ram_visibility(),
            _ => {}
        }
    }
    Ok(())
}
