use crate::app::{App, AppResult, EditKind, InputMode, Page};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};

/// Handle mouse input: the wheel moves the selection, a left click picks the tab, config
/// node or table row it landed on.
///
/// Only the Normal and Details modes react. While a modal owns the screen, a click on
/// the page behind it would act on something the user cannot see.
pub fn handle_mouse_events(mouse: MouseEvent, app: &mut App) {
    match app.input_mode {
        // The KyA gate is a decision, and a decision is not something a stray wheel
        // event or a click on the page behind it should be able to make. Scrolling is
        // not offered either: the overlay's own ↑/↓ do that, and a wheel event here
        // would be indistinguishable from one aimed at whatever it is covering
        // (issue #395).
        InputMode::AcceptKya => {}
        InputMode::Normal => match mouse.kind {
            MouseEventKind::ScrollUp => app.on_up(),
            MouseEventKind::ScrollDown => app.on_down(),
            MouseEventKind::Down(MouseButton::Left) => app.click_at(mouse.column, mouse.row),
            // Dragging a schedule window's edge along the day bar (issue #414). Only
            // SCHEDULE has anything to drag; `drag_schedule` is a no-op elsewhere and
            // while nothing is held.
            MouseEventKind::Drag(MouseButton::Left) => {
                app.drag_schedule(mouse.column, mouse.row)
            }
            // Let go on any button release, not just the left one: a release arriving
            // for another button while the left is held would otherwise leave the edge
            // stuck to the pointer after the gesture ended.
            MouseEventKind::Up(_) => app.release_schedule_drag(),
            _ => {}
        },
        // The scrollable overlay is the one modal with anything to scroll.
        InputMode::Details => match mouse.kind {
            MouseEventKind::ScrollUp => app.scroll_details(-1),
            MouseEventKind::ScrollDown => app.scroll_details(1),
            _ => {}
        },
        _ => {}
    }
}

/// Handle keyboard input without allowing page shortcuts to leak into modal input.
pub async fn handle_key_events(key: KeyEvent, app: &mut App) -> AppResult<()> {
    match app.input_mode {
        // The KyA, before anything else in this function and before any page shortcut
        // can be read (issue #395). First arm on purpose: the question is what running
        // the node is conditional on, so there must be no key that acts on the node
        // while it is unanswered -- not `r`, not Tab, not a page's `d`.
        //
        // Declining quits, exactly as `nodo`'s CLI onboarding exits 1 on a refusal.
        // Esc and q decline rather than dismiss, because they are the keys that mean
        // "I am not doing this" everywhere else in this interface, and a gate they
        // merely closed would be a gate that could be walked past.
        InputMode::AcceptKya => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Char('y') | KeyCode::Char('Y')) => app.accept_kya(),
                (_, KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc | KeyCode::Char('q')) => {
                    app.decline_kya()
                }
                (_, KeyCode::Up) => app.scroll_details(-1),
                (_, KeyCode::Down) => app.scroll_details(1),
                (_, KeyCode::PageUp) => app.scroll_details(-10),
                (_, KeyCode::PageDown) => app.scroll_details(10),
                _ => {}
            }
            return Ok(());
        }
        // Yes/no confirmation for destructive actions.
        InputMode::Confirm => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Char('y') | KeyCode::Char('Y')) => app.confirm_pending().await,
                (_, KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc | KeyCode::Enter) => {
                    app.close_input()
                }
                _ => {}
            }
            return Ok(());
        }
        // Every key a change would touch, shown before any of it is written. Only
        // y applies it: Enter is a scroll key on a diff this long, and must not be
        // the one that commits a dozen keys.
        InputMode::ConfirmWrites => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Char('y') | KeyCode::Char('Y')) => app.confirm_pending().await,
                (_, KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc) => {
                    app.cancel_pending_writes()
                }
                (_, KeyCode::Up) => app.scroll_details(-1),
                (_, KeyCode::Down) => app.scroll_details(1),
                (_, KeyCode::PageUp) => app.scroll_details(-10),
                (_, KeyCode::PageDown) => app.scroll_details(10),
                _ => {}
            }
            return Ok(());
        }
        // Which of a lever's keys to edit. Same keys as the profile picker, since
        // it is the same gesture: choose a row, Enter opens it.
        InputMode::PickLeverKey => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Up) => app.move_lever_key_selection(-1),
                (_, KeyCode::Down) => app.move_lever_key_selection(1),
                (_, KeyCode::Enter) => app.submit_input().await,
                (_, KeyCode::Esc | KeyCode::Char('q')) => app.close_input(),
                _ => {}
            }
            return Ok(());
        }
        // Profile picker: choose a posture, then see its diff.
        InputMode::PickProfile => {
            match (key.modifiers, key.code) {
                (KeyModifiers::CONTROL, KeyCode::Char('c')) => app.quit(),
                (_, KeyCode::Up) => app.move_profile_selection(-1),
                (_, KeyCode::Down) => app.move_profile_selection(1),
                (_, KeyCode::Enter) => app.submit_input().await,
                (_, KeyCode::Esc | KeyCode::Char('q')) => app.close_input(),
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
        // Text-entry modals: Connect, EditConfig, AddConfigItem, FilterConfig.
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
        // Esc gives up an unapplied schedule before it gives up the interface: quitting
        // with an edit on screen would throw it away silently, and a second Esc still
        // quits. Only while there is something to discard, so the global meaning of the
        // key is untouched everywhere else.
        (KeyModifiers::NONE, KeyCode::Esc)
            if app.page() == Page::Schedule && app.schedule_is_dirty() =>
        {
            app.discard_schedule_draft()
        }
        (KeyModifiers::CONTROL, KeyCode::Char('c'))
        | (KeyModifiers::NONE, KeyCode::Esc)
        | (KeyModifiers::NONE, KeyCode::Char('q')) => app.quit(),
        // Two rows, two axes, two sets of keys (issue #395). Tab/Shift+Tab move
        // between the pages of the OPEN group, wrapping inside it.
        //
        // crossterm reports Shift+Tab as BackTab under the legacy encoding and as
        // Tab + SHIFT under the kitty protocol, so match both or the binding dies
        // depending on the terminal.
        (KeyModifiers::NONE, KeyCode::Tab) => app.next_page(),
        (_, KeyCode::BackTab) | (KeyModifiers::SHIFT, KeyCode::Tab) => app.previous_page(),
        // `[`/`]` move between GROUPS, landing on the first page of each -- except
        // where the page claims them: SCHEDULE uses them to switch which window the
        // arrows act on. The arm below catches every other page.
        (KeyModifiers::NONE, KeyCode::Char(']')) if app.page() != Page::Schedule => {
            app.next_group()
        }
        (KeyModifiers::NONE, KeyCode::Char('[')) if app.page() != Page::Schedule => {
            app.previous_group()
        }
        // 1..5 jump straight to a group, one-based as they are counted on screen.
        // Nothing else in this interface binds a digit, so there is no page that has
        // to be excepted the way SCHEDULE is above.
        (KeyModifiers::NONE, KeyCode::Char(digit @ '1'..='5')) => {
            app.select_group_by_number(digit as usize - '0' as usize)
        }
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
            app.adjust_selected_price(1)
        }
        (_, KeyCode::Char('-') | KeyCode::Char('_')) if app.page() == Page::Pricing => {
            app.adjust_selected_price(-1)
        }
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Pricing => {
            app.open_price_editor()
        }
        (KeyModifiers::NONE, KeyCode::Char('g')) if app.page() == Page::Pricing => app.open_payment_rate_editor("ergo"),
        (KeyModifiers::NONE, KeyCode::Char('b')) if app.page() == Page::Pricing => app.open_payment_rate_editor("bitcoin"),
        // ENERGY mirrors Config's `e` and adds Enter, because the page is a list of
        // one-key decisions and Enter is what "work this row" means on every other
        // list in this interface (issue #395).
        (_, KeyCode::Enter) if app.page() == Page::Energy => app.open_energy_editor(),
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Energy => {
            app.open_energy_editor()
        }
        // The CELL page: Enter works the selected lever, `e` reaches the keys behind
        // it, `p` picks a posture and `d` says how this node differs from one.
        // The SCHEDULE page: ←/→ and ↑/↓ reach it through on_left/on_right/on_up, so
        // only the keys with no arrow of their own are here.
        (_, KeyCode::Enter) if app.page() == Page::Schedule => app.commit_schedule(),
        (KeyModifiers::NONE, KeyCode::Char('w')) if app.page() == Page::Schedule => {
            app.toggle_schedule_enabled()
        }
        (KeyModifiers::NONE, KeyCode::Char('c')) if app.page() == Page::Schedule => {
            app.toggle_schedule_on_close()
        }
        // A schedule is a list of windows: `a` appends one (empty, so it refuses
        // nothing until its hours are moved), `d` removes the selected one, and
        // `[`/`]` switch which window ←/→/↑/↓ act on. Mirrors Config's `a`/`d` on its
        // own lists.
        (KeyModifiers::NONE, KeyCode::Char('a')) if app.page() == Page::Schedule => {
            app.add_schedule_window()
        }
        (KeyModifiers::NONE, KeyCode::Char('d')) if app.page() == Page::Schedule => {
            app.remove_schedule_window()
        }
        (KeyModifiers::NONE, KeyCode::Char('[')) if app.page() == Page::Schedule => {
            app.select_schedule_window(-1)
        }
        (KeyModifiers::NONE, KeyCode::Char(']')) if app.page() == Page::Schedule => {
            app.select_schedule_window(1)
        }
        (_, KeyCode::Enter | KeyCode::Char(' ')) if app.page() == Page::Cell => {
            app.toggle_selected_lever()
        }
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Cell => {
            app.open_lever_editor()
        }
        (KeyModifiers::NONE, KeyCode::Char('p')) if app.page() == Page::Cell => {
            app.open_profile_picker()
        }
        (KeyModifiers::NONE, KeyCode::Char('d')) if app.page() == Page::Cell => {
            app.show_profile_deviations()
        }
        (KeyModifiers::NONE, KeyCode::Char('n')) if app.page() == Page::Cell => {
            app.open_nat_guide()
        }
        // Clients' +/- open an amount modal rather than nudging in place, unlike
        // Peers/Pricing: a balance has no natural step size to nudge by.
        (_, KeyCode::Char('+') | KeyCode::Char('=')) if app.page() == Page::Clients => {
            app.open_credit_client(false)
        }
        (_, KeyCode::Char('-') | KeyCode::Char('_')) if app.page() == Page::Clients => {
            app.open_credit_client(true)
        }
        // CHAT (issue #431): `o` opens a new thread, Enter replies in the selected
        // one, `c`/`R` close and reopen it. Mirrors Peers' `d`/Clients' `+`/`-` in
        // being a direct action, not a confirmation -- closing is reversible and
        // nothing here waits on the peer.
        (KeyModifiers::NONE, KeyCode::Char('o')) if app.page() == Page::Chat => {
            app.open_new_conversation_prompt()
        }
        (_, KeyCode::Enter) if app.page() == Page::Chat => app.open_reply_prompt(),
        (KeyModifiers::NONE, KeyCode::Char('c')) if app.page() == Page::Chat => {
            app.close_selected_conversation()
        }
        (_, KeyCode::Char('R')) if app.page() == Page::Chat => {
            app.reopen_selected_conversation()
        }
        (KeyModifiers::NONE, KeyCode::Char('e')) if app.page() == Page::Config => {
            app.open_config_editor()
        }
        // Lists are the one config shape a single-value editor cannot cover: `a`
        // appends an element, `d` removes the selected one.
        (KeyModifiers::NONE, KeyCode::Char('a')) if app.page() == Page::Config => {
            app.open_config_list_add()
        }
        (KeyModifiers::NONE, KeyCode::Char('d')) if app.page() == Page::Config => {
            app.open_delete_config_item_confirm()
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
