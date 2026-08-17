use crate::app::{App, AppResult, EditKind, InputMode, Page};
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
            let is_bool_editor =
                app.input_mode == InputMode::EditConfig && app.edit_kind == EditKind::Bool;
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Enter) => app.submit_input().await,
                (_, KeyCode::Esc) => app.close_input(),
                // ↑/↓ step a number, cycle an enum, or (with ←/→/Space) flip a
                // checkbox — additive on top of typing for number/enum, the only
                // way to change a checkbox (see the char/backspace guard below).
                (_, KeyCode::Up) if app.input_mode == InputMode::EditConfig => {
                    app.adjust_edit_value(1)
                }
                (_, KeyCode::Down) if app.input_mode == InputMode::EditConfig => {
                    app.adjust_edit_value(-1)
                }
                (_, KeyCode::Left | KeyCode::Right | KeyCode::Char(' ')) if is_bool_editor => {
                    app.adjust_edit_value(1)
                }
                (KeyModifiers::CONTROL, KeyCode::Char('u')) if !is_bool_editor => {
                    app.input.clear();
                    app.on_input_changed();
                }
                // A checkbox has exactly two states, both reachable above; free
                // text entry would just let you type something that isn't a bool.
                (_, KeyCode::Backspace | KeyCode::Char(_)) if is_bool_editor => {}
                (_, KeyCode::Backspace) => {
                    app.input.pop();
                    app.on_input_changed();
                }
                (_, KeyCode::Char(character)) => {
                    app.input.push(character);
                    app.on_input_changed();
                }
                _ => {}
            }
            return Ok(());
        }
    }

    match (key.modifiers, key.code) {
        (KeyModifiers::CONTROL, KeyCode::Char('c'))
        | (KeyModifiers::NONE, KeyCode::Esc)
        | (KeyModifiers::NONE, KeyCode::Char('q')) => app.quit(),
        // Tab cycles pages forward, Shift+Tab backward; both wrap. crossterm reports
        // Shift+Tab as BackTab under the legacy encoding and as Tab + SHIFT under the
        // kitty keyboard protocol, so match both or the binding silently dies depending
        // on the terminal. ←/→ are no longer page navigation: they are page-local now
        // and ignored by pages that do not claim them.
        (KeyModifiers::NONE, KeyCode::Tab) => app.next_page(),
        (_, KeyCode::BackTab) | (KeyModifiers::SHIFT, KeyCode::Tab) => app.previous_page(),
        (_, KeyCode::Up) => app.on_up(),
        (_, KeyCode::Down) => app.on_down(),
        (_, KeyCode::Right) => app.on_right(),
        (_, KeyCode::Left) => app.on_left(),
        (KeyModifiers::NONE, KeyCode::Char('r')) => app.refresh(true).await,
        (KeyModifiers::NONE, KeyCode::Char('g')) if app.page() == Page::Instances => {
            app.toggle_instances_grouped()
        }
        (KeyModifiers::NONE, KeyCode::Char('k')) if app.page() == Page::Instances => {
            app.open_kill_instance_confirm()
        }
        (KeyModifiers::NONE, KeyCode::Char('c')) if app.page() == Page::Peers => {
            app.open_connect()
        }
        (_, KeyCode::Char('+') | KeyCode::Char('=')) if app.page() == Page::Peers => {
            app.adjust_selected_peer_reputation(1)
        }
        (_, KeyCode::Char('-') | KeyCode::Char('_')) if app.page() == Page::Peers => {
            app.adjust_selected_peer_reputation(-1)
        }
        // Pricing mirrors the Peers page's +/- and Config's `e`: nudge in place, or open the
        // ordinary editor for an exact figure.
        (_, KeyCode::Char('+') | KeyCode::Char('=')) if app.page() == Page::Pricing => {
            app.adjust_selected_price(1).await
        }
        (_, KeyCode::Char('-') | KeyCode::Char('_')) if app.page() == Page::Pricing => {
            app.adjust_selected_price(-1).await
        }
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Pricing => {
            app.open_price_editor()
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
        // Same key as Services' delete, on the page's other destructive target.
        (KeyModifiers::NONE, KeyCode::Char('d')) if app.page() == Page::Peers => {
            app.open_disconnect_peer_confirm()
        }
        // Enter/Space expands or collapses the selected config section. `e` still
        // opens the value editor, so these never fight over the same key.
        (_, KeyCode::Enter | KeyCode::Char(' ')) if app.page() == Page::Config => {
            app.toggle_selected_config_node()
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
