//! The DOCS page: this installation's own `docs/` folder, read inside the console.
//!
//! An index of every Markdown file on the left, the selected one rendered on the
//! right. Read-only and local: files are read when they are selected, never on a
//! frame, and nothing here touches the network.
//!
//! Rendering is our own, over `pulldown-cmark`'s event stream, rather than a
//! Markdown widget crate: the page has to wrap to whatever width the pane got,
//! remember where each heading and link landed after wrapping (for `#anchor` links,
//! clicks and search), and draw in the console's theme -- and each of those is a
//! few lines against the events and a fight against somebody else's widget.

use crate::app::{App, InputMode, Page};
use crate::ui::{accent, muted, section_block, selected_style, text_colour, warn};
use pulldown_cmark::{
    Alignment as ColumnAlignment, CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag,
    TagEnd,
};
use ratatui::layout::Position;
use ratatui::prelude::*;
use ratatui::widgets::block::{Position as TitlePosition, Title};
use ratatui::widgets::{Paragraph, Wrap};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Component, Path, PathBuf};
use std::time::SystemTime;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// Points the page at a docs folder explicitly, for an installation laid out in a
/// way none of the guesses in [`locate_docs_dir`] finds.
pub const DOCS_DIR_ENV: &str = "NODO_DOCS_DIR";

/// Deepest subfolder the index descends into. `docs/` is two levels deep today;
/// the limit is there so a symlink loop is a short index rather than a hang.
const MAX_DEPTH: usize = 6;

/// Lines the mouse wheel scrolls a page by.
const WHEEL_LINES: isize = 3;

// ---------------------------------------------------------------------------
// Finding the folder
// ---------------------------------------------------------------------------

/// Where `docs/` is, or every place that was looked in.
///
/// In order: `$NODO_DOCS_DIR`; the installation root the rest of the console
/// resolves `config.yaml` against (`Paths::root`, which is where `docs/KyA.md` is
/// read from too); then upward from the running binary, then from the working
/// directory. The last two are what find the folder for a prebuilt binary, whose
/// compiled-in root is the CI checkout it was built in, not this node: `nodo tui`
/// runs it from `<root>/src/commands/tui/target/release/`.
///
/// The upward walks only accept a folder holding `KyA.md`, the one document every
/// nodo installation has, so a stray `~/docs` is not mistaken for this one. The
/// explicit variable is taken at its word.
pub fn locate_docs_dir(
    explicit: Option<PathBuf>,
    compiled_root: &Path,
    executable: Option<PathBuf>,
    working_dir: Option<PathBuf>,
) -> Result<PathBuf, Vec<PathBuf>> {
    let mut looked = Vec::new();
    if let Some(dir) = explicit {
        if dir.is_dir() {
            return Ok(dir);
        }
        looked.push(dir);
    }
    let mut candidates = vec![compiled_root.join("docs")];
    for start in [executable, working_dir].into_iter().flatten() {
        candidates.extend(start.ancestors().map(|ancestor| ancestor.join("docs")));
    }
    for candidate in candidates {
        if looked.contains(&candidate) {
            continue;
        }
        if candidate.join("KyA.md").is_file() {
            return Ok(candidate);
        }
        looked.push(candidate);
    }
    Err(looked)
}

/// [`locate_docs_dir`] with this process's own environment.
fn discover(compiled_root: &Path) -> Result<PathBuf, Vec<PathBuf>> {
    locate_docs_dir(
        std::env::var_os(DOCS_DIR_ENV)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from),
        compiled_root,
        std::env::current_exe().ok(),
        std::env::current_dir().ok(),
    )
}

// ---------------------------------------------------------------------------
// The index
// ---------------------------------------------------------------------------

/// One row of the index: a subfolder heading, or a document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IndexRow {
    Folder {
        /// `proposals/`, relative to `docs/`.
        name: String,
        depth: usize,
    },
    File {
        path: PathBuf,
        /// `proposals/78-follow-up-review.md`, relative to `docs/`.
        relative: String,
        /// The first `# ` heading, or the file name when there is none.
        title: String,
        depth: usize,
    },
}

impl IndexRow {
    pub fn is_file(&self) -> bool {
        matches!(self, IndexRow::File { .. })
    }

    fn path(&self) -> Option<&Path> {
        match self {
            IndexRow::File { path, .. } => Some(path),
            IndexRow::Folder { .. } => None,
        }
    }
}

fn is_markdown(path: &Path) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| {
            extension.eq_ignore_ascii_case("md") || extension.eq_ignore_ascii_case("markdown")
        })
        .unwrap_or(false)
}

/// README first, then INDEX, then everything else alphabetically -- the order a
/// reader wants to meet a folder in, not the order `ls` happens to give.
fn file_order(name: &str) -> (u8, String) {
    let lower = name.to_lowercase();
    let stem = lower.rsplit_once('.').map(|(stem, _)| stem).unwrap_or(&lower);
    let rank = match stem {
        "readme" => 0,
        "index" => 1,
        _ => 2,
    };
    (rank, lower)
}

/// Every Markdown file under `root`, folder by folder: a folder's own files first,
/// then each subfolder under a heading of its own. Folders with no Markdown
/// anywhere below them (`docs/skill/` holds a script too) are left out rather than
/// drawn as empty headings.
pub fn build_index(root: &Path) -> Vec<IndexRow> {
    let mut rows = Vec::new();
    walk(root, "", 0, &mut rows);
    rows
}

fn walk(dir: &Path, prefix: &str, depth: usize, rows: &mut Vec<IndexRow>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let mut files = Vec::new();
    let mut folders = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') {
            continue;
        }
        let path = entry.path();
        if path.is_dir() {
            folders.push((name, path));
        } else if is_markdown(&path) {
            files.push((name, path));
        }
    }
    files.sort_by_key(|(name, _)| file_order(name));
    folders.sort_by_key(|(name, _)| name.to_lowercase());

    for (name, path) in files {
        let relative = format!("{prefix}{name}");
        let title = read_title(&path).unwrap_or_else(|| name.clone());
        rows.push(IndexRow::File { path, relative, title, depth });
    }
    if depth >= MAX_DEPTH {
        return;
    }
    for (name, path) in folders {
        let folder = format!("{prefix}{name}/");
        let mut below = Vec::new();
        walk(&path, &folder, depth + 1, &mut below);
        if below.iter().any(IndexRow::is_file) {
            rows.push(IndexRow::Folder { name: folder, depth });
            rows.extend(below);
        }
    }
}

/// The document's title, from its head only: the index is built from every file
/// and should not have to read all of PACKING.md to name it.
fn read_title(path: &Path) -> Option<String> {
    let file = fs::File::open(path).ok()?;
    let head: Vec<String> = BufReader::new(file)
        .lines()
        .take(200)
        .map_while(Result::ok)
        .collect();
    document_title(&head.join("\n"))
}

/// The first level-one heading, `# Title` or `Title` over `===`, skipping YAML
/// front matter and fenced code (where a `#` is a shell comment, not a title).
pub fn document_title(text: &str) -> Option<String> {
    let lines: Vec<&str> = text.lines().collect();
    let mut index = 0;
    if lines.first().map(|line| line.trim() == "---").unwrap_or(false) {
        index = 1;
        while index < lines.len() && !matches!(lines[index].trim(), "---" | "...") {
            index += 1;
        }
        index += 1;
    }
    let mut fence: Option<&str> = None;
    while index < lines.len() {
        let line = lines[index].trim_end();
        let trimmed = line.trim_start();
        if let Some(open) = fence {
            if trimmed.starts_with(open) {
                fence = None;
            }
        } else if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            fence = Some(&trimmed[..3]);
        } else if let Some(title) = trimmed.strip_prefix("# ") {
            return Some(clean_title(title));
        } else if !trimmed.is_empty()
            && lines
                .get(index + 1)
                .map(|next| {
                    let next = next.trim();
                    !next.is_empty() && next.chars().all(|c| c == '=')
                })
                .unwrap_or(false)
        {
            return Some(clean_title(trimmed));
        }
        index += 1;
    }
    None
}

fn clean_title(title: &str) -> String {
    title
        .trim()
        .trim_end_matches('#')
        .trim()
        .replace(['`', '*'], "")
}

// ---------------------------------------------------------------------------
// Markdown -> terminal lines
// ---------------------------------------------------------------------------

/// A document laid out for one width.
#[derive(Debug, Clone, Default)]
pub struct Rendered {
    pub lines: Vec<Line<'static>>,
    /// Each line's text, for search.
    pub plain: Vec<String>,
    /// GitHub-style heading slug -> the line the heading starts on, so
    /// `CONFIG.md#applying-a-change` can land where it points.
    pub anchors: HashMap<String, usize>,
    /// Every link, in reading order, and where it was drawn.
    pub links: Vec<LinkSpot>,
}

/// Where one link (or one wrapped piece of it) landed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkSpot {
    pub line: usize,
    /// Terminal columns, `[start, end)`, from the left edge of the text area.
    pub columns: (u16, u16),
    /// Character offsets into the line, `[start, end)`, for highlighting.
    pub chars: (usize, usize),
    pub target: String,
}

/// A run of text in one style, and the link it belongs to if any.
#[derive(Debug, Clone, PartialEq)]
struct Seg {
    text: String,
    style: Style,
    link: Option<usize>,
}

impl Seg {
    fn new(text: impl Into<String>, style: Style) -> Self {
        Self { text: text.into(), style, link: None }
    }

    fn width(&self) -> usize {
        self.text.chars().map(cell_width).sum()
    }
}

fn code_colour() -> Color {
    crate::theme::current().series[2]
}

fn is_external(target: &str) -> bool {
    target.contains("://") || target.starts_with("mailto:")
}

/// A heading's anchor, the way GitHub makes it: lower case, punctuation dropped,
/// spaces to hyphens. `## Applying a change` -> `applying-a-change`.
pub fn slug(text: &str) -> String {
    text.trim()
        .to_lowercase()
        .chars()
        .filter_map(|c| match c {
            ' ' => Some('-'),
            c if c.is_alphanumeric() || c == '-' || c == '_' => Some(c),
            _ => None,
        })
        .collect()
}

enum Container {
    Quote,
    List { next: Option<u64> },
    /// A list item: its marker until the first line of it has been drawn, then
    /// that many spaces, so continuation lines hang under the text.
    Item { marker: Option<String>, width: usize },
}

struct Table {
    alignments: Vec<ColumnAlignment>,
    rows: Vec<Vec<Vec<Seg>>>,
    header_rows: usize,
}

struct Renderer {
    width: usize,
    out: Rendered,
    containers: Vec<Container>,
    inline: Vec<Seg>,
    bold: usize,
    italic: usize,
    strike: usize,
    link: Option<(usize, String, usize)>,
    link_targets: Vec<String>,
    image: bool,
    heading: Option<HeadingLevel>,
    code: Option<(String, String)>,
    table: Option<Table>,
    need_blank: bool,
    slugs: HashMap<String, usize>,
}

/// Lay `source` out for a pane `width` columns wide.
pub fn render_markdown(source: &str, width: u16) -> Rendered {
    let mut renderer = Renderer {
        width: (width as usize).max(20),
        out: Rendered::default(),
        containers: Vec::new(),
        inline: Vec::new(),
        bold: 0,
        italic: 0,
        strike: 0,
        link: None,
        link_targets: Vec::new(),
        image: false,
        heading: None,
        code: None,
        table: None,
        need_blank: false,
        slugs: HashMap::new(),
    };
    let options = Options::ENABLE_TABLES
        | Options::ENABLE_STRIKETHROUGH
        | Options::ENABLE_TASKLISTS
        | Options::ENABLE_FOOTNOTES
        | Options::ENABLE_YAML_STYLE_METADATA_BLOCKS;
    for event in Parser::new_ext(source, options) {
        renderer.event(event);
    }
    renderer.flush_text(true);
    renderer.out
}

impl Renderer {
    fn style(&self) -> Style {
        let mut style = Style::default().fg(text_colour());
        if self.bold > 0 {
            style = style.add_modifier(Modifier::BOLD);
        }
        if self.italic > 0 {
            style = style.add_modifier(Modifier::ITALIC);
        }
        if self.strike > 0 {
            style = style.add_modifier(Modifier::CROSSED_OUT);
        }
        if self.image {
            style = style.fg(muted());
        }
        if self.link.is_some() {
            style = style.fg(accent()).add_modifier(Modifier::UNDERLINED);
        }
        style
    }

    fn push(&mut self, text: impl Into<String>, style: Style) {
        let link = self.link.as_ref().map(|(id, _, _)| *id);
        self.inline.push(Seg { text: text.into(), style, link });
    }

    fn event(&mut self, event: Event) {
        if let Some((_, buffer)) = self.code.as_mut() {
            match event {
                Event::Text(text) => buffer.push_str(&text),
                Event::End(TagEnd::CodeBlock) | Event::End(TagEnd::MetadataBlock(_)) => {
                    let (label, buffer) = self.code.take().unwrap_or_default();
                    self.code_block(&label, &buffer);
                }
                _ => {}
            }
            return;
        }
        match event {
            Event::Start(tag) => self.start(tag),
            Event::End(tag) => self.end(tag),
            Event::Text(text) => {
                let style = self.style();
                self.push(text.into_string(), style);
            }
            Event::Code(text) | Event::InlineMath(text) | Event::DisplayMath(text) => {
                let mut style = self.style().fg(code_colour());
                if self.link.is_some() {
                    style = style.add_modifier(Modifier::UNDERLINED);
                }
                self.push(text.into_string(), style);
            }
            Event::Html(html) | Event::InlineHtml(html) => {
                let html = html.trim_end_matches('\n');
                // Comments are notes to the next editor, not to the reader.
                if !html.trim_start().starts_with("<!--") {
                    self.push(html.to_string(), Style::default().fg(muted()));
                    if html.contains('\n') || self.inline.len() == 1 {
                        self.push("\n", Style::default());
                    }
                }
            }
            Event::SoftBreak => {
                let style = self.style();
                self.push(" ", style);
            }
            Event::HardBreak => self.push("\n", Style::default()),
            Event::Rule => {
                self.flush_text(true);
                self.block_start();
                let width = self.width.saturating_sub(self.prefix_width());
                let rule = vec![Seg::new("─".repeat(width), Style::default().fg(muted()))];
                self.emit(rule);
                self.need_blank = true;
            }
            Event::TaskListMarker(done) => {
                let (mark, style) = if done {
                    ("[x] ", Style::default().fg(accent()))
                } else {
                    ("[ ] ", Style::default().fg(muted()))
                };
                self.push(mark, style);
            }
            Event::FootnoteReference(name) => {
                self.push(format!("[^{name}]"), Style::default().fg(muted()));
            }
        }
    }

    fn start(&mut self, tag: Tag) {
        match tag {
            Tag::Paragraph => {}
            Tag::Heading { level, .. } => {
                self.flush_text(true);
                self.heading = Some(level);
            }
            Tag::BlockQuote(_) => {
                self.flush_text(true);
                self.block_start();
                self.containers.push(Container::Quote);
            }
            Tag::CodeBlock(kind) => {
                self.flush_text(true);
                let label = match kind {
                    CodeBlockKind::Fenced(info) => {
                        info.split_whitespace().next().unwrap_or("").to_string()
                    }
                    CodeBlockKind::Indented => String::new(),
                };
                self.code = Some((label, String::new()));
            }
            Tag::MetadataBlock(_) => {
                self.flush_text(true);
                self.code = Some(("front matter".to_string(), String::new()));
            }
            Tag::List(start) => {
                // Text directly inside a tight item, before a nested list.
                self.flush_text(false);
                if !self.in_list() {
                    self.block_start();
                }
                self.containers.push(Container::List { next: start });
            }
            Tag::Item => {
                self.flush_text(false);
                let depth = self
                    .containers
                    .iter()
                    .filter(|container| matches!(container, Container::List { .. }))
                    .count();
                let marker = match self.containers.last_mut() {
                    Some(Container::List { next: Some(number) }) => {
                        let marker = format!("{number}. ");
                        *number += 1;
                        marker
                    }
                    _ => match depth {
                        0 | 1 => "• ",
                        2 => "◦ ",
                        _ => "▪ ",
                    }
                    .to_string(),
                };
                let width = marker.width();
                self.containers.push(Container::Item { marker: Some(marker), width });
            }
            Tag::FootnoteDefinition(name) => {
                self.flush_text(true);
                self.push(format!("[^{name}]: "), Style::default().fg(muted()));
            }
            Tag::Table(alignments) => {
                self.flush_text(true);
                self.table = Some(Table { alignments, rows: Vec::new(), header_rows: 0 });
            }
            Tag::TableHead | Tag::TableRow => {
                if let Some(table) = self.table.as_mut() {
                    table.rows.push(Vec::new());
                }
            }
            Tag::TableCell => self.inline.clear(),
            Tag::Emphasis => self.italic += 1,
            Tag::Strong => self.bold += 1,
            Tag::Strikethrough => self.strike += 1,
            Tag::Link { dest_url, .. } => {
                let id = self.link_targets.len();
                self.link_targets.push(dest_url.to_string());
                self.link = Some((id, dest_url.to_string(), self.inline.len()));
            }
            Tag::Image { .. } => {
                self.image = true;
                self.push("[image: ", Style::default().fg(muted()));
            }
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph | TagEnd::FootnoteDefinition | TagEnd::HtmlBlock => {
                self.flush_text(true)
            }
            TagEnd::Heading(_) => self.flush_heading(),
            TagEnd::BlockQuote(_) => {
                self.flush_text(true);
                self.containers.pop();
                self.need_blank = true;
            }
            TagEnd::List(_) => {
                self.flush_text(false);
                self.containers.pop();
                if !self.in_list() {
                    self.need_blank = true;
                }
            }
            TagEnd::Item => {
                self.flush_text(false);
                self.containers.pop();
            }
            TagEnd::TableCell => {
                let cell = std::mem::take(&mut self.inline);
                if let Some(row) = self.table.as_mut().and_then(|table| table.rows.last_mut()) {
                    row.push(cell);
                }
            }
            TagEnd::TableHead => {
                if let Some(table) = self.table.as_mut() {
                    table.header_rows = table.rows.len();
                }
            }
            TagEnd::Table => {
                if let Some(table) = self.table.take() {
                    self.emit_table(table);
                }
            }
            TagEnd::Emphasis => self.italic = self.italic.saturating_sub(1),
            TagEnd::Strong => self.bold = self.bold.saturating_sub(1),
            TagEnd::Strikethrough => self.strike = self.strike.saturating_sub(1),
            TagEnd::Link => {
                if let Some((id, target, start)) = self.link.take() {
                    let text: String =
                        self.inline[start..].iter().map(|seg| seg.text.as_str()).collect();
                    // An external address is shown as well as its text: it cannot be
                    // followed from here, so the reader needs to see where it goes.
                    // `<https://…>` autolinks already are their address.
                    if is_external(&target) && text.trim() != target.trim_start_matches("mailto:") {
                        self.inline.push(Seg {
                            text: format!(" <{target}>"),
                            style: Style::default().fg(muted()),
                            link: Some(id),
                        });
                    }
                }
            }
            TagEnd::Image => {
                self.push("]", Style::default().fg(muted()));
                self.image = false;
            }
            _ => {}
        }
    }

    fn in_list(&self) -> bool {
        self.containers
            .iter()
            .any(|container| matches!(container, Container::List { .. }))
    }

    /// The blank line between two blocks, when one is owed.
    fn block_start(&mut self) {
        if self.need_blank && !self.out.lines.is_empty() {
            let prefix = self.prefix(false);
            self.emit_raw(prefix, Vec::new());
        }
        self.need_blank = false;
    }

    fn prefix_width(&self) -> usize {
        self.containers
            .iter()
            .map(|container| match container {
                Container::Quote => 2,
                Container::List { .. } => 0,
                Container::Item { width, .. } => *width,
            })
            .sum()
    }

    /// What goes in front of a line: a bar per quote, a marker or its width in
    /// spaces per list item. `first` consumes any marker not yet drawn.
    fn prefix(&mut self, first: bool) -> Vec<Seg> {
        let mut prefix = Vec::new();
        for container in self.containers.iter_mut() {
            match container {
                Container::Quote => prefix.push(Seg::new("▍ ", Style::default().fg(muted()))),
                Container::List { .. } => {}
                Container::Item { marker, width } => {
                    let text = if first { marker.take() } else { None }
                        .unwrap_or_else(|| " ".repeat(*width));
                    prefix.push(Seg::new(text, Style::default().fg(accent())));
                }
            }
        }
        prefix
    }

    /// The paragraph (or tight list item's text) collected so far, wrapped.
    /// `blank_after` is false for tight list text, which must not open a gap
    /// before the next item.
    fn flush_text(&mut self, blank_after: bool) {
        if self.inline.iter().all(|seg| seg.text.trim().is_empty()) {
            self.inline.clear();
            return;
        }
        let segs = std::mem::take(&mut self.inline);
        self.block_start();
        let available = self.width.saturating_sub(self.prefix_width()).max(8);
        for line in wrap(&segs, available) {
            self.emit(line);
        }
        self.need_blank = blank_after;
    }

    fn flush_heading(&mut self) {
        let level = self.heading.take().unwrap_or(HeadingLevel::H6);
        let mut segs = std::mem::take(&mut self.inline);
        let plain: String = segs.iter().map(|seg| seg.text.as_str()).collect();
        let (colour, modifier) = match level {
            HeadingLevel::H1 | HeadingLevel::H2 => (accent(), Modifier::BOLD),
            HeadingLevel::H3 => (text_colour(), Modifier::BOLD),
            _ => (text_colour(), Modifier::BOLD | Modifier::ITALIC),
        };
        for seg in segs.iter_mut() {
            if seg.style.fg == Some(text_colour()) {
                seg.style = seg.style.fg(colour);
            }
            seg.style = seg.style.add_modifier(modifier);
        }
        if !self.out.lines.is_empty() {
            self.need_blank = true;
        }
        self.block_start();
        let base = slug(&plain);
        let count = self.slugs.entry(base.clone()).or_insert(0);
        let anchor = if *count == 0 { base } else { format!("{base}-{count}") };
        *count += 1;
        self.out.anchors.entry(anchor).or_insert(self.out.lines.len());

        let available = self.width.saturating_sub(self.prefix_width()).max(8);
        let lines = wrap(&segs, available);
        let widest = lines
            .iter()
            .map(|line| line.iter().map(Seg::width).sum::<usize>())
            .max()
            .unwrap_or(0);
        for line in lines {
            self.emit(line);
        }
        let rule = match level {
            HeadingLevel::H1 => Some(("━", accent())),
            HeadingLevel::H2 => Some(("─", muted())),
            _ => None,
        };
        if let Some((glyph, colour)) = rule {
            self.emit(vec![Seg::new(glyph.repeat(widest), Style::default().fg(colour))]);
        }
        self.need_blank = true;
    }

    fn code_block(&mut self, label: &str, buffer: &str) {
        self.block_start();
        let gutter = Style::default().fg(muted());
        if !label.is_empty() {
            self.emit(vec![Seg::new(format!("▏{label}"), gutter)]);
        }
        let available = self.width.saturating_sub(self.prefix_width() + 2).max(8);
        for line in buffer.trim_end_matches('\n').split('\n') {
            let line = line.replace('\t', "    ");
            for chunk in split_to_width(&line, available) {
                self.emit(vec![
                    Seg::new("▏ ", gutter),
                    Seg::new(chunk, Style::default().fg(code_colour())),
                ]);
            }
        }
        self.need_blank = true;
    }

    fn emit_table(&mut self, table: Table) {
        self.block_start();
        let columns = table.rows.iter().map(Vec::len).max().unwrap_or(0);
        if columns == 0 {
            return;
        }
        let separator = " │ ";
        let available = self.width.saturating_sub(self.prefix_width());
        let budget = available.saturating_sub(separator.width() * (columns - 1));
        let mut widths = vec![1usize; columns];
        for row in &table.rows {
            for (column, cell) in row.iter().enumerate() {
                let natural: usize = cell.iter().map(Seg::width).sum();
                widths[column] = widths[column].max(natural);
            }
        }
        // Narrow the widest column one cell at a time until the table fits, so a
        // short column keeps its natural width and a long one wraps instead.
        while widths.iter().sum::<usize>() > budget {
            let (widest, width) = widths
                .iter()
                .copied()
                .enumerate()
                .max_by_key(|(_, width)| *width)
                .unwrap_or((0, 0));
            if width <= 1 {
                break;
            }
            widths[widest] -= 1;
        }

        let border = Style::default().fg(muted());
        for (index, row) in table.rows.iter().enumerate() {
            let header = index < table.header_rows;
            let cells: Vec<Vec<Vec<Seg>>> = (0..columns)
                .map(|column| {
                    let mut cell = row.get(column).cloned().unwrap_or_default();
                    if header {
                        for seg in cell.iter_mut() {
                            seg.style = seg.style.add_modifier(Modifier::BOLD);
                        }
                    }
                    wrap(&cell, widths[column])
                })
                .collect();
            let height = cells.iter().map(Vec::len).max().unwrap_or(1).max(1);
            for line in 0..height {
                let mut segs = Vec::new();
                for (column, cell) in cells.iter().enumerate() {
                    if column > 0 {
                        segs.push(Seg::new(separator, border));
                    }
                    let content = cell.get(line).cloned().unwrap_or_default();
                    let used: usize = content.iter().map(Seg::width).sum();
                    let gap = widths[column].saturating_sub(used);
                    let alignment = table
                        .alignments
                        .get(column)
                        .copied()
                        .unwrap_or(ColumnAlignment::None);
                    let (before, after) = match alignment {
                        ColumnAlignment::Right => (gap, 0),
                        ColumnAlignment::Center => (gap / 2, gap - gap / 2),
                        _ => (0, gap),
                    };
                    // Trailing padding on the last column is only trailing spaces.
                    let after = if column + 1 == columns { 0 } else { after };
                    if before > 0 {
                        segs.push(Seg::new(" ".repeat(before), Style::default()));
                    }
                    segs.extend(content);
                    if after > 0 {
                        segs.push(Seg::new(" ".repeat(after), Style::default()));
                    }
                }
                // Only a table with more columns than the pane has room for even
                // one character of each overflows; its right edge is cut instead.
                self.emit(clip(segs, available));
            }
            if header && index + 1 == table.header_rows {
                let rule = widths
                    .iter()
                    .map(|width| "─".repeat(*width))
                    .collect::<Vec<_>>()
                    .join("─┼─");
                self.emit(clip(vec![Seg::new(rule, border)], available));
            }
        }
        self.need_blank = true;
    }

    fn emit(&mut self, content: Vec<Seg>) {
        let prefix = self.prefix(true);
        self.emit_raw(prefix, content);
    }

    fn emit_raw(&mut self, prefix: Vec<Seg>, content: Vec<Seg>) {
        let line_number = self.out.lines.len();
        let mut column = 0usize;
        let mut chars = 0usize;
        let mut spans = Vec::new();
        let mut plain = String::new();
        for seg in prefix.into_iter().chain(merge(content)) {
            let width = seg.width();
            let count = seg.text.chars().count();
            if let Some(id) = seg.link {
                let target = self.link_targets.get(id).cloned().unwrap_or_default();
                match self.out.links.last_mut() {
                    Some(spot)
                        if spot.line == line_number
                            && spot.target == target
                            && spot.columns.1 as usize == column =>
                    {
                        spot.columns.1 = (column + width) as u16;
                        spot.chars.1 = chars + count;
                    }
                    _ => self.out.links.push(LinkSpot {
                        line: line_number,
                        columns: (column as u16, (column + width) as u16),
                        chars: (chars, chars + count),
                        target,
                    }),
                }
            }
            column += width;
            chars += count;
            plain.push_str(&seg.text);
            spans.push(Span::styled(seg.text, seg.style));
        }
        self.out.lines.push(Line::from(spans));
        self.out.plain.push(plain);
    }
}

/// Adjacent runs in the same style and link become one span.
fn merge(segs: Vec<Seg>) -> Vec<Seg> {
    let mut merged: Vec<Seg> = Vec::new();
    for seg in segs {
        match merged.last_mut() {
            Some(last) if last.style == seg.style && last.link == seg.link => {
                last.text.push_str(&seg.text)
            }
            _ => merged.push(seg),
        }
    }
    merged
}

/// How many columns `c` takes, never fewer than it will be drawn in.
///
/// Per character, where ratatui measures whole strings, and the two disagree on
/// one thing the docs actually contain: an emoji variation selector (`⚠️` is
/// `⚠` + U+FE0F) widens the character before it to two columns. Counted as one
/// column of its own, which is exact after a narrow character and one too many
/// after a wide one -- a line wrapped a column early, never one that overflows.
fn cell_width(c: char) -> usize {
    if c == '\u{FE0F}' {
        1
    } else {
        c.width().unwrap_or(0)
    }
}

/// `segs`, cut at `width` columns.
fn clip(segs: Vec<Seg>, width: usize) -> Vec<Seg> {
    let mut used = 0;
    let mut out = Vec::new();
    for seg in segs {
        if used + seg.width() <= width {
            used += seg.width();
            out.push(seg);
            continue;
        }
        let piece = split_to_width(&seg.text, width - used).swap_remove(0);
        if !piece.is_empty() && piece.width() <= width - used {
            out.push(Seg { text: piece, ..seg });
        }
        break;
    }
    out
}

/// Cut `text` into pieces no wider than `width` columns.
fn split_to_width(text: &str, width: usize) -> Vec<String> {
    let mut pieces = vec![String::new()];
    let mut used = 0;
    for c in text.chars() {
        let w = cell_width(c);
        if used + w > width && used > 0 {
            pieces.push(String::new());
            used = 0;
        }
        pieces.last_mut().unwrap().push(c);
        used += w;
    }
    pieces
}

enum Token {
    Word(Vec<Seg>),
    Space(Seg),
    Break,
}

/// Greedy word wrap across styled runs. A word is everything between two spaces,
/// even when it changes style halfway (`**bold**,` keeps its comma); a word wider
/// than the line is cut.
fn wrap(segs: &[Seg], width: usize) -> Vec<Vec<Seg>> {
    let width = width.max(1);
    let mut tokens: Vec<Token> = Vec::new();
    for seg in segs {
        let mut word = String::new();
        let flush = |word: &mut String, tokens: &mut Vec<Token>| {
            if word.is_empty() {
                return;
            }
            let piece = Seg { text: std::mem::take(word), style: seg.style, link: seg.link };
            match tokens.last_mut() {
                Some(Token::Word(pieces)) => pieces.push(piece),
                _ => tokens.push(Token::Word(vec![piece])),
            }
        };
        for c in seg.text.chars() {
            match c {
                '\n' => {
                    flush(&mut word, &mut tokens);
                    tokens.push(Token::Break);
                }
                ' ' | '\t' => {
                    flush(&mut word, &mut tokens);
                    if !matches!(tokens.last(), Some(Token::Space(_))) {
                        tokens.push(Token::Space(Seg {
                            text: " ".to_string(),
                            style: seg.style,
                            link: seg.link,
                        }));
                    }
                }
                c => word.push(c),
            }
        }
        flush(&mut word, &mut tokens);
    }

    let mut lines: Vec<Vec<Seg>> = vec![Vec::new()];
    let mut used = 0usize;
    let mut space: Option<Seg> = None;
    for token in tokens {
        match token {
            Token::Break => {
                lines.push(Vec::new());
                used = 0;
                space = None;
            }
            Token::Space(seg) => {
                if used > 0 {
                    space = Some(seg);
                }
            }
            Token::Word(pieces) => {
                let word_width: usize = pieces.iter().map(Seg::width).sum();
                if used > 0 && used + 1 + word_width > width {
                    lines.push(Vec::new());
                    used = 0;
                    space = None;
                }
                if let Some(seg) = space.take() {
                    lines.last_mut().unwrap().push(seg);
                    used += 1;
                }
                for piece in pieces {
                    let mut rest = piece.text.as_str();
                    while !rest.is_empty() {
                        let room = width.saturating_sub(used);
                        let mut taken = 0;
                        let mut end = 0;
                        for (index, c) in rest.char_indices() {
                            let w = cell_width(c);
                            if taken + w > room {
                                break;
                            }
                            taken += w;
                            end = index + c.len_utf8();
                        }
                        if end == 0 {
                            if used == 0 {
                                // Wider than an empty line (a wide glyph in a
                                // one-column cell): place it anyway.
                                end = rest.chars().next().map(char::len_utf8).unwrap_or(0);
                                taken = rest[..end].chars().map(cell_width).sum();
                            } else {
                                lines.push(Vec::new());
                                used = 0;
                                continue;
                            }
                        }
                        lines.last_mut().unwrap().push(Seg {
                            text: rest[..end].to_string(),
                            style: piece.style,
                            link: piece.link,
                        });
                        used += taken;
                        rest = &rest[end..];
                    }
                }
            }
        }
    }
    while lines.len() > 1 && lines.last().map(Vec::is_empty).unwrap_or(false) {
        lines.pop();
    }
    lines
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

/// One occurrence of the search text: line, and character range within it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Match {
    pub line: usize,
    pub start: usize,
    pub end: usize,
}

fn fold(c: char) -> char {
    c.to_lowercase().next().unwrap_or(c)
}

/// Every case-insensitive occurrence of `query`, in reading order.
pub fn find_matches(plain: &[String], query: &str) -> Vec<Match> {
    let needle: Vec<char> = query.chars().map(fold).collect();
    if needle.is_empty() {
        return Vec::new();
    }
    let mut found = Vec::new();
    for (line, text) in plain.iter().enumerate() {
        let hay: Vec<char> = text.chars().map(fold).collect();
        let mut start = 0;
        while start + needle.len() <= hay.len() {
            if hay[start..start + needle.len()] == needle[..] {
                found.push(Match { line, start, end: start + needle.len() });
                start += needle.len();
            } else {
                start += 1;
            }
        }
    }
    found
}

/// `line` with the character ranges in `marks` restyled, splitting spans where a
/// mark begins or ends.
fn highlight(line: &Line<'static>, marks: &[(usize, usize, Style)]) -> Line<'static> {
    if marks.is_empty() {
        return line.clone();
    }
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut index = 0usize;
    for span in &line.spans {
        let mut run = String::new();
        let mut run_style: Option<Style> = None;
        for c in span.content.chars() {
            let style = marks
                .iter()
                .find(|(start, end, _)| index >= *start && index < *end)
                .map(|(_, _, mark)| span.style.patch(*mark))
                .unwrap_or(span.style);
            if let Some(previous) = run_style.filter(|previous| *previous != style) {
                spans.push(Span::styled(std::mem::take(&mut run), previous));
            }
            run_style = Some(style);
            run.push(c);
            index += 1;
        }
        if let Some(style) = run_style {
            spans.push(Span::styled(run, style));
        }
    }
    Line::from(spans)
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/// Which pane the arrow keys move.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Focus {
    #[default]
    Index,
    Page,
}

/// The open document: its text as last read, and its layout for the last width
/// it was drawn at.
#[derive(Debug)]
pub struct OpenDoc {
    pub path: PathBuf,
    source: Result<String, String>,
    modified: Option<SystemTime>,
    cache: Option<(u16, Rendered)>,
    /// First line on screen.
    pub scroll: usize,
    /// A `#heading` to land on once the page has been laid out.
    pending_anchor: Option<String>,
}

impl OpenDoc {
    fn read(path: &Path) -> Self {
        let source = fs::read_to_string(path).map_err(|error| error.to_string());
        Self {
            path: path.to_path_buf(),
            source,
            modified: modified(path),
            cache: None,
            scroll: 0,
            pending_anchor: None,
        }
    }

    /// The layout for `width`, from the cache when the width has not changed.
    pub fn rendered(&mut self, width: u16) -> &Rendered {
        let stale = self.cache.as_ref().map(|(cached, _)| *cached != width).unwrap_or(true);
        if stale {
            let rendered = match &self.source {
                Ok(text) => render_markdown(text, width),
                Err(error) => render_markdown(
                    &format!("Could not read `{}`: {error}", self.path.display()),
                    width,
                ),
            };
            self.cache = Some((width, rendered));
        }
        let rendered = &self.cache.as_ref().unwrap().1;
        if let Some(anchor) = self.pending_anchor.take() {
            if let Some(line) = rendered.anchors.get(&anchor.to_lowercase()) {
                self.scroll = *line;
            }
        }
        &self.cache.as_ref().unwrap().1
    }

    fn layout(&self) -> Option<&Rendered> {
        self.cache.as_ref().map(|(_, rendered)| rendered)
    }
}

fn modified(path: &Path) -> Option<SystemTime> {
    fs::metadata(path).and_then(|meta| meta.modified()).ok()
}

/// Everything the DOCS page remembers.
#[derive(Debug, Default)]
pub struct DocsState {
    /// `None` until the page is first opened; then the folder, or where it was
    /// looked for.
    pub dir: Option<Result<PathBuf, Vec<PathBuf>>>,
    pub rows: Vec<IndexRow>,
    /// Index into `rows`; always a file row.
    pub selected: Option<usize>,
    pub index_offset: usize,
    pub focus: Focus,
    pub open: Option<OpenDoc>,
    /// Where each followed link was followed from: document and scroll position.
    pub history: Vec<(PathBuf, usize)>,
    pub search: Option<String>,
    pub current_match: Option<usize>,
    /// Index into the open page's `links`.
    pub selected_link: Option<usize>,
    /// Where the index rows and the page text were drawn last frame.
    pub index_area: Rect,
    pub page_area: Rect,
}

impl DocsState {
    /// A state reading `dir`, for tests and for `$NODO_DOCS_DIR`.
    pub fn at(dir: &Path) -> Self {
        let mut state = Self::default();
        state.load(Ok(dir.to_path_buf()));
        state
    }

    fn load(&mut self, dir: Result<PathBuf, Vec<PathBuf>>) {
        self.rows = match &dir {
            Ok(dir) => build_index(dir),
            Err(_) => Vec::new(),
        };
        self.dir = Some(dir);
        let current = self.open.as_ref().map(|doc| doc.path.clone());
        let row = current
            .as_deref()
            .and_then(|path| self.row_of(path))
            .or_else(|| self.rows.iter().position(IndexRow::is_file));
        self.selected = row;
        match (current, row) {
            // A rescan keeps the page being read, and where it was read to.
            (Some(path), _) if path.is_file() => self.reload_if_changed(true),
            (_, Some(row)) => self.open_row(row),
            _ => self.open = None,
        }
    }

    pub fn docs_dir(&self) -> Option<&Path> {
        match &self.dir {
            Some(Ok(dir)) => Some(dir),
            _ => None,
        }
    }

    fn row_of(&self, path: &Path) -> Option<usize> {
        self.rows.iter().position(|row| row.path() == Some(path))
    }

    fn open_row(&mut self, row: usize) {
        let Some(path) = self.rows.get(row).and_then(IndexRow::path).map(Path::to_path_buf) else {
            return;
        };
        self.selected = Some(row);
        if self.open.as_ref().map(|doc| doc.path == path).unwrap_or(false) {
            return;
        }
        self.open = Some(OpenDoc::read(&path));
        self.current_match = None;
        self.selected_link = None;
    }

    fn open_path(&mut self, path: &Path, anchor: Option<String>, scroll: usize) {
        if self.open.as_ref().map(|doc| doc.path != path).unwrap_or(true) {
            self.open = Some(OpenDoc::read(path));
        }
        if let Some(doc) = self.open.as_mut() {
            doc.scroll = scroll;
            doc.pending_anchor = anchor;
        }
        if let Some(row) = self.row_of(path) {
            self.selected = Some(row);
        }
        self.current_match = None;
        self.selected_link = None;
    }

    /// Re-read the open page when it changed on disk since it was read. `force`
    /// re-reads regardless (`r`).
    pub fn reload_if_changed(&mut self, force: bool) {
        let Some(doc) = self.open.as_mut() else {
            return;
        };
        let now = modified(&doc.path);
        if force || now != doc.modified {
            let scroll = doc.scroll;
            *doc = OpenDoc::read(&doc.path.clone());
            doc.scroll = scroll;
        }
    }

    /// Move the index selection `delta` documents, skipping folder headings.
    pub fn move_selection(&mut self, delta: isize) {
        let files: Vec<usize> = (0..self.rows.len()).filter(|i| self.rows[*i].is_file()).collect();
        if files.is_empty() {
            return;
        }
        let current = self
            .selected
            .and_then(|selected| files.iter().position(|row| *row == selected))
            .unwrap_or(0) as isize;
        let next = (current + delta).clamp(0, files.len() as isize - 1) as usize;
        self.open_row(files[next]);
    }

    fn page_height(&self) -> usize {
        (self.page_area.height as usize).max(1)
    }

    fn line_count(&self) -> usize {
        self.open
            .as_ref()
            .and_then(OpenDoc::layout)
            .map(|rendered| rendered.lines.len())
            .unwrap_or(0)
    }

    /// The furthest the page can scroll: its last line at the bottom of the pane.
    pub fn max_scroll(&self) -> usize {
        self.line_count().saturating_sub(self.page_height())
    }

    pub fn scroll_by(&mut self, delta: isize) {
        let max = self.max_scroll();
        if let Some(doc) = self.open.as_mut() {
            doc.scroll = (doc.scroll as isize + delta).clamp(0, max as isize) as usize;
        }
    }

    fn scroll_to_show(&mut self, line: usize) {
        let height = self.page_height();
        let max = self.max_scroll();
        if let Some(doc) = self.open.as_mut() {
            if line < doc.scroll || line >= doc.scroll + height {
                doc.scroll = line.saturating_sub(height / 3).min(max);
            }
        }
    }

    pub fn matches(&self) -> Vec<Match> {
        match (&self.search, self.open.as_ref().and_then(OpenDoc::layout)) {
            (Some(query), Some(rendered)) => find_matches(&rendered.plain, query),
            _ => Vec::new(),
        }
    }

    /// Step to the next (`1`) or previous (`-1`) match, from the one last visited
    /// or else from the top of the screen.
    pub fn step_match(&mut self, delta: isize) -> Option<(usize, usize)> {
        let matches = self.matches();
        if matches.is_empty() {
            self.current_match = None;
            return None;
        }
        let next = match self.current_match {
            Some(current) => (current as isize + delta).rem_euclid(matches.len() as isize) as usize,
            None => {
                let top = self.open.as_ref().map(|doc| doc.scroll).unwrap_or(0);
                let after = matches.iter().position(|found| found.line >= top);
                match (delta > 0, after) {
                    (true, Some(index)) => index,
                    (true, None) => 0,
                    (false, Some(0)) | (false, None) => matches.len() - 1,
                    (false, Some(index)) => index - 1,
                }
            }
        };
        self.current_match = Some(next);
        self.scroll_to_show(matches[next].line);
        Some((next + 1, matches.len()))
    }

    /// Select the next (`1`) or previous (`-1`) link, starting from the screen.
    pub fn step_link(&mut self, delta: isize) -> Option<String> {
        let (links, top) = match self.open.as_ref() {
            Some(doc) => (doc.layout().map(|r| r.links.clone()).unwrap_or_default(), doc.scroll),
            None => return None,
        };
        if links.is_empty() {
            self.selected_link = None;
            return None;
        }
        let next = match self.selected_link {
            Some(current) => (current as isize + delta).rem_euclid(links.len() as isize) as usize,
            None => {
                let after = links.iter().position(|spot| spot.line >= top);
                match (delta > 0, after) {
                    (true, Some(index)) => index,
                    (true, None) => 0,
                    (false, Some(0)) | (false, None) => links.len() - 1,
                    (false, Some(index)) => index - 1,
                }
            }
        };
        self.selected_link = Some(next);
        self.scroll_to_show(links[next].line);
        Some(links[next].target.clone())
    }

    /// The link drawn under a click, if any.
    fn link_at(&self, position: Position) -> Option<usize> {
        if !self.page_area.contains(position) {
            return None;
        }
        let doc = self.open.as_ref()?;
        let line = doc.scroll + (position.y - self.page_area.y) as usize;
        let column = position.x - self.page_area.x;
        doc.layout()?.links.iter().position(|spot| {
            spot.line == line && column >= spot.columns.0 && column < spot.columns.1
        })
    }

    /// The index row drawn under a click, if it is a document.
    fn row_at(&self, position: Position) -> Option<usize> {
        if !self.index_area.contains(position) {
            return None;
        }
        let row = self.index_offset + (position.y - self.index_area.y) as usize;
        self.rows.get(row).filter(|row| row.is_file()).map(|_| row)
    }

    /// Where `target`, a link on the open page, points: a document and an anchor.
    ///
    /// Relative to the open page, as a Markdown viewer on GitHub would resolve it,
    /// and only to a Markdown file inside this installation -- the page reads
    /// documentation, not whatever a link happens to name.
    pub fn resolve(&self, target: &str) -> Result<(PathBuf, Option<String>), String> {
        let (file, anchor) = match target.split_once('#') {
            Some((file, anchor)) => (file, Some(anchor.to_string())),
            None => (target, None),
        };
        let current = self.open.as_ref().map(|doc| doc.path.clone()).ok_or("No page is open")?;
        if file.is_empty() {
            return Ok((current, anchor));
        }
        let base = current.parent().unwrap_or(Path::new("."));
        let path = normalize(&base.join(file.replace("%20", " ")));
        let root = self
            .docs_dir()
            .and_then(Path::parent)
            .map(Path::to_path_buf)
            .unwrap_or_default();
        if !path.starts_with(&root) {
            return Err(format!("{file} is outside this installation"));
        }
        if !is_markdown(&path) {
            return Err(format!("{} is not a Markdown page", self.label(&path)));
        }
        if !path.is_file() {
            return Err(format!("{} does not exist", self.label(&path)));
        }
        Ok((path, anchor))
    }

    /// How a path is named on screen: relative to `docs/`, or to the installation
    /// for a page outside it (`src/commands/tui/README.md`).
    pub fn label(&self, path: &Path) -> String {
        let Some(dir) = self.docs_dir() else {
            return path.display().to_string();
        };
        if let Ok(relative) = path.strip_prefix(dir) {
            return relative.display().to_string();
        }
        dir.parent()
            .and_then(|root| path.strip_prefix(root).ok())
            .map(|relative| relative.display().to_string())
            .unwrap_or_else(|| path.display().to_string())
    }
}

/// `a/b/../c` -> `a/c`, without touching the filesystem (a link to a file that
/// does not exist has to be reported, not canonicalised into an error).
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

// ---------------------------------------------------------------------------
// The page's actions
// ---------------------------------------------------------------------------

impl App {
    /// Find and index `docs/` the first time the page is shown. Called after every
    /// event rather than from the draw path, which never reads the filesystem.
    pub fn sync_docs(&mut self) {
        if self.page() == Page::Docs && self.docs.dir.is_none() {
            let dir = discover(&self.paths.root);
            self.docs.load(dir);
        }
    }

    /// `r`: look for the folder again, re-read the index and the open page.
    pub fn reload_docs(&mut self) {
        let dir = discover(&self.paths.root);
        self.docs.load(dir);
        self.docs.reload_if_changed(true);
        self.status = match &self.docs.dir {
            Some(Ok(_)) => format!("{} documents", self.docs.rows.iter().filter(|r| r.is_file()).count()),
            _ => "No docs/ folder found".to_string(),
        };
    }

    pub fn docs_up_down(&mut self, delta: isize) {
        match self.docs.focus {
            Focus::Index => self.docs.move_selection(delta),
            Focus::Page => self.docs.scroll_by(delta),
        }
    }

    /// PgUp/PgDn: a screenful, less a line of overlap to keep one's place by.
    pub fn docs_page(&mut self, direction: isize) {
        match self.docs.focus {
            Focus::Index => {
                let rows = (self.docs.index_area.height as isize).max(2) - 1;
                self.docs.move_selection(direction * rows);
            }
            Focus::Page => {
                let rows = (self.docs.page_height() as isize - 1).max(1);
                self.docs.scroll_by(direction * rows);
            }
        }
    }

    /// Home/End.
    pub fn docs_jump(&mut self, to_end: bool) {
        let delta = if to_end { isize::MAX / 2 } else { isize::MIN / 2 };
        self.docs_up_down(delta);
    }

    pub fn docs_focus(&mut self, focus: Focus) {
        self.docs.focus = focus;
    }

    /// Enter: on the index, read the selected page; on the page, follow the
    /// selected link.
    pub fn docs_enter(&mut self) {
        match self.docs.focus {
            Focus::Index => {
                if let Some(row) = self.docs.selected {
                    self.docs.open_row(row);
                }
                self.docs.focus = Focus::Page;
            }
            Focus::Page => match self.docs.selected_link {
                Some(link) => self.follow_docs_link(link),
                None => self.status = "No link selected: l selects the next one".to_string(),
            },
        }
    }

    fn follow_docs_link(&mut self, link: usize) {
        let Some(target) = self
            .docs
            .open
            .as_ref()
            .and_then(OpenDoc::layout)
            .and_then(|rendered| rendered.links.get(link))
            .map(|spot| spot.target.clone())
        else {
            return;
        };
        if is_external(&target) {
            // Nothing here opens a browser; the terminal's clipboard is the
            // shortest way from this page to one.
            self.copy_to_clipboard(&target);
            return;
        }
        match self.docs.resolve(&target) {
            Ok((path, anchor)) => {
                if let Some(doc) = self.docs.open.as_ref() {
                    self.docs.history.push((doc.path.clone(), doc.scroll));
                }
                let label = self.docs.label(&path);
                self.docs.open_path(&path, anchor, 0);
                self.docs.focus = Focus::Page;
                self.status = format!("{label} • ⌫ back");
            }
            Err(reason) => self.status = reason,
        }
    }

    /// Backspace: the page a link was followed from, where it was left.
    pub fn docs_back(&mut self) {
        match self.docs.history.pop() {
            Some((path, scroll)) => {
                self.docs.open_path(&path, None, scroll);
                self.status = self.docs.label(&path);
            }
            None => self.status = "Nothing to go back to".to_string(),
        }
    }

    /// Whether Esc has something on this page to undo before it means "quit".
    pub fn docs_escape_pending(&self) -> bool {
        self.docs.search.is_some() || !self.docs.history.is_empty()
    }

    /// Esc: drop the search first, then step back through followed links.
    pub fn docs_escape(&mut self) {
        if self.docs.search.take().is_some() {
            self.docs.current_match = None;
            self.status = "Search cleared".to_string();
        } else {
            self.docs_back();
        }
    }

    pub fn open_docs_search(&mut self) {
        self.input_mode = InputMode::SearchDocs;
        self.input = self.docs.search.clone().unwrap_or_default();
        self.input_title = "Search this page".to_string();
    }

    pub fn submit_docs_search(&mut self) {
        let query = self.input.trim().to_string();
        self.close_input();
        self.docs.current_match = None;
        if query.is_empty() {
            self.docs.search = None;
            self.status = "Search cleared".to_string();
            return;
        }
        self.docs.search = Some(query.clone());
        self.docs.focus = Focus::Page;
        self.status = match self.docs.step_match(1) {
            Some((index, total)) => format!("\"{query}\": {index}/{total} • n/N next/previous"),
            None => format!("\"{query}\": no matches on this page"),
        };
    }

    pub fn docs_step_match(&mut self, delta: isize) {
        let Some(query) = self.docs.search.clone() else {
            self.status = "No search: / searches this page".to_string();
            return;
        };
        self.status = match self.docs.step_match(delta) {
            Some((index, total)) => format!("\"{query}\": {index}/{total}"),
            None => format!("\"{query}\": no matches on this page"),
        };
    }

    pub fn docs_step_link(&mut self, delta: isize) {
        self.docs.focus = Focus::Page;
        self.status = match self.docs.step_link(delta) {
            Some(target) => format!("{target} • ⏎ follow"),
            None => "No links on this page".to_string(),
        };
    }

    /// A left click: a document in the index opens it, a link on the page follows
    /// it, and either pane takes the arrow keys.
    pub fn click_docs(&mut self, position: Position) {
        if let Some(row) = self.docs.row_at(position) {
            self.docs.focus = Focus::Index;
            self.docs.open_row(row);
        } else if self.docs.page_area.contains(position) {
            self.docs.focus = Focus::Page;
            if let Some(link) = self.docs.link_at(position) {
                self.docs.selected_link = Some(link);
                self.follow_docs_link(link);
            }
        }
    }

    /// The wheel: over the index it moves the selection, as it does on every
    /// table; anywhere else it scrolls the page being read.
    pub fn scroll_docs_at(&mut self, column: u16, row: u16, direction: isize) {
        if self.docs.index_area.contains(Position::new(column, row)) {
            self.docs.move_selection(direction);
        } else {
            self.docs.scroll_by(direction * WHEEL_LINES);
        }
    }

    /// A right click: select what is under the pointer, as a left click would
    /// without following it. False when there is nothing there to act on.
    pub(crate) fn point_docs_at(&mut self, position: Position) -> bool {
        if let Some(row) = self.docs.row_at(position) {
            self.docs.focus = Focus::Index;
            self.docs.open_row(row);
            return true;
        }
        if self.docs.page_area.contains(position) {
            self.docs.focus = Focus::Page;
            if let Some(link) = self.docs.link_at(position) {
                self.docs.selected_link = Some(link);
            }
            return true;
        }
        false
    }
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

/// Below this width the DOCS page shows one pane at a time.
const NARROW_WIDTH: u16 = 60;

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let docs = &mut app.docs;
    docs.index_area = Rect::ZERO;
    docs.page_area = Rect::ZERO;
    let looked = match &docs.dir {
        None => {
            frame.render_widget(
                Paragraph::new("Reading docs/…").block(section_block(" DOCS ", muted())),
                area,
            );
            return;
        }
        Some(Err(looked)) => looked.clone(),
        Some(Ok(_)) => Vec::new(),
    };
    if !looked.is_empty() || docs.rows.is_empty() {
        draw_missing(frame, area, &looked);
        return;
    }

    // Narrow (issue #453): two panes of eighteen columns are an index nobody can
    // read beside a page nobody can read, so only the one with the keyboard is
    // drawn, full width. ←/→ already move between them.
    if area.width < NARROW_WIDTH {
        match docs.focus {
            Focus::Index => draw_index(frame, docs, area),
            Focus::Page => draw_page(frame, docs, area),
        }
        return;
    }
    let index_width = (area.width * 3 / 10).clamp(22, 42).min(area.width / 2);
    let split =
        Layout::horizontal([Constraint::Length(index_width), Constraint::Min(10)]).split(area);
    draw_index(frame, docs, split[0]);
    draw_page(frame, docs, split[1]);
}

fn draw_missing(frame: &mut Frame, area: Rect, looked: &[PathBuf]) {
    let mut lines = vec![
        Line::from(Span::styled(
            if looked.is_empty() {
                "The docs/ folder has no Markdown files in it."
            } else {
                "No docs/ folder found for this installation."
            },
            Style::default().fg(warn()).bold(),
        )),
        Line::from(""),
    ];
    if !looked.is_empty() {
        lines.push(Line::from("Looked in:"));
        for path in looked.iter().take(12) {
            lines.push(Line::from(Span::styled(
                format!("  {}", path.display()),
                Style::default().fg(muted()),
            )));
        }
        lines.push(Line::from(""));
    }
    lines.push(Line::from(format!(
        "Set {DOCS_DIR_ENV} to the folder and restart, or read them online:"
    )));
    lines.push(Line::from(Span::styled(
        "  https://github.com/celaut-project/nodo/tree/dev/docs",
        Style::default().fg(accent()),
    )));
    frame.render_widget(
        Paragraph::new(lines)
            .block(section_block(" DOCS ", warn()))
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_index(frame: &mut Frame, docs: &mut DocsState, area: Rect) {
    let count = docs.rows.iter().filter(|row| row.is_file()).count();
    let colour = if docs.focus == Focus::Index { accent() } else { muted() };
    let block = section_block(format!(" DOCS • {count} "), colour);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    docs.index_area = inner;

    let height = inner.height as usize;
    if let Some(selected) = docs.selected {
        if selected < docs.index_offset {
            docs.index_offset = selected;
        } else if height > 0 && selected >= docs.index_offset + height {
            docs.index_offset = selected + 1 - height;
        }
    }
    docs.index_offset = docs.index_offset.min(docs.rows.len().saturating_sub(height));

    let width = inner.width as usize;
    let lines: Vec<Line> = docs
        .rows
        .iter()
        .enumerate()
        .skip(docs.index_offset)
        .take(height)
        .map(|(index, row)| match row {
            IndexRow::Folder { name, depth } => Line::from(Span::styled(
                truncate(&format!("{}▾ {name}", "  ".repeat(*depth)), width),
                Style::default().fg(muted()).bold(),
            )),
            IndexRow::File { relative, title, depth, .. } => {
                let indent = "  ".repeat(*depth + 1);
                let name = relative.rsplit('/').next().unwrap_or(relative);
                // A page with no heading is titled by its name already.
                let named = title != name;
                let title = truncate(&format!("{indent}{title}"), width);
                let room = width.saturating_sub(title.width() + 2);
                let selected = docs.selected == Some(index);
                let title_style = match (selected, docs.focus) {
                    (true, Focus::Index) => selected_style(),
                    (true, Focus::Page) => Style::default().fg(accent()).bold(),
                    _ => Style::default().fg(text_colour()),
                };
                let mut spans = vec![Span::styled(title, title_style)];
                // The file name as well, where it fits: titles are what a reader
                // looks for, names are what other pages link to.
                if named && room >= 6 {
                    spans.push(Span::raw("  "));
                    spans.push(Span::styled(truncate(name, room), Style::default().fg(muted())));
                }
                Line::from(spans)
            }
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), inner);
}

fn draw_page(frame: &mut Frame, docs: &mut DocsState, area: Rect) {
    let colour = if docs.focus == Focus::Page { accent() } else { muted() };
    let label = docs
        .open
        .as_ref()
        .map(|doc| docs.label(&doc.path))
        .unwrap_or_default();
    let probe = section_block("", colour);
    let inner = probe.inner(area);
    docs.page_area = inner;
    let height = inner.height as usize;
    let matches = docs.matches();
    let search = docs.search.clone();
    let current_match = docs.current_match;
    let selected_link = docs.selected_link;
    let Some(doc) = docs.open.as_mut() else {
        frame.render_widget(section_block(" (no page) ", colour), area);
        return;
    };
    let rendered = doc.rendered(inner.width).clone();
    let total = rendered.lines.len();
    doc.scroll = doc.scroll.min(total.saturating_sub(height));
    let scroll = doc.scroll;

    let matches = if matches.is_empty() {
        search.as_deref().map(|query| find_matches(&rendered.plain, query)).unwrap_or_default()
    } else {
        matches
    };
    let hit = Style::default().add_modifier(Modifier::REVERSED);
    let lines: Vec<Line> = rendered
        .lines
        .iter()
        .enumerate()
        .skip(scroll)
        .take(height)
        .map(|(number, line)| {
            let mut marks: Vec<(usize, usize, Style)> = Vec::new();
            for (index, found) in matches.iter().enumerate().filter(|(_, m)| m.line == number) {
                let style = if Some(index) == current_match { selected_style() } else { hit };
                marks.push((found.start, found.end, style));
            }
            if let Some(spot) = selected_link.and_then(|link| rendered.links.get(link)) {
                if spot.line == number {
                    marks.push((spot.chars.0, spot.chars.1, selected_style()));
                }
            }
            highlight(line, &marks)
        })
        .collect();

    let last = (scroll + height).min(total);
    let percent = if total <= height { 100 } else { last * 100 / total };
    let position = format!(" {}–{last}/{total} • {percent}% ", (scroll + 1).min(total.max(1)));
    let mut block = section_block(format!(" {label} "), colour).title(
        Title::from(Span::styled(position, Style::default().fg(muted())))
            .position(TitlePosition::Bottom)
            .alignment(Alignment::Right),
    );
    if let Some(query) = search {
        let summary = match current_match {
            Some(index) => format!(" /{query} {}/{} ", index + 1, matches.len()),
            None => format!(" /{query} {} ", matches.len()),
        };
        block = block.title(
            Title::from(Span::styled(summary, Style::default().fg(warn())))
                .position(TitlePosition::Bottom)
                .alignment(Alignment::Left),
        );
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn truncate(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_string();
    }
    let mut out = String::new();
    let mut used = 0;
    for c in text.chars() {
        let w = cell_width(c);
        if used + w + 1 > width {
            break;
        }
        out.push(c);
        used += w;
    }
    out.push('…');
    out
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::handler::{handle_key_events, handle_mouse_events};
    use crossterm::event::{
        KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
    };
    use ratatui::{backend::TestBackend, Terminal};

    /// A throwaway docs folder holding `files`, each `(relative path, text)`.
    fn fixture(name: &str, files: &[(&str, &str)]) -> PathBuf {
        let dir = std::env::temp_dir()
            .join(format!("nodo-tui-docs-{name}-{}", std::process::id()))
            .join("docs");
        let _ = fs::remove_dir_all(dir.parent().unwrap());
        for (relative, text) in files {
            let path = dir.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text).unwrap();
        }
        dir
    }

    fn repo_docs() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../docs")
            .canonicalize()
            .unwrap()
    }

    fn titles(rows: &[IndexRow]) -> Vec<String> {
        rows.iter()
            .map(|row| match row {
                IndexRow::Folder { name, depth } => format!("{}[{name}]", "  ".repeat(*depth)),
                IndexRow::File { relative, title, depth, .. } => {
                    format!("{}{relative} = {title}", "  ".repeat(*depth))
                }
            })
            .collect()
    }

    fn plain(rendered: &Rendered) -> Vec<String> {
        rendered.plain.iter().map(|line| line.trim_end().to_string()).collect()
    }

    fn on_docs(dir: &Path) -> App {
        let mut app = App::default();
        app.tabs.select_page(Page::Docs);
        app.docs = DocsState::at(dir);
        app
    }

    fn draw(app: &mut App, width: u16, height: u16) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| crate::ui::render(app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..buffer.area.height)
            .map(|y| (0..buffer.area.width).map(|x| buffer.get(x, y).symbol()).collect())
            .collect()
    }

    fn key(app: &mut App, code: KeyCode) {
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(handle_key_events(KeyEvent::new(code, KeyModifiers::NONE), app)).unwrap();
    }

    fn mouse(app: &mut App, kind: MouseEventKind, column: u16, row: u16) {
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(handle_mouse_events(
            MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE },
            app,
        ))
        .unwrap();
    }

    fn scroll(app: &App) -> usize {
        app.docs.open.as_ref().unwrap().scroll
    }

    fn open_label(app: &App) -> String {
        app.docs.label(&app.docs.open.as_ref().unwrap().path)
    }

    /// A long page: forty numbered paragraphs under two headings.
    fn long_page() -> String {
        let mut text = String::from("# Long\n\n");
        for n in 1..=40 {
            text.push_str(&format!("Paragraph {n}.\n\n"));
            if n == 30 {
                text.push_str("## Second part\n\n");
            }
        }
        text
    }

    // -- index ---------------------------------------------------------------

    #[test]
    fn the_index_nests_folders_and_puts_readme_and_index_first() {
        let dir = fixture(
            "index",
            &[
                ("zeta.md", "# Zeta page\n"),
                ("alpha.md", "# Alpha page\n"),
                ("INDEX.md", "# The index\n"),
                ("README.md", "# Read me\n"),
                ("notes.txt", "not markdown"),
                ("guides/setup.md", "# Setup\n"),
                ("guides/README.md", "# Guides\n"),
                ("guides/deep/more.md", "# More\n"),
                ("scripts/build.sh", "#!/bin/sh"),
                (".hidden/secret.md", "# Hidden\n"),
            ],
        );
        assert_eq!(
            titles(&build_index(&dir)),
            vec![
                "README.md = Read me",
                "INDEX.md = The index",
                "alpha.md = Alpha page",
                "zeta.md = Zeta page",
                "[guides/]",
                "  guides/README.md = Guides",
                "  guides/setup.md = Setup",
                "  [guides/deep/]",
                "    guides/deep/more.md = More",
            ],
            "folders with no Markdown (scripts/) and hidden ones are left out"
        );
    }

    #[test]
    fn a_title_is_the_first_level_one_heading_outside_front_matter_and_code() {
        assert_eq!(document_title("# Plain\n\ntext"), Some("Plain".to_string()));
        assert_eq!(
            document_title("---\nname: x\n# not: a title\n---\n\n# After `front` matter\n"),
            Some("After front matter".to_string())
        );
        assert_eq!(
            document_title("```bash\n# a shell comment\n```\n\n# Real\n"),
            Some("Real".to_string())
        );
        assert_eq!(document_title("Setext\n======\n"), Some("Setext".to_string()));
        assert_eq!(document_title("## Only a subheading\n"), None);
    }

    /// Every document in this repository's docs/ is listed, with a title.
    #[test]
    fn the_repository_docs_are_all_indexed() {
        let rows = build_index(&repo_docs());
        let files: Vec<&IndexRow> = rows.iter().filter(|row| row.is_file()).collect();
        let on_disk = fs::read_dir(repo_docs())
            .unwrap()
            .flatten()
            .filter(|entry| is_markdown(&entry.path()))
            .count();
        assert!(files.len() > on_disk, "top level plus subfolders: {}", titles(&rows).join("\n"));
        assert!(titles(&rows).contains(&"KyA.md = Know Your Assumptions (KyA) for Nodo".to_string())
            || titles(&rows).iter().any(|row| row.starts_with("KyA.md = ")));
        assert!(rows.contains(&IndexRow::Folder { name: "proposals/".to_string(), depth: 0 }));
    }

    #[test]
    fn the_folder_is_found_from_the_binary_when_the_compiled_root_is_elsewhere() {
        let dir = fixture("locate", &[("KyA.md", "# KyA\n")]);
        let root = dir.parent().unwrap().to_path_buf();
        let binary = root.join("src/commands/tui/target/release/tui");
        let nowhere = PathBuf::from("/nonexistent/ci/checkout");

        assert_eq!(locate_docs_dir(None, &root, None, None), Ok(dir.clone()));
        assert_eq!(locate_docs_dir(None, &nowhere, Some(binary), None), Ok(dir.clone()));
        assert_eq!(locate_docs_dir(None, &nowhere, None, Some(root.clone())), Ok(dir.clone()));
        // Explicit wins, and needs no KyA.md.
        let other = fixture("locate-explicit", &[("x.md", "# X\n")]);
        assert_eq!(locate_docs_dir(Some(other.clone()), &root, None, None), Ok(other));
    }

    #[test]
    fn a_missing_folder_is_reported_with_where_it_was_looked_for() {
        let nowhere = PathBuf::from("/nonexistent/ci/checkout");
        let looked = locate_docs_dir(Some(PathBuf::from("/nonexistent/explicit")), &nowhere, None, None)
            .unwrap_err();
        assert_eq!(looked[0], PathBuf::from("/nonexistent/explicit"));
        assert!(looked.contains(&nowhere.join("docs")));

        let mut app = App::default();
        app.tabs.select_page(Page::Docs);
        app.docs.load(Err(looked));
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("No docs/ folder found"), "{screen}");
        assert!(screen.contains("/nonexistent/explicit"), "{screen}");
        assert!(screen.contains(DOCS_DIR_ENV), "{screen}");
        // And nothing on the page panics without a document.
        for code in [KeyCode::Down, KeyCode::PageDown, KeyCode::End, KeyCode::Enter, KeyCode::Backspace] {
            key(&mut app, code);
        }
        key(&mut app, KeyCode::Char('n'));
        key(&mut app, KeyCode::Char('l'));
    }

    // -- rendering -----------------------------------------------------------

    #[test]
    fn headings_are_styled_underlined_and_anchored() {
        let rendered = render_markdown(
            "# Top\n\nIntro.\n\n## Applying a change\n\n### Detail\n\n## Applying a change\n",
            60,
        );
        let lines = plain(&rendered);
        assert_eq!(lines[0], "Top");
        assert_eq!(lines[1], "━━━");
        assert!(lines.contains(&"Applying a change".to_string()));
        assert!(lines.contains(&"─────────────────".to_string()));
        assert!(rendered.lines[0].spans[0].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(rendered.lines[0].spans[0].style.fg, Some(accent()));
        assert_eq!(lines[rendered.anchors["applying-a-change"]], "Applying a change");
        assert_eq!(lines[rendered.anchors["detail"]], "Detail");
        // A repeated heading gets GitHub's numbered anchor.
        assert!(rendered.anchors["applying-a-change-1"] > rendered.anchors["applying-a-change"]);
        assert_eq!(slug("What `nodo tui` finds (and why)"), "what-nodo-tui-finds-and-why");
    }

    #[test]
    fn inline_styles_survive_to_the_spans() {
        let rendered = render_markdown("Some **bold**, *italic*, `code` and ~~gone~~.", 80);
        let spans = &rendered.lines[0].spans;
        let find = |text: &str| spans.iter().find(|span| span.content.contains(text)).unwrap().style;
        assert!(find("bold").add_modifier.contains(Modifier::BOLD));
        assert!(find("italic").add_modifier.contains(Modifier::ITALIC));
        assert_eq!(find("code").fg, Some(code_colour()));
        assert!(find("gone").add_modifier.contains(Modifier::CROSSED_OUT));
        assert_eq!(plain(&rendered), vec!["Some bold, italic, code and gone."]);
    }

    #[test]
    fn code_blocks_keep_their_lines_and_indentation() {
        let rendered = render_markdown(
            "Run:\n\n```bash\nsudo apt-get install -y \\\n    curl git\n```\n\nDone.",
            40,
        );
        assert_eq!(
            plain(&rendered),
            vec![
                "Run:",
                "",
                "▏bash",
                "▏ sudo apt-get install -y \\",
                "▏     curl git",
                "",
                "Done.",
            ]
        );
        assert_eq!(rendered.lines[3].spans[1].style.fg, Some(code_colour()));
        // Too long for the pane: cut, not word-wrapped, and never wider than it.
        let long = render_markdown(&format!("```\n{}\n```", "x".repeat(100)), 30);
        assert!(long.plain.iter().all(|line| line.width() <= 30));
        assert_eq!(long.plain.len(), 4);
    }

    #[test]
    fn lists_nest_and_hang_their_continuation_lines() {
        let rendered = render_markdown(
            "- one\n- two is long enough to wrap onto a second line here\n  - nested\n\n1. first\n2. second\n",
            30,
        );
        assert_eq!(
            plain(&rendered),
            vec![
                "• one",
                "• two is long enough to wrap",
                "  onto a second line here",
                "  ◦ nested",
                "",
                "1. first",
                "2. second",
            ]
        );
        let tasks = render_markdown("- [x] done\n- [ ] open\n", 30);
        assert_eq!(plain(&tasks), vec!["• [x] done", "• [ ] open"]);
    }

    #[test]
    fn quotes_rules_and_images_are_legible() {
        let rendered = render_markdown("> quoted\n> text\n\n---\n\n![a diagram](x.png)\n", 20);
        let lines = plain(&rendered);
        assert_eq!(lines[0], "▍ quoted text");
        assert_eq!(lines[2], "─".repeat(20));
        assert_eq!(lines[4], "[image: a diagram]");
    }

    #[test]
    fn tables_line_up_and_wrap_to_the_pane() {
        let source = "| Key | Meaning |\n|---|---|\n| `r` | Refresh everything on the page right now |\n| q | Quit |\n";
        let wide = plain(&render_markdown(source, 80));
        assert_eq!(
            wide,
            vec![
                "Key │ Meaning",
                "────┼─────────────────────────────────────────",
                "r   │ Refresh everything on the page right now",
                "q   │ Quit",
            ]
        );
        let narrow = render_markdown(source, 24);
        assert!(narrow.plain.iter().all(|line| line.width() <= 24), "{:#?}", narrow.plain);
        assert!(narrow.plain.len() > wide.len(), "the long cell wrapped: {:#?}", narrow.plain);
        // Columns stay aligned: every row's separator sits in the same column.
        let columns: Vec<usize> = narrow
            .plain
            .iter()
            .filter_map(|line| line.find(['│', '┼']).map(|byte| line[..byte].width()))
            .collect();
        assert!(columns.windows(2).all(|pair| pair[0] == pair[1]), "{:#?}", narrow.plain);
    }

    #[test]
    fn links_are_recorded_where_they_were_drawn() {
        let rendered = render_markdown(
            "See [CONFIG](CONFIG.md#applying-a-change) and [the site](https://example.org).",
            80,
        );
        let line = &rendered.plain[0];
        assert_eq!(line, "See CONFIG and the site <https://example.org>.");
        let first = &rendered.links[0];
        assert_eq!(first.target, "CONFIG.md#applying-a-change");
        assert_eq!(first.columns, (4, 10));
        assert_eq!(&line[4..10], "CONFIG");
        let second = &rendered.links[1];
        assert_eq!(second.target, "https://example.org");
        let covered: String = line.chars().skip(second.chars.0).take(second.chars.1 - second.chars.0).collect();
        assert_eq!(covered, "the site <https://example.org>");
        // A link wrapped over two lines is clickable on both.
        let wrapped = render_markdown("[a link that wraps across lines](X.md)", 12);
        assert!(wrapped.links.len() >= 2);
        assert!(wrapped.links.iter().all(|spot| spot.target == "X.md"));
    }

    #[test]
    fn words_wrap_at_the_pane_width() {
        let rendered = render_markdown(
            "The quick brown fox jumps over the lazy dog, and a supercalifragilisticexpialidocious word.",
            20,
        );
        assert_eq!(
            plain(&rendered),
            vec![
                "The quick brown fox",
                "jumps over the lazy",
                "dog, and a",
                "supercalifragilistic",
                "expialidocious word.",
            ]
        );
    }

    /// The property the whole page rests on, over every real document: nothing is
    /// ever laid out wider than the pane it was laid out for.
    #[test]
    fn no_repository_document_overflows_its_pane() {
        for row in build_index(&repo_docs()) {
            let IndexRow::File { path, relative, .. } = row else { continue };
            let text = fs::read_to_string(&path).unwrap();
            for width in [20u16, 37, 58, 100] {
                let rendered = render_markdown(&text, width);
                assert_eq!(rendered.lines.len(), rendered.plain.len());
                for (number, line) in rendered.plain.iter().enumerate() {
                    assert!(
                        line.width() <= width as usize,
                        "{relative} at {width}: line {number} is {} wide: {line:?}",
                        line.width()
                    );
                }
            }
        }
    }

    #[test]
    fn search_is_case_insensitive_and_finds_every_occurrence() {
        let plain = vec!["Nodo nodo NODO".to_string(), "none".to_string(), "a nodo".to_string()];
        let found = find_matches(&plain, "nOdO");
        assert_eq!(found.len(), 4);
        assert_eq!(found[1], Match { line: 0, start: 5, end: 9 });
        assert_eq!(found[3].line, 2);
        assert!(find_matches(&plain, "").is_empty());
        let marked = highlight(&Line::from("abcdef"), &[(2, 4, selected_style())]);
        let contents: Vec<&str> = marked.spans.iter().map(|span| span.content.as_ref()).collect();
        assert_eq!(contents, vec!["ab", "cd", "ef"]);
    }

    // -- the page ------------------------------------------------------------

    #[test]
    fn scrolling_stops_at_both_ends_and_says_where_it_is() {
        let dir = fixture("scroll", &[("long.md", &long_page())]);
        let mut app = on_docs(&dir);
        draw(&mut app, 80, 24);
        key(&mut app, KeyCode::Right);
        assert_eq!(app.docs.focus, Focus::Page);

        key(&mut app, KeyCode::Up);
        assert_eq!(scroll(&app), 0, "no scrolling above the top");
        key(&mut app, KeyCode::Down);
        assert_eq!(scroll(&app), 1);
        let height = app.docs.page_area.height as usize;
        key(&mut app, KeyCode::PageDown);
        assert_eq!(scroll(&app), 1 + height - 1, "a page less one line of overlap");
        key(&mut app, KeyCode::End);
        let max = app.docs.max_scroll();
        assert_eq!(scroll(&app), max);
        assert!(max > 0);
        key(&mut app, KeyCode::Down);
        key(&mut app, KeyCode::PageDown);
        assert_eq!(scroll(&app), max, "no scrolling past the end");
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("Paragraph 40."), "{screen}");
        assert!(screen.contains("• 100%"), "{screen}");
        key(&mut app, KeyCode::Home);
        assert_eq!(scroll(&app), 0);
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains(&format!("1–{height}/")), "{screen}");
    }

    #[test]
    fn the_index_keys_select_and_open_documents() {
        let dir = fixture(
            "select",
            &[("a.md", "# Alpha\n"), ("b.md", "# Beta\n"), ("sub/c.md", "# Gamma\n")],
        );
        let mut app = on_docs(&dir);
        draw(&mut app, 80, 24);
        assert_eq!(open_label(&app), "a.md", "the first document opens with the page");
        key(&mut app, KeyCode::Down);
        assert_eq!(open_label(&app), "b.md");
        key(&mut app, KeyCode::Down);
        assert_eq!(open_label(&app), "sub/c.md", "the folder heading is stepped over");
        key(&mut app, KeyCode::Down);
        assert_eq!(open_label(&app), "sub/c.md", "and the last stays last");
        key(&mut app, KeyCode::Home);
        assert_eq!(open_label(&app), "a.md");
        key(&mut app, KeyCode::End);
        assert_eq!(open_label(&app), "sub/c.md");
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.docs.focus, Focus::Page, "Enter reads the selected page");
        key(&mut app, KeyCode::Left);
        assert_eq!(app.docs.focus, Focus::Index);
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("▾ sub/"), "{screen}");
        assert!(screen.contains("Gamma"), "{screen}");
    }

    #[test]
    fn the_mouse_picks_documents_scrolls_the_pane_under_it_and_follows_links() {
        let dir = fixture(
            "mouse",
            &[("a.md", &format!("[to b](b.md)\n\n{}", long_page())), ("b.md", "# Beta\n")],
        );
        let mut app = on_docs(&dir);
        let screen = draw(&mut app, 100, 30);
        let page = app.docs.page_area;

        mouse(&mut app, MouseEventKind::ScrollDown, page.x + 5, page.y + 5);
        assert_eq!(scroll(&app), WHEEL_LINES as usize);
        mouse(&mut app, MouseEventKind::ScrollUp, page.x + 5, page.y + 5);
        assert_eq!(scroll(&app), 0);

        // The wheel over the index moves the selection instead.
        let index = app.docs.index_area;
        mouse(&mut app, MouseEventKind::ScrollDown, index.x + 2, index.y);
        assert_eq!(open_label(&app), "b.md");

        // A click on a row opens it: a.md is the first.
        assert!(screen[index.y as usize].contains("a.md"), "{}", screen.join("\n"));
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), index.x + 3, index.y);
        assert_eq!(open_label(&app), "a.md");
        assert_eq!(app.docs.focus, Focus::Index);

        // A click on a link follows it.
        draw(&mut app, 100, 30);
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), page.x + 1, page.y);
        assert_eq!(open_label(&app), "b.md");
        assert_eq!(app.docs.history.len(), 1);
    }

    #[test]
    fn links_between_documents_land_on_their_heading_and_come_back() {
        let dir = fixture(
            "links",
            &[
                ("a.md", "# A\n\nSee [the second part](sub/b.md#second-part), [top](#a), [gone](nope.md), [yaml](../x.yaml) and [web](https://example.org).\n"),
                ("sub/b.md", &long_page().replace("# Long", "# B")),
            ],
        );
        let mut app = on_docs(&dir);
        draw(&mut app, 100, 30);
        key(&mut app, KeyCode::Char('l'));
        assert_eq!(app.docs.focus, Focus::Page);
        assert_eq!(app.docs.selected_link, Some(0));
        key(&mut app, KeyCode::Enter);
        assert_eq!(open_label(&app), "sub/b.md");
        draw(&mut app, 100, 30);
        let doc = app.docs.open.as_ref().unwrap();
        let anchor = doc.layout().unwrap().anchors["second-part"];
        assert_eq!(doc.scroll, anchor.min(app.docs.max_scroll()));
        assert!(doc.scroll > 0);
        assert_eq!(app.docs.selected, app.docs.row_of(&dir.join("sub/b.md")), "the index follows");

        key(&mut app, KeyCode::Backspace);
        assert_eq!(open_label(&app), "a.md");
        assert!(app.docs.history.is_empty());

        // The ones that cannot be followed say why and stay put.
        draw(&mut app, 100, 30);
        for (link, expected) in [(2, "does not exist"), (3, "not a Markdown page")] {
            app.docs.selected_link = Some(link);
            key(&mut app, KeyCode::Enter);
            assert!(app.status.contains(expected), "{}", app.status);
            assert_eq!(open_label(&app), "a.md");
        }
        app.docs.selected_link = Some(4);
        key(&mut app, KeyCode::Enter);
        assert!(app.status.contains("Copied"), "{}", app.status);
        assert_eq!(open_label(&app), "a.md");
        assert!(app.docs.resolve("../../outside.md").unwrap_err().contains("outside"));
    }

    #[test]
    fn search_finds_steps_and_esc_clears_before_it_quits() {
        let dir = fixture("search", &[("long.md", &long_page())]);
        let mut app = on_docs(&dir);
        draw(&mut app, 80, 24);
        key(&mut app, KeyCode::Char('/'));
        assert_eq!(app.input_mode, InputMode::SearchDocs);
        for c in "paragraph 3".chars() {
            key(&mut app, KeyCode::Char(c));
        }
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.input_mode, InputMode::Normal);
        assert_eq!(app.docs.search.as_deref(), Some("paragraph 3"));
        // "Paragraph 3." and "Paragraph 30." to "Paragraph 39.".
        assert_eq!(app.docs.matches().len(), 11);
        assert_eq!(app.docs.current_match, Some(0));
        assert!(app.status.contains("1/11"), "{}", app.status);

        key(&mut app, KeyCode::Char('n'));
        assert_eq!(app.docs.current_match, Some(1));
        let line = app.docs.matches()[1].line;
        let top = scroll(&app);
        assert!(line >= top && line < top + app.docs.page_area.height as usize, "scrolled into view");
        key(&mut app, KeyCode::Char('N'));
        key(&mut app, KeyCode::Char('N'));
        assert_eq!(app.docs.current_match, Some(10), "wraps backwards");
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("/paragraph 3 11/11"), "{screen}");

        key(&mut app, KeyCode::Esc);
        assert!(app.docs.search.is_none());
        assert!(app.running, "the first Esc only cleared the search");
        key(&mut app, KeyCode::Esc);
        assert!(!app.running);
    }

    #[test]
    fn an_edited_page_is_read_again() {
        let dir = fixture("reload", &[("a.md", "# Before\n")]);
        let mut app = on_docs(&dir);
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("Before"));

        let path = dir.join("a.md");
        fs::write(&path, "# After\n").unwrap();
        let later = SystemTime::now() + std::time::Duration::from_secs(5);
        fs::File::options().write(true).open(&path).unwrap().set_modified(later).unwrap();
        app.docs.reload_if_changed(false);
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("After"), "{screen}");
    }

    #[test]
    fn the_layout_is_cached_per_width() {
        let dir = fixture("cache", &[("a.md", &long_page())]);
        let mut app = on_docs(&dir);
        draw(&mut app, 80, 24);
        let first = app.docs.open.as_ref().unwrap().cache.as_ref().unwrap().0;
        draw(&mut app, 80, 24);
        assert_eq!(app.docs.open.as_ref().unwrap().cache.as_ref().unwrap().0, first);
        draw(&mut app, 120, 24);
        assert!(app.docs.open.as_ref().unwrap().cache.as_ref().unwrap().0 > first);
    }

    #[test]
    fn the_sixth_group_key_and_a_click_on_its_label_open_the_page() {
        let mut app = App::default();
        key(&mut app, KeyCode::Char('6'));
        assert_eq!(app.page(), Page::Docs);
        // Found from the compiled-in root, which in a test is this checkout.
        assert_eq!(app.docs.docs_dir(), Some(repo_docs().as_path()));
        assert!(app.docs.open.is_some(), "a first page is open");
        let screen = draw(&mut app, 80, 24).join("\n");
        assert!(screen.contains("DOCS •"), "{screen}");

        key(&mut app, KeyCode::Char('1'));
        assert_eq!(app.page(), Page::Overview);
        let screen = draw(&mut app, 80, 24);
        let x = screen[1].find("DOCS").map(|byte| screen[1][..byte].width()).unwrap() as u16;
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), x + 1, 1);
        assert_eq!(app.page(), Page::Docs);
        key(&mut app, KeyCode::Char('['));
        assert_eq!(app.page().group(), crate::app::PageGroup::Settings);
    }

    /// Every key this page answers is on its footer.
    #[test]
    fn the_footer_documents_every_docs_key() {
        let controls = crate::ui::page_controls(Page::Docs);
        for key in [
            "\u{2191}/\u{2193}",
            "PgUp/PgDn",
            "Home/End",
            "\u{2190}/\u{2192}",
            "\u{23ce} open/follow",
            "l/L link",
            "/ search",
            "n/N match",
            "\u{232b} back",
            "r reload",
        ] {
            assert!(controls.contains(key), "{key:?} missing from: {controls}");
        }
    }

    #[test]
    fn right_click_offers_the_page_actions() {
        let dir = fixture("menu", &[("a.md", "# Alpha\n"), ("b.md", "# Beta\n")]);
        let mut app = on_docs(&dir);
        let screen = draw(&mut app, 100, 30);
        let row = screen.iter().position(|line| line.contains("Beta")).unwrap() as u16;
        let x = app.docs.index_area.x + 3;
        mouse(&mut app, MouseEventKind::Down(MouseButton::Right), x, row);
        assert_eq!(app.input_mode, InputMode::ContextMenu);
        assert_eq!(open_label(&app), "b.md", "right-clicking a row selects it");
        let menu = draw(&mut app, 100, 30).join("\n");
        assert!(menu.contains("Search this page"), "{menu}");

        // "Search this page…" is the second entry.
        key(&mut app, KeyCode::Down);
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.input_mode, InputMode::SearchDocs);
    }

    /// What the page looks like, over a real document; printed with
    /// `cargo test docs_page_snapshot -- --nocapture`.
    #[test]
    fn docs_page_snapshot() {
        let mut app = on_docs(&repo_docs());
        let row = app.docs.row_of(&repo_docs().join("CONFIG.md")).unwrap();
        app.docs.open_row(row);
        app.docs.focus = Focus::Page;
        let screen = draw(&mut app, 100, 30);
        println!("{}", screen.iter().map(|line| line.trim_end()).collect::<Vec<_>>().join("\n"));
        let text = screen.join("\n");
        assert!(text.contains("CONFIG.md"), "{text}");
        assert!(text.contains("Configuration Reference"), "{text}");
        assert!(text.contains("▾ proposals/") || text.contains("DOCS •"), "{text}");
    }
}
