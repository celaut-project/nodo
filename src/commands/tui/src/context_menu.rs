//! Right-click menus (issue #438): the actions an element offers, listed where it
//! was clicked.
//!
//! An entry is a label and the key that already does the thing. Choosing one
//! presses that key through `handle_key_events`, exactly as typing it would -- so
//! the menu cannot do anything the keyboard does not, and the two cannot drift
//! apart: there is no second implementation of any action here, only a list of
//! which keys each page answers, which a test holds to that page's footer.

use crate::app::{App, InputMode, Page};
use crate::ui::{accent, muted, popup_background, text_colour};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::Position;
use ratatui::prelude::*;
use ratatui::widgets::{Block, BorderType, Clear, Paragraph};

/// One action in a menu: what it is called, and the key that does it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MenuItem {
    pub label: &'static str,
    pub key: KeyCode,
}

impl MenuItem {
    const fn new(label: &'static str, key: char) -> Self {
        Self { label, key: KeyCode::Char(key) }
    }

    /// The key as the footer spells it.
    pub fn key_hint(&self) -> String {
        match self.key {
            KeyCode::Enter => "\u{23ce}".to_string(),
            KeyCode::Backspace => "\u{232b}".to_string(),
            KeyCode::Char(key) => key.to_string(),
            other => format!("{other:?}"),
        }
    }
}

/// An open menu: its entries, the highlighted one, and where it was opened.
#[derive(Debug, Clone)]
pub struct ContextMenu {
    pub items: Vec<MenuItem>,
    pub selected: usize,
    pub anchor: Position,
    /// Where each entry was drawn last frame, for a click to find it.
    pub item_areas: Vec<Rect>,
}

/// What a row on `page` offers. Empty where a page has no per-row action, and so
/// no menu. Every key here is one `handle_key_events` answers on that page.
pub fn page_actions(page: Page) -> Vec<MenuItem> {
    match page {
        Page::Instances => vec![
            MenuItem::new("Open a tunnel…", 't'),
            MenuItem::new("Kill", 'k'),
            MenuItem::new("Tree / flat view", 'g'),
        ],
        Page::Tunnels => vec![
            MenuItem::new("Details", 'i'),
            MenuItem::new("Close", 'd'),
            MenuItem::new("New tunnel…", 'n'),
        ],
        Page::Services => vec![
            MenuItem::new("Execute", 'e'),
            MenuItem::new("Details", 'i'),
            MenuItem::new("Get by hash…", 'g'),
            MenuItem::new("Delete", 'd'),
        ],
        Page::Peers => vec![
            MenuItem::new("Raise reputation", '+'),
            MenuItem::new("Lower reputation", '-'),
            MenuItem::new("Forget", 'd'),
            MenuItem::new("Connect a peer…", 'c'),
        ],
        Page::Clients => vec![MenuItem::new("Credit…", '+'), MenuItem::new("Debit…", '-')],
        Page::Chat => vec![
            MenuItem { label: "Reply", key: KeyCode::Enter },
            MenuItem::new("Close", 'c'),
            MenuItem::new("Reopen", 'R'),
            MenuItem::new("New chat…", 'o'),
        ],
        Page::Config => vec![
            MenuItem { label: "Expand / collapse", key: KeyCode::Enter },
            MenuItem::new("Edit", 'e'),
            MenuItem::new("Add to list", 'a'),
            MenuItem::new("Remove", 'd'),
        ],
        Page::Docs => vec![
            MenuItem { label: "Open / follow link", key: KeyCode::Enter },
            MenuItem::new("Search this page…", '/'),
            MenuItem::new("Next match", 'n'),
            MenuItem::new("Next link", 'l'),
            MenuItem { label: "Back", key: KeyCode::Backspace },
        ],
        _ => Vec::new(),
    }
}

impl App {
    /// Right-click: select what is under the pointer, the way a left click would,
    /// then list what can be done to it. Nothing under the pointer, no menu --
    /// there is nothing for its actions to act on.
    pub fn open_context_menu(&mut self, column: u16, row: u16) {
        let items = page_actions(self.page());
        if items.is_empty() {
            return;
        }
        let position = Position::new(column, row);
        if self.page() == Page::Config {
            // Selected, never toggled: a left click on the selected section collapses
            // it, and opening a menu must not change what the menu is about.
            let Some(identifier) = self.config_tree_state.rendered_at(position).map(<[_]>::to_vec)
            else {
                return;
            };
            self.config_tree_state.select(identifier);
        } else if self.page() == Page::Docs {
            if !self.point_docs_at(position) {
                return;
            }
        } else if !self.select_row_at(position) {
            return;
        }
        self.context_menu = Some(ContextMenu {
            items,
            selected: 0,
            anchor: position,
            item_areas: Vec::new(),
        });
        self.input_mode = InputMode::ContextMenu;
    }

    pub fn move_context_selection(&mut self, delta: i32) {
        if let Some(menu) = self.context_menu.as_mut() {
            let count = menu.items.len() as i32;
            menu.selected = (menu.selected as i32 + delta).rem_euclid(count) as usize;
        }
    }

    pub fn close_context_menu(&mut self) {
        self.context_menu = None;
        self.input_mode = InputMode::Normal;
    }

    /// Close the menu and hand back the key its highlighted entry stands for, for
    /// the caller to press.
    pub fn take_context_choice(&mut self) -> Option<KeyEvent> {
        let menu = self.context_menu.take()?;
        self.input_mode = InputMode::Normal;
        menu.items
            .get(menu.selected)
            .map(|item| KeyEvent::new(item.key, KeyModifiers::NONE))
    }

    /// The entry under a click, highlighted; `None` for a click outside the menu.
    pub fn context_item_at(&mut self, column: u16, row: u16) -> Option<usize> {
        let menu = self.context_menu.as_mut()?;
        let index = menu
            .item_areas
            .iter()
            .position(|area| area.contains(Position::new(column, row)))?;
        menu.selected = index;
        Some(index)
    }
}

/// The menu, opened at the pointer and pushed back inside the frame when the
/// pointer is too close to an edge for it to fit.
pub fn draw(frame: &mut Frame, app: &mut App) {
    let Some(menu) = app.context_menu.as_mut() else {
        return;
    };
    let width = menu
        .items
        .iter()
        .map(|item| item.label.chars().count() + item.key_hint().chars().count() + 6)
        .max()
        .unwrap_or(10) as u16
        + 2;
    let height = menu.items.len() as u16 + 2;
    let screen = frame.size();
    let area = Rect {
        x: menu.anchor.x.min(screen.width.saturating_sub(width)),
        y: menu.anchor.y.min(screen.height.saturating_sub(height)),
        width: width.min(screen.width),
        height: height.min(screen.height),
    };
    frame.render_widget(Clear, area);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(accent()))
        .style(Style::default().fg(text_colour()).bg(popup_background()));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    menu.item_areas.clear();
    for (index, item) in menu.items.iter().enumerate() {
        let row = Rect { y: inner.y + index as u16, height: 1, ..inner }.intersection(inner);
        if row.is_empty() {
            break;
        }
        let selected = index == menu.selected;
        let hint = item.key_hint();
        let gap = (row.width as usize).saturating_sub(item.label.chars().count() + hint.chars().count() + 2);
        let line = Line::from(vec![
            Span::styled(if selected { "▸ " } else { "  " }, Style::default().fg(accent()).bold()),
            Span::styled(
                item.label,
                if selected {
                    Style::default().fg(text_colour()).bold()
                } else {
                    Style::default().fg(text_colour())
                },
            ),
            Span::raw(" ".repeat(gap)),
            Span::styled(hint, Style::default().fg(muted())),
        ]);
        frame.render_widget(Paragraph::new(line), row);
        menu.item_areas.push(row);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{PendingAction, Service, StatefulList};
    use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
    use ratatui::{backend::TestBackend, Terminal};

    /// The menus press keys; this is what keeps them pressing ones that do
    /// something. The footer is where each page documents its keys, so a menu
    /// entry whose key the footer does not mention is one that has drifted.
    #[test]
    fn every_menu_key_is_one_its_page_documents() {
        for page in Page::ALL {
            let controls = crate::ui::page_controls(page);
            for item in page_actions(page) {
                assert!(
                    controls.contains(&format!("{} ", item.key_hint()))
                        || controls.contains(&format!("{}/", item.key_hint())),
                    "{page:?}'s menu offers {:?} ({}), which its footer does not document: {controls}",
                    item.label,
                    item.key_hint(),
                );
            }
        }
    }

    fn service(id: &str) -> Service {
        Service {
            id: id.to_string(),
            tag: format!("tag-{id}"),
            size_bytes: 0,
            total_size_bytes: None,
        }
    }

    fn on_services_page() -> App {
        let mut app = App::default();
        app.tabs.index = Page::ALL.iter().position(|page| *page == Page::Services).unwrap();
        app.services = StatefulList::with_items(vec![service("svc-1"), service("svc-2"), service("svc-3")]);
        app.services.next();
        app
    }

    fn draw(app: &mut App) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(120, 32)).unwrap();
        terminal.draw(|frame| crate::ui::render(app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..buffer.area.height)
            .map(|y| (0..buffer.area.width).map(|x| buffer.get(x, y).symbol()).collect())
            .collect()
    }

    fn mouse(app: &mut App, kind: MouseEventKind, column: u16, row: u16) {
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(crate::handler::handle_mouse_events(
            MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE },
            app,
        ))
        .unwrap();
    }

    /// Right-click the row showing `id`, the way an operator would.
    fn right_click_row(app: &mut App, id: &str) {
        let lines = draw(app);
        let row = lines.iter().position(|line| line.contains(id)).expect("row on screen") as u16;
        mouse(app, MouseEventKind::Down(MouseButton::Right), 40, row);
    }

    #[test]
    fn right_clicking_a_row_selects_it_and_lists_its_actions() {
        let mut app = on_services_page();

        right_click_row(&mut app, "svc-2");

        assert_eq!(app.input_mode, InputMode::ContextMenu);
        assert_eq!(app.services.selected().map(|s| s.id.as_str()), Some("svc-2"));
        let menu = app.context_menu.as_ref().unwrap();
        assert_eq!(menu.items, page_actions(Page::Services));
        let screen = draw(&mut app).join("\n");
        assert!(screen.contains("Execute"), "{screen}");
        assert!(screen.contains("Delete"), "{screen}");
    }

    /// Choosing an entry does what its key does, to the row that was clicked.
    #[test]
    fn clicking_an_entry_runs_it_on_that_row() {
        let mut app = on_services_page();
        right_click_row(&mut app, "svc-2");
        draw(&mut app);
        let delete = page_actions(Page::Services)
            .iter()
            .position(|item| item.label == "Delete")
            .unwrap();
        let area = app.context_menu.as_ref().unwrap().item_areas[delete];

        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), area.x + 2, area.y);

        assert!(app.context_menu.is_none());
        assert_eq!(app.input_mode, InputMode::Confirm);
        assert!(matches!(
            app.pending_action,
            Some(PendingAction::DeleteService { ref id, .. }) if id == "svc-2"
        ));
    }

    #[test]
    fn the_keyboard_works_the_menu_too() {
        let mut app = on_services_page();
        right_click_row(&mut app, "svc-3");
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(async {
            // Execute is the first entry; Enter chooses it.
            crate::handler::handle_key_events(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE), &mut app)
                .await
                .unwrap();
        });

        assert!(matches!(
            app.pending_action,
            Some(PendingAction::ExecuteService { ref id, .. }) if id == "svc-3"
        ));
    }

    #[test]
    fn a_click_outside_the_menu_dismisses_it_without_acting() {
        let mut app = on_services_page();
        right_click_row(&mut app, "svc-2");
        draw(&mut app);

        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), 0, 0);

        assert_eq!(app.input_mode, InputMode::Normal);
        assert!(app.context_menu.is_none());
        assert!(app.pending_action.is_none());
        assert_eq!(app.page(), Page::Services, "the click did not reach the tabs behind it");
    }

    #[test]
    fn nothing_under_the_pointer_opens_no_menu() {
        let mut app = on_services_page();
        draw(&mut app);
        // Row 0 is the group tabs, not a table row.
        mouse(&mut app, MouseEventKind::Down(MouseButton::Right), 40, 0);
        assert_eq!(app.input_mode, InputMode::Normal);

        // And a page with no per-row actions has no menu at all.
        app.tabs.index = Page::ALL.iter().position(|page| *page == Page::Overview).unwrap();
        app.open_context_menu(40, 10);
        assert_eq!(app.input_mode, InputMode::Normal);
    }
}
