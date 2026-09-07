//! The hours this node takes work in, as something you can see.
//!
//! `activity_window` is two times of day and a closing policy, and typed into two
//! separate fields it reads as two unrelated numbers: that `START 22:00` with
//! `END 06:00` means "open all night" is a fact about the code, not about anything on
//! screen. The same is true of the two answers an operator actually wants — is it open
//! *now*, and how many hours a day is this machine being rented out — neither of which
//! is a value in the file.
//!
//! So the page draws the day and this module is the arithmetic behind it. Kept apart
//! from the drawing because the wrap-around is the part worth testing, and a test that
//! has to build a `Frame` to ask "is 03:00 inside 22:00→06:00" tests the wrong thing.
//!
//! Mirrors `src/utils/activity_window.py`, which is what the node actually enforces:
//! `parse_clock` accepts the same spellings, START is inclusive, END exclusive, and a
//! window whose end precedes its start is one night rather than an empty set. Where the
//! two must agree, they agree by both being tested against the same cases.

pub const MINUTES_PER_DAY: u16 = 24 * 60;

/// How far one keypress moves an edge. Half an hour is the granularity operators
/// actually use for a shift, and it keeps a full day inside 48 presses.
pub const STEP_MINUTES: u16 = 30;

/// What closing time does to work already running.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OnClose {
    /// Stop taking new work; what runs keeps running until its balance is gone.
    Refuse,
    /// Also stop everything not descended from a dev client, mid-flight.
    Stop,
}

impl OnClose {
    pub fn parse(text: &str) -> Self {
        if text.trim().eq_ignore_ascii_case("stop") {
            Self::Stop
        } else {
            Self::Refuse
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Refuse => "refuse",
            Self::Stop => "stop",
        }
    }
}

/// `HH:MM` as minutes since midnight, or None when it is not a time of day.
///
/// Accepts what an operator plausibly types -- `7:00`, `07:00`, `07:00:30` -- and
/// nothing else, matching `activity_window.parse_clock`. Seconds are accepted and
/// dropped: they parse there, and a window is not to the second here.
/// `24:00` is not a time; midnight is `00:00`, and a window ending at midnight is
/// expressed by wrapping.
pub fn parse_clock(text: &str) -> Option<u16> {
    let parts: Vec<&str> = text.trim().split(':').collect();
    if parts.len() < 2 || parts.len() > 3 {
        return None;
    }
    let hours: u16 = parts[0].parse().ok()?;
    let minutes: u16 = parts[1].parse().ok()?;
    if parts.len() == 3 {
        let seconds: u16 = parts[2].parse().ok()?;
        if seconds > 59 {
            return None;
        }
    }
    if hours > 23 || minutes > 59 {
        return None;
    }
    Some(hours * 60 + minutes)
}

/// Minutes since midnight as `HH:MM`, the spelling the config file uses.
pub fn format_clock(minutes: u16) -> String {
    let wrapped = minutes % MINUTES_PER_DAY;
    format!("{:02}:{:02}", wrapped / 60, wrapped % 60)
}

/// The hours this node works, as the config file states them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Window {
    pub enabled: bool,
    pub start: u16,
    pub end: u16,
    pub on_close: OnClose,
}

impl Default for Window {
    fn default() -> Self {
        Self {
            enabled: false,
            start: 0,
            end: 0,
            on_close: OnClose::Refuse,
        }
    }
}

impl Window {
    /// True while nothing is being refused: the window is off, or it is the whole day.
    ///
    /// `START == END` reads as always open rather than as never open, which is what
    /// lets an operator enable this before choosing the hours without locking the node
    /// out of its own network.
    pub fn always_open(&self) -> bool {
        !self.enabled || self.start == self.end
    }

    /// Whether the window runs through midnight, which is one window and not two.
    pub fn wraps(&self) -> bool {
        !self.always_open() && self.end < self.start
    }

    /// Whether `minute` of the day falls inside the window.
    pub fn contains(&self, minute: u16) -> bool {
        if self.always_open() {
            return true;
        }
        let minute = minute % MINUTES_PER_DAY;
        if self.start < self.end {
            minute >= self.start && minute < self.end
        } else {
            minute >= self.start || minute < self.end
        }
    }

    /// How much of the day the node is open for, in minutes.
    pub fn open_minutes(&self) -> u16 {
        if self.always_open() {
            return MINUTES_PER_DAY;
        }
        if self.start < self.end {
            self.end - self.start
        } else {
            MINUTES_PER_DAY - self.start + self.end
        }
    }

    /// `open_minutes` as `Hh MMm`, for a line an operator reads rather than computes.
    pub fn open_duration(&self) -> String {
        let total = self.open_minutes();
        let (hours, minutes) = (total / 60, total % 60);
        if minutes == 0 {
            format!("{hours} h")
        } else {
            format!("{hours} h {minutes:02} m")
        }
    }

    /// Minutes until the state flips, from `minute`: how long until it closes when
    /// open, until it opens when closed. None while nothing ever changes.
    pub fn minutes_until_flip(&self, minute: u16) -> Option<u16> {
        if self.always_open() {
            return None;
        }
        let minute = minute % MINUTES_PER_DAY;
        let edge = if self.contains(minute) {
            self.end
        } else {
            self.start
        };
        Some((edge + MINUTES_PER_DAY - minute) % MINUTES_PER_DAY)
    }
}

/// Which edge of the window an edit moves.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    Start,
    End,
}

impl Edge {
    pub fn other(self) -> Self {
        match self {
            Self::Start => Self::End,
            Self::End => Self::Start,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Start => "opens",
            Self::End => "closes",
        }
    }

    pub fn config_key(self) -> &'static str {
        match self {
            Self::Start => "START",
            Self::End => "END",
        }
    }
}

/// Move a time of day by `steps` of `STEP_MINUTES`, wrapping at midnight.
///
/// Wrapping rather than clamping, because the edges of this value are the same instant:
/// nudging 00:00 back an hour means 23:00, and clamping it at 00:00 would make the
/// night shift unreachable from one direction.
pub fn nudge(minute: u16, steps: i32) -> u16 {
    let day = i32::from(MINUTES_PER_DAY);
    let moved = i32::from(minute % MINUTES_PER_DAY) + steps * i32::from(STEP_MINUTES);
    (((moved % day) + day) % day) as u16
}

/// Snap a time of day to the nearest `STEP_MINUTES`, so a value typed into the config
/// by hand lands on the grid the cursor moves along.
pub fn snap(minute: u16) -> u16 {
    let step = STEP_MINUTES;
    let rounded = ((minute % MINUTES_PER_DAY) + step / 2) / step * step;
    rounded % MINUTES_PER_DAY
}

#[cfg(test)]
mod tests {
    use super::*;

    fn window(start: &str, end: &str) -> Window {
        Window {
            enabled: true,
            start: parse_clock(start).unwrap(),
            end: parse_clock(end).unwrap(),
            on_close: OnClose::Refuse,
        }
    }

    #[test]
    fn a_clock_reads_what_an_operator_types() {
        assert_eq!(parse_clock("07:00"), Some(7 * 60));
        assert_eq!(parse_clock("7:00"), Some(7 * 60));
        assert_eq!(parse_clock("07:00:30"), Some(7 * 60));
        assert_eq!(parse_clock(" 22:15 "), Some(22 * 60 + 15));
    }

    #[test]
    fn nothing_else_is_a_time_of_day() {
        // The same refusals as `activity_window.parse_clock`: 24:00 is not a time, and
        // midnight is 00:00.
        for text in ["", "9pm", "24:00", "23:60", "-1:00", "12", "12:00:00:00", "12:0x"] {
            assert_eq!(parse_clock(text), None, "{text:?} parsed as a time");
        }
    }

    #[test]
    fn a_clock_round_trips_through_its_own_spelling() {
        for minute in [0, 1, 59, 60, 690, 1320, MINUTES_PER_DAY - 1] {
            assert_eq!(parse_clock(&format_clock(minute)), Some(minute));
        }
    }

    #[test]
    fn a_daytime_window_holds_its_own_hours() {
        let day = window("09:00", "18:00");
        assert!(day.contains(parse_clock("09:00").unwrap()), "START is inclusive");
        assert!(day.contains(parse_clock("17:59").unwrap()));
        assert!(!day.contains(parse_clock("18:00").unwrap()), "END is exclusive");
        assert!(!day.contains(parse_clock("08:59").unwrap()));
        assert!(!day.wraps());
        assert_eq!(day.open_minutes(), 9 * 60);
    }

    #[test]
    fn a_night_shift_is_one_window_and_not_an_empty_set() {
        // The case the whole page exists for: two fields showing 22:00 and 06:00 say
        // nothing about this being a single stretch through midnight.
        let night = window("22:00", "06:00");
        assert!(night.wraps());
        for open in ["22:00", "23:59", "00:00", "03:00", "05:59"] {
            assert!(night.contains(parse_clock(open).unwrap()), "{open} should be open");
        }
        for closed in ["06:00", "12:00", "21:59"] {
            assert!(!night.contains(parse_clock(closed).unwrap()), "{closed} should be closed");
        }
        assert_eq!(night.open_minutes(), 8 * 60);
        assert_eq!(night.open_duration(), "8 h");
    }

    #[test]
    fn equal_edges_are_always_open() {
        // Not never open: enabling the window before choosing the hours must refuse
        // nothing, or an operator locks the node out of its own network by accident.
        let same = window("13:00", "13:00");
        assert!(same.always_open());
        assert!(same.contains(0));
        assert!(same.contains(parse_clock("13:00").unwrap()));
        assert_eq!(same.open_minutes(), MINUTES_PER_DAY);
        assert!(!same.wraps());
    }

    #[test]
    fn a_disabled_window_is_open_whatever_its_hours_say() {
        let off = Window {
            enabled: false,
            ..window("09:00", "10:00")
        };
        assert!(off.always_open());
        assert!(off.contains(parse_clock("03:00").unwrap()));
        assert_eq!(off.minutes_until_flip(0), None);
    }

    #[test]
    fn the_next_flip_counts_forward_across_midnight() {
        let night = window("22:00", "06:00");
        // Open at 23:00, closing at 06:00: seven hours.
        assert_eq!(night.minutes_until_flip(parse_clock("23:00").unwrap()), Some(7 * 60));
        // Closed at 21:00, opening at 22:00.
        assert_eq!(night.minutes_until_flip(parse_clock("21:00").unwrap()), Some(60));
        // On the closing edge itself the node is already closed, so the wait is until
        // it opens again.
        assert_eq!(night.minutes_until_flip(parse_clock("06:00").unwrap()), Some(16 * 60));
    }

    #[test]
    fn an_edge_moves_by_half_an_hour_and_wraps_at_midnight() {
        assert_eq!(nudge(parse_clock("09:00").unwrap(), 1), parse_clock("09:30").unwrap());
        assert_eq!(nudge(parse_clock("09:00").unwrap(), -1), parse_clock("08:30").unwrap());
        // Wrapping, not clamping: the ends of a day are the same instant, and clamping
        // would put the night shift out of reach from one side.
        assert_eq!(nudge(0, -1), parse_clock("23:30").unwrap());
        assert_eq!(nudge(parse_clock("23:30").unwrap(), 1), 0);
        assert_eq!(nudge(0, -48), 0);
    }

    #[test]
    fn an_hour_typed_by_hand_snaps_onto_the_grid() {
        assert_eq!(snap(parse_clock("22:17").unwrap()), parse_clock("22:30").unwrap());
        assert_eq!(snap(parse_clock("22:14").unwrap()), parse_clock("22:00").unwrap());
        assert_eq!(snap(parse_clock("23:50").unwrap()), 0, "rounds up into the next day");
        assert_eq!(snap(parse_clock("00:00").unwrap()), 0);
    }

    #[test]
    fn on_close_reads_the_config_and_nothing_else_as_stop() {
        assert_eq!(OnClose::parse("stop"), OnClose::Stop);
        assert_eq!(OnClose::parse(" STOP "), OnClose::Stop);
        assert_eq!(OnClose::parse("refuse"), OnClose::Refuse);
        // Anything unrecognised is the safe half: the node refuses new work rather
        // than destroying what is running. A malformed value never reaches here --
        // `config_validation` stops the node on it -- so this is about a value this
        // page has not heard of, not about guessing.
        assert_eq!(OnClose::parse("whatever"), OnClose::Refuse);
    }
}
