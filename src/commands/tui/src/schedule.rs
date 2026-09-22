//! The hours this node takes work in, as something you can see.
//!
//! Typed into flat fields, `activity_window` reads as two unrelated numbers per
//! window: that `START 22:00` with `END 06:00` means "open all night" is a fact
//! about the code rather than about anything on screen. So is whether it is open
//! *now*, which is in no field at all.
//!
//! The page draws the day; this module is the arithmetic behind it. Kept apart
//! because the wrap-around is the part worth testing, and a test that builds a
//! `Frame` to ask "is 03:00 inside 22:00→06:00" tests the wrong thing.
//!
//! Mirrors `src/utils/activity_window.py`, which is what the node enforces: START
//! inclusive, END exclusive, an end preceding its start is one night rather than an
//! empty set, and the node is open inside *any* configured window. Both sides are
//! tested against the same cases.

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

/// One open stretch of the day, as the config file states it.
///
/// Bare `START`/`END`: whether it is enforced at all and what happens at closing time
/// are properties of the whole [`Schedule`], not of one entry in its list.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Window {
    pub start: u16,
    pub end: u16,
}

impl Window {
    /// A freshly added window, or one whose two edges were never moved apart: it
    /// contributes no hours rather than reading as a whole day. That is the safe
    /// default -- appending a window must not silently open the node 24/7 before its
    /// hours are chosen, the way a lone window's equal edges used to.
    pub fn is_empty(&self) -> bool {
        self.start == self.end
    }

    /// Whether this window runs through midnight, which is one window and not two.
    pub fn wraps(&self) -> bool {
        !self.is_empty() && self.end < self.start
    }

    /// Whether `minute` of the day falls inside this window.
    pub fn contains(&self, minute: u16) -> bool {
        if self.is_empty() {
            return false;
        }
        let minute = minute % MINUTES_PER_DAY;
        if self.start < self.end {
            minute >= self.start && minute < self.end
        } else {
            minute >= self.start || minute < self.end
        }
    }

    /// How many minutes of the day this window alone covers.
    pub fn open_minutes(&self) -> u16 {
        if self.is_empty() {
            return 0;
        }
        if self.start < self.end {
            self.end - self.start
        } else {
            MINUTES_PER_DAY - self.start + self.end
        }
    }
}

/// The whole `activity_window` section: the switch, what closing time does, and every
/// window it is enforced through.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Schedule {
    pub enabled: bool,
    pub on_close: OnClose,
    pub windows: Vec<Window>,
}

impl Default for Schedule {
    fn default() -> Self {
        Self {
            enabled: false,
            on_close: OnClose::Refuse,
            windows: Vec::new(),
        }
    }
}

impl Schedule {
    /// True while nothing is being refused: the section is off, there are no windows,
    /// or every one of them is empty (see [`Window::is_empty`]).
    pub fn always_open(&self) -> bool {
        !self.enabled || self.windows.iter().all(Window::is_empty)
    }

    /// Whether `minute` of the day is inside any configured window.
    pub fn contains(&self, minute: u16) -> bool {
        if self.always_open() {
            return true;
        }
        self.windows.iter().any(|window| window.contains(minute))
    }

    /// How much of the day the node is open for, in minutes -- the union of every
    /// window, so two overlapping ones do not count the same minute twice.
    pub fn open_minutes(&self) -> u16 {
        if self.always_open() {
            return MINUTES_PER_DAY;
        }
        (0..MINUTES_PER_DAY)
            .filter(|&minute| self.contains(minute))
            .count() as u16
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

    /// Minutes until the open/closed state flips, from `minute`. None while nothing
    /// ever changes -- switched off, no usable windows, or windows that between them
    /// cover the whole day.
    pub fn minutes_until_flip(&self, minute: u16) -> Option<u16> {
        if self.always_open() {
            return None;
        }
        let minute = minute % MINUTES_PER_DAY;
        let now_open = self.contains(minute);
        for offset in 1..=MINUTES_PER_DAY {
            let candidate = (minute + offset) % MINUTES_PER_DAY;
            if self.contains(candidate) != now_open {
                return Some(offset);
            }
        }
        None
    }
}

/// Which edge of a window an edit moves.
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
/// nudging 00:00 back an hour means 23:00, and clamping it at 00:00 would put the night
/// shift out of reach from one direction.
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

/// Where the 24-hour bar was drawn, and what one of its cells is worth.
///
/// The geometry the mouse needs, recorded by the draw so the click handler does not
/// have to reconstruct a layout that depends on the width the pane happened to get.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ScheduleBar {
    /// Terminal column of the bar's first cell (midnight).
    pub x: u16,
    /// Terminal row the bar itself is drawn on.
    pub y: u16,
    /// How many cells the bar is wide. Always a whole number of hours.
    pub width: u16,
    /// Minutes one cell covers: 60, 30, 20 or 15.
    pub per_slot: u16,
}

impl ScheduleBar {
    /// The time of day column `x` points at, snapped to the edit grid.
    ///
    /// `None` outside the bar. Clamped to the last cell rather than wrapping at the
    /// right-hand end: a drag that runs off the edge means "as late as it goes", and
    /// wrapping it round to midnight would turn an overshoot into a different window.
    pub fn minute_at(&self, x: u16) -> Option<u16> {
        if self.width == 0 || x < self.x || x >= self.x + self.width {
            return None;
        }
        let slot = (x - self.x).min(self.width - 1);
        Some(snap(slot * self.per_slot))
    }

    /// Whether `position` is on the bar's own row.
    pub fn contains(&self, x: u16, y: u16) -> bool {
        y == self.y && x >= self.x && x < self.x + self.width
    }
}

/// Which window edge a pointer at `minute` should grab, out of `windows`.
///
/// The nearer edge of the nearest window, measured around the clock so the edges of
/// a window through midnight are as reachable as any other. `None` when there is
/// nothing to grab.
///
/// Picking the nearest edge rather than requiring the pointer to be *on* one is what
/// makes this usable at this resolution: one cell can be a whole hour, so an edge
/// occupies no column of its own to aim at.
pub fn nearest_edge(windows: &[Window], minute: u16) -> Option<(usize, Edge)> {
    windows
        .iter()
        .enumerate()
        .flat_map(|(index, window)| {
            [
                (index, Edge::Start, distance_around_clock(window.start, minute)),
                (index, Edge::End, distance_around_clock(window.end, minute)),
            ]
        })
        .min_by_key(|(_, _, distance)| *distance)
        .map(|(index, edge, _)| (index, edge))
}

/// Minutes between two times of day, the short way round.
///
/// 23:30 and 00:30 are an hour apart, not twenty-three: the day is a circle, and an
/// edge near midnight must not read as the furthest point from its own neighbour.
fn distance_around_clock(left: u16, right: u16) -> u16 {
    let left = left % MINUTES_PER_DAY;
    let right = right % MINUTES_PER_DAY;
    let forward = (left + MINUTES_PER_DAY - right) % MINUTES_PER_DAY;
    forward.min(MINUTES_PER_DAY - forward)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schedule(windows: &[(&str, &str)]) -> Schedule {
        Schedule {
            enabled: true,
            on_close: OnClose::Refuse,
            windows: windows
                .iter()
                .map(|(start, end)| Window {
                    start: parse_clock(start).unwrap(),
                    end: parse_clock(end).unwrap(),
                })
                .collect(),
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
        let day = schedule(&[("09:00", "18:00")]);
        assert!(day.contains(parse_clock("09:00").unwrap()), "START is inclusive");
        assert!(day.contains(parse_clock("17:59").unwrap()));
        assert!(!day.contains(parse_clock("18:00").unwrap()), "END is exclusive");
        assert!(!day.contains(parse_clock("08:59").unwrap()));
        assert!(!day.windows[0].wraps());
        assert_eq!(day.open_minutes(), 9 * 60);
    }

    #[test]
    fn a_night_shift_is_one_window_and_not_an_empty_set() {
        // The case the whole page exists for: two fields showing 22:00 and 06:00 say
        // nothing about this being a single stretch through midnight.
        let night = schedule(&[("22:00", "06:00")]);
        assert!(night.windows[0].wraps());
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
    fn the_node_is_open_in_any_of_several_windows() {
        // A night shift and a weekday lunch break: two unrelated open stretches.
        let split = schedule(&[("22:00", "06:00"), ("12:00", "13:00")]);
        assert!(split.contains(parse_clock("23:00").unwrap()));
        assert!(split.contains(parse_clock("12:30").unwrap()));
        assert!(!split.contains(parse_clock("09:00").unwrap()));
        assert!(!split.contains(parse_clock("18:00").unwrap()));
        assert_eq!(split.open_minutes(), 9 * 60);
    }

    #[test]
    fn overlapping_windows_do_not_double_count_their_shared_minutes() {
        let overlap = schedule(&[("09:00", "13:00"), ("11:00", "15:00")]);
        assert_eq!(overlap.open_minutes(), 6 * 60);
    }

    #[test]
    fn equal_edges_are_dropped_rather_than_read_as_a_whole_day() {
        // Not "always open": a degenerate entry contributes nothing, so a second entry
        // can still say what the real hours are.
        let empty = schedule(&[("13:00", "13:00")]);
        assert!(empty.windows[0].is_empty());
        assert!(empty.always_open());
        assert!(empty.contains(0));
        assert_eq!(empty.open_minutes(), MINUTES_PER_DAY);

        let mixed = schedule(&[("13:00", "13:00"), ("09:00", "10:00")]);
        assert!(!mixed.always_open());
        assert!(mixed.contains(parse_clock("09:30").unwrap()));
        assert!(!mixed.contains(parse_clock("13:00").unwrap()));
        assert_eq!(mixed.open_minutes(), 60);
    }

    #[test]
    fn no_windows_at_all_is_always_open() {
        let none = Schedule {
            enabled: true,
            on_close: OnClose::Refuse,
            windows: Vec::new(),
        };
        assert!(none.always_open());
        assert!(none.contains(parse_clock("03:00").unwrap()));
        assert_eq!(none.minutes_until_flip(0), None);
    }

    #[test]
    fn a_disabled_schedule_is_open_whatever_its_windows_say() {
        let off = Schedule {
            enabled: false,
            ..schedule(&[("09:00", "10:00")])
        };
        assert!(off.always_open());
        assert!(off.contains(parse_clock("03:00").unwrap()));
        assert_eq!(off.minutes_until_flip(0), None);
    }

    #[test]
    fn the_next_flip_counts_forward_across_midnight() {
        let night = schedule(&[("22:00", "06:00")]);
        // Open at 23:00, closing at 06:00: seven hours.
        assert_eq!(night.minutes_until_flip(parse_clock("23:00").unwrap()), Some(7 * 60));
        // Closed at 21:00, opening at 22:00.
        assert_eq!(night.minutes_until_flip(parse_clock("21:00").unwrap()), Some(60));
        // On the closing edge itself the node is already closed, so the wait is until
        // it opens again.
        assert_eq!(night.minutes_until_flip(parse_clock("06:00").unwrap()), Some(16 * 60));
    }

    #[test]
    fn the_next_flip_looks_past_the_nearer_window_to_the_next_one() {
        let split = schedule(&[("22:00", "06:00"), ("12:00", "13:00")]);
        // At 07:00 the night shift is long closed; the lunch window opens next.
        assert_eq!(split.minutes_until_flip(parse_clock("07:00").unwrap()), Some(5 * 60));
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
    fn a_column_of_the_bar_is_a_time_of_day() {
        // A 96-cell bar: quarter-hour cells, the finest the page draws.
        let bar = ScheduleBar {
            x: 1,
            y: 5,
            width: 96,
            per_slot: 15,
        };

        assert_eq!(bar.minute_at(1), Some(0), "the first cell is midnight");
        assert_eq!(bar.minute_at(1 + 48), Some(12 * 60), "halfway is noon");
        // Snapped onto the edit grid, so a dragged edge lands where a nudged one
        // would rather than on an odd quarter the arrows can never return to.
        // `snap` rounds to the nearest half hour, ties upward.
        assert_eq!(bar.minute_at(1 + 1), Some(30), "00:15 snaps to 00:30");
        assert_eq!(bar.minute_at(1 + 2), Some(30), "00:30 is already on the grid");
        assert_eq!(bar.minute_at(1 + 3), Some(60), "00:45 snaps to 01:00");
        // Off the bar on either side.
        assert_eq!(bar.minute_at(0), None);
        assert_eq!(bar.minute_at(1 + 96), None);
    }

    #[test]
    fn an_hour_wide_cell_still_reads_as_a_time() {
        // The coarsest the page draws, on a narrow terminal.
        let bar = ScheduleBar {
            x: 0,
            y: 3,
            width: 24,
            per_slot: 60,
        };
        assert_eq!(bar.minute_at(0), Some(0));
        assert_eq!(bar.minute_at(9), Some(9 * 60));
        assert_eq!(bar.minute_at(23), Some(23 * 60));
    }

    #[test]
    fn a_drag_grabs_the_nearer_edge_of_the_nearer_window() {
        let windows = schedule(&[("09:00", "18:00"), ("20:00", "22:00")]).windows;

        // Just inside the first window's opening: its START.
        assert_eq!(
            nearest_edge(&windows, parse_clock("09:30").unwrap()),
            Some((0, Edge::Start))
        );
        // Near its closing: its END.
        assert_eq!(
            nearest_edge(&windows, parse_clock("17:30").unwrap()),
            Some((0, Edge::End))
        );
        // Closer to the second window than to the first.
        assert_eq!(
            nearest_edge(&windows, parse_clock("19:45").unwrap()),
            Some((1, Edge::Start))
        );
        assert_eq!(
            nearest_edge(&windows, parse_clock("21:50").unwrap()),
            Some((1, Edge::End))
        );
    }

    /// The day is a circle. An edge at 23:00 is an hour from 00:00, not twenty-three,
    /// or the night shift's own edges become the hardest ones on the bar to grab.
    #[test]
    fn the_edges_of_a_night_shift_are_reachable_from_either_side_of_midnight() {
        let windows = schedule(&[("22:00", "06:00")]).windows;

        assert_eq!(
            nearest_edge(&windows, parse_clock("23:00").unwrap()),
            Some((0, Edge::Start))
        );
        // Past midnight, still nearer the start than the end.
        assert_eq!(
            nearest_edge(&windows, parse_clock("00:30").unwrap()),
            Some((0, Edge::Start))
        );
        assert_eq!(
            nearest_edge(&windows, parse_clock("05:00").unwrap()),
            Some((0, Edge::End))
        );
    }

    #[test]
    fn there_is_nothing_to_grab_on_an_empty_schedule() {
        assert_eq!(nearest_edge(&[], 0), None);
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
