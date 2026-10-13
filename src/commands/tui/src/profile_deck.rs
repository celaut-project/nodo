//! Presentation data for the first-run profile deck: a character, a colour scheme and
//! five attribute bars for each profile (docs/proposals/profile-deck.md, phase 2).
//!
//! Nothing here writes configuration. A profile's *meaning* stays in `cell.rs`, and
//! this module only decides how it looks. Two rules keep the two from drifting:
//!
//! - The attribute bars are computed from the profile's own `writes`, never typed in.
//!   A card that said "EXPOSURE 0" for a profile that opens the node to the internet
//!   would be a lie in the one place the operator reads before choosing.
//! - A test requires a persona for every id in `cell::profiles()`, so a new profile
//!   without a character fails the build instead of showing a blank card.

use crate::cell::{self, Profile};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};

/// Width and height of a sprite, in pixels. One terminal cell shows two vertical
/// pixels, so a sprite is `SPRITE_WIDTH` cells wide and `SPRITE_HEIGHT / 2` tall.
pub const SPRITE_WIDTH: usize = 14;
pub const SPRITE_HEIGHT: usize = 24;

/// Rows of pixels, one character per pixel:
/// `.` transparent, `k` outline, `s` skin, `w` white, and four colours of the
/// persona's palette: `a` primary, `b` secondary, `d` dark, `g` glow.
pub type Sprite = [&'static str; SPRITE_HEIGHT];

/// The neon scheme of one persona. Independent of the global theme on purpose: the
/// deck is an identity screen, so it keeps its own look in every theme.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Palette {
    pub primary: (u8, u8, u8),
    pub secondary: (u8, u8, u8),
    pub dark: (u8, u8, u8),
    pub glow: (u8, u8, u8),
}

impl Palette {
    pub fn primary_color(&self) -> Color {
        rgb(self.primary)
    }
}

fn rgb((r, g, b): (u8, u8, u8)) -> Color {
    Color::Rgb(r, g, b)
}

const OUTLINE: (u8, u8, u8) = (25, 25, 40);
const SKIN: (u8, u8, u8) = (234, 178, 140);
const WHITE: (u8, u8, u8) = (235, 235, 245);

/// One character on the deck.
#[derive(Debug, Clone, Copy)]
pub struct Persona {
    /// Matches `cell::Profile::id`.
    pub id: &'static str,
    pub codename: &'static str,
    pub class: &'static str,
    /// One short line in the voice of the character.
    pub tagline: &'static str,
    pub palette: Palette,
    pub sprite: &'static Sprite,
}

const SPRITE_JUST_ME: Sprite = [
    ".....aaaa.....",
    "...aaaaaaaa...",
    ".aaaaaaaaaaaa.",
    "aaaaaddddaaaaa",
    "..dddddddddd..",
    "....kssssk....",
    "....skssks....",
    "....ssssss....",
    ".....kssk.....",
    "...dddssddd..w",
    "..dddddddddddw",
    ".dddaddddaddw.",
    ".dddaddddaddw.",
    ".dddaddddaddw.",
    ".ddbbbbbbbbdw.",
    ".dddddbbdddww.",
    ".dddddddddddw.",
    ".ddddddddddd..",
    "..dddddddddd..",
    "..dddd..dddd..",
    "..kkkk..kkkk..",
    "..kkkk..kkkk..",
    ".bbbbb..bbbbb.",
    ".bbbbb..bbbbb.",
];

const SPRITE_CAUTIOUS: Sprite = [
    "....bbbbbb....",
    "...bbbbbbbb...",
    "..bbbbbbbbbb..",
    ".bbbbbbbbbbbb.",
    "....ssssss....",
    "...kkkkkkkk...",
    "...kgkkkkgk...",
    "....ssssss....",
    ".....ssss.....",
    "..aaawwwwaaa..",
    ".aaaaawwaaaaa.",
    ".aaaabwwbaaaa.",
    ".aaaaabbaaaaa.",
    ".aaaaaabaaaaa.",
    "sbaaaaabaaaaab",
    "sbaaaaaaaaaaab",
    ".baaaaaaaaaab.",
    "..aaaaaaaaaa..",
    "..aaaaaaaaaa..",
    "..aaa....aaa..",
    "..kkk....kkk..",
    "..kkk....kkk..",
    ".kkkk....kkkk.",
    ".kkkk....kkkk.",
];

const SPRITE_OPEN_RENTER: Sprite = [
    "......aa......",
    ".....aaaa.....",
    "....aaaaaa....",
    "....aaaaaa....",
    "....ssssss....",
    "...gggggggg...",
    "...gggggggg...",
    "....ssssss....",
    ".....ssss.....",
    "..bbbssssbbb..",
    ".bbbbbaabbbbb.",
    ".bbabbaabbabb.",
    ".bbabbaabbabb.",
    ".sbbbbaabbbbs.",
    ".sbbbbbbbbbbs.",
    "..dbbbbbbbbd..",
    "..dddddddddd..",
    "..dddd..dddd..",
    "..dddd..dddd..",
    "..kkkk..kkkk..",
    "..kkkk..kkkk..",
    "..aaaa..aaaa..",
    ".aaaaa..aaaaa.",
    ".aaaaa..aaaaa.",
];

const SPRITE_LAN_LAB: Sprite = [
    "....kkkkkk....",
    "...kkkkkkkk...",
    "...kssssssk...",
    "..aggkssggka..",
    "...ssssssss...",
    "....ssssss....",
    ".....kssk.....",
    "...wwwssswww..",
    "..wwwwawwwwww.",
    ".wwwawwawwwwaw",
    ".wwwawwawwwwaw",
    ".wwwawwawwwwaw",
    "gwwwwwwwwwwwww",
    "gwwbbwwwwbbwww",
    "gwwwwwwwwwwww.",
    "gwwwwwwwwwwww.",
    ".wwwwwwwwwwww.",
    "..wwwwwwwwww..",
    "..wwwwwwwwww..",
    "..aaaa..aaaa..",
    "..aaaa..aaaa..",
    "..kkkk..kkkk..",
    ".kkkkk..kkkkk.",
    ".kkkkk..kkkkk.",
];

const SPRITE_WORKBENCH: Sprite = [
    ".....dddd.....",
    "...dddaaddd...",
    "..dddaaaaddd..",
    "..ddaaaaaadd..",
    "..dakkkkkkad..",
    "..dakgkkgkad..",
    "..dakkkkkkad..",
    "..ddakkkkadd..",
    "...ddaaaadd...",
    "..aaaaddaaaa..",
    ".aaaaaaaaaaaa.",
    ".aaabaaaabaaa.",
    ".aaabaaaabaaa.",
    "sbaaaaaaaaaabs",
    "skkkkkkkkkkkks",
    "skgggggggggkks",
    ".kkgkgkgkgkkk.",
    "..kkkkkkkkkk..",
    "..aaaaaaaaaa..",
    "..bbbb..bbbb..",
    "..bbbb..bbbb..",
    "..kkkk..kkkk..",
    ".kkkkk..kkkkk.",
    ".kkkkk..kkkkk.",
];

static PERSONAS: &[Persona] = &[
    Persona {
        id: "just-me",
        codename: "RONIN",
        class: "Lone operator",
        tagline: "\"My machine. My rules. Nobody in.\"",
        palette: Palette {
            primary: (57, 255, 20),
            secondary: (120, 80, 40),
            dark: (14, 90, 24),
            glow: (180, 255, 160),
        },
        sprite: &SPRITE_JUST_ME,
    },
    Persona {
        id: "cautious",
        codename: "FIXER",
        class: "Careful broker",
        tagline: "\"I rent it out. On a short leash.\"",
        palette: Palette {
            primary: (255, 225, 0),
            secondary: (60, 50, 70),
            dark: (150, 120, 0),
            glow: (255, 255, 255),
        },
        sprite: &SPRITE_CAUTIOUS,
    },
    Persona {
        id: "open-renter",
        codename: "NETRUNNER",
        class: "Open trader",
        tagline: "\"Every cycle sold. The grid pays.\"",
        palette: Palette {
            primary: (255, 0, 200),
            secondary: (60, 0, 120),
            dark: (130, 0, 100),
            glow: (0, 255, 255),
        },
        sprite: &SPRITE_OPEN_RENTER,
    },
    Persona {
        id: "lan-lab",
        codename: "RIPPERDOC",
        class: "Mesh technician",
        tagline: "\"My machines. One mesh. Free for all.\"",
        palette: Palette {
            primary: (0, 240, 255),
            secondary: (255, 0, 128),
            dark: (0, 110, 130),
            glow: (255, 255, 120),
        },
        sprite: &SPRITE_LAN_LAB,
    },
    Persona {
        id: "workbench",
        codename: "TINKERER",
        class: "Developer",
        tagline: "\"Break it here. Nothing is real money.\"",
        palette: Palette {
            primary: (255, 120, 0),
            secondary: (50, 50, 60),
            dark: (140, 60, 0),
            glow: (0, 255, 120),
        },
        sprite: &SPRITE_WORKBENCH,
    },
];

pub fn personas() -> &'static [Persona] {
    PERSONAS
}

/// The persona for a profile id, if there is one.
pub fn persona(id: &str) -> Option<&'static Persona> {
    PERSONAS.iter().find(|persona| persona.id == id)
}

// --- Attribute bars --------------------------------------------------------

/// Highest value of a bar.
pub const STAT_MAX: u8 = 5;

/// The five bars on a card, each `0..=STAT_MAX`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stats {
    /// How reachable the node is from outside: open to the internet, taking
    /// deposits from strangers, announcing itself, publishing its address.
    pub exposure: u8,
    /// How much the profile is set up to earn: deposits, scarcity pricing, refill,
    /// low-demand work, no host ceiling. Simulated payments earn nothing, and free
    /// capacity earns less.
    pub income: u8,
    /// How much the node shares with other nodes: delegation, announcing, full
    /// admission, no isolation between internal children, private addresses.
    pub openness: u8,
    /// How hard the node is closed: isolation, tunnel-only exposure, integrity
    /// checks, denied device nodes, host ceilings.
    pub isolation: u8,
    /// How much can go wrong on purpose: debt allowed, debug logs, lenient
    /// admission, no host ceiling.
    pub risk: u8,
}

impl Stats {
    /// Label and value of each bar, in the order a card shows them.
    pub fn bars(&self) -> [(&'static str, u8); 5] {
        [
            ("EXPOSURE", self.exposure),
            ("INCOME", self.income),
            ("OPENNESS", self.openness),
            ("ISOLATION", self.isolation),
            ("RISK", self.risk),
        ]
    }
}

fn value<'a>(profile: &'a Profile, key: &str) -> Option<&'a str> {
    profile
        .writes
        .iter()
        .find(|(path, _)| *path == key)
        .map(|(_, value)| *value)
}

fn is(profile: &Profile, key: &str, expected: &str) -> bool {
    value(profile, key) == Some(expected)
}

fn number(profile: &Profile, key: &str) -> f64 {
    value(profile, key)
        .and_then(|text| text.parse::<f64>().ok())
        .unwrap_or(0.0)
}

fn points(conditions: &[bool]) -> u8 {
    (conditions.iter().filter(|met| **met).count() as u8).min(STAT_MAX)
}

/// The bars of a profile, read from its `writes`.
pub fn stats(profile: &Profile) -> Stats {
    let deposits = is(profile, "client.ACCEPT_NEW_DEPOSITS", "true");
    let host_limits = is(profile, "host_limits.ENABLED", "true");
    let simulated = is(profile, "general_flags.SIMULATE_PAYMENTS", "true");
    let free = number(profile, "free_tier.FREE_WHILE_SCARCITY_BELOW") > 0.0;

    // A condition listed twice weighs two points: being open to the internet is
    // worth more to the bar than one more announcement.
    let exposure = points(&[
        !is(profile, "network.DISABLE_EXPOSE_OUTSIDE", "true"),
        !is(profile, "network.DISABLE_EXPOSE_OUTSIDE", "true"),
        deposits,
        is(
            profile,
            "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS",
            "true",
        ),
        is(
            profile,
            "general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF",
            "true",
        ),
    ]);

    let earning = points(&[
        deposits,
        number(profile, "pricing.SCARCITY_MAX_MULTIPLIER") > 1.0,
        is(profile, "deposits.AUTOMATIC_REFILL", "true"),
        is(profile, "low_demand.ENABLED", "true"),
        deposits && !host_limits,
    ]);
    let income = if simulated {
        0
    } else if free {
        earning.saturating_sub(1)
    } else {
        earning
    };

    let openness = points(&[
        is(profile, "network.DELEGATE_EXECUTION", "true"),
        is(
            profile,
            "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS",
            "true",
        ),
        is(profile, "workload_admission.POLICY", "full"),
        is(profile, "network.ISOLATE_INTERNAL_CHILDREN", "false"),
        is(profile, "network.ANNOUNCE_PRIVATE_ADDRESSES", "true"),
    ]);

    let isolation = points(&[
        is(profile, "network.ISOLATE_INTERNAL_CHILDREN", "true"),
        is(profile, "network.DISABLE_EXPOSE_OUTSIDE", "true"),
        is(profile, "hashing.CHECK_INTEGRITY_ON_SERVE", "true"),
        is(
            profile,
            "virtualizers.ch.SECURITY.DEVICE_NODES_POLICY",
            "deny",
        ),
        host_limits,
    ]);

    // Debt weighs two points, for the same reason as the open exposure above.
    let risk = points(&[
        is(profile, "costs.ALLOW_DEBT", "true"),
        is(profile, "costs.ALLOW_DEBT", "true"),
        is(profile, "logs.DEBUG_MODE", "true"),
        is(profile, "workload_admission.ON_UNSATISFIABLE", "warn"),
        !host_limits,
    ]);

    Stats {
        exposure,
        income,
        openness,
        isolation,
        risk,
    }
}

/// Three or four short lines about what the profile does, read from its `writes`.
/// What the card shows under the bars.
pub fn traits(profile: &Profile) -> Vec<String> {
    let mut lines = Vec::new();
    lines.push(
        if is(profile, "network.DISABLE_EXPOSE_OUTSIDE", "true") {
            "Tunnel only: no direct exposure"
        } else {
            "Reachable from outside"
        }
        .to_string(),
    );
    lines.push(
        if is(profile, "general_flags.SIMULATE_PAYMENTS", "true") {
            "Payments are simulated"
        } else if is(profile, "client.ACCEPT_NEW_DEPOSITS", "true") {
            "Takes deposits from clients"
        } else {
            "No new deposits"
        }
        .to_string(),
    );
    if is(profile, "network.DELEGATE_EXECUTION", "true") {
        lines.push("Delegates work to peers".to_string());
    }
    lines.push(
        if is(profile, "host_limits.ENABLED", "true") {
            "Host limits on"
        } else {
            "No host limits"
        }
        .to_string(),
    );
    lines
}

/// How many keys of the profile differ from `document`: the "N keys will change"
/// line of a card.
pub fn changes(profile: &Profile, document: Option<&serde_yaml::Value>) -> usize {
    cell::changes(profile.writes, document).len()
}

// --- Sprite drawing --------------------------------------------------------

fn pixel(character: char, palette: &Palette) -> Option<Color> {
    let color = match character {
        'a' => palette.primary,
        'b' => palette.secondary,
        'd' => palette.dark,
        'g' => palette.glow,
        'k' => OUTLINE,
        's' => SKIN,
        'w' => WHITE,
        _ => return None,
    };
    Some(rgb(color))
}

/// The sprite as terminal lines. Two pixel rows make one text row, drawn with the
/// half blocks `▀` and `▄`; a cell with a pixel above and one below uses the
/// foreground for the top and the background for the bottom.
pub fn sprite_lines(persona: &Persona) -> Vec<Line<'static>> {
    let rows: Vec<Vec<char>> = persona
        .sprite
        .iter()
        .map(|row| row.chars().collect())
        .collect();
    rows.chunks(2)
        .map(|pair| {
            let spans: Vec<Span<'static>> = (0..SPRITE_WIDTH)
                .map(|x| {
                    let top = pixel(pair[0][x], &persona.palette);
                    let bottom = pair.get(1).and_then(|row| pixel(row[x], &persona.palette));
                    match (top, bottom) {
                        (Some(top), Some(bottom)) if top == bottom => {
                            Span::styled("█", Style::default().fg(top))
                        }
                        (Some(top), Some(bottom)) => {
                            Span::styled("▀", Style::default().fg(top).bg(bottom))
                        }
                        (Some(top), None) => Span::styled("▀", Style::default().fg(top)),
                        (None, Some(bottom)) => Span::styled("▄", Style::default().fg(bottom)),
                        (None, None) => Span::raw(" "),
                    }
                })
                .collect();
            Line::from(spans)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stats_of(id: &str) -> Stats {
        let profile = cell::profiles()
            .iter()
            .find(|profile| profile.id == id)
            .unwrap();
        stats(profile)
    }

    /// A new profile without a character would show a blank card.
    #[test]
    fn every_profile_has_a_persona_and_nothing_else_has() {
        for profile in cell::profiles() {
            assert!(
                persona(profile.id).is_some(),
                "no persona for {}",
                profile.id
            );
        }
        for persona in personas() {
            assert!(
                cell::profiles()
                    .iter()
                    .any(|profile| profile.id == persona.id),
                "persona {} has no profile",
                persona.id
            );
        }
    }

    #[test]
    fn the_personas_are_in_the_order_of_the_dial() {
        let profiles: Vec<&str> = cell::profiles().iter().map(|profile| profile.id).collect();
        let personas: Vec<&str> = personas().iter().map(|persona| persona.id).collect();
        assert_eq!(profiles, personas);
    }

    #[test]
    fn every_sprite_has_the_right_size_and_only_known_pixels() {
        for persona in personas() {
            assert_eq!(persona.sprite.len(), SPRITE_HEIGHT, "{}", persona.id);
            for (y, row) in persona.sprite.iter().enumerate() {
                assert_eq!(row.chars().count(), SPRITE_WIDTH, "{} row {y}", persona.id);
                for character in row.chars() {
                    assert!(
                        ".kswabdg".contains(character),
                        "{} row {y}: unknown pixel {character:?}",
                        persona.id
                    );
                }
            }
        }
    }

    #[test]
    fn a_sprite_draws_as_half_the_rows_and_the_full_width() {
        for persona in personas() {
            let lines = sprite_lines(persona);
            assert_eq!(lines.len(), SPRITE_HEIGHT / 2);
            for line in &lines {
                assert_eq!(line.width(), SPRITE_WIDTH);
            }
        }
    }

    /// Each persona is something you can see: not a blank sprite, and not a sprite
    /// that is the same colour as its neighbour.
    #[test]
    fn a_sprite_is_not_empty_and_the_palettes_differ() {
        for persona in personas() {
            let pixels = persona
                .sprite
                .iter()
                .flat_map(|row| row.chars())
                .filter(|character| *character != '.')
                .count();
            assert!(pixels > 100, "{} is almost empty", persona.id);
        }
        for (index, one) in personas().iter().enumerate() {
            for other in &personas()[index + 1..] {
                assert_ne!(one.palette.primary, other.palette.primary);
            }
        }
    }

    #[test]
    fn the_bars_never_pass_the_top() {
        for profile in cell::profiles() {
            for (label, value) in stats(profile).bars() {
                assert!(value <= STAT_MAX, "{} {label} = {value}", profile.id);
            }
        }
    }

    /// The numbers a card shows. If a profile changes and a bar moves, this fails
    /// and the change is made on purpose.
    #[test]
    fn the_bars_of_the_five_profiles_are_pinned() {
        let expect = |id: &str, exposure, income, openness, isolation, risk| {
            assert_eq!(
                stats_of(id),
                Stats {
                    exposure,
                    income,
                    openness,
                    isolation,
                    risk
                },
                "{id}"
            );
        };
        expect("just-me", 0, 0, 0, 3, 0);
        expect("cautious", 2, 2, 0, 5, 0);
        expect("open-renter", 5, 5, 2, 2, 1);
        expect("lan-lab", 4, 2, 5, 0, 2);
        expect("workbench", 0, 0, 1, 2, 5);
    }

    /// The reason the bars are computed: opening the node to the internet must show.
    #[test]
    fn opening_the_node_raises_exposure() {
        assert!(stats_of("open-renter").exposure > stats_of("cautious").exposure);
        assert!(stats_of("cautious").exposure > stats_of("just-me").exposure);
    }

    #[test]
    fn simulated_payments_earn_nothing() {
        assert_eq!(stats_of("workbench").income, 0);
    }

    #[test]
    fn the_traits_say_what_the_profile_does() {
        let lines = |id: &str| {
            let profile = cell::profiles().iter().find(|p| p.id == id).unwrap();
            traits(profile)
        };
        assert!(lines("open-renter").contains(&"Reachable from outside".to_string()));
        assert!(lines("cautious").contains(&"Tunnel only: no direct exposure".to_string()));
        assert!(lines("workbench").contains(&"Payments are simulated".to_string()));
        assert!(lines("open-renter").contains(&"Delegates work to peers".to_string()));
        for profile in cell::profiles() {
            let count = traits(profile).len();
            assert!(
                (3..=4).contains(&count),
                "{} has {count} traits",
                profile.id
            );
        }
    }
}
