//! Drawing the profile deck: one full-modal card for each profile, a character on the
//! left and its data on the right (docs/proposals/profile-deck.md, phase 3).
//!
//! The geometry is a pure function, `layout`, and the card is drawn into a `Buffer`
//! instead of a `Frame`. The first keeps the click zones of phase 4 and the drawing
//! in one place. The second lets phase 5 draw two cards into temporary buffers and
//! slide them.

use crate::app::App;
use crate::cell::{self, Profile};
use crate::profile_deck::{self, Persona, STAT_MAX};
use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Constraint, Layout, Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Widget, Wrap};
use ratatui::Frame;
use std::time::{Duration, Instant};

/// Smallest terminal the deck is drawn on. Below this the TUI shows the plain list,
/// which stays usable on any size.
pub const MIN_WIDTH: u16 = 80;
pub const MIN_HEIGHT: u16 = 26;

/// Largest the modal grows, so a very large terminal does not get a stretched card.
const MAX_WIDTH: u16 = 120;
const MAX_HEIGHT: u16 = 40;

const DECK_BG: Color = Color::Rgb(8, 6, 20);
const DECK_BG_SCAN: Color = Color::Rgb(13, 10, 28);
const PANEL_BG: Color = Color::Rgb(16, 11, 36);
const TEXT: Color = Color::Rgb(230, 230, 245);
const MUTED: Color = Color::Rgb(140, 140, 175);
const WARN: Color = Color::Rgb(255, 225, 0);
const GOOD: Color = Color::Rgb(57, 255, 20);

/// Where each part of the deck is. Everything a click can hit has a rectangle here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeckLayout {
    pub modal: Rect,
    pub inner: Rect,
    pub prev: Rect,
    pub next: Rect,
    /// The area of the card: character on the left, data on the right.
    pub card: Rect,
    /// One rectangle for each profile, in order.
    pub dots: Vec<Rect>,
    pub choose: Rect,
    pub skip: Rect,
}

/// The labels of the two buttons.
pub fn choose_label() -> &'static str {
    "[ ENTER · JACK IN ]"
}

pub fn skip_label(first_run: bool) -> &'static str {
    if first_run {
        "[ ESC · SKIP ]"
    } else {
        "[ ESC · BACK ]"
    }
}

fn width_of(text: &str) -> u16 {
    text.chars().count() as u16
}

/// The geometry of the deck on `area`, or `None` when the area is too small.
pub fn layout(area: Rect, count: usize, first_run: bool) -> Option<DeckLayout> {
    if area.width < MIN_WIDTH || area.height < MIN_HEIGHT || count == 0 {
        return None;
    }
    let width = (area.width - 2).min(MAX_WIDTH);
    let height = (area.height - 2).min(MAX_HEIGHT);
    let modal = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    let inner = Rect::new(modal.x + 1, modal.y + 1, modal.width - 2, modal.height - 2);

    // Two footer rows (dots, buttons) and one blank row above them.
    let body = Rect::new(inner.x, inner.y, inner.width, inner.height - 3);
    let dots_row = inner.y + inner.height - 2;
    let buttons_row = inner.y + inner.height - 1;

    let arrow_width = 3;
    let arrow_height = 3.min(body.height);
    let arrow_y = body.y + (body.height - arrow_height) / 2;
    let prev = Rect::new(body.x, arrow_y, arrow_width, arrow_height);
    let next = Rect::new(
        body.x + body.width - arrow_width,
        arrow_y,
        arrow_width,
        arrow_height,
    );
    let card = Rect::new(
        body.x + arrow_width + 1,
        body.y,
        body.width - 2 * (arrow_width + 1),
        body.height,
    );

    // The dots: one cell each, two apart, centred.
    let dots_width = (count as u16) * 2 - 1;
    let dots_x = inner.x + (inner.width.saturating_sub(dots_width)) / 2;
    let dots = (0..count as u16)
        .map(|index| Rect::new(dots_x + index * 2, dots_row, 1, 1))
        .collect();

    // The buttons: centred as a pair, with a gap between them.
    let choose_width = width_of(choose_label());
    let skip_width = width_of(skip_label(first_run));
    let gap = 3;
    let pair = choose_width + gap + skip_width;
    let buttons_x = inner.x + inner.width.saturating_sub(pair) / 2;
    let choose = Rect::new(buttons_x, buttons_row, choose_width, 1);
    let skip = Rect::new(buttons_x + choose_width + gap, buttons_row, skip_width, 1);

    Some(DeckLayout {
        modal,
        inner,
        prev,
        next,
        card,
        dots,
        choose,
        skip,
    })
}

/// How long one slide takes.
pub const SLIDE: Duration = Duration::from_millis(200);

/// How often the screen is redrawn while a slide runs: about 30 frames a second.
pub const FRAME: Duration = Duration::from_millis(33);

/// A card sliding out and the next one sliding in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeckSlide {
    /// Index of the profile that is leaving.
    pub from: usize,
    /// True when the new card comes in from the right (the user went forward).
    pub forward: bool,
    pub started: Instant,
}

impl DeckSlide {
    pub fn new(from: usize, forward: bool, started: Instant) -> Self {
        Self {
            from,
            forward,
            started,
        }
    }

    /// How far the slide has gone, from `0.0` to `1.0`, on a straight clock.
    pub fn progress(&self, now: Instant) -> f32 {
        let elapsed = now.saturating_duration_since(self.started);
        (elapsed.as_secs_f32() / SLIDE.as_secs_f32()).clamp(0.0, 1.0)
    }

    pub fn done(&self, now: Instant) -> bool {
        self.progress(now) >= 1.0
    }

    /// The position of the slide with easing: fast at the start, soft at the end,
    /// like a menu in a game.
    pub fn eased(&self, now: Instant) -> f32 {
        ease_out_cubic(self.progress(now))
    }
}

pub fn ease_out_cubic(t: f32) -> f32 {
    let t = t.clamp(0.0, 1.0);
    1.0 - (1.0 - t).powi(3)
}

/// What a click on the deck landed on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Hit {
    Prev,
    Next,
    /// The dot of the profile at this index.
    Dot(usize),
    Choose,
    Skip,
    /// Anywhere on the card: the same as the choose button.
    Card,
}

/// What `position` lands on, if anything. Buttons first, then the arrows, then the
/// dots, and last the card, so the small targets win over the big one.
///
/// A dot is one cell wide to draw but three wide to hit: nobody can click a single
/// cell reliably.
pub fn hit(layout: &DeckLayout, position: Position) -> Option<Hit> {
    if layout.choose.contains(position) {
        return Some(Hit::Choose);
    }
    if layout.skip.contains(position) {
        return Some(Hit::Skip);
    }
    if layout.prev.contains(position) {
        return Some(Hit::Prev);
    }
    if layout.next.contains(position) {
        return Some(Hit::Next);
    }
    for (index, dot) in layout.dots.iter().enumerate() {
        let target = Rect::new(dot.x.saturating_sub(1), dot.y, dot.width + 2, dot.height);
        if target.contains(position) {
            return Some(Hit::Dot(index));
        }
    }
    if layout.card.contains(position) {
        return Some(Hit::Card);
    }
    None
}

/// Draw the whole deck for the profile `app.cell.profile`.
pub fn draw(frame: &mut Frame, app: &App, layout: &DeckLayout) {
    draw_at(frame, app, layout, Instant::now());
}

/// `draw`, with the time given, so a test can look at any moment of a slide.
pub fn draw_at(frame: &mut Frame, app: &App, layout: &DeckLayout, now: Instant) {
    let profiles = cell::profiles();
    let index = app.cell.profile.min(profiles.len().saturating_sub(1));
    let Some(profile) = profiles.get(index) else {
        return;
    };
    let Some(persona) = profile_deck::persona(profile.id) else {
        return;
    };
    let accent = persona.palette.primary_color();

    frame.render_widget(Clear, layout.modal);
    let buffer = frame.buffer_mut();
    paint_background(buffer, layout.modal);

    let title = Line::from(vec![
        Span::styled(" ◢◤ ", Style::default().fg(accent)),
        Span::styled(
            "SELECT YOUR RIG",
            Style::default().fg(accent).add_modifier(Modifier::BOLD),
        ),
        Span::styled(" ◥◣ ", Style::default().fg(accent)),
    ]);
    let counter = Line::from(Span::styled(
        format!(" {}/{} ", index + 1, profiles.len()),
        Style::default().fg(accent).add_modifier(Modifier::BOLD),
    ));
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Double)
        .border_style(Style::default().fg(accent))
        .title(title)
        .title(counter.alignment(Alignment::Right))
        .render(layout.modal, buffer);

    let slide = app.deck_anim.filter(|slide| !slide.done(now));
    match slide.and_then(|slide| profiles.get(slide.from).map(|from| (slide, from))) {
        Some((slide, from)) => render_sliding(buffer, layout.card, from, profile, app, slide, now),
        None => render_card(buffer, layout.card, profile, persona, app),
    }
    render_arrow(buffer, layout.prev, "◀", accent);
    render_arrow(buffer, layout.next, "▶", accent);

    for (position, dot) in layout.dots.iter().enumerate() {
        let (glyph, style) = if position == index {
            (
                "●",
                Style::default().fg(accent).add_modifier(Modifier::BOLD),
            )
        } else {
            ("○", Style::default().fg(MUTED))
        };
        Span::styled(glyph, style).render(*dot, buffer);
    }

    // The choose button is filled, the skip button is outlined in warning colour and
    // bold: skipping must be as easy to see as choosing.
    Span::styled(
        choose_label(),
        Style::default()
            .fg(DECK_BG)
            .bg(accent)
            .add_modifier(Modifier::BOLD),
    )
    .render(layout.choose, buffer);
    Span::styled(
        skip_label(app.profile_first_run),
        Style::default().fg(WARN).add_modifier(Modifier::BOLD),
    )
    .render(layout.skip, buffer);
}

/// Two cards in motion: the one leaving and the one arriving, each drawn into a
/// buffer of its own and copied into the screen with a horizontal offset, cut at the
/// edge of the card area so that nothing slides over the arrows or the frame.
fn render_sliding(
    buffer: &mut Buffer,
    area: Rect,
    from: &'static Profile,
    to: &'static Profile,
    app: &App,
    slide: DeckSlide,
    now: Instant,
) {
    let (Some(from_persona), Some(to_persona)) =
        (profile_deck::persona(from.id), profile_deck::persona(to.id))
    else {
        return;
    };
    let width = area.width as i32;
    let travelled = (slide.eased(now) * width as f32).round() as i32;
    // Going forward the old card leaves to the left and the new one comes from the
    // right. Going back it is the other way round.
    let direction = if slide.forward { -1 } else { 1 };
    let leaving = direction * travelled;
    let arriving = leaving - direction * width;

    let mut old = Buffer::empty(area);
    render_card(&mut old, area, from, from_persona, app);
    let mut new = Buffer::empty(area);
    render_card(&mut new, area, to, to_persona, app);
    blit(&old, buffer, area, leaving);
    blit(&new, buffer, area, arriving);
}

/// Copy `source` into `target` moved `offset` columns, keeping inside `area`.
///
/// A cell that was never drawn (an empty symbol on the terminal's own background) is
/// not copied, so the scanlines of the deck show through the gaps of a card.
fn blit(source: &Buffer, target: &mut Buffer, area: Rect, offset: i32) {
    for y in area.y..area.y + area.height {
        for x in area.x..area.x + area.width {
            let cell = source.get(x, y);
            if cell.symbol() == " " && cell.bg == Color::Reset {
                continue;
            }
            let moved = x as i32 + offset;
            if moved < area.x as i32 || moved >= (area.x + area.width) as i32 {
                continue;
            }
            *target.get_mut(moved as u16, y) = cell.clone();
        }
    }
}

/// The dark background with a faint line on every other row: the scanlines.
fn paint_background(buffer: &mut Buffer, area: Rect) {
    for y in area.y..area.y + area.height {
        let background = if (y - area.y) % 2 == 0 {
            DECK_BG
        } else {
            DECK_BG_SCAN
        };
        for x in area.x..area.x + area.width {
            let cell = buffer.get_mut(x, y);
            cell.set_symbol(" ");
            cell.set_style(Style::default().fg(TEXT).bg(background));
        }
    }
}

fn render_arrow(buffer: &mut Buffer, area: Rect, glyph: &'static str, accent: Color) {
    let middle = Rect::new(area.x, area.y + area.height / 2, area.width, 1);
    Paragraph::new(Span::styled(
        glyph,
        Style::default().fg(accent).add_modifier(Modifier::BOLD),
    ))
    .alignment(Alignment::Center)
    .render(middle, buffer);
}

/// One card: the character in the left 40 %, the data in the rest.
///
/// Takes a `Buffer` and an area so the same card can be drawn into a temporary
/// buffer (phase 5).
pub fn render_card(
    buffer: &mut Buffer,
    area: Rect,
    profile: &'static Profile,
    persona: &Persona,
    app: &App,
) {
    let columns =
        Layout::horizontal([Constraint::Percentage(40), Constraint::Percentage(60)]).split(area);
    render_character(buffer, columns[0], persona);
    render_data(buffer, columns[1], profile, persona, app);
}

fn render_character(buffer: &mut Buffer, area: Rect, persona: &Persona) {
    let accent = persona.palette.primary_color();
    // Two lines under the sprite: the platform, and the name.
    let footer = 3;
    let scale = if area.width >= (profile_deck::SPRITE_WIDTH * 2 + 2) as u16
        && area.height >= (profile_deck::SPRITE_HEIGHT + footer as usize) as u16
    {
        2
    } else {
        1
    };
    let sprite = profile_deck::sprite_lines_scaled(persona, scale);
    let sprite_height = sprite.len() as u16;
    let sprite_width = (profile_deck::SPRITE_WIDTH * scale) as u16;

    let total = sprite_height + footer;
    let top = area.y + area.height.saturating_sub(total) / 2;
    let left = area.x + area.width.saturating_sub(sprite_width) / 2;
    let sprite_area = Rect::new(
        left,
        top,
        sprite_width.min(area.width),
        sprite_height.min(area.height),
    );
    Paragraph::new(sprite).render(sprite_area, buffer);

    let platform_width = (sprite_width + 4).min(area.width);
    let platform_x = area.x + (area.width - platform_width) / 2;
    let platform_y = top + sprite_height;
    if platform_y < area.y + area.height {
        Paragraph::new(Span::styled(
            "▀".repeat(platform_width as usize),
            Style::default().fg(accent),
        ))
        .render(Rect::new(platform_x, platform_y, platform_width, 1), buffer);
    }
    let name_y = platform_y + 1;
    if name_y < area.y + area.height {
        Paragraph::new(vec![
            Line::from(Span::styled(
                persona.codename,
                Style::default().fg(accent).add_modifier(Modifier::BOLD),
            )),
            Line::from(Span::styled(persona.class, Style::default().fg(MUTED))),
        ])
        .alignment(Alignment::Center)
        .render(
            Rect::new(
                area.x,
                name_y,
                area.width,
                2.min(area.y + area.height - name_y),
            ),
            buffer,
        );
    }
}

/// The bar of one attribute: filled blocks in the accent, empty ones dim.
fn bar(value: u8, accent: Color) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    for position in 0..STAT_MAX {
        if position < value {
            spans.push(Span::styled("█ ", Style::default().fg(accent)));
        } else {
            spans.push(Span::styled(
                "▒ ",
                Style::default().fg(Color::Rgb(60, 55, 85)),
            ));
        }
    }
    spans
}

/// Remove blank lines, last first, until `lines` fits in `height`.
fn fit_lines(mut lines: Vec<Line<'static>>, height: usize) -> Vec<Line<'static>> {
    while lines.len() > height {
        let blank = lines.iter().rposition(|line| line.width() == 0);
        match blank {
            Some(position) => {
                lines.remove(position);
            }
            None => break,
        }
    }
    lines
}

fn render_data(
    buffer: &mut Buffer,
    area: Rect,
    profile: &'static Profile,
    persona: &Persona,
    app: &App,
) {
    let accent = persona.palette.primary_color();
    let report = cell::report(profile, app.config_document.as_ref());
    let current = report.deviations.is_empty();

    let mut title = vec![Span::styled(
        format!(" {} ", profile.label),
        Style::default().fg(accent).add_modifier(Modifier::BOLD),
    )];
    if current {
        title.push(Span::styled(
            "◆ CURRENT ",
            Style::default().fg(GOOD).add_modifier(Modifier::BOLD),
        ));
    }
    // The text is laid out first, so the panel can be as tall as its content and
    // sit in the middle of the card instead of leaving a tall empty box.
    let text_width = area.width.saturating_sub(4);
    if text_width < 4 || area.height < 5 {
        return;
    }
    let stats = profile_deck::stats(profile);
    let mut lines: Vec<Line<'static>> = Vec::new();
    lines.push(Line::from(Span::styled(
        persona.tagline,
        Style::default().fg(accent).add_modifier(Modifier::ITALIC),
    )));
    lines.push(Line::from(""));

    // The blurb is wrapped by hand, so the number of lines is known before the
    // panel is filled.
    for text in wrap(profile.blurb, text_width as usize) {
        lines.push(Line::from(Span::styled(text, Style::default().fg(TEXT))));
    }
    lines.push(Line::from(""));

    for (label, value) in stats.bars() {
        let mut spans = vec![Span::styled(
            format!("{label:<10}"),
            Style::default().fg(MUTED),
        )];
        spans.extend(bar(value, accent));
        spans.push(Span::styled(
            format!(" {value}/{STAT_MAX}"),
            Style::default().fg(MUTED),
        ));
        lines.push(Line::from(spans));
    }
    lines.push(Line::from(""));

    for text in profile_deck::traits(profile) {
        lines.push(Line::from(vec![
            Span::styled("▸ ", Style::default().fg(accent)),
            Span::styled(text, Style::default().fg(TEXT)),
        ]));
    }
    lines.push(Line::from(""));

    lines.push(if current {
        Line::from(Span::styled(
            "You are here: nothing would change",
            Style::default().fg(GOOD).add_modifier(Modifier::BOLD),
        ))
    } else {
        let count = report.deviations.len();
        Line::from(Span::styled(
            format!(
                "{count} {} will change",
                if count == 1 { "key" } else { "keys" }
            ),
            Style::default().fg(WARN).add_modifier(Modifier::BOLD),
        ))
    });

    let lines = fit_lines(lines, area.height.saturating_sub(2) as usize);
    let panel_height = (lines.len() as u16 + 2).min(area.height);
    let panel = Rect::new(
        area.x,
        area.y + (area.height - panel_height) / 2,
        area.width,
        panel_height,
    );
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Plain)
        .border_style(Style::default().fg(accent))
        .style(Style::default().fg(TEXT).bg(PANEL_BG))
        .title(Line::from(title));
    let inner = block.inner(panel);
    block.render(panel, buffer);
    // A margin inside the panel.
    let inner = Rect::new(
        inner.x + 1,
        inner.y,
        inner.width.saturating_sub(2),
        inner.height,
    );
    Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .render(inner, buffer);
}

/// Break `text` at spaces into lines of at most `width` characters.
fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        if !current.is_empty() && current.chars().count() + 1 + word.chars().count() > width {
            lines.push(std::mem::take(&mut current));
        }
        if !current.is_empty() {
            current.push(' ');
        }
        current.push_str(word);
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn contains(outer: Rect, inner: Rect) -> bool {
        inner.x >= outer.x
            && inner.y >= outer.y
            && inner.x + inner.width <= outer.x + outer.width
            && inner.y + inner.height <= outer.y + outer.height
    }

    fn overlaps(one: Rect, other: Rect) -> bool {
        one.x < other.x + other.width
            && other.x < one.x + one.width
            && one.y < other.y + other.height
            && other.y < one.y + one.height
    }

    fn screen(width: u16, height: u16, index: usize) -> (Buffer, App) {
        let mut app = App::default();
        app.cell.profile = index;
        app.profile_first_run = true;
        app.config_document = None;
        let area = Rect::new(0, 0, width, height);
        let layout = layout(area, cell::profiles().len(), true).expect("fits");
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| draw(frame, &app, &layout)).unwrap();
        (terminal.backend().buffer().clone(), app)
    }

    fn text_of(buffer: &Buffer) -> String {
        let mut text = String::new();
        for y in 0..buffer.area.height {
            for x in 0..buffer.area.width {
                text.push_str(buffer.get(x, y).symbol());
            }
            text.push('\n');
        }
        text
    }

    #[test]
    fn a_small_terminal_gets_no_deck() {
        let count = cell::profiles().len();
        assert!(layout(Rect::new(0, 0, MIN_WIDTH - 1, 40), count, true).is_none());
        assert!(layout(Rect::new(0, 0, 200, MIN_HEIGHT - 1), count, true).is_none());
        assert!(layout(Rect::new(0, 0, MIN_WIDTH, MIN_HEIGHT), count, true).is_some());
    }

    #[test]
    fn every_zone_is_inside_the_modal_and_none_overlap() {
        let count = cell::profiles().len();
        for (width, height) in [(80, 26), (100, 30), (120, 40), (200, 60)] {
            let area = Rect::new(0, 0, width, height);
            let layout = layout(area, count, true).unwrap();
            assert!(contains(area, layout.modal), "{width}x{height}");
            let mut zones = vec![
                layout.prev,
                layout.next,
                layout.card,
                layout.choose,
                layout.skip,
            ];
            zones.extend(layout.dots.iter().copied());
            for zone in &zones {
                assert!(contains(layout.inner, *zone), "{width}x{height} {zone:?}");
            }
            for (index, one) in zones.iter().enumerate() {
                for other in &zones[index + 1..] {
                    assert!(
                        !overlaps(*one, *other),
                        "{width}x{height} {one:?} {other:?}"
                    );
                }
            }
            assert_eq!(layout.dots.len(), count);
        }
    }

    #[test]
    fn a_click_lands_on_the_part_it_is_over() {
        let layout = layout(Rect::new(0, 0, 100, 32), 5, true).unwrap();
        let at = |rect: Rect| Position::new(rect.x + rect.width / 2, rect.y + rect.height / 2);
        assert_eq!(hit(&layout, at(layout.choose)), Some(Hit::Choose));
        assert_eq!(hit(&layout, at(layout.skip)), Some(Hit::Skip));
        assert_eq!(hit(&layout, at(layout.prev)), Some(Hit::Prev));
        assert_eq!(hit(&layout, at(layout.next)), Some(Hit::Next));
        assert_eq!(hit(&layout, at(layout.card)), Some(Hit::Card));
        for (index, dot) in layout.dots.iter().enumerate() {
            assert_eq!(hit(&layout, at(*dot)), Some(Hit::Dot(index)));
        }
        // The cells beside the first and the last dot hit them too.
        let first = layout.dots[0];
        let last = layout.dots[layout.dots.len() - 1];
        assert_eq!(
            hit(&layout, Position::new(first.x - 1, first.y)),
            Some(Hit::Dot(0))
        );
        assert_eq!(
            hit(&layout, Position::new(last.x + 1, last.y)),
            Some(Hit::Dot(layout.dots.len() - 1))
        );
        // The border and the space outside are not parts of the deck.
        assert_eq!(hit(&layout, Position::new(0, 0)), None);
        assert_eq!(
            hit(&layout, Position::new(layout.modal.x, layout.modal.y)),
            None
        );
    }

    fn draw_slide(slide: Option<DeckSlide>, index: usize, now: Instant) -> Buffer {
        let mut app = App::default();
        app.cell.profile = index;
        app.profile_first_run = true;
        app.config_document = None;
        app.deck_anim = slide;
        let layout = layout(Rect::new(0, 0, 100, 32), cell::profiles().len(), true).unwrap();
        let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
        terminal
            .draw(|frame| draw_at(frame, &app, &layout, now))
            .unwrap();
        terminal.backend().buffer().clone()
    }

    fn card_text(buffer: &Buffer) -> String {
        let layout = layout(Rect::new(0, 0, 100, 32), cell::profiles().len(), true).unwrap();
        let mut text = String::new();
        for y in layout.card.y..layout.card.y + layout.card.height {
            for x in layout.card.x..layout.card.x + layout.card.width {
                text.push_str(buffer.get(x, y).symbol());
            }
            text.push('\n');
        }
        text
    }

    #[test]
    fn a_slide_runs_from_nothing_to_done_in_the_time_it_has() {
        let start = Instant::now();
        let slide = DeckSlide::new(0, true, start);
        assert_eq!(slide.progress(start), 0.0);
        assert!((slide.progress(start + SLIDE / 2) - 0.5).abs() < 0.001);
        assert!(!slide.done(start + SLIDE / 2));
        assert!(slide.done(start + SLIDE));
        assert!(slide.done(start + SLIDE * 10), "it stops at the end");
        // A clock that reads before the start does not go negative.
        assert_eq!(slide.progress(start - Duration::from_millis(5)), 0.0);
    }

    #[test]
    fn the_easing_starts_fast_and_stays_between_zero_and_one() {
        assert_eq!(ease_out_cubic(0.0), 0.0);
        assert_eq!(ease_out_cubic(1.0), 1.0);
        assert!(
            ease_out_cubic(0.5) > 0.5,
            "ease-out is ahead of a straight line"
        );
        let mut last = 0.0;
        for step in 0..=100 {
            let value = ease_out_cubic(step as f32 / 100.0);
            assert!((0.0..=1.0).contains(&value));
            assert!(value >= last, "never goes back");
            last = value;
        }
        assert_eq!(ease_out_cubic(-3.0), 0.0);
        assert_eq!(ease_out_cubic(9.0), 1.0);
    }

    /// At its first frame a slide looks like the card it leaves, and at its last
    /// like the card it arrives at: no jump when the animation starts or ends.
    #[test]
    fn a_slide_starts_on_the_old_card_and_ends_on_the_new_one() {
        let start = Instant::now();
        for forward in [true, false] {
            let slide = DeckSlide::new(1, forward, start);
            let first = card_text(&draw_slide(Some(slide), 2, start));
            let old = card_text(&draw_slide(None, 1, start));
            assert_eq!(first, old, "forward = {forward}");

            let last = card_text(&draw_slide(Some(slide), 2, start + SLIDE));
            let new = card_text(&draw_slide(None, 2, start));
            assert_eq!(last, new, "forward = {forward}");
        }
    }

    #[test]
    fn halfway_both_cards_are_in_view_and_neither_is_whole() {
        let start = Instant::now();
        let slide = DeckSlide::new(1, true, start);
        let middle = card_text(&draw_slide(Some(slide), 2, start + SLIDE / 2));
        assert_ne!(middle, card_text(&draw_slide(None, 1, start)));
        assert_ne!(middle, card_text(&draw_slide(None, 2, start)));
    }

    /// Going forward the new card comes from the right, going back from the left.
    #[test]
    fn the_new_card_comes_from_the_side_the_user_is_going_to() {
        let start = Instant::now();
        let layout = layout(Rect::new(0, 0, 100, 32), cell::profiles().len(), true).unwrap();
        // Early in the slide, count the drawn cells in each half of the card area.
        let drawn = |buffer: &Buffer, left: bool| {
            let half = layout.card.width / 2;
            let (from, to) = if left {
                (layout.card.x, layout.card.x + half)
            } else {
                (layout.card.x + half, layout.card.x + layout.card.width)
            };
            let mut count = 0;
            for y in layout.card.y..layout.card.y + layout.card.height {
                for x in from..to {
                    if buffer.get(x, y).symbol() != " " {
                        count += 1;
                    }
                }
            }
            count
        };
        let at = start + SLIDE / 4;
        let forward = draw_slide(Some(DeckSlide::new(1, true, start)), 2, at);
        let backward = draw_slide(Some(DeckSlide::new(1, false, start)), 0, at);
        let rest = draw_slide(None, 1, start);
        // The resting card has its data on the right; a forward slide has moved it
        // left and so shows less on the right than at rest, a backward one more of
        // the left.
        assert!(drawn(&forward, false) != drawn(&rest, false));
        assert!(drawn(&backward, true) != drawn(&rest, true));
    }

    /// The slide never paints outside the card area: the arrows and the frame stay.
    #[test]
    fn a_slide_does_not_touch_the_arrows_or_the_frame() {
        let start = Instant::now();
        let layout = layout(Rect::new(0, 0, 100, 32), cell::profiles().len(), true).unwrap();
        let slide = DeckSlide::new(0, true, start);
        let moving = draw_slide(Some(slide), 1, start + SLIDE / 3);
        let still = draw_slide(None, 1, start);
        for zone in [layout.prev, layout.next] {
            for y in zone.y..zone.y + zone.height {
                for x in zone.x..zone.x + zone.width {
                    assert_eq!(moving.get(x, y), still.get(x, y), "({x},{y})");
                }
            }
        }
        for x in layout.modal.x..layout.modal.x + layout.modal.width {
            assert_eq!(moving.get(x, layout.modal.y), still.get(x, layout.modal.y));
        }
    }

    #[test]
    fn the_modal_is_centred() {
        let area = Rect::new(0, 0, 100, 30);
        let layout = layout(area, 5, true).unwrap();
        assert_eq!(layout.modal.x, 1);
        assert_eq!(layout.modal.y, 1);
        assert_eq!(layout.modal.width, 98);
        assert_eq!(layout.modal.height, 28);
    }

    #[test]
    fn each_card_shows_its_character_and_its_data() {
        for (index, profile) in cell::profiles().iter().enumerate() {
            let persona = profile_deck::persona(profile.id).unwrap();
            let (buffer, _) = screen(100, 32, index);
            let text = text_of(&buffer);
            assert!(text.contains("SELECT YOUR RIG"), "{}", profile.id);
            assert!(text.contains(profile.label), "{}: {text}", profile.id);
            assert!(text.contains(persona.codename), "{}: {text}", profile.id);
            assert!(text.contains("EXPOSURE"), "{}", profile.id);
            assert!(text.contains("ISOLATION"), "{}", profile.id);
            assert!(text.contains(&format!("{}/{}", index + 1, cell::profiles().len())));
        }
    }

    /// Skipping must be as easy to see as choosing: both buttons are on screen, at
    /// the smallest size the deck is drawn on.
    #[test]
    fn both_buttons_show_at_the_smallest_size() {
        let (buffer, _) = screen(MIN_WIDTH, MIN_HEIGHT, 0);
        let text = text_of(&buffer);
        assert!(text.contains(choose_label()), "{text}");
        assert!(text.contains(skip_label(true)), "{text}");
    }

    #[test]
    fn the_key_traits_and_the_change_count_show_at_the_smallest_size() {
        let (buffer, _) = screen(MIN_WIDTH, MIN_HEIGHT, 1);
        let text = text_of(&buffer);
        assert!(text.contains("will change"), "{text}");
        assert!(text.contains("▸ Tunnel only"), "{text}");
    }

    /// The popup paints every cell it covers.
    #[test]
    fn the_deck_repaints_every_cell_of_the_modal() {
        let (buffer, _) = screen(100, 32, 2);
        let area = Rect::new(0, 0, 100, 32);
        let layout = layout(area, 5, true).unwrap();
        for y in layout.modal.y..layout.modal.y + layout.modal.height {
            for x in layout.modal.x..layout.modal.x + layout.modal.width {
                assert_ne!(buffer.get(x, y).bg, Color::Reset, "({x},{y})");
            }
        }
    }

    #[test]
    fn the_current_profile_is_marked() {
        // With no config at all, no profile matches completely.
        let (buffer, _) = screen(100, 32, 0);
        assert!(!text_of(&buffer).contains("CURRENT"));
    }

    #[test]
    fn the_dots_mark_the_open_card() {
        let (buffer, _) = screen(100, 32, 3);
        let layout = layout(Rect::new(0, 0, 100, 32), 5, true).unwrap();
        for (position, dot) in layout.dots.iter().enumerate() {
            let symbol = buffer.get(dot.x, dot.y).symbol();
            assert_eq!(symbol, if position == 3 { "●" } else { "○" });
        }
    }

    #[test]
    fn a_large_terminal_gets_a_larger_character() {
        let small = profile_deck::sprite_lines_scaled(&profile_deck::personas()[0], 1);
        let large = profile_deck::sprite_lines_scaled(&profile_deck::personas()[0], 2);
        assert_eq!(large.len(), small.len() * 2);
        assert_eq!(large[0].width(), small[0].width() * 2);
    }

    #[test]
    fn wrapping_never_passes_the_width() {
        for profile in cell::profiles() {
            for width in [10, 20, 33, 60] {
                for line in wrap(profile.blurb, width) {
                    assert!(
                        line.chars().count()
                            <= width.max(
                                line.split(' ')
                                    .map(|w| w.chars().count())
                                    .max()
                                    .unwrap_or(0)
                            )
                    );
                }
            }
        }
    }
}
