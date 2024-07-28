use crate::app::{App, AppResult};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Handles the key events and updates the state of [`App`].
pub async fn handle_key_events(key_event: KeyEvent, app: &mut App<'_>) -> AppResult<()> {
    if app.connect_popup {
        match key_event.code {
            KeyCode::Enter => app.connect().await,
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
            KeyCode::Char('o') => app.next_block_view(),
            KeyCode::Char('p') => app.previous_block_view(),
            KeyCode::Char('d') => app.press_d().await,
            KeyCode::Char('e') => app.press_e().await,
            _ => {}
        }
    }
    Ok(())
}
