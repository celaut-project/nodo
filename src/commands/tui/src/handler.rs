use crate::app::{App, AppResult, InputMode, Page};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Handle keyboard input without allowing page shortcuts to leak into modal input.
pub async fn handle_key_events(key: KeyEvent, app: &mut App) -> AppResult<()> {
    match app.input_mode {
        // Yes/no confirmation for destructive actions.
        InputMode::Confirm => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Char('y') | KeyCode::Char('Y')) => app.confirm_pending(),
                (_, KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc | KeyCode::Enter) => {
                    app.close_input()
                }
                _ => {}
            }
            return Ok(());
        }
        // Read-only, scrollable overlay (service details).
        InputMode::Details => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Esc | KeyCode::Char('q')) => app.close_details(),
                (_, KeyCode::Up) => app.scroll_details(-1),
                (_, KeyCode::Down) => app.scroll_details(1),
                (_, KeyCode::PageUp) => app.scroll_details(-10),
                (_, KeyCode::PageDown) => app.scroll_details(10),
                _ => {}
            }
            return Ok(());
        }
        InputMode::Normal => {}
        // Text-entry modals: Connect, EditConfig, FilterConfig.
        _ => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Enter) => app.submit_input().await,
                (_, KeyCode::Esc) => app.close_input(),
                (KeyModifiers::CONTROL, KeyCode::Char('u')) => app.input.clear(),
                (_, KeyCode::Backspace) => {
                    app.input.pop();
                }
                (_, KeyCode::Char(character)) => app.input.push(character),
                _ => {}
            }
            return Ok(());
        }
    }

    match (key.modifiers, key.code) {
        (KeyModifiers::CONTROL, KeyCode::Char('c'))
        | (KeyModifiers::NONE, KeyCode::Esc)
        | (KeyModifiers::NONE, KeyCode::Char('q')) => app.quit(),
        (_, KeyCode::Left) => app.on_left(),
        (_, KeyCode::Right) => app.on_right(),
        (_, KeyCode::Up) => app.on_up(),
        (_, KeyCode::Down) => app.on_down(),
        (_, KeyCode::Tab) => app.toggle_focus(),
        (KeyModifiers::NONE, KeyCode::Char('r')) => app.refresh(true).await,
        (KeyModifiers::NONE, KeyCode::Char('g')) if app.page() == Page::Instances => {
            app.toggle_instances_grouped()
        }
        (KeyModifiers::NONE, KeyCode::Char('k')) if app.page() == Page::Instances => {
            app.open_kill_instance_confirm()
        }
        (KeyModifiers::NONE, KeyCode::Char('c')) if app.page() == Page::Network => {
            app.open_connect()
        }
        (_, KeyCode::Char('+') | KeyCode::Char('=')) if app.page() == Page::Network => {
            app.adjust_selected_peer_reputation(1)
        }
        (_, KeyCode::Char('-') | KeyCode::Char('_')) if app.page() == Page::Network => {
            app.adjust_selected_peer_reputation(-1)
        }
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Config => {
            app.open_config_editor()
        }
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Services => {
            app.execute_selected_service()
        }
        (KeyModifiers::NONE, KeyCode::Char('i')) if app.page() == Page::Services => {
            app.open_service_details()
        }
        (KeyModifiers::NONE, KeyCode::Char('d')) if app.page() == Page::Services => {
            app.open_delete_service_confirm()
        }
        (KeyModifiers::NONE, KeyCode::Char('/')) if app.page() == Page::Config => {
            app.open_config_filter()
        }
        (KeyModifiers::NONE, KeyCode::Char('x')) if app.page() == Page::Config => {
            app.clear_config_filter()
        }
        _ => {}
    }
    Ok(())
}
