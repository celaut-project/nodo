//! Colour themes. Every colour the interface draws comes from one [`Theme`].
//!
//! Fields are named for the role they play (`good`, `warn`, `bad`) rather than for
//! the colour, so adding a widget does not add a field.
//!
//! `ubuntu` is the default and states its palette in RGB, because naming a specific
//! palette is what it is for. The others use named ANSI colours and so inherit what
//! the operator's terminal means by "red".

use ratatui::style::Color;

/// Every colour the interface can draw, by the role it plays.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    /// The name this theme is selected by, and what `ui.THEME` holds.
    pub name: &'static str,
    /// Selected tabs, focused borders, the node's own identity.
    pub accent: Color,
    /// Labels, dividers and help text: there to be read past rather than read.
    pub muted: Color,
    /// Working, healthy, running, local.
    pub good: Color,
    /// Worth a look, not yet a problem.
    pub warn: Color,
    /// Costs the operator something, and the ACTION REQUIRED banner.
    pub bad: Color,
    /// Ordinary text, for a value as opposed to its label.
    pub text: Color,
    /// Text drawn *on* an accent or warning background.
    pub inverse_text: Color,
    /// The whole frame's background, painted over `frame.size()` before anything
    /// else so no cell shows the terminal's own colour through.
    pub background: Color,
    /// The background of a popup, which has to cover whatever it is drawn over.
    pub popup_background: Color,
    /// The unfilled part of a gauge.
    pub gauge_background: Color,
    /// Four hues for things that sit side by side and need telling apart. A
    /// numbered set rather than named fields: the difference between them is the
    /// whole meaning.
    pub series: [Color; 4],
}

/// The Ubuntu terminal palette, and the default.
///
/// Ubuntu's own orange and aubergine, with the standard Ubuntu ANSI set for the
/// rest, so a nodo console beside `htop` in the same terminal agrees with it about
/// what red means.
pub const UBUNTU: Theme = Theme {
    name: "ubuntu",
    accent: Color::Rgb(0xE9, 0x54, 0x20),
    muted: Color::Rgb(0x77, 0x21, 0x6F),
    good: Color::Rgb(0x4E, 0x9A, 0x06),
    warn: Color::Rgb(0xC4, 0xA0, 0x00),
    bad: Color::Rgb(0xCC, 0x00, 0x00),
    text: Color::Rgb(0xFF, 0xFF, 0xFF),
    inverse_text: Color::Rgb(0x30, 0x0A, 0x24),
    background: Color::Rgb(0x30, 0x0A, 0x24),
    popup_background: Color::Rgb(0x30, 0x0A, 0x24),
    gauge_background: Color::Rgb(0x30, 0x0A, 0x24),
    series: [
        Color::Rgb(0x34, 0x65, 0xA4), // blue
        Color::Rgb(0x75, 0x50, 0x7B), // plum
        Color::Rgb(0x06, 0x98, 0x9A), // teal
        Color::Rgb(0xE9, 0x54, 0x20), // orange
    ],
};

/// The palette this console had before themes existed, kept so an operator used to
/// the cyan accent can keep it.
pub const DARK: Theme = Theme {
    name: "dark",
    accent: Color::Cyan,
    muted: Color::DarkGray,
    good: Color::Green,
    warn: Color::Yellow,
    bad: Color::Red,
    text: Color::White,
    inverse_text: Color::Black,
    background: Color::Black,
    popup_background: Color::Black,
    gauge_background: Color::Black,
    series: [
        Color::LightBlue,
        Color::LightMagenta,
        Color::LightGreen,
        Color::Yellow,
    ],
};

/// For a light terminal, where `White` text is invisible.
///
/// Not an inversion: the roles keep their hues, because red still has to mean
/// trouble. Only the colours chosen against a dark background change.
pub const LIGHT: Theme = Theme {
    name: "light",
    accent: Color::Blue,
    muted: Color::Gray,
    good: Color::Green,
    warn: Color::Rgb(0xB0, 0x70, 0x00),
    bad: Color::Red,
    text: Color::Black,
    inverse_text: Color::White,
    background: Color::White,
    popup_background: Color::White,
    gauge_background: Color::White,
    series: [Color::Blue, Color::Magenta, Color::Green, Color::Cyan],
};

/// No colour at all: weight and position carry everything.
///
/// A constraint rather than an aesthetic. Anything this theme cannot express is
/// something the interface was saying with colour *alone*, which is a thing it
/// should not be doing.
pub const MONO: Theme = Theme {
    name: "mono",
    accent: Color::White,
    muted: Color::DarkGray,
    good: Color::White,
    warn: Color::White,
    bad: Color::White,
    text: Color::White,
    inverse_text: Color::Black,
    // The one theme that keeps the terminal's own background. Mono exists for a
    // terminal whose colours are already a deliberate choice (a genuinely
    // monochrome one, a recording, a printout); painting over it would be this
    // theme asserting the single thing it is meant not to assert.
    background: Color::Reset,
    popup_background: Color::Black,
    gauge_background: Color::Black,
    series: [Color::White, Color::Gray, Color::White, Color::Gray],
};

/// Every theme, in the order the CONFIG page offers them.
pub const ALL: [Theme; 4] = [UBUNTU, DARK, LIGHT, MONO];

/// The names `ui.THEME` accepts, for the CONFIG page's picker and for an error
/// message that can list them.
pub const NAMES: [&str; 5] = ["default", "ubuntu", "dark", "light", "mono"];

impl Default for Theme {
    fn default() -> Self {
        UBUNTU
    }
}

impl Theme {
    /// The theme called `name`, or [`UBUNTU`] for anything unrecognised.
    ///
    /// An unknown name falls back rather than failing: a console that refuses to
    /// start over a misspelt colour scheme cannot be used to fix the misspelling.
    pub fn by_name(name: &str) -> Theme {
        let name = name.trim().to_ascii_lowercase();
        if name.is_empty() || name == "default" {
            return UBUNTU;
        }
        ALL.into_iter()
            .find(|theme| theme.name == name)
            .unwrap_or(UBUNTU)
    }

    /// The theme this run should use: `--theme` beats `NODO_TUI_THEME` beats
    /// `ui.THEME` beats the default, in increasing order of deliberateness.
    ///
    /// The flag and the variable exist so a theme can be tried without going through
    /// the config transaction, which would make comparing two themes a pair of node
    /// restarts.
    pub fn resolve(document: Option<&serde_yaml::Value>, argv: &[String]) -> Theme {
        if let Some(name) = theme_flag(argv) {
            return Theme::by_name(&name);
        }
        if let Ok(name) = std::env::var("NODO_TUI_THEME") {
            if !name.trim().is_empty() {
                return Theme::by_name(&name);
            }
        }
        let configured = document
            .and_then(|document| document.get("ui"))
            .and_then(|value| value.get("THEME"))
            .and_then(|value| value.as_str());
        match configured {
            Some(name) => Theme::by_name(name),
            None => UBUNTU,
        }
    }
}

/// Re-resolve the theme after `document` was reloaded from a fresh `config.yaml`.
///
/// A `ui.THEME` edit lands in the file, restarts the node, and reloads the TUI's
/// own copy of the document -- but until something calls this, the process-global
/// theme is stuck at whatever `main.rs` resolved before the first frame, so the
/// picker's change was invisible without quitting and relaunching the TUI. `--theme`
/// and `NODO_TUI_THEME` still win, using `std::env::args` directly rather than a
/// stored `argv`: neither one can have changed since the process started.
pub fn refresh_from_document(document: Option<&serde_yaml::Value>) {
    // Under test this runs on every `App::refresh_local`, on whichever thread
    // `cargo test` gave that test -- which would otherwise race the theme that
    // `ui::themes` pins for the length of a render. Production is single-threaded,
    // so the lock costs a wait that never happens outside a test binary.
    #[cfg(test)]
    let _guard = TEST_SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let argv: Vec<String> = std::env::args().skip(1).collect();
    set_current(Theme::resolve(document, &argv));
}

/// Serialises every test that reads or writes the process-global [`current`] theme,
/// in this module and in `ui::themes`, so two such tests on two `cargo test` threads
/// cannot see each other's palette mid-render.
#[cfg(test)]
pub(crate) static TEST_SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// `--theme <name>` or `--theme=<name>` from `argv`, whichever spelling was used.
///
/// Parsed by hand, like `--version` in `main.rs`: this binary takes three options,
/// and a CLI crate would be one more thing CI builds for every shipped target.
fn theme_flag(argv: &[String]) -> Option<String> {
    let mut args = argv.iter();
    while let Some(argument) = args.next() {
        if let Some(value) = argument.strip_prefix("--theme=") {
            return Some(value.to_string());
        }
        if argument == "--theme" {
            return args.next().cloned();
        }
    }
    None
}

/// The theme in force for this process.
///
/// A global, set once in `main.rs`, because a theme is a property of the *run*
/// rather than of any one widget: the alternative is a `&Theme` parameter on all
/// forty-odd draw functions for a value that never changes.
///
/// `RwLock` rather than `OnceLock` so the tests can pin a theme and put it back.
static CURRENT: std::sync::RwLock<Theme> = std::sync::RwLock::new(UBUNTU);

/// The theme in force. Every colour in `ui.rs` comes from here.
///
/// Falls back to the default on a poisoned lock: the worst case is one frame in the
/// wrong colour, which beats a console that cannot draw.
pub fn current() -> Theme {
    CURRENT.read().map(|theme| *theme).unwrap_or(UBUNTU)
}

/// Install the theme for this process. Called from `main.rs` at startup and again,
/// via [`refresh_from_document`], whenever a config write reloads the document.
pub fn set_current(theme: Theme) {
    if let Ok(mut current) = CURRENT.write() {
        *current = theme;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_is_the_ubuntu_palette() {
        assert_eq!(Theme::default(), UBUNTU);
        assert_eq!(Theme::by_name("default"), UBUNTU);
        assert_eq!(Theme::by_name(""), UBUNTU);
        // By value: this is the one theme whose purpose is a specific palette, so
        // "it is some orange" is not the property being claimed.
        assert_eq!(UBUNTU.accent, Color::Rgb(0xE9, 0x54, 0x20));
        assert_eq!(UBUNTU.background, Color::Rgb(0x30, 0x0A, 0x24));
        assert_eq!(UBUNTU.popup_background, Color::Rgb(0x30, 0x0A, 0x24));
        assert_eq!(UBUNTU.text, Color::Rgb(0xFF, 0xFF, 0xFF));
    }


    #[test]
    fn every_theme_is_reachable_by_its_name() {
        for theme in ALL {
            assert_eq!(Theme::by_name(theme.name), theme, "{}", theme.name);
            // Case is not a decision to get right in a YAML file.
            assert_eq!(
                Theme::by_name(&theme.name.to_ascii_uppercase()),
                theme,
                "{}",
                theme.name
            );
        }
    }

    /// A picker offering a name that silently falls back would be lying.
    #[test]
    fn every_advertised_name_resolves_to_a_real_theme() {
        for name in NAMES {
            let theme = Theme::by_name(name);
            assert!(
                name == "default" || theme.name == name,
                "{name} resolved to {}",
                theme.name
            );
        }
    }

    /// A console that refuses to start over a colour scheme cannot be used to fix
    /// the colour scheme.
    #[test]
    fn an_unknown_theme_falls_back_instead_of_failing() {
        assert_eq!(Theme::by_name("solarized-aubergine"), UBUNTU);
    }

    #[test]
    fn the_flag_is_read_in_both_spellings() {
        let flag = |args: &[&str]| {
            theme_flag(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>())
        };

        assert_eq!(flag(&["--theme", "mono"]).as_deref(), Some("mono"));
        assert_eq!(flag(&["--theme=light"]).as_deref(), Some("light"));
        assert_eq!(flag(&["--version"]), None);
        // A trailing `--theme` with nothing after it is not a name.
        assert_eq!(flag(&["--theme"]), None);
    }

    /// The flag is the most deliberate thing the operator did, so it wins.
    #[test]
    fn the_flag_beats_the_config() {
        let document: serde_yaml::Value =
            serde_yaml::from_str("ui:\n  THEME: dark\n").unwrap();

        assert_eq!(
            Theme::resolve(Some(&document), &["--theme=mono".to_string()]),
            MONO
        );
        assert_eq!(Theme::resolve(Some(&document), &[]), DARK);
    }

    #[test]
    fn a_config_with_no_ui_block_gets_the_default() {
        let document: serde_yaml::Value = serde_yaml::from_str("main:\n  STORAGE: x\n").unwrap();

        assert_eq!(Theme::resolve(Some(&document), &[]), UBUNTU);
        assert_eq!(Theme::resolve(None, &[]), UBUNTU);
    }

    /// Keeps mono honest as widgets are added: anything it cannot express is
    /// something the interface was saying with colour alone.
    #[test]
    fn the_mono_theme_uses_no_hue_at_all() {
        for colour in [MONO.accent, MONO.good, MONO.warn, MONO.bad, MONO.text] {
            assert!(
                matches!(colour, Color::White | Color::Gray | Color::DarkGray | Color::Black),
                "{colour:?} is a hue"
            );
        }
    }

    /// The failure this prevents is text that is simply not there.
    #[test]
    fn the_light_theme_is_legible_on_a_pale_background() {
        assert_eq!(LIGHT.text, Color::Black);
        assert_ne!(LIGHT.muted, Color::DarkGray);
        assert_ne!(LIGHT.popup_background, Color::Black);
        assert_eq!(LIGHT.background, Color::White);
    }

    /// Every theme but `mono` states a background. `mono` is the one that must not:
    /// it exists for a terminal whose colours are already a deliberate choice.
    #[test]
    fn only_the_mono_theme_defers_to_the_terminal_background() {
        for theme in [UBUNTU, DARK, LIGHT] {
            assert_ne!(theme.background, Color::Reset, "{}", theme.name);
            assert_ne!(theme.background, theme.text, "{}", theme.name);
        }
        assert_eq!(MONO.background, Color::Reset);
    }

    /// Series colours tell adjacent things apart, so they have to differ.
    #[test]
    fn the_series_colours_are_distinct_where_there_are_hues_to_spend() {
        for theme in [UBUNTU, DARK, LIGHT] {
            let mut seen = Vec::new();
            for colour in theme.series {
                assert!(
                    !seen.contains(&colour),
                    "{} repeats {colour:?} in its series",
                    theme.name
                );
                seen.push(colour);
            }
        }
    }
}
