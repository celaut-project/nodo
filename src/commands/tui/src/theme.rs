//! Colour themes, and the default that matches the terminal this runs in.
//!
//! Every colour the interface draws comes from one [`Theme`] struct. Before this,
//! `ui.rs` held four `const`s and seventy-odd bare `Color::` literals scattered
//! through the draw functions, which is not a theme with hard-coded values — it is
//! no theme at all, because half the palette was unreachable from any one place.
//!
//! **Roles, not colours.** The fields are named for what they mean (`good`, `warn`,
//! `bad`, `muted`, `accent`) rather than for what they look like, so a theme is a
//! set of answers rather than a lookup table. A theme that had to name "the colour
//! of the memory gauge" would need a new field for every widget ever added; one that
//! names "the colour of a thing that is fine" does not.
//!
//! **The default is Ubuntu.** `nodo` is installed by a bash script onto a Linux
//! server, and the terminal that script ran in is overwhelmingly GNOME Terminal on
//! Ubuntu. Matching its palette — the aubergine background, the `#E95420` orange —
//! means the console looks like part of the machine it is administering rather than
//! like an application that has been dropped on top of it.
//!
//! **Indexed colours where the terminal has an opinion, RGB where it does not.** The
//! `dark`, `light` and `mono` themes use `Color::Indexed`/named ANSI colours, so they
//! inherit whatever the operator has configured their terminal to mean by "red" —
//! which is the point of those themes. `ubuntu` states its palette in RGB, because
//! naming a specific palette is what it is for, and a themed console that changed
//! colour with the terminal's own settings would not be one.

use ratatui::style::Color;

/// Every colour the interface can draw, by the role it plays.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    /// The name this theme is selected by, which is also what `ui.THEME` holds.
    pub name: &'static str,
    /// Selected tabs, focused borders, the node's own identity. The one colour that
    /// means "this is the thing you are looking at".
    pub accent: Color,
    /// Labels, dividers, help text, and anything else that is there to be read past
    /// rather than read.
    pub muted: Color,
    /// Working, healthy, running, local.
    pub good: Color,
    /// Worth a look, not yet a problem: a stopped node, a remote instance, an
    /// unapplied edit.
    pub warn: Color,
    /// Costs the operator something: an unacknowledged payment, a refused deposit, a
    /// penalty — and the ACTION REQUIRED banner.
    pub bad: Color,
    /// Ordinary text, for a value as opposed to its label.
    pub text: Color,
    /// Text drawn *on* an accent or warning background, where the ordinary text
    /// colour would be unreadable.
    pub inverse_text: Color,
    /// The background of a popup, which has to cover whatever it is drawn over.
    pub popup_background: Color,
    /// The unfilled part of a gauge.
    pub gauge_background: Color,
    /// Four hues for cards and charts that sit side by side and need telling apart.
    /// Not roles — the difference between them is the whole meaning — so they are a
    /// numbered set rather than five more named fields.
    pub series: [Color; 4],
}

/// The Ubuntu terminal palette, and the default.
///
/// `#E95420` is Ubuntu's orange and `#300A24` the aubergine GNOME Terminal ships as
/// its background there. The greens, yellows and reds are the standard Ubuntu ANSI
/// set rather than invented neighbours, so a nodo console sitting beside `htop` or
/// `journalctl` in the same terminal agrees with them about what red means.
pub const UBUNTU: Theme = Theme {
    name: "ubuntu",
    accent: Color::Rgb(0xE9, 0x54, 0x20),
    muted: Color::Rgb(0x77, 0x21, 0x6F),
    good: Color::Rgb(0x4E, 0x9A, 0x06),
    warn: Color::Rgb(0xC4, 0xA0, 0x00),
    bad: Color::Rgb(0xCC, 0x00, 0x00),
    text: Color::Rgb(0xFF, 0xFF, 0xFF),
    inverse_text: Color::Rgb(0x30, 0x0A, 0x24),
    popup_background: Color::Rgb(0x30, 0x0A, 0x24),
    gauge_background: Color::Rgb(0x30, 0x0A, 0x24),
    series: [
        Color::Rgb(0x34, 0x65, 0xA4), // blue
        Color::Rgb(0x75, 0x50, 0x7B), // plum
        Color::Rgb(0x06, 0x98, 0x9A), // teal
        Color::Rgb(0xE9, 0x54, 0x20), // orange
    ],
};

/// The palette this console had before themes existed.
///
/// Kept as a theme rather than deleted, because an operator who is used to the cyan
/// accent should be able to keep it — and because it is the one palette every
/// screenshot and every existing test was written against.
pub const DARK: Theme = Theme {
    name: "dark",
    accent: Color::Cyan,
    muted: Color::DarkGray,
    good: Color::Green,
    warn: Color::Yellow,
    bad: Color::Red,
    text: Color::White,
    inverse_text: Color::Black,
    popup_background: Color::Black,
    gauge_background: Color::Black,
    series: [
        Color::LightBlue,
        Color::LightMagenta,
        Color::LightGreen,
        Color::Yellow,
    ],
};

/// For a light terminal, where `DarkGray` on white is unreadable and `White` text is
/// invisible.
///
/// The swap is not "invert the dark theme": the roles keep their hues, because red
/// still has to mean trouble. What changes is the two that were chosen against a dark
/// background — `text` becomes black, `muted` becomes a grey that is dark enough to
/// read on white — and the blues and greens step down to their non-light variants,
/// which are the ones with enough contrast against a pale background.
pub const LIGHT: Theme = Theme {
    name: "light",
    accent: Color::Blue,
    muted: Color::Gray,
    good: Color::Green,
    warn: Color::Rgb(0xB0, 0x70, 0x00),
    bad: Color::Red,
    text: Color::Black,
    inverse_text: Color::White,
    popup_background: Color::White,
    gauge_background: Color::White,
    series: [Color::Blue, Color::Magenta, Color::Green, Color::Cyan],
};

/// No colour at all: weight and position carry everything.
///
/// For a terminal that is genuinely monochrome, for an operator who cannot
/// distinguish the hues this interface leans on, and for a recording or a screenshot
/// that has to survive being printed. It is a real constraint rather than an
/// aesthetic: anything this theme cannot express is something the interface was
/// saying with colour *alone*, which is a thing it should not be doing. The gauges,
/// the status words and the ACTION REQUIRED tag all still read, because each of them
/// also carries text.
pub const MONO: Theme = Theme {
    name: "mono",
    accent: Color::White,
    muted: Color::DarkGray,
    good: Color::White,
    warn: Color::White,
    bad: Color::White,
    text: Color::White,
    inverse_text: Color::Black,
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
    /// `default` is an accepted spelling of `ubuntu` rather than a fifth theme: it is
    /// what somebody writes in a config file meaning "whatever you think", and
    /// resolving it here means `tui.theme: default` and an absent key produce the
    /// same interface.
    ///
    /// An unknown name falls back rather than failing. A console that refuses to
    /// start over a misspelt colour scheme is a console that cannot be used to fix
    /// the misspelling — and this is the *only* setting in the interface where that
    /// trade is obviously right, because nothing about it can be wrong in a way that
    /// matters.
    pub fn by_name(name: &str) -> Theme {
        let name = name.trim().to_ascii_lowercase();
        if name.is_empty() || name == "default" {
            return UBUNTU;
        }
        ALL.into_iter()
            .find(|theme| theme.name == name)
            .unwrap_or(UBUNTU)
    }

    /// The theme this run should use, from the config document, the environment and
    /// the command line, in increasing order of deliberateness.
    ///
    /// `--theme` beats `NODO_TUI_THEME` beats `ui.THEME` beats the default. The flag
    /// and the variable exist so a theme can be tried without editing config.yaml and
    /// restarting the node through the config transaction — which is a heavy price
    /// for looking at a colour, and would make comparing two themes a pair of node
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

/// `--theme <name>` or `--theme=<name>` from `argv`, whichever spelling was used.
///
/// Parsed by hand, like `--version` in `main.rs`: this binary takes three options in
/// total, and a CLI crate to recognise them would be one more thing CI has to build
/// for every shipped target.
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
/// A global, set once in `main.rs` before the first frame, because a theme is a
/// property of the *run* rather than of any one widget. The alternative is passing a
/// `&Theme` through every one of the forty-odd draw functions and their helpers,
/// several of which (`section_block`, `metric_line`, `spark`) exist precisely to be
/// callable without ceremony — and a parameter threaded through everything for a
/// value that never changes is how a codebase acquires forty signatures nobody can
/// read.
///
/// `RwLock` rather than `OnceLock` so the tests can pin a theme and put it back.
/// Reads are uncontended in the single-threaded draw path.
static CURRENT: std::sync::RwLock<Theme> = std::sync::RwLock::new(UBUNTU);

/// The theme in force. Every colour in `ui.rs` comes from here.
///
/// Falls back to the default if the lock is poisoned: a panicking draw must not turn
/// into a console that cannot draw at all, and the worst case is that one frame is
/// the wrong colour.
pub fn current() -> Theme {
    CURRENT.read().map(|theme| *theme).unwrap_or(UBUNTU)
}

/// Install the theme for this process. Called once from `main.rs`.
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
        // The colours the issue asks for, by value: this is the one theme whose
        // whole purpose is to be a specific palette, so "it is some orange" is not
        // the property being claimed.
        assert_eq!(UBUNTU.accent, Color::Rgb(0xE9, 0x54, 0x20));
        assert_eq!(UBUNTU.popup_background, Color::Rgb(0x30, 0x0A, 0x24));
        assert_eq!(UBUNTU.text, Color::Rgb(0xFF, 0xFF, 0xFF));
    }

    #[test]
    fn every_theme_is_reachable_by_its_name() {
        for theme in ALL {
            assert_eq!(Theme::by_name(theme.name), theme, "{}", theme.name);
            // Case is not a decision the operator should have to get right in a
            // YAML file.
            assert_eq!(
                Theme::by_name(&theme.name.to_ascii_uppercase()),
                theme,
                "{}",
                theme.name
            );
        }
    }

    /// Every advertised name resolves. A picker offering a value that falls back to
    /// something else would be lying about what it does.
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

    /// A misspelt theme falls back rather than failing. A console that refuses to
    /// start over a colour scheme cannot be used to fix the colour scheme.
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
        // A trailing `--theme` with nothing after it is not a name. Falling through
        // to the default beats treating the next thing on the line as one.
        assert_eq!(flag(&["--theme"]), None);
    }

    /// The precedence the resolution claims: the flag is the most deliberate thing
    /// the operator did, so it wins.
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

    /// The mono theme really is monochrome. It is not decoration: anything it cannot
    /// express is something the interface was saying with colour alone, and this is
    /// what keeps that honest as widgets are added.
    #[test]
    fn the_mono_theme_uses_no_hue_at_all() {
        for colour in [MONO.accent, MONO.good, MONO.warn, MONO.bad, MONO.text] {
            assert!(
                matches!(colour, Color::White | Color::Gray | Color::DarkGray | Color::Black),
                "{colour:?} is a hue"
            );
        }
    }

    /// The light theme does not draw white on white, or near-white grey on white.
    /// The failure it exists to prevent is text that is simply not there.
    #[test]
    fn the_light_theme_is_legible_on_a_pale_background() {
        assert_eq!(LIGHT.text, Color::Black);
        assert_ne!(LIGHT.muted, Color::DarkGray);
        assert_ne!(LIGHT.popup_background, Color::Black);
    }

    /// Series colours are for telling adjacent things apart, so they have to differ
    /// from each other in every theme that has hues to spend.
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
