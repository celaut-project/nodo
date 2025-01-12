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
        match (key_event.modifiers, key_event.code) {
            (KeyModifiers::NONE, KeyCode::Esc) | (KeyModifiers::NONE, KeyCode::Char('q')) => {
                app.quit();
            }
            (KeyModifiers::NONE, KeyCode::Left) => app.on_left(),
            (KeyModifiers::NONE, KeyCode::Up) => app.on_up(),
            (KeyModifiers::NONE, KeyCode::Right) => app.on_right(),
            (KeyModifiers::NONE, KeyCode::Down) => app.on_down(),
            
            (KeyModifiers::SHIFT, KeyCode::Up) => app.change_mode_view(),
            (KeyModifiers::SHIFT, KeyCode::Left) => app.previous_block_view(),
            (KeyModifiers::SHIFT, KeyCode::Right) => app.next_block_view(),
            
            (KeyModifiers::NONE, KeyCode::Char('d')) => app.press_d().await,
            (KeyModifiers::NONE, KeyCode::Char('e')) => app.press_e().await,
        
            (KeyModifiers::CONTROL, KeyCode::Char('c')) | (KeyModifiers::CONTROL, KeyCode::Char('C')) => {
                app.quit();
            }
        
            (KeyModifiers::NONE, KeyCode::Char('c')) | (KeyModifiers::NONE, KeyCode::Char('C')) => {
                if app.tabs.index == 0 {
                    app.open_popup();
                }
            }
        
            _ => {}
        } 
    }
    Ok(())
}
