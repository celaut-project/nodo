//! The env vars a service asks for, as a form between the spend confirmation and
//! `nodo execute`.
//!
//! `nodo service_envs <id> --json` lists them: a variable a network templates on
//! (`${VAR}` in its formal) is required, every other declared one is optional. The
//! form asks for all of them and runs `nodo execute --no-input -e <k> <v>… <id>` with
//! the ones that have a value. `--no-input` because `nodo` inherits this terminal:
//! without it, a variable the form did not send would be asked for on a screen the
//! TUI is drawing.
//!
//! Every value is masked, since nothing says which one is a secret. Ctrl+R, or a click
//! on the eye at the right of a field, shows the value of that field only; it is
//! masked again when the focus leaves it.

use crate::app::{App, CommandKind, InputMode};
use crate::layout_util::{display_width, truncate_ellipsis};
use crate::ui::{accent, bad, centered_rect, good, muted, popup_background, text_colour, warn};
use ratatui::layout::Position;
use ratatui::{prelude::*, widgets::*};

/// The button of a masked field: click to show the value.
pub const EYE_SHOW: &str = "[👁]";
/// The button of the field whose value is shown: click to mask it again.
pub const EYE_HIDE: &str = "[◡]";
/// What a masked value with text in it reads as. A fixed length, so the mask does not
/// tell how long the value is.
pub const MASK: &str = "••••••••";
/// The most fields drawn at once; the list scrolls to the focus past that.
const VISIBLE_FIELDS: usize = 8;

/// One entry of `nodo service_envs --json`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EnvSpec {
    pub name: String,
    pub tags: Vec<String>,
    pub prose: String,
    pub required: bool,
    pub networks: Vec<String>,
}

fn strings(value: &serde_json::Value) -> Vec<String> {
    value
        .as_array()
        .map(|items| items.iter().filter_map(|item| item.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

impl EnvSpec {
    fn from_json(value: &serde_json::Value) -> Option<Self> {
        Some(Self {
            name: value.get("name")?.as_str()?.to_string(),
            tags: value.get("tags").map(strings).unwrap_or_default(),
            prose: value.get("prose").and_then(|v| v.as_str()).unwrap_or_default().to_string(),
            required: value.get("required").and_then(|v| v.as_bool()).unwrap_or(false),
            networks: value.get("networks").map(strings).unwrap_or_default(),
        })
    }
}

/// The variables in `nodo service_envs --json` output, or why there are none to read.
///
/// The object is the last line of stdout: acquiring a missing service may print before
/// it.
pub fn parse_service_envs(stdout: &str) -> Result<Vec<EnvSpec>, String> {
    let line = stdout
        .lines()
        .map(str::trim)
        .rev()
        .find(|line| line.starts_with('{'))
        .ok_or_else(|| "no answer from nodo service_envs".to_string())?;
    let parsed: serde_json::Value =
        serde_json::from_str(line).map_err(|error| format!("unreadable answer: {error}"))?;
    if let Some(error) = parsed.get("error").and_then(|v| v.as_str()) {
        return Err(error.to_string());
    }
    let envs = parsed
        .get("envs")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "unreadable answer: no envs".to_string())?;
    envs.iter()
        .map(|entry| EnvSpec::from_json(entry).ok_or_else(|| "unreadable answer: an env without a name".to_string()))
        .collect()
}

/// The `nodo execute` invocation for `id` with `envs`, in the order given.
pub fn execute_args(id: &str, envs: &[(String, String)]) -> Vec<String> {
    let mut args = vec!["execute".to_string(), "--no-input".to_string()];
    for (key, value) in envs {
        args.extend(["-e".to_string(), key.clone(), value.clone()]);
    }
    args.push(id.to_string());
    args
}

#[derive(Debug, Clone, Default)]
pub struct EnvForm {
    pub service_id: String,
    pub label: String,
    pub specs: Vec<EnvSpec>,
    pub values: Vec<String>,
    pub focus: usize,
    /// Whether the focused field's value is shown. Only the focused one can be.
    pub revealed: bool,
    /// Why the last run was refused; cleared by the next keystroke.
    pub error: Option<String>,
}

impl EnvForm {
    pub fn new(service_id: String, label: String, specs: Vec<EnvSpec>) -> Self {
        let values = vec![String::new(); specs.len()];
        Self { service_id, label, specs, values, ..Self::default() }
    }

    /// Whether the value of field `index` is drawn as typed.
    pub fn is_revealed(&self, index: usize) -> bool {
        self.revealed && index == self.focus
    }

    /// The pairs to send: every field with a value, in the form's order.
    pub fn envs(&self) -> Vec<(String, String)> {
        self.specs
            .iter()
            .zip(&self.values)
            .filter(|(_, value)| !value.is_empty())
            .map(|(spec, value)| (spec.name.clone(), value.clone()))
            .collect()
    }

    /// The first required field without a value, if any.
    pub fn first_unanswered(&self) -> Option<usize> {
        self.specs
            .iter()
            .zip(&self.values)
            .position(|(spec, value)| spec.required && value.is_empty())
    }

    /// The first field drawn, so that the focus is always on screen.
    fn scroll(&self) -> usize {
        (self.focus + 1).saturating_sub(VISIBLE_FIELDS)
    }
}

impl App {
    /// The answer of `nodo service_envs` for a confirmed execution: run it now if the
    /// service asks for nothing, otherwise open the form.
    pub(crate) fn on_service_envs(&mut self, id: String, label: String, outcome: Result<Vec<EnvSpec>, String>) {
        match outcome {
            Err(error) => {
                self.status = format!("Execute service {label} failed: {error}");
            }
            Ok(specs) if specs.is_empty() => self.run_execute(&id, &label, &[]),
            Ok(specs) => {
                self.env_form = EnvForm::new(id, label.clone(), specs);
                self.input_mode = InputMode::ExecuteEnvs;
                self.input_title = format!("Env vars · {label}");
                self.status =
                    "Tab/↑/↓ move • Enter next, runs on the last • Ctrl+R show • Esc cancel".to_string();
            }
        }
    }

    fn run_execute(&mut self, id: &str, label: &str, envs: &[(String, String)]) {
        self.spawn_command(
            CommandKind::Generic,
            format!("Execute service {label}"),
            execute_args(id, envs),
        );
    }

    pub fn cancel_env_form(&mut self) {
        self.env_form = EnvForm::default();
        self.env_form_eye_buttons.clear();
        self.input_mode = InputMode::Normal;
        self.status = "Execution cancelled".to_string();
    }

    /// Move the focus; the field it leaves is masked again.
    pub fn env_form_move(&mut self, delta: i32) {
        let count = self.env_form.specs.len() as i32;
        if count == 0 {
            return;
        }
        self.env_form.focus = (self.env_form.focus as i32 + delta).rem_euclid(count) as usize;
        self.env_form.revealed = false;
    }

    /// Ctrl+R and the eye: show or mask field `index`, which takes the focus.
    pub fn env_form_toggle_reveal(&mut self, index: usize) {
        if index >= self.env_form.specs.len() {
            return;
        }
        if index == self.env_form.focus {
            self.env_form.revealed = !self.env_form.revealed;
        } else {
            self.env_form.focus = index;
            self.env_form.revealed = true;
        }
    }

    pub fn env_form_type(&mut self, character: char) {
        self.env_form.error = None;
        let focus = self.env_form.focus;
        if let Some(value) = self.env_form.values.get_mut(focus) {
            value.push(character);
        }
    }

    pub fn env_form_backspace(&mut self) {
        self.env_form.error = None;
        let focus = self.env_form.focus;
        if let Some(value) = self.env_form.values.get_mut(focus) {
            value.pop();
        }
    }

    pub fn env_form_clear_field(&mut self) {
        self.env_form.error = None;
        let focus = self.env_form.focus;
        if let Some(value) = self.env_form.values.get_mut(focus) {
            value.clear();
        }
    }

    /// Enter: on to the next field, and on the last one, run.
    pub fn env_form_enter(&mut self) {
        if self.env_form.focus + 1 < self.env_form.specs.len() {
            self.env_form_move(1);
        } else {
            self.submit_env_form();
        }
    }

    /// Run `nodo execute` with the values, unless a required one is empty.
    pub fn submit_env_form(&mut self) {
        if let Some(index) = self.env_form.first_unanswered() {
            let spec = &self.env_form.specs[index];
            self.env_form.error = Some(format!(
                "{} is required by network {}",
                spec.name,
                spec.networks.join("; ")
            ));
            if index != self.env_form.focus {
                self.env_form.focus = index;
                self.env_form.revealed = false;
            }
            return;
        }
        let form = std::mem::take(&mut self.env_form);
        self.env_form_eye_buttons.clear();
        self.input_mode = InputMode::Normal;
        self.run_execute(&form.service_id, &form.label, &form.envs());
    }

    /// A click in the form: on an eye, show or mask that field. Anything else in the
    /// form, or behind it, does nothing.
    pub fn click_env_form(&mut self, column: u16, row: u16) {
        let position = Position::new(column, row);
        if let Some((index, _)) = self
            .env_form_eye_buttons
            .iter()
            .find(|(_, area)| area.contains(position))
            .cloned()
        {
            self.env_form_toggle_reveal(index);
        }
    }
}

/// The popup. Each field is one row: name, value (masked unless revealed) and, at the
/// right edge, the eye. The eyes' areas are kept for the click hit test.
pub fn draw(frame: &mut Frame, app: &mut App) {
    app.env_form_eye_buttons.clear();
    let form = &app.env_form;
    let shown = form.specs.len().min(VISIBLE_FIELDS);
    let area = centered_rect(86, shown as u16 + 12, frame.size());
    frame.render_widget(Clear, area);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(accent()))
        .style(Style::default().fg(text_colour()).bg(popup_background()))
        .title(Span::styled(
            format!(" {} ", app.input_title.to_uppercase()),
            Style::default().fg(accent()).bold(),
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height == 0 || inner.width < 8 {
        return;
    }

    let intro = Line::from(Span::styled(
        "The service asks for these. * required, the rest can stay empty.",
        Style::default().fg(muted()),
    ));
    frame.render_widget(Paragraph::new(intro), Rect { height: 1, ..inner });

    let name_width = form
        .specs
        .iter()
        .map(|spec| display_width(&spec.name) + 1)
        .max()
        .unwrap_or(0)
        .min(24);
    let first = form.scroll();
    let mut eyes = Vec::new();
    for (row, index) in (first..form.specs.len()).take(shown).enumerate() {
        let y = inner.y + 2 + row as u16;
        if y >= inner.bottom() {
            break;
        }
        let spec = &form.specs[index];
        let focused = index == form.focus;
        let revealed = form.is_revealed(index);
        let eye = if revealed { EYE_HIDE } else { EYE_SHOW };
        let eye_width = display_width(eye) as u16;

        let name = format!("{}{}", spec.name, if spec.required { "*" } else { "" });
        let value_width =
            (inner.width as usize).saturating_sub(2 + name_width + 1 + 1 + eye_width as usize + 1);
        let value = &form.values[index];
        let drawn = if revealed {
            tail(value, value_width)
        } else if value.is_empty() {
            String::new()
        } else {
            MASK.to_string()
        };
        let mut spans = vec![
            Span::styled(if focused { "▸ " } else { "  " }, Style::default().fg(accent()).bold()),
            Span::styled(
                format!("{:<width$} ", truncate_ellipsis(&name, name_width), width = name_width),
                if focused {
                    Style::default().fg(text_colour()).bold()
                } else {
                    Style::default().fg(muted())
                },
            ),
            Span::styled(drawn, Style::default().fg(good())),
        ];
        if focused {
            spans.push(Span::styled("▏", Style::default().fg(accent()).bold()));
        }
        frame.render_widget(Paragraph::new(Line::from(spans)), Rect::new(inner.x, y, inner.width, 1));

        let eye_area = Rect::new(inner.right().saturating_sub(eye_width + 1), y, eye_width, 1);
        frame.render_widget(
            Paragraph::new(Span::styled(eye, Style::default().fg(accent()).bold())),
            eye_area,
        );
        eyes.push((index, eye_area));
    }

    let mut lines: Vec<Line> = Vec::new();
    if form.specs.len() > shown {
        lines.push(Line::from(Span::styled(
            format!("{} of {} shown · ↑/↓ for the rest", shown, form.specs.len()),
            Style::default().fg(muted()),
        )));
    }
    if let Some(spec) = form.specs.get(form.focus) {
        let need = if spec.required {
            format!("Required by network {}.", spec.networks.join("; "))
        } else {
            "Optional: left empty, it is not set.".to_string()
        };
        lines.push(Line::from(Span::styled(need, Style::default().fg(muted()))));
        if !spec.tags.is_empty() {
            lines.push(Line::from(Span::styled(
                format!("Format: {}", spec.tags.join(", ")),
                Style::default().fg(muted()),
            )));
        }
        if !spec.prose.is_empty() {
            lines.push(Line::from(Span::styled(spec.prose.clone(), Style::default().fg(muted()))));
        }
    }
    lines.push(match &form.error {
        Some(error) => Line::from(Span::styled(format!("!! {error}"), Style::default().fg(bad()))),
        None => Line::from(""),
    });
    lines.push(Line::from(Span::styled(
        "Tab/↑/↓ move · Enter next, runs on the last · Ctrl+R or 👁 show · Ctrl+U clear · Esc cancel",
        Style::default().fg(warn()),
    )));
    let top = inner.y + 3 + shown as u16;
    if top < inner.bottom() {
        frame.render_widget(
            Paragraph::new(lines).wrap(Wrap { trim: false }),
            Rect::new(inner.x, top, inner.width, inner.bottom() - top),
        );
    }
    app.env_form_eye_buttons = eyes;
}

/// The end of `text` that fits in `width` columns: what is being typed stays in view.
fn tail(text: &str, width: usize) -> String {
    if display_width(text) <= width {
        return text.to_string();
    }
    let mut kept: Vec<char> = Vec::new();
    let mut used = 1; // the leading ellipsis
    for character in text.chars().rev() {
        let character_width = display_width(&character.to_string());
        if used + character_width > width {
            break;
        }
        used += character_width;
        kept.push(character);
    }
    kept.reverse();
    format!("…{}", kept.into_iter().collect::<String>())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{App, InputMode};
    use crossterm::event::{
        KeyCode, KeyEvent, KeyEventKind, KeyEventState, KeyModifiers, MouseButton, MouseEvent,
        MouseEventKind,
    };
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn spec(name: &str, required: bool) -> EnvSpec {
        EnvSpec {
            name: name.to_string(),
            tags: vec!["text".to_string()],
            prose: format!("what {name} is for"),
            required,
            networks: if required { vec!["pow:ergo".to_string()] } else { Vec::new() },
        }
    }

    /// The form open on BLOCK (required), TOKEN and LOG (optional).
    fn open_form() -> App {
        let mut app = App::new();
        app.on_service_envs(
            "svc-abc".to_string(),
            "hello-world".to_string(),
            Ok(vec![spec("BLOCK", true), spec("TOKEN", false), spec("LOG", false)]),
        );
        app
    }

    fn type_text(app: &mut App, text: &str) {
        text.chars().for_each(|character| app.env_form_type(character));
    }

    fn screen(app: &mut App) -> String {
        let mut terminal = Terminal::new(TestBackend::new(110, 30)).unwrap();
        terminal.draw(|frame| draw(frame, app)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        (0..buffer.area.height)
            .map(|row| {
                (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn key(app: &mut App, modifiers: KeyModifiers, code: KeyCode) {
        let event = KeyEvent {
            code,
            modifiers,
            kind: KeyEventKind::Press,
            state: KeyEventState::NONE,
        };
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(crate::handler::handle_key_events(event, app)).unwrap();
    }

    fn click(app: &mut App, column: u16, row: u16) {
        let event = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            modifiers: KeyModifiers::NONE,
        };
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        rt.block_on(crate::handler::handle_mouse_events(event, app)).unwrap();
    }

    fn eye(app: &App, index: usize) -> Rect {
        app.env_form_eye_buttons
            .iter()
            .find(|(field, _)| *field == index)
            .map(|(_, area)| *area)
            .expect("the field has an eye")
    }

    #[test]
    fn it_reads_the_envs_of_service_envs_json() {
        let stdout = "ℹ️  acquiring…\n{\"service\": \"ab\", \"envs\": [{\"name\": \"BLOCK\", \"tags\": [\"hex\"], \"prose\": \"tip\", \"required\": true, \"networks\": [\"pow:ergo\"]}], \"read_at\": 1}\n";
        let specs = parse_service_envs(stdout).unwrap();
        assert_eq!(specs.len(), 1);
        assert_eq!(specs[0].name, "BLOCK");
        assert_eq!(specs[0].tags, vec!["hex"]);
        assert!(specs[0].required);
        assert_eq!(specs[0].networks, vec!["pow:ergo"]);
    }

    #[test]
    fn it_reports_the_error_of_service_envs_json() {
        let error = parse_service_envs("{\"error\": \"not on this node\"}").unwrap_err();
        assert_eq!(error, "not on this node");
        assert!(parse_service_envs("").is_err());
        assert!(parse_service_envs("{\"envs\": [{\"tags\": []}]}").is_err());
    }

    #[test]
    fn execute_never_asks_on_the_tuis_terminal() {
        let args = execute_args(
            "svc",
            &[("A".to_string(), "1".to_string()), ("B".to_string(), "two words".to_string())],
        );
        assert_eq!(args, vec!["execute", "--no-input", "-e", "A", "1", "-e", "B", "two words", "svc"]);
    }

    #[test]
    fn a_service_with_envs_opens_the_form_and_runs_nothing() {
        let app = open_form();
        assert_eq!(app.input_mode, InputMode::ExecuteEnvs);
        assert_eq!(app.env_form.specs.len(), 3);
        assert!(app.command_task.is_none());
    }

    #[test]
    fn an_unreadable_answer_runs_nothing() {
        let mut app = App::new();
        app.on_service_envs("svc".to_string(), "svc".to_string(), Err("boom".to_string()));
        assert_eq!(app.input_mode, InputMode::Normal);
        assert!(app.command_task.is_none());
        assert!(app.status.contains("boom"), "{}", app.status);
    }

    #[test]
    fn an_empty_required_field_stops_the_run_and_takes_the_focus() {
        let mut app = open_form();
        app.env_form_move(1);
        app.submit_env_form();
        assert_eq!(app.input_mode, InputMode::ExecuteEnvs);
        assert_eq!(app.env_form.focus, 0);
        assert!(app.env_form.error.as_deref().unwrap().contains("BLOCK is required"));
        assert!(app.command_task.is_none());
    }

    #[test]
    fn only_fields_with_a_value_are_sent() {
        let mut app = open_form();
        type_text(&mut app, "tip");
        app.env_form_move(2);
        type_text(&mut app, "debug");
        assert_eq!(
            app.env_form.envs(),
            vec![("BLOCK".to_string(), "tip".to_string()), ("LOG".to_string(), "debug".to_string())]
        );
    }

    #[test]
    fn values_are_masked_by_default() {
        let mut app = open_form();
        type_text(&mut app, "s3cr3t");
        let drawn = screen(&mut app);
        assert!(!drawn.contains("s3cr3t"), "{drawn}");
        assert!(drawn.contains(MASK), "{drawn}");
        assert!(drawn.contains(EYE_SHOW), "{drawn}");
    }

    #[test]
    fn ctrl_r_shows_and_masks_the_focused_value() {
        let mut app = open_form();
        type_text(&mut app, "s3cr3t");

        key(&mut app, KeyModifiers::CONTROL, KeyCode::Char('r'));
        let drawn = screen(&mut app);
        assert!(drawn.contains("s3cr3t"), "{drawn}");
        assert!(drawn.contains(EYE_HIDE), "{drawn}");

        key(&mut app, KeyModifiers::CONTROL, KeyCode::Char('r'));
        assert!(!screen(&mut app).contains("s3cr3t"));
    }

    #[test]
    fn a_click_on_the_eye_shows_the_value_and_a_second_masks_it() {
        let mut app = open_form();
        type_text(&mut app, "s3cr3t");
        screen(&mut app);

        let area = eye(&app, 0);
        click(&mut app, area.x, area.y);
        assert!(screen(&mut app).contains("s3cr3t"));

        let area = eye(&app, 0);
        click(&mut app, area.x, area.y);
        assert!(!screen(&mut app).contains("s3cr3t"));
    }

    #[test]
    fn the_eye_of_another_field_focuses_it_and_masks_the_first() {
        let mut app = open_form();
        type_text(&mut app, "s3cr3t");
        app.env_form_move(1);
        type_text(&mut app, "t0k3n");
        app.env_form_move(-1);
        app.env_form_toggle_reveal(0);
        screen(&mut app);

        let area = eye(&app, 1);
        click(&mut app, area.x, area.y);
        let drawn = screen(&mut app);
        assert_eq!(app.env_form.focus, 1);
        assert!(drawn.contains("t0k3n"), "{drawn}");
        assert!(!drawn.contains("s3cr3t"), "{drawn}");
    }

    #[test]
    fn leaving_a_revealed_field_masks_it() {
        let mut app = open_form();
        type_text(&mut app, "s3cr3t");
        app.env_form_toggle_reveal(0);
        key(&mut app, KeyModifiers::NONE, KeyCode::Tab);
        assert!(!screen(&mut app).contains("s3cr3t"));
    }

    #[test]
    fn a_click_off_the_eyes_changes_nothing() {
        let mut app = open_form();
        type_text(&mut app, "s3cr3t");
        screen(&mut app);
        click(&mut app, 0, 0);
        assert_eq!(app.input_mode, InputMode::ExecuteEnvs);
        assert_eq!(app.env_form.focus, 0);
        assert!(!app.env_form.revealed);
    }

    #[test]
    fn the_focused_fields_need_and_prose_are_shown() {
        let mut app = open_form();
        let drawn = screen(&mut app);
        assert!(drawn.contains("BLOCK*"), "{drawn}");
        assert!(drawn.contains("Required by network pow:ergo"), "{drawn}");
        assert!(drawn.contains("what BLOCK is for"), "{drawn}");

        app.env_form_move(1);
        let drawn = screen(&mut app);
        assert!(drawn.contains("Optional"), "{drawn}");
    }

    #[test]
    fn a_long_list_scrolls_to_the_focus() {
        let mut app = App::new();
        let specs = (0..12).map(|i| spec(&format!("VAR_{i:02}"), false)).collect();
        app.on_service_envs("svc".to_string(), "svc".to_string(), Ok(specs));
        app.env_form_move(-1);
        let drawn = screen(&mut app);
        assert!(drawn.contains("▸ VAR_11"), "{drawn}");
        assert!(app.env_form_eye_buttons.iter().any(|(index, _)| *index == 11));
    }

    #[test]
    fn a_revealed_long_value_keeps_its_end_in_view() {
        assert_eq!(tail("abcdef", 4), "…def");
        assert_eq!(tail("abc", 4), "abc");
    }
}
