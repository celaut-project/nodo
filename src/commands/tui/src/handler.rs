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
        match (key_event.code, key_event.modifiers) {
            (KeyCode::Esc, _) | (KeyCode::Char('q'), _) => app.quit(),
        
            (KeyCode::Left, _) => app.on_left(),
            (KeyCode::Up, _) => app.on_up(),
            (KeyCode::Right, _) => app.on_right(),
            (KeyCode::Down, _) => app.on_down(),
        
            (KeyCode::Up, KeyModifiers::SHIFT) | (KeyCode::Down, KeyModifiers::SHIFT) => {
                app.change_mode_view()
            }
            (KeyCode::Left, KeyModifiers::SHIFT) => app.previous_block_view(),
            (KeyCode::Right, KeyModifiers::SHIFT) => app.next_block_view(),
        
            (KeyCode::Char('d'), _) => app.press_d().await,
            (KeyCode::Char('e'), _) => app.press_e().await,
        
            (KeyCode::Char('c'), KeyModifiers::CONTROL) | (KeyCode::Char('C'), KeyModifiers::CONTROL) => {
                app.quit()
            }
        
            (KeyCode::Char('c'), _) | (KeyCode::Char('C'), _) => {
                if app.tabs.index == 0 {
                    app.open_popup();
                }
            }
        
            _ => {}
        }        
    }
    Ok(())
}
