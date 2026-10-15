//! The arithmetic every page needs to draw into whatever size the terminal happens
//! to be (issue #453): the smallest screen worth laying out at all, text cut to a
//! width with an ellipsis rather than mid-glyph, table columns that give way by
//! importance, and rows of a page that give way by importance.
//!
//! One place for it because the failure it prevents is the same everywhere: a
//! layout written for 80×24 that, at 30 columns, crushes every column to two
//! characters or lets a `Layout` solver squeeze the tab bar to nothing.

use ratatui::layout::{Constraint, Flex, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Wrap};
use ratatui::Frame;
use unicode_width::UnicodeWidthChar;
use unicode_width::UnicodeWidthStr;

/// The narrowest terminal the console lays out. Below it, a notice.
///
/// Forty columns is what the compact tab rows need (`app::tab_row_titles`) with
/// room left for a table's most important column and a popup's text; anything
/// narrower is a strip no page can say anything useful in.
pub const MIN_WIDTH: u16 = 40;

/// The shortest terminal the console lays out: the bordered group row (3), the
/// page row (1), one table showing one row -- its two borders, the header and the
/// blank line under it, and the row (5) -- and the two-line footer (2).
pub const MIN_HEIGHT: u16 = 11;

/// Whether `area` is below the size [`MIN_WIDTH`]×[`MIN_HEIGHT`] the pages are laid
/// out for.
pub fn too_small(area: Rect) -> bool {
    area.width < MIN_WIDTH || area.height < MIN_HEIGHT
}

/// What is drawn instead of a page when the terminal is [`too_small`]: what size it
/// is, what size it needs, and that the keys still work. Fitted to whatever room
/// there is, down to a single cell, and centred when there is room to centre.
pub fn draw_too_small(frame: &mut Frame, area: Rect, style: Style, accent: Style) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let lines = vec![
        Line::from(Span::styled("Terminal too small", accent.bold())),
        Line::from(Span::styled(
            format!("{}×{} — needs {MIN_WIDTH}×{MIN_HEIGHT}", area.width, area.height),
            style,
        )),
        Line::from(Span::styled("Enlarge the window", style)),
        Line::from(Span::styled("q quits", style)),
    ];
    let paragraph = Paragraph::new(lines)
        .alignment(ratatui::layout::Alignment::Center)
        .wrap(Wrap { trim: true });
    let height = (paragraph.line_count(area.width) as u16).min(area.height);
    let top = area.y + (area.height - height) / 2;
    frame.render_widget(paragraph, Rect { y: top, height, ..area });
}

/// Columns `text` occupies on a terminal: a CJK character or an emoji is two, a
/// combining mark none.
pub fn display_width(text: &str) -> usize {
    UnicodeWidthStr::width(text)
}

/// `text` cut to at most `width` columns, ending in `…` when anything was cut.
///
/// Measured in terminal columns rather than characters, so a line of CJK text or
/// emoji does not overrun its cell by the width of every wide glyph in it, and cut
/// between characters rather than inside one.
pub fn truncate_ellipsis(text: &str, width: usize) -> String {
    if display_width(text) <= width {
        return text.to_string();
    }
    if width == 0 {
        return String::new();
    }
    let budget = width - 1; // the ellipsis
    let mut used = 0;
    let mut out = String::new();
    for glyph in text.chars() {
        let glyph_width = glyph.width().unwrap_or(0);
        if used + glyph_width > budget {
            break;
        }
        used += glyph_width;
        out.push(glyph);
    }
    out.push('…');
    out
}

/// Whether `text` holds a glyph two columns wide (CJK, most emoji).
pub fn has_wide_glyph(text: &str) -> bool {
    text.chars().any(|glyph| glyph.width().unwrap_or(0) > 1)
}

/// Whether any span of `lines` holds a glyph two columns wide.
pub fn lines_have_wide_glyph(lines: &[Line<'_>]) -> bool {
    lines
        .iter()
        .any(|line| line.spans.iter().any(|span| has_wide_glyph(&span.content)))
}

/// The area to give a wrapping `Paragraph` so that none of it is drawn outside
/// `area`.
///
/// ratatui 0.26's word wrapper can start a two-column glyph in the last column of
/// its area, and the glyph's second half is then written one cell past the area --
/// over the pane's right border, or into the next pane (issue #453; pinned by
/// `wrap_guard_keeps_wide_glyphs_inside`). Wrapping one column narrower keeps that
/// half inside. Only when the text has such a glyph (`wide`), so every other line
/// wraps exactly where it always did.
pub fn wrap_guard(area: Rect, wide: bool) -> Rect {
    if wide && area.width > 1 {
        Rect { width: area.width - 1, ..area }
    } else {
        area
    }
}

/// A row of key hints joined by `separator`, kept to `width` columns by dropping
/// whole hints from the end and marking the cut with `…`.
///
/// Whole hints rather than a hard cut: `e exec` is a key that does not exist, and
/// a footer is read as a list of keys. The first hint is kept even when it alone
/// does not fit, cut with an ellipsis, so the line never goes blank.
pub fn fit_hints(text: &str, separator: &str, width: usize) -> String {
    if display_width(text) <= width {
        return text.to_string();
    }
    let hints: Vec<&str> = text.split(separator).collect();
    let more = format!("{separator}…");
    let mut kept = String::new();
    for hint in &hints {
        let candidate = if kept.is_empty() {
            hint.to_string()
        } else {
            format!("{kept}{separator}{hint}")
        };
        if display_width(&candidate) + display_width(&more) > width {
            break;
        }
        kept = candidate;
    }
    if kept.is_empty() {
        return truncate_ellipsis(hints.first().copied().unwrap_or(""), width);
    }
    format!("{kept}{more}")
}

/// A `width`×`height` box centred in `area`, never larger than `area` and never
/// outside it -- what a fixed-size popup is drawn into on a terminal smaller than
/// the popup.
pub fn centered_rect_clamped(width: u16, height: u16, area: Rect) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect {
        x: area.x + (area.width - width) / 2,
        y: area.y + (area.height - height) / 2,
        width,
        height,
    }
}

/// `rect` moved (not shrunk, unless it is bigger) so that all of it lies inside
/// `area`: a menu opened at the right edge of the screen opens leftwards instead of
/// off it.
pub fn keep_inside(rect: Rect, area: Rect) -> Rect {
    let width = rect.width.min(area.width);
    let height = rect.height.min(area.height);
    let x = rect.x.clamp(area.x, area.right().saturating_sub(width));
    let y = rect.y.clamp(area.y, area.bottom().saturating_sub(height));
    Rect { x, y, width, height }
}

/// Heights for a page's stacked sections when the terminal may be too short for all
/// of them: `(min, want)` per section, granted in `priority` order (index into
/// `sections`, most important first).
///
/// Every section first gets its `min` while that still fits, highest priority first;
/// a section whose `min` does not fit gets 0 and is not drawn, rather than drawn as a
/// border with nothing inside it. What is left then grows sections toward `want`, in
/// the same order. Section order on screen is the caller's; only who gives way is
/// decided here.
pub fn allocate_heights(available: u16, sections: &[(u16, u16)], priority: &[usize]) -> Vec<u16> {
    let mut heights = vec![0u16; sections.len()];
    let mut left = available;
    for &index in priority {
        let (min, _) = sections[index];
        if min <= left {
            heights[index] = min;
            left -= min;
        }
    }
    for &index in priority {
        let (min, want) = sections[index];
        if heights[index] == 0 && min > 0 {
            continue;
        }
        let grow = want.saturating_sub(heights[index]).min(left);
        heights[index] += grow;
        left -= grow;
    }
    heights
}

/// `area` cut into rows of `heights`, top to bottom; a 0 is an empty `Rect` at the
/// spot the section would have been.
pub fn stack(area: Rect, heights: &[u16]) -> Vec<Rect> {
    let mut y = area.y;
    heights
        .iter()
        .map(|height| {
            let height = (*height).min(area.bottom().saturating_sub(y));
            let rect = Rect { x: area.x, y, width: area.width, height };
            y += height;
            rect
        })
        .collect()
}

/// One table column: how it would like to be laid out, how narrow it can get before
/// it stops being worth showing, and how much it matters. Priority 0 is kept longest.
#[derive(Debug, Clone, Copy)]
pub struct Column<'a> {
    pub header: &'a str,
    pub constraint: Constraint,
    pub min: u16,
    pub priority: u8,
}

impl<'a> Column<'a> {
    pub const fn new(header: &'a str, constraint: Constraint, min: u16, priority: u8) -> Self {
        Self { header, constraint, min, priority }
    }
}

/// Which columns fit in `available` columns, as indexes in their original order.
///
/// Drops the least important column (highest `priorities` value; the rightmost of a
/// tie) until the rest fit at their minimum widths with `spacing` between them. The
/// most important column always stays, so a table never draws with no column.
pub fn visible_columns(mins: &[u16], priorities: &[u8], available: u16, spacing: u16) -> Vec<usize> {
    let mut keep: Vec<usize> = (0..mins.len()).collect();
    let needed = |keep: &[usize]| -> u32 {
        keep.iter().map(|index| mins[*index] as u32).sum::<u32>()
            + spacing as u32 * keep.len().saturating_sub(1) as u32
    };
    while keep.len() > 1 && needed(&keep) > available as u32 {
        let (position, _) = keep
            .iter()
            .enumerate()
            .max_by_key(|(position, index)| (priorities[**index], *position))
            .expect("non-empty");
        keep.remove(position);
    }
    keep
}

/// The columns a table keeps at a width, and how wide each is drawn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FittedColumns {
    /// Indexes into the table's full column list, in order.
    pub keep: Vec<usize>,
    /// Drawn width of each kept column, parallel to `keep`.
    pub widths: Vec<u16>,
    /// Offset of each kept column from the left of the column area, parallel to
    /// `keep`. What a hit test needs to find a column the way it was drawn.
    pub offsets: Vec<u16>,
}

impl FittedColumns {
    /// The drawn `(offset, width)` of the full table's column `index`, if it is shown.
    pub fn column(&self, index: usize) -> Option<(u16, u16)> {
        let position = self.keep.iter().position(|kept| *kept == index)?;
        Some((self.offsets[position], self.widths[position]))
    }
}

/// Lay out `columns` in `available` cells with ratatui's own one-cell spacing.
///
/// When every kept column gets at least its `min` from the same `Layout` a `Table`
/// would run, those widths are used unchanged -- so a table that fits draws exactly
/// as it always has. Otherwise each kept column starts at its `min` and the spare
/// cells go, most important column first, toward what its constraint asked for, then
/// to the flexible columns.
pub fn fit_columns(columns: &[Column<'_>], available: u16) -> FittedColumns {
    const SPACING: u16 = 1;
    let mins: Vec<u16> = columns.iter().map(|column| column.min).collect();
    let priorities: Vec<u8> = columns.iter().map(|column| column.priority).collect();
    let keep = visible_columns(&mins, &priorities, available, SPACING);
    let constraints: Vec<Constraint> = keep.iter().map(|index| columns[*index].constraint).collect();
    let natural = Layout::horizontal(constraints.clone())
        .flex(Flex::Start)
        .spacing(SPACING)
        .split(Rect::new(0, 0, available, 1));
    let natural_fits = natural
        .iter()
        .zip(&keep)
        .all(|(rect, index)| rect.width >= columns[*index].min.min(available));
    let widths: Vec<u16> = if natural_fits {
        natural.iter().map(|rect| rect.width).collect()
    } else {
        let mut widths: Vec<u16> = keep.iter().map(|index| columns[*index].min).collect();
        let used: u16 = widths.iter().sum::<u16>() + SPACING * keep.len().saturating_sub(1) as u16;
        let mut spare = available.saturating_sub(used);
        if keep.len() == 1 {
            widths[0] = available;
            spare = 0;
        }
        let mut order: Vec<usize> = (0..keep.len()).collect();
        order.sort_by_key(|position| (columns[keep[*position]].priority, *position));
        for &position in &order {
            let preferred = match columns[keep[position]].constraint {
                Constraint::Length(n) | Constraint::Min(n) | Constraint::Max(n) => n,
                Constraint::Percentage(p) => (available as u32 * p as u32 / 100) as u16,
                _ => widths[position],
            };
            let grow = preferred.saturating_sub(widths[position]).min(spare);
            widths[position] += grow;
            spare -= grow;
        }
        if spare > 0 {
            let flexible = (0..keep.len())
                .rev()
                .find(|position| {
                    matches!(
                        columns[keep[*position]].constraint,
                        Constraint::Min(_) | Constraint::Fill(_) | Constraint::Percentage(_)
                    )
                })
                .unwrap_or(keep.len() - 1);
            widths[flexible] += spare;
        }
        widths
    };
    let mut offsets = Vec::with_capacity(widths.len());
    let mut x = 0u16;
    for width in &widths {
        offsets.push(x);
        x = x.saturating_add(*width).saturating_add(SPACING);
    }
    FittedColumns { keep, widths, offsets }
}

/// The items of `items` at `keep`'s indexes, in that order.
pub fn pick<T>(items: Vec<T>, keep: &[usize]) -> Vec<T> {
    let mut slots: Vec<Option<T>> = items.into_iter().map(Some).collect();
    keep.iter().filter_map(|index| slots.get_mut(*index).and_then(Option::take)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncation_counts_columns_not_characters() {
        assert_eq!(truncate_ellipsis("abcdef", 6), "abcdef");
        assert_eq!(truncate_ellipsis("abcdef", 4), "abc…");
        assert_eq!(truncate_ellipsis("abcdef", 1), "…");
        assert_eq!(truncate_ellipsis("abcdef", 0), "");
        // Two columns each: four glyphs are eight columns.
        assert_eq!(truncate_ellipsis("東京大阪", 8), "東京大阪");
        assert_eq!(truncate_ellipsis("東京大阪", 5), "東京…");
        // A wide glyph that would straddle the budget is left out whole, not split.
        assert_eq!(truncate_ellipsis("東京大阪", 4), "東…");
        assert_eq!(display_width(&truncate_ellipsis("a🦀b🦀c", 4)), 4);
        assert_eq!(truncate_ellipsis("a🦀b🦀c", 4), "a🦀…");
        for width in 0..12 {
            let cut = truncate_ellipsis("x東🦀y京z🦀", width);
            assert!(display_width(&cut) <= width, "{cut:?} at {width}");
        }
    }

    #[test]
    fn wrap_guard_keeps_wide_glyphs_inside() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        let body = "a body long enough to wrap at most widths, with 東京 and 🦀 in it";
        let leaks = |guard: bool| -> Vec<u16> {
            (8u16..60)
                .filter(|width| {
                    let mut terminal = Terminal::new(TestBackend::new(width + 1, 20)).unwrap();
                    terminal
                        .draw(|frame| {
                            // A column of markers just past the area: a spilled glyph's
                            // second half overwrites one with a blank.
                            let marks = vec![Line::from("X"); 20];
                            frame.render_widget(Paragraph::new(marks), Rect::new(*width, 0, 1, 20));
                            let area = Rect::new(0, 0, *width, 20);
                            let area = wrap_guard(area, guard && has_wide_glyph(body));
                            frame.render_widget(Paragraph::new(body).wrap(Wrap { trim: false }), area);
                        })
                        .unwrap();
                    let buffer = terminal.backend().buffer();
                    (0..20).any(|row| buffer.get(*width, row).symbol() != "X")
                })
                .collect()
        };
        // The bug this works around is real: unguarded, a glyph spills past the area
        // at some widths. If a ratatui upgrade fixes it, this half fails and the
        // guard can go.
        assert!(!leaks(false).is_empty());
        assert_eq!(leaks(true), Vec::<u16>::new());
        assert_eq!(wrap_guard(Rect::new(1, 2, 10, 3), false), Rect::new(1, 2, 10, 3));
        assert_eq!(wrap_guard(Rect::new(1, 2, 10, 3), true), Rect::new(1, 2, 9, 3));
        assert_eq!(wrap_guard(Rect::new(1, 2, 1, 3), true), Rect::new(1, 2, 1, 3));
    }

    #[test]
    fn hints_are_dropped_whole_from_the_end() {
        let text = "a one  •  b two  •  c three";
        assert_eq!(fit_hints(text, "  •  ", 40), text);
        assert_eq!(fit_hints(text, "  •  ", 21), "a one  •  b two  •  …");
        assert_eq!(fit_hints(text, "  •  ", 20), "a one  •  …");
        assert_eq!(fit_hints(text, "  •  ", 12), "a one  •  …");
        // Not even the first fits beside the marker: it is cut instead.
        assert_eq!(fit_hints(text, "  •  ", 4), "a o…");
        for width in 0..30 {
            assert!(display_width(&fit_hints(text, "  •  ", width)) <= width);
        }
    }

    #[test]
    fn a_popup_never_leaves_the_screen() {
        let area = Rect::new(2, 3, 10, 4);
        assert_eq!(centered_rect_clamped(4, 2, area), Rect::new(5, 4, 4, 2));
        assert_eq!(centered_rect_clamped(40, 20, area), area);
        assert_eq!(centered_rect_clamped(5, 5, Rect::ZERO), Rect::ZERO);
        let menu = keep_inside(Rect::new(9, 6, 6, 3), area);
        assert_eq!(menu, Rect::new(6, 4, 6, 3));
        assert_eq!(keep_inside(Rect::new(0, 0, 50, 50), area), area);
    }

    #[test]
    fn sections_give_way_in_priority_order() {
        // A table (min 5, wants 20) above a detail card (min 3, wants 16).
        let sections = [(5, 20), (3, 16)];
        assert_eq!(allocate_heights(40, &sections, &[0, 1]), vec![20, 16]);
        assert_eq!(allocate_heights(18, &sections, &[0, 1]), vec![15, 3]);
        // Not enough for the card's minimum beside the table's: the card goes.
        assert_eq!(allocate_heights(7, &sections, &[0, 1]), vec![7, 0]);
        assert_eq!(allocate_heights(2, &sections, &[0, 1]), vec![0, 0]);
        assert_eq!(allocate_heights(0, &sections, &[0, 1]), vec![0, 0]);
        let rects = stack(Rect::new(0, 1, 10, 9), &[3, 0, 6]);
        assert_eq!(rects[0], Rect::new(0, 1, 10, 3));
        assert_eq!(rects[1].height, 0);
        assert_eq!(rects[2], Rect::new(0, 4, 10, 6));
    }

    #[test]
    fn the_least_important_column_goes_first() {
        let mins = [10, 6, 6, 6];
        let priorities = [0, 3, 1, 2];
        assert_eq!(visible_columns(&mins, &priorities, 100, 1), vec![0, 1, 2, 3]);
        assert_eq!(visible_columns(&mins, &priorities, 31, 1), vec![0, 1, 2, 3]);
        assert_eq!(visible_columns(&mins, &priorities, 30, 1), vec![0, 2, 3]);
        assert_eq!(visible_columns(&mins, &priorities, 17, 1), vec![0, 2]);
        assert_eq!(visible_columns(&mins, &priorities, 3, 1), vec![0]);
        assert_eq!(visible_columns(&mins, &priorities, 0, 1), vec![0]);
    }

    #[test]
    fn a_table_that_fits_is_laid_out_exactly_as_ratatui_would() {
        let columns = [
            Column::new("A", Constraint::Length(10), 4, 0),
            Column::new("B", Constraint::Min(20), 6, 1),
            Column::new("C", Constraint::Length(8), 4, 2),
        ];
        let fitted = fit_columns(&columns, 60);
        assert_eq!(fitted.keep, vec![0, 1, 2]);
        assert_eq!(fitted.widths, vec![10, 40, 8]);
        assert_eq!(fitted.offsets, vec![0, 11, 52]);
        // Short: everything at its minimum first, then the most important grows.
        let fitted = fit_columns(&columns, 20);
        assert_eq!(fitted.keep, vec![0, 1, 2]);
        assert_eq!(fitted.widths, vec![8, 6, 4]);
        // Shorter: C goes, B keeps its minimum.
        let fitted = fit_columns(&columns, 12);
        assert_eq!(fitted.keep, vec![0, 1]);
        assert_eq!(fitted.widths, vec![5, 6]);
        assert_eq!(fitted.column(1), Some((6, 6)));
        assert_eq!(fitted.column(2), None);
        // One column left takes the whole width, however little that is.
        let fitted = fit_columns(&columns, 3);
        assert_eq!(fitted.keep, vec![0]);
        assert_eq!(fitted.widths, vec![3]);
        for available in 0..80 {
            let fitted = fit_columns(&columns, available);
            let used: u16 = fitted.widths.iter().sum::<u16>() + fitted.widths.len().saturating_sub(1) as u16;
            assert!(used <= available.max(fitted.widths[0]), "{fitted:?} at {available}");
        }
    }

    #[test]
    fn picking_keeps_order() {
        assert_eq!(pick(vec!["a", "b", "c", "d"], &[0, 2, 3]), vec!["a", "c", "d"]);
        assert_eq!(pick(vec![1, 2], &[5]), Vec::<i32>::new());
    }
}
