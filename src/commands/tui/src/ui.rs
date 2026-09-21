use crate::app::{
    format_bytes, format_bytes_compact, format_rate_compact, percent, segment_token, shorten,
    unix_now, App, DemandByHour,
    Client, ClientDetail, ConfigEntry, DonationWallet, EditKind, InputMode, Instance, Money, Page,
    LedgerEarnings, PageGroup, PaymentRow, Peer, PeerDetail, PriceEntry, ReputationEvent, ReputationTotals, Service,
    ServiceDetail,
};
use crate::cell::{self, Lever, LeverStatus, Organelle};
use crate::schedule;
use ratatui::{prelude::*, widgets::*};
use std::collections::{HashMap, HashSet};
use tui_tree_widget::{Tree, TreeItem};

// Every colour drawn in this file comes from `crate::theme` (issue #395).
//
// Functions rather than constants because the theme is chosen at startup from
// config/env/flag, and a `const` cannot be.

/// Selected tabs, focused borders, the node's own identity.
#[inline]
fn accent() -> Color {
    crate::theme::current().accent
}

/// Labels, dividers and help text: there to be read past rather than read.
#[inline]
fn muted() -> Color {
    crate::theme::current().muted
}

/// Working, healthy, running, local.
#[inline]
fn good() -> Color {
    crate::theme::current().good
}

/// Worth a look, not yet a problem.
#[inline]
fn warn() -> Color {
    crate::theme::current().warn
}

/// For the things that cost the operator something: a payment nobody acknowledged,
/// a deposit that was refused, a penalty. `warn` already means "look at this later".
#[inline]
fn bad() -> Color {
    crate::theme::current().bad
}

/// Ordinary text: a value, as opposed to the label beside it.
#[inline]
fn text_colour() -> Color {
    crate::theme::current().text
}

/// Text drawn *on* an accent or warning background, where `text` would be
/// unreadable.
#[inline]
fn inverse_text() -> Color {
    crate::theme::current().inverse_text
}

/// The background a popup paints over whatever it covers.
#[inline]
fn popup_background() -> Color {
    crate::theme::current().popup_background
}

/// The whole frame's background, painted before anything else.
#[inline]
fn background() -> Color {
    crate::theme::current().background
}

/// One of four hues for things that sit side by side and have to be told apart.
/// Indexed rather than named because the difference between them IS the meaning:
/// a field called "the colour of the donations card" would need a sibling for every
/// widget ever added.
#[inline]
fn series(index: usize) -> Color {
    crate::theme::current().series[index % 4]
}

pub fn render(app: &mut App, frame: &mut Frame) {
    // The frame's own background, before anything else. Without it every cell no
    // widget happens to cover keeps the terminal's colour, and a theme that cannot
    // set the background is a theme showing through to somebody else's palette.
    frame.render_widget(
        Block::default().style(Style::default().bg(background())),
        frame.size(),
    );

    // The page row only exists for a group that has more than one page. A row
    // holding a single already-selected title says nothing and costs the page below
    // it a line, so OVERVIEW, EARNINGS and LOGS get their space back.
    let page_row = if app.tabs.group().pages().len() > 1 { 1 } else { 0 };
    let layout = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(page_row),
        Constraint::Min(8),
        Constraint::Length(2),
    ])
    .split(frame.size());

    // Remembered for the mouse: a click has only the coordinates, so the hit test needs
    // where things ended up this frame. Cleared here so a page without a table (or the
    // instances tree) cannot inherit the previous page's rows.
    app.tabs_area = layout[0];
    app.page_tabs_area = if page_row > 0 { layout[1] } else { Rect::ZERO };
    app.list_area = Rect::ZERO;

    draw_tabs(frame, app, layout[0]);
    if page_row > 0 {
        draw_page_tabs(frame, app, layout[1]);
    }
    match app.page() {
        Page::Overview => draw_overview(frame, app, layout[2]),
        Page::Instances => draw_instances(frame, app, layout[2]),
        Page::Services => draw_services(frame, app, layout[2]),
        Page::Peers => draw_peers(frame, app, layout[2]),
        Page::Clients => draw_clients(frame, app, layout[2]),
        Page::Earnings => draw_earnings(frame, app, layout[2]),
        Page::Cell => draw_cell(frame, app, layout[2]),
        Page::Pricing => draw_pricing(frame, app, layout[2]),
        Page::Schedule => draw_schedule(frame, app, layout[2]),
        Page::Energy => draw_energy(frame, app, layout[2]),
        Page::Config => draw_config(frame, app, layout[2]),
        Page::Logs => draw_logs(frame, app, layout[2]),
    }
    draw_footer(frame, app, layout[3]);

    match app.input_mode {
        InputMode::Normal => {}
        InputMode::Confirm => draw_confirm_popup(frame, app),
        InputMode::AcceptKya | InputMode::Details | InputMode::ConfirmWrites => {
            draw_details_popup(frame, app)
        }
        InputMode::PickProfile => draw_profile_popup(frame, app),
        InputMode::Connect
        | InputMode::EditConfig
        | InputMode::AddConfigItem
        | InputMode::FilterConfig
        | InputMode::CreditClient => draw_input_popup(frame, app),
    }
}

/// The top row: the five groups, one of which is open (issue #395).
///
/// Twelve tabs in one row was twelve titles to read, with the last cut off at 80
/// columns. Groups became the primary row and their pages the secondary one.
///
/// Labelled by the group rather than by its first page: a group named `INSTANCES`
/// would send somebody looking for CLIENTS past a label about instances.
fn draw_tabs(frame: &mut Frame, app: &App, area: Rect) {
    let titles = PageGroup::ALL
        .iter()
        .map(|group| Line::from(group.title()))
        .collect::<Vec<_>>();
    let status_color = if app.node_info.service_status == "running" {
        good()
    } else {
        warn()
    };
    let title = Line::from(vec![
        Span::styled(
            " NODO ",
            Style::default().fg(inverse_text()).bg(accent()).bold(),
        ),
        Span::raw("  operations console  "),
        Span::styled(
            if app.node_info.service_status.is_empty() {
                "unknown"
            } else {
                &app.node_info.service_status
            },
            Style::default().fg(status_color),
        ),
    ]);
    let tabs = Tabs::new(titles)
        .block(Block::bordered().title(title))
        .select(app.tabs.group().index())
        .style(Style::default().fg(muted()))
        .highlight_style(Style::default().fg(accent()).bold())
        .divider(crate::app::TAB_DIVIDER);
    frame.render_widget(tabs, area);
}

/// The second row: the pages inside the open group, and only those.
///
/// Drawn only when the group holds more than one. `render` decides that and gives
/// this function no space otherwise, so the two cannot disagree about whether the
/// row exists.
///
/// Borderless rather than boxed: a second box would read as a second thing to
/// navigate rather than as the inside of the first.
fn draw_page_tabs(frame: &mut Frame, app: &App, area: Rect) {
    let group = app.tabs.group();
    let pages = group.pages();
    let selected = pages
        .iter()
        .position(|page| *page == app.page())
        .unwrap_or(0);
    let titles = pages
        .iter()
        .map(|page| Line::from(page.title()))
        .collect::<Vec<_>>();
    let tabs = Tabs::new(titles)
        .select(selected)
        .style(Style::default().fg(muted()))
        .highlight_style(Style::default().fg(text_colour()).bold().underlined())
        .divider(crate::app::TAB_DIVIDER);
    frame.render_widget(tabs, area);
}

fn draw_overview(frame: &mut Frame, app: &App, area: Rect) {
    // The banner takes its rows off the top of the page and gives them back the
    // moment the condition is fixed, rather than reserving space for an alert that
    // is usually absent. A permanently empty strip above the cards would be a strip
    // the eye stops reading, which is precisely the failure being fixed.
    let banner_height = alert_banner_height(app, area.width);
    let area = if banner_height > 0 {
        let split =
            Layout::vertical([Constraint::Length(banner_height), Constraint::Min(0)]).split(area);
        draw_alert_banner(frame, app, split[0]);
        split[1]
    } else {
        area
    };
    let rows = Layout::vertical([
        Constraint::Length(9),
        Constraint::Length(7),
        Constraint::Min(6),
    ])
    .split(area);
    let top = Layout::horizontal([
        Constraint::Percentage(25),
        Constraint::Percentage(25),
        Constraint::Percentage(25),
        Constraint::Percentage(25),
    ])
    .split(rows[0]);

    draw_card(
        frame,
        top[0],
        "NODE",
        vec![
            metric_line(
                "Status",
                nonempty(&app.node_info.service_status, "checking…"),
            ),
            metric_line("Address", nonempty(&app.node_info.address, "—")),
            metric_line("Version", shorten(&app.node_info.version, 18)),
            metric_line("Power", node_power_line(&app.node_energy)),
            metric_line("Elec.", node_cost_line(&app.node_energy)),
        ],
        accent(),
    );
    draw_card(
        frame,
        top[1],
        "WORKLOAD",
        vec![
            metric_line("Instances", app.instances.items.len().to_string()),
            metric_line(
                "Memory now",
                format_bytes(app.stats.instance_memory_current),
            ),
            metric_line(
                "Reserved",
                format!(
                    "{} RAM / {} disk",
                    format_bytes(app.stats.instance_memory_reserved),
                    format_bytes(app.stats.instance_disk_reserved)
                ),
            ),
        ],
        series(0),
    );
    draw_card(
        frame,
        top[2],
        "STORAGE",
        vec![
            metric_line(
                "Host disk",
                format!(
                    "{} / {} ({}%)",
                    format_bytes(app.stats.disk_used),
                    format_bytes(app.stats.disk_total),
                    percent(app.stats.disk_used, app.stats.disk_total)
                ),
            ),
            metric_line("Nodo data", format_bytes(app.stats.storage_bytes)),
            metric_line("Services", app.services.items.len().to_string()),
        ],
        series(1),
    );
    draw_card(
        frame,
        top[3],
        "NETWORK",
        vec![
            metric_line("Peers", app.peers.items.len().to_string()),
            metric_line("Clients", app.clients.items.len().to_string()),
        ],
        series(2),
    );

    let middle =
        Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).split(rows[1]);
    draw_ergo(frame, app, middle[0]);
    draw_health(frame, app, middle[1]);

    // Each panel summarises a page that is otherwise a whole tab away, reading the
    // same state that page reads. Nothing here fetches: a summary with its own data
    // path can disagree with the page it summarises.
    let summaries = Layout::horizontal([
        Constraint::Percentage(34),
        Constraint::Percentage(33),
        Constraint::Percentage(33),
    ])
    .split(rows[2]);
    draw_card(
        frame,
        summaries[0],
        "EARNINGS",
        earnings_summary_lines(app),
        series(2),
    );
    draw_card(
        frame,
        summaries[1],
        "SCHEDULE",
        schedule_summary_lines(app),
        series(0),
    );
    draw_card(
        frame,
        summaries[2],
        "ENERGY",
        energy_summary_lines(app),
        warn(),
    );
}

/// The EARNINGS page in five lines: what came in, over the windows that fit.
///
/// Summed across payment networks, which the page itself does not do: there the
/// total would be money the operator cannot spend as one sum, but here the question
/// is "is this node earning at all". The network count is named so the figure is not
/// read as a single balance.
fn earnings_summary_lines(app: &App) -> Vec<Line<'static>> {
    if app.earnings.is_empty() {
        return vec![
            Line::from(Span::styled(
                "Nothing paid in yet.",
                Style::default().fg(muted()),
            )),
            Line::from(Span::styled(
                "A node nobody has paid has earned zero,",
                Style::default().fg(muted()),
            )),
            Line::from(Span::styled(
                "which is a measurement, not a gap.",
                Style::default().fg(muted()),
            )),
        ];
    }

    let sum = |pick: fn(&LedgerEarnings) -> u128| -> u128 {
        app.earnings.iter().map(pick).sum()
    };
    let mut lines = vec![
        metric_line("Last day", app.money.format_raw(&sum(|e| e.day).to_string())),
        metric_line("Last week", app.money.format_raw(&sum(|e| e.week).to_string())),
        metric_line(
            "Last month",
            app.money.format_raw(&sum(|e| e.month).to_string()),
        ),
        metric_line("All time", app.money.format_raw(&sum(|e| e.total).to_string())),
    ];

    // Named rather than folded into the totals: a network that keeps refusing
    // deposits is the operator's problem to see. It is money a client tried to pay
    // and this node could not validate, so nothing arrived for it.
    let refused = sum(|e| e.refused);
    if refused > 0 {
        lines.push(Line::from(vec![
            Span::styled(format!("{:<12}", "Refused"), Style::default().fg(muted())),
            Span::styled(
                app.money.format_raw(&refused.to_string()),
                Style::default().fg(bad()).bold(),
            ),
        ]));
    } else {
        lines.push(Line::from(Span::styled(
            format!(
                "across {} payment network{}",
                app.earnings.len(),
                if app.earnings.len() == 1 { "" } else { "s" }
            ),
            Style::default().fg(muted()),
        )));
    }
    lines
}

/// The SCHEDULE page in four lines: open or closed now, and when that changes.
///
/// Reads `schedule()`, the draft when one is being edited, so this agrees with the
/// SCHEDULE page rather than with disk. An unapplied edit is named: a summary that
/// quietly previewed one would report a schedule the node is not enforcing.
fn schedule_summary_lines(app: &App) -> Vec<Line<'static>> {
    let schedule = app.schedule();
    let now = app.now_minute;

    if !schedule.enabled {
        return vec![
            Line::from(vec![
                Span::styled(format!("{:<12}", "Hours"), Style::default().fg(muted())),
                Span::styled("not enforced", Style::default().fg(good()).bold()),
            ]),
            Line::from(Span::styled(
                "This node takes work at any hour.",
                Style::default().fg(muted()),
            )),
        ];
    }

    let open = schedule.contains(now);
    let mut lines = vec![Line::from(vec![
        Span::styled(format!("{:<12}", "Right now"), Style::default().fg(muted())),
        Span::styled(
            if open { "OPEN" } else { "CLOSED" },
            Style::default()
                .fg(if open { good() } else { bad() })
                .bold(),
        ),
    ])];

    lines.push(match schedule.minutes_until_flip(now) {
        Some(minutes) => metric_line(
            if open { "Closes in" } else { "Opens in" },
            format_duration_minutes(minutes),
        ),
        // `always_open` and a schedule with no usable window both land here, and they
        // are different facts. A window list that refuses every hour is a node that
        // takes no work at all, which is worth saying plainly rather than leaving as
        // a blank where a countdown belongs.
        None if open => metric_line("Closes in", "never — open all day"),
        None => metric_line("Opens in", "never — no hours set"),
    });

    let open_minutes: u32 = schedule
        .windows
        .iter()
        .map(|window| window.open_minutes() as u32)
        .sum();
    lines.push(metric_line(
        "Open for",
        format!(
            "{} a day · {} window{}",
            format_duration_minutes(open_minutes.min(u16::MAX as u32) as u16),
            schedule.windows.len(),
            if schedule.windows.len() == 1 { "" } else { "s" }
        ),
    ));
    lines.push(metric_line("At closing", schedule.on_close.as_str()));

    if app.schedule_is_dirty() {
        lines.push(Line::from(Span::styled(
            "unapplied edit — shown, not enforced",
            Style::default().fg(warn()),
        )));
    }
    lines
}

/// The ENERGY page in four lines: the draw, what it costs, and where it came from.
///
/// The source gets its own line because it is the difference between a reading and a
/// guess: `model` is an estimate from coefficients nobody may have measured, and a
/// `floor` misses whatever the counter does not cover. Neither is the machine's
/// consumption, and the number alone would present one as the other.
fn energy_summary_lines(app: &App) -> Vec<Line<'static>> {
    let energy = &app.node_energy;
    if energy.watts.is_none() {
        return vec![
            Line::from(vec![
                Span::styled(format!("{:<12}", "Power"), Style::default().fg(muted())),
                Span::styled("unmeasured", Style::default().fg(muted()).bold()),
            ]),
            Line::from(Span::styled(
                "No sample yet. Nothing is assumed:",
                Style::default().fg(muted()),
            )),
            Line::from(Span::styled(
                "a guessed wattage reads like a",
                Style::default().fg(muted()),
            )),
            Line::from(Span::styled(
                "measured one. See the ENERGY page.",
                Style::default().fg(muted()),
            )),
        ];
    }

    let mut lines = vec![
        metric_line("Power", format_watts(energy.watts)),
        metric_line("Electricity", node_cost_line(energy)),
    ];

    // The same qualifier `node_power_line` puts on the NODE card, on its own line
    // here because there is room for it to be read rather than skimmed past.
    let source = if energy.backend.is_empty() {
        "measured".to_string()
    } else if energy.backend == "model" {
        "model estimate — not measured".to_string()
    } else if energy.is_floor {
        format!("{} — a floor, not the whole machine", energy.backend)
    } else {
        energy.backend.clone()
    };
    lines.push(Line::from(Span::styled(
        source,
        Style::default().fg(if energy.backend == "model" || energy.is_floor {
            warn()
        } else {
            muted()
        }),
    )));

    if energy.price_per_kwh <= 0.0 {
        // Zero is the honest default rather than a missing value: a cost computed
        // from somebody else's tariff is a number nobody can act on.
        lines.push(Line::from(Span::styled(
            "no tariff set — watts only",
            Style::default().fg(muted()),
        )));
    }
    lines
}

/// Minutes as something a person reads: `45m`, `2h 30m`, `8h`.
///
/// Not `150 minutes`: these are countdowns, and reading one should not need
/// division.
fn format_duration_minutes(minutes: u16) -> String {
    let hours = minutes / 60;
    let rest = minutes % 60;
    match (hours, rest) {
        (0, minutes) => format!("{minutes}m"),
        (hours, 0) => format!("{hours}h"),
        (hours, minutes) => format!("{hours}h {minutes}m"),
    }
}

/// How many rows the ACTION REQUIRED banner needs; zero when nothing is wrong, so a
/// healthy OVERVIEW is exactly the page it was without it.
///
/// `width` is needed because these messages wrap. Measured rather than assumed: a
/// fixed row per alert silently truncated the second one.
fn alert_banner_height(app: &App, width: u16) -> u16 {
    if app.alerts.is_empty() {
        return 0;
    }
    let rows: usize = app
        .alerts
        .iter()
        .map(|alert| alert_banner_lines(&alert.summary, width).len())
        .sum();
    rows as u16 + 2
}

/// One alert's message, split into the lines it will actually occupy.
///
/// The single place that decides this, so `alert_banner_height` and
/// `draw_alert_banner` cannot disagree — they already did once, and the second
/// alert was silently cut off.
///
/// The badge occupies its full padded width on the first line, two columns more
/// than the word inside it.
fn alert_banner_lines(summary: &str, width: u16) -> Vec<String> {
    let inner = width.saturating_sub(2).max(1) as usize;
    let badge = ACTION_REQUIRED_TAG.chars().count();
    let first_width = inner.saturating_sub(badge).max(1);

    // Wrap the first line against the width left beside the badge, then the rest
    // against the full width, since continuation lines carry no badge.
    let all = wrapped(summary, first_width);
    let Some(first) = all.first().cloned() else {
        return vec![String::new()];
    };
    let rest = summary
        .strip_prefix(first.as_str())
        .map(str::trim_start)
        .unwrap_or("");
    let mut lines = vec![first];
    if !rest.is_empty() {
        lines.extend(wrapped(rest, inner));
    }
    lines
}

/// The tag that marks a line the operator has to act on. Matches `ACTION_REQUIRED`
/// in `src/utils/operator_alerts.py`, so the TUI and `nodo info` say the same words.
const ACTION_REQUIRED_TAG: &str = " ACTION REQUIRED ";

/// The things the operator has to act on, at the top of the first page they see.
///
/// Both conditions were already detected and written to `storage/app.log`, which
/// nobody opens until something is visibly broken. Drawn above everything, and gone
/// the moment they are fixed.
///
/// One line each: the full instructions are long and already in `.gateway_notice`
/// and `nodo info`.
fn draw_alert_banner(frame: &mut Frame, app: &App, area: Rect) {
    // Wrapped here rather than by `Paragraph::wrap`, so the lines drawn are exactly
    // the lines `alert_banner_height` counted. Letting the widget wrap independently
    // is what silently dropped the second alert: the box was sized for one row each
    // and the paragraph produced more.
    let mut lines: Vec<Line> = Vec::new();
    for alert in app.alerts.iter() {
        for (index, line) in alert_banner_lines(&alert.summary, area.width)
            .into_iter()
            .enumerate()
        {
            // The badge is drawn only where it actually is — on the first line of
            // each message. A continuation line that repeated the colour would read
            // as a second alert.
            if index == 0 {
                lines.push(Line::from(vec![
                    Span::styled(
                        ACTION_REQUIRED_TAG,
                        Style::default().fg(inverse_text()).bg(bad()).bold(),
                    ),
                    Span::styled(line, Style::default().fg(text_colour()).bold()),
                ]));
            } else {
                lines.push(Line::from(Span::styled(
                    line,
                    Style::default().fg(text_colour()).bold(),
                )));
            }
        }
    }
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::bordered()
                .title(Span::styled(
                    " THIS NODE NEEDS YOU ",
                    Style::default().fg(bad()).bold(),
                ))
                .border_style(Style::default().fg(bad())),
        ),
        area,
    );
}

fn draw_card<'a>(frame: &mut Frame, area: Rect, title: &str, lines: Vec<Line<'a>>, color: Color) {
    let block = Block::bordered()
        .title(Span::styled(
            format!(" {title} "),
            Style::default().fg(color).bold(),
        ))
        .border_style(Style::default().fg(muted()));
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn metric_line(label: &str, value: impl Into<String>) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<12}"), Style::default().fg(muted())),
        Span::styled(value.into(), Style::default().fg(text_colour()).bold()),
    ])
}

/// One block per payment system this node offers, and never a total.
///
/// Two payment systems are two balances on different chains in different money, and
/// only one can pay any given peer: a sum would name a figure the operator cannot
/// spend, and picking one would hide the other.
fn draw_ergo(frame: &mut Frame, app: &App, area: Rect) {
    let mut lines: Vec<Line> = Vec::new();

    if app.node_info.wallets.is_empty() {
        lines.push(Line::from(Span::styled(
            "No payment system configured, so nobody can pay this node.",
            Style::default().fg(warn()),
        )));
    }

    for wallet in &app.node_info.wallets {
        let balance = format_wallet_balance(wallet.balance, &wallet.unit);
        let name = if wallet.ledger.is_empty() {
            "Wallet".to_string()
        } else {
            wallet.ledger.to_uppercase()
        };
        lines.push(Line::from(vec![
            Span::styled(format!("{name:<9}"), Style::default().fg(muted())),
            Span::styled(balance, Style::default().fg(series(2)).bold()),
        ]));
        lines.push(Line::from(Span::styled(
            format!(
                "  at   {}",
                shorten(nonempty(&wallet.address, "not configured"), 28)
            ),
            Style::default().fg(text_colour()),
        )));
        // Only when there is one: a contract that sweeps nowhere is the default, and a
        // "not configured" line per contract would be most of the card.
        if !wallet.cold_address.is_empty() {
            lines.push(Line::from(Span::styled(
                format!("  cold {}", shorten(&wallet.cold_address, 28)),
                Style::default().fg(text_colour()),
            )));
        }
    }

    lines.push(Line::from(Span::styled(
        format!(
            "Proof    {}",
            shorten(nonempty(&app.node_info.reputation_proof, "not registered"), 28)
        ),
        Style::default().fg(text_colour()),
    )));
    lines.push(Line::from(Span::styled(
        nonempty(
            &app.node_info.error,
            "On-chain balances, not node balances • refreshes every 60s",
        ),
        Style::default().fg(if app.node_info.error.is_empty() {
            muted()
        } else {
            warn()
        }),
    )));

    draw_card(frame, area, "WALLETS", lines, series(2));
}

/// A balance with the unit the chain reported, or a dash when it could not be read.
///
/// The unit comes off the wire rather than being assumed: this card is no longer
/// Ergo's, and a hard-coded "ERG" beside a Bitcoin balance would be a lie about money.
fn format_wallet_balance(balance: Option<f64>, unit: &str) -> String {
    match balance {
        Some(amount) if unit.is_empty() => format!("{amount}"),
        Some(amount) => format!("{amount} {unit}"),
        None => "—".to_string(),
    }
}

fn draw_health(frame: &mut Frame, app: &App, area: Rect) {
    let block = Block::bordered()
        .title(Span::styled(
            " HOST CAPACITY ",
            Style::default().fg(warn()).bold(),
        ))
        .border_style(Style::default().fg(muted()));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let rows = Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(2),
        Constraint::Length(1),
    ])
    .split(inner);
    draw_gauge(frame, rows[0], "CPU", app.stats.cpu_percent, warn());
    draw_gauge(
        frame,
        rows[1],
        "RAM",
        percent(app.stats.memory_used, app.stats.memory_total),
        accent(),
    );
    frame.render_widget(
        Paragraph::new(format!(
            "{} used of {}",
            format_bytes(app.stats.memory_used),
            format_bytes(app.stats.memory_total)
        ))
        .style(Style::default().fg(muted())),
        rows[2],
    );
}

fn draw_gauge(frame: &mut Frame, area: Rect, label: &str, value: u64, color: Color) {
    // The percentage carries its own background. `Gauge` swaps fg and bg for the
    // cells the label covers, so a foreground-only label lands on a bar of the same
    // colour once the fill reaches it -- invisible under `mono`.
    let gauge = Gauge::default()
        .block(
            Block::default()
                .title(label)
                .style(Style::default().fg(muted()).bg(background())),
        )
        .gauge_style(
            Style::default()
                .fg(color)
                .bg(crate::theme::current().gauge_background),
        )
        .percent(value.min(100) as u16)
        .label(Span::styled(
            format!("{value}%"),
            Style::default().fg(inverse_text()).bg(color).bold(),
        ));
    frame.render_widget(gauge, area);
}

fn draw_instances(frame: &mut Frame, app: &mut App, area: Rect) {
    if app.instances_grouped {
        draw_instances_tree(frame, app, area);
        return;
    }
    // 14 = 12 detail lines + the block's two border rows. The card carries the figures
    // the row has no width for: the disk allocation, the vCPU allowance the CPU% is
    // measured against, the cumulative disk/net totals, the burn rate, and the
    // attributed watts (issue #258).
    let layout = Layout::vertical([Constraint::Min(8), Constraint::Length(14)]).split(area);
    let rows = app.instances.items.iter().map(|instance| {
        let location = if instance.is_local() {
            "local".to_string()
        } else {
            shorten(&instance.location, 14)
        };
        let location_style = if instance.is_local() {
            Style::default().fg(good())
        } else {
            Style::default().fg(warn())
        };
        Row::new(vec![
            Cell::from(instance.name.clone()),
            Cell::from(location).style(location_style),
            Cell::from(shorten(&instance.id, 18)),
            Cell::from(instance.service.clone()),
            Cell::from(instance.ip.clone()),
            Cell::from(instance.virtualizer.clone()),
            Cell::from(format_cpu_percent(instance.usage.cpu_percent))
                .style(Style::default().fg(cpu_load_color(instance))),
            // Used against allocated in one cell: two columns made the operator do the
            // division, which is the whole question being asked of this page.
            Cell::from(format!(
                "{} / {}",
                instance
                    .usage
                    .memory_current
                    .map(format_bytes_compact)
                    .unwrap_or_else(|| "—".to_string()),
                format_bytes_compact(instance.memory_limit)
            )),
            Cell::from(format!(
                "{} / {}",
                format_rate_compact(instance.usage.net_rx_rate),
                format_rate_compact(instance.usage.net_tx_rate)
            )),
            Cell::from(app.money.format_raw(&instance.balance)),
            Cell::from(format_burn_rate(instance.mu_per_hour, &app.money)),
        ])
    });
    let local_count = app.instances.items.iter().filter(|i| i.is_local()).count();
    let remote_count = app.instances.items.len() - local_count;
    let table = Table::new(
        rows,
        [
            Constraint::Length(16),
            Constraint::Length(14),
            Constraint::Length(19),
            Constraint::Length(18),
            Constraint::Length(15),
            Constraint::Length(7),
            Constraint::Length(7),
            Constraint::Length(14),
            Constraint::Length(14),
            Constraint::Length(14),
            Constraint::Min(12),
        ],
    )
    .header(header_row(vec![
        "Name",
        "Location",
        "Instance",
        "Service",
        "IP",
        "VM",
        "CPU%",
        "RAM now/max",
        "Net ↓/↑ per s",
        "Balance",
        "Burn/h",
    ]))
    .block(section_block(
        format!(
            " INSTANCES • {} local • {} remote ",
            local_count, remote_count
        ),
        series(0),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = layout[0];
    frame.render_stateful_widget(table, layout[0], &mut app.instances.state);

    let money = &app.money;
    let detail = if let Some(instance) = app.instances.selected() {
        let mut lines = vec![
            metric_line("Instance", instance.id.clone()),
            metric_line(
                "Location",
                if instance.is_local() {
                    "local".to_string()
                } else {
                    format!("remote • peer {}", instance.location)
                },
            ),
            metric_line("Service", instance.service.clone()),
            metric_line("Endpoint", nonempty(&instance.ip, "—")),
            metric_line("CPU", cpu_detail(instance)),
            metric_line(
                "RAM",
                format!(
                    "{} / {}",
                    instance
                        .usage
                        .memory_current
                        .map(format_bytes)
                        .unwrap_or_else(|| "—".to_string()),
                    format_bytes(instance.memory_limit)
                ),
            ),
            metric_line(
                "Disk",
                format!(
                    "read {} • wrote {} • {} allocated",
                    optional_bytes(instance.usage.disk_read_bytes),
                    optional_bytes(instance.usage.disk_write_bytes),
                    format_bytes(instance.disk_limit)
                ),
            ),
            metric_line("Net", net_detail(instance)),
            metric_line("Balance", money.format_raw(&instance.balance)),
            metric_line("Burn", burn_detail(instance, money)),
            metric_line("Energy", energy_detail(instance)),
        ];
        // `observe` attaches to a local process, so it is only offered for local
        // instances. Full id, so the line can be copied as-is.
        if instance.is_local() {
            lines.push(metric_line(
                "Live view",
                format!("nodo observe {}", instance.id),
            ));
        }
        lines
    } else {
        vec![Line::from(Span::styled(
            "Select an instance to inspect its complete identity and allocation.",
            Style::default().fg(muted()),
        ))]
    };
    draw_card(
        frame,
        layout[1],
        "SELECTED INSTANCE",
        detail,
        series(0),
    );
}

/// A CPU reading for a table cell. `—` covers both "no cgroup to read" (delegated or
/// stopped) and "only one sample so far", which are equally not-a-measurement; a `0%`
/// there would claim the instance is idle.
fn format_cpu_percent(cpu_percent: Option<f64>) -> String {
    match cpu_percent {
        Some(value) if value.is_finite() => format!("{value:.0}%"),
        _ => "—".to_string(),
    }
}

/// Colour for the CPU cell: muted when there is no reading, and a warning once the
/// instance is within a tenth of its whole vCPU allowance — the point at which the
/// figure stops being informational and starts meaning "this one is throttling".
fn cpu_load_color(instance: &Instance) -> Color {
    match (instance.usage.cpu_percent, instance.cpu_allowance_percent()) {
        (None, _) => muted(),
        (Some(used), Some(allowance)) if allowance > 0.0 && used >= allowance * 0.9 => warn(),
        _ => good(),
    }
}

/// The CPU line for the detail card: what the instance is using, next to the allowance
/// that makes the number legible. `observe` reports cumulative core time, so `180%` is
/// unremarkable on a 2-vCPU guest and impossible on a 1-vCPU one — without the
/// allowance beside it the percentage cannot be judged.
fn cpu_detail(instance: &Instance) -> String {
    let used = format_cpu_percent(instance.usage.cpu_percent);
    match instance.cpu_allowance_percent() {
        Some(allowance) => format!(
            "{used} of {allowance:.0}% allowance ({:.2} vCPU)",
            instance.vcpus.unwrap_or(0.0)
        ),
        None => format!("{used} • no vCPU quota recorded"),
    }
}

/// The network line for the detail card: current rates plus the totals they accumulate
/// into. Orientation is the host tap's (see `InstanceUsage`), so `↓` is traffic the
/// host took *from* the VM.
fn net_detail(instance: &Instance) -> String {
    let rate = |value: Option<f64>| match value {
        Some(value) if value.is_finite() && value >= 0.0 => {
            format!("{}/s", format_bytes(value.round() as u64))
        }
        _ => "—".to_string(),
    };
    format!(
        "↓ {} ↑ {} • total {} / {}",
        rate(instance.usage.net_rx_rate),
        rate(instance.usage.net_tx_rate),
        optional_bytes(instance.usage.net_rx_bytes),
        optional_bytes(instance.usage.net_tx_bytes)
    )
}

fn optional_bytes(bytes: Option<u64>) -> String {
    bytes.map(format_bytes).unwrap_or_else(|| "—".to_string())
}

/// The `Burn/h` table cell: an hourly spend rate in the display unit, or `—` when the
/// instance has never been charged (a `0` there would claim it is free, not unknown).
/// A rate only needs formatting, not a second unit system, so it reuses `format_mu`.
fn format_burn_rate(mu_per_hour: Option<f64>, money: &Money) -> String {
    match mu_per_hour {
        Some(rate) if rate.is_finite() && rate >= 0.0 => money.format_mu(rate.round() as u64),
        _ => "—".to_string(),
    }
}

/// A power reading, in whole watts once there are enough of them for the decimals to
/// be noise. `—` covers "no sample" and "nothing to measure it with" alike; a `0 W`
/// there would claim the machine is drawing nothing.
fn format_watts(watts: Option<f64>) -> String {
    match watts {
        Some(value) if value.is_finite() && value >= 0.0 => {
            if value < 10.0 {
                format!("{value:.2} W")
            } else {
                format!("{value:.0} W")
            }
        }
        _ => "—".to_string(),
    }
}

/// The energy line for the detail card: what this guest drew, and how much of the
/// node's own figure that is. The share is against the whole machine, so two guests'
/// percentages plus the host's unattributed rest come to 100 — a guest using a
/// twentieth of one core reads as a twentieth of one core, not as the whole node.
fn energy_detail(instance: &Instance) -> String {
    match (instance.energy_watts, instance.energy_share) {
        (Some(watts), Some(share)) if watts.is_finite() && share.is_finite() => {
            format!(
                "{} · {:.0}% of node • CPU-share of measured draw",
                format_watts(Some(watts)),
                (share * 100.0).clamp(0.0, 100.0)
            )
        }
        _ => "— • no samples yet".to_string(),
    }
}

/// The NODE card's power line: the figure, and which source stands behind it.
///
/// Named rather than described: a package counter, a rail and a plug at the wall
/// measure different things, and `floor` says the number is short of the machine.
/// A combined source arrives already named for its parts (`rapl+nvml`).
fn node_power_line(energy: &crate::app::NodeEnergy) -> String {
    match energy.watts {
        Some(watts) if watts.is_finite() && watts >= 0.0 => {
            let kind = if energy.backend.is_empty() {
                "measured".to_string()
            } else if energy.backend == "model" {
                "model estimate".to_string()
            } else if energy.is_floor {
                format!("{} floor", energy.backend)
            } else {
                energy.backend.clone()
            };
            format!("{} · {}", format_watts(Some(watts)), kind)
        }
        _ => "—".to_string(),
    }
}

fn node_cost_line(energy: &crate::app::NodeEnergy) -> String {
    match energy.watts {
        Some(watts)
            if watts.is_finite() && watts >= 0.0 && energy.price_per_kwh > 0.0 =>
        {
            let per_hour = (watts / 1000.0) * energy.price_per_kwh;
            let currency = if energy.currency.is_empty() {
                "EUR"
            } else {
                energy.currency.as_str()
            };
            format!("{per_hour:.4} {currency}/h")
        }
        Some(_) => "— • no tariff".to_string(),
        None => "—".to_string(),
    }
}

/// The burn line for the detail card: per-minute and per-hour, with the sample count
/// and age -- a rate from two stale samples must not read like a fresh one.
///
/// Prices *reserved* resources at current scarcity, so it is cost at present prices
/// rather than measured usage (#245), and the label says so.
fn burn_detail(instance: &Instance, money: &Money) -> String {
    match (instance.mu_per_minute, instance.mu_per_hour) {
        (Some(per_minute), Some(per_hour)) => {
            let samples = instance.consumption_samples.unwrap_or(0);
            let age = instance
                .consumption_age_secs
                .map(format_age_secs)
                .unwrap_or_else(|| "—".to_string());
            format!(
                "{} /min • {} /h • {} sample{} averaged, updated {} ago • reserved-resource cost at current prices",
                money.format_mu(per_minute.round().max(0.0) as u64),
                money.format_mu(per_hour.round().max(0.0) as u64),
                samples,
                if samples == 1 { "" } else { "s" },
                age,
            )
        }
        // No maintenance tick has charged this instance yet (or it is delegated).
        _ => "— • no samples yet".to_string(),
    }
}

/// A compact "how long ago" for the burn-rate average: seconds under a minute and a
/// half, then minutes, then hours, so an operator can tell a fresh figure from a stale one.
fn format_age_secs(secs: f64) -> String {
    let secs = secs.max(0.0);
    if secs < 90.0 {
        format!("{:.0}s", secs)
    } else if secs < 5400.0 {
        format!("{:.0}m", secs / 60.0)
    } else {
        format!("{:.1}h", secs / 3600.0)
    }
}

/// Render instances as a dependency tree grouped by `father_id`, porting the
/// Python `list_instances(groupable=True)` builder from commands/instances.py.
fn draw_instances_tree(frame: &mut Frame, app: &App, area: Rect) {
    let items = &app.instances.items;
    let inst_map: HashMap<&str, &Instance> = items
        .iter()
        .filter(|instance| !instance.id.is_empty())
        .map(|instance| (instance.id.as_str(), instance))
        .collect();

    // Group children under their parent; a father_id that is empty, "None", or
    // not present locally makes the node a root (mirrors the Python logic).
    let mut children: HashMap<&str, Vec<&str>> = HashMap::new();
    let mut has_parent: HashSet<&str> = HashSet::new();
    for instance in items.iter().filter(|instance| !instance.id.is_empty()) {
        let father = instance.father_id.as_str();
        if !father.is_empty() && father != "None" && inst_map.contains_key(father) {
            children.entry(father).or_default().push(instance.id.as_str());
            has_parent.insert(instance.id.as_str());
        }
    }
    let roots: Vec<&str> = items
        .iter()
        .filter(|instance| !instance.id.is_empty())
        .map(|instance| instance.id.as_str())
        .filter(|id| !has_parent.contains(id))
        .collect();

    // A root is only a root because its father is not another instance here. When that
    // father is one of our clients, say so: without it an instance a client started is
    // indistinguishable from one with no parent at all. Reuses the already-loaded
    // clients list (refreshed every sweep), so this costs no query.
    let client_ids: HashSet<&str> = app
        .clients
        .items
        .iter()
        .map(|client| client.id.as_str())
        .collect();

    let mut lines: Vec<Line> = Vec::new();
    let mut printed: HashSet<&str> = HashSet::new();
    for root in &roots {
        build_tree_lines(
            &app.money,
            root,
            0,
            &inst_map,
            &children,
            &client_ids,
            &mut printed,
            &mut lines,
        );
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "No instances to display.",
            Style::default().fg(muted()),
        )));
    }

    frame.render_widget(
        Paragraph::new(lines)
            .block(section_block(
                format!(
                    " INSTANCE DEPENDENCY TREE • {} nodes • g toggles flat view ",
                    inst_map.len()
                ),
                series(0),
            ))
            .wrap(Wrap { trim: false }),
        area,
    );
}

// Eight, counting the recursion's own bookkeeping (`printed`, `lines`). A context
// struct to carry them would be more code than the function it serves.
#[allow(clippy::too_many_arguments)]
fn build_tree_lines<'a>(
    money: &Money,
    node_id: &'a str,
    depth: usize,
    inst_map: &HashMap<&'a str, &'a Instance>,
    children: &HashMap<&'a str, Vec<&'a str>>,
    client_ids: &HashSet<&str>,
    printed: &mut HashSet<&'a str>,
    lines: &mut Vec<Line<'static>>,
) {
    if printed.contains(node_id) {
        return;
    }
    printed.insert(node_id);
    let Some(instance) = inst_map.get(node_id) else {
        return;
    };

    let indent = "    ".repeat(depth);
    let marker = if depth == 0 { "● " } else { "└─ " };
    let label = if instance.name.is_empty() {
        shorten(&instance.id, 20)
    } else {
        instance.name.clone()
    };
    let location = if instance.is_local() {
        "local".to_string()
    } else {
        format!("peer {}", shorten(&instance.location, 12))
    };
    let mut spans = vec![
        Span::raw(format!("{indent}{marker}")),
        Span::styled(label, Style::default().fg(text_colour()).bold()),
        Span::styled(format!("  [{}]", instance.service), Style::default().fg(muted())),
        Span::styled(
            format!("  {location}"),
            Style::default().fg(if instance.is_local() { good() } else { warn() }),
        ),
        Span::styled(
            format!("  balance {}", money.format_raw(&instance.balance)),
            Style::default().fg(accent()),
        ),
    ];
    // Only roots can have a father this tree does not already show: a child is nested
    // here precisely because its father is another instance above it.
    if depth == 0 {
        if let Some(parent) = external_parent_label(instance, client_ids) {
            spans.push(parent);
        }
    }
    lines.push(Line::from(spans));

    if let Some(kids) = children.get(node_id) {
        for kid in kids {
            build_tree_lines(
                money,
                kid,
                depth + 1,
                inst_map,
                children,
                client_ids,
                printed,
                lines,
            );
        }
    }
}

/// Who started an instance whose father is not another instance on this node. `None`
/// when it has no father, or one this node runs (the tree already nests those).
///
/// Mirrors the `internal_service`/`client`/`unknown` split in
/// `src/commands/instances.py`.
fn external_parent_label(instance: &Instance, client_ids: &HashSet<&str>) -> Option<Span<'static>> {
    let father = instance.father_id.as_str();
    if father.is_empty() || father == "None" {
        return None;
    }
    Some(if client_ids.contains(father) {
        Span::styled(
            format!("  ← client {}", shorten(father, 20)),
            Style::default().fg(series(1)),
        )
    } else {
        // A father that is neither a local instance nor a known client: the row still
        // says where it came from, rather than reading as "started by nobody".
        Span::styled(
            format!("  ← {} (unknown)", shorten(father, 20)),
            Style::default().fg(muted()),
        )
    })
}

fn draw_services(frame: &mut Frame, app: &mut App, area: Rect) {
    // The card grows with the selected service's reputation history, and yields to the
    // table when the terminal is short, like the peer and client cards.
    const MIN_TABLE_HEIGHT: u16 = 8;
    let card = service_detail_lines(app.services.selected(), app.service_detail.as_ref());
    let available = area.height.saturating_sub(MIN_TABLE_HEIGHT);
    let card_height = (card.len() as u16 + 2).clamp(5, available.max(5));
    let layout =
        Layout::vertical([Constraint::Min(MIN_TABLE_HEIGHT), Constraint::Length(card_height)])
            .split(area);
    let rows = app.services.items.iter().map(|service| {
        Row::new(vec![
            service.tag.clone(),
            service.id.clone(),
            format_bytes(service.size_bytes),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(28),
            Constraint::Min(42),
            Constraint::Length(14),
        ],
    )
    .header(header_row(vec!["Tag", "Content ID", "Stored size"]))
    .block(section_block(
        format!(" SERVICES • {} available ", app.services.items.len()),
        series(1),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = layout[0];
    frame.render_stateful_widget(table, layout[0], &mut app.services.state);

    frame.render_widget(
        Paragraph::new(card)
            .block(section_block(" SELECTED SERVICE ", series(1)))
            .style(Style::default().fg(text_colour())),
        layout[1],
    );
}

/// The selected service: what it is, and how it has behaved here.
///
/// The reputation is the service's own, over every instance of it that ever ran
/// here: an instance is gone minutes after it misbehaves, so a score tied to one
/// would answer nothing the next time the service is started.
fn service_detail_lines(
    service: Option<&Service>,
    detail: Option<&ServiceDetail>,
) -> Vec<Line<'static>> {
    let Some(service) = service else {
        return vec![Line::from(Span::styled(
            "Select a service, then press e to execute it.",
            Style::default().fg(muted()),
        ))];
    };

    let mut lines = vec![
        Line::from(Span::styled(
            service.id.clone(),
            Style::default().fg(text_colour()),
        )),
        Line::from(Span::styled(
            format!(
                "{} • {}",
                nonempty(&service.tag, "untagged"),
                format_bytes(service.size_bytes)
            ),
            Style::default().fg(text_colour()),
        )),
    ];

    let Some(detail) = detail.filter(|detail| detail.service_id == service.id) else {
        return lines;
    };

    lines.push(metric_line(
        "Reputation",
        detail
            .score
            .map(|score| score.to_string())
            // Never scored is not the same as scored to zero, and an operator choosing
            // a service should be able to tell the two apart.
            .unwrap_or_else(|| "not scored yet".to_string()),
    ));
    if !detail.events.is_empty() {
        lines.extend(reputation_event_lines(&detail.events));
    }
    lines
}

/// The peers page: who we talk to, and everything we have paid them.
///
/// Separate from clients: a peer is someone we pay, a client someone who pays us.
fn draw_peers(frame: &mut Frame, app: &mut App, area: Rect) {
    // The card sizes itself to what the selected peer actually has: contracts, the
    // payments made to it, the events behind its score. It yields first when the
    // terminal is short -- a card that squeezed the table off-screen would leave no
    // way to pick the peer it is describing.
    const MIN_TABLE_HEIGHT: u16 = 7;
    let available = area.height.saturating_sub(MIN_TABLE_HEIGHT);
    let selected = app.peers.selected();
    let detail_source = app.peer_detail.as_ref();
    // Prefer the roomy breakdown, but fall back to one line per contract rather
    // than let a short terminal clip the contracts away silently -- an empty
    // card reads as "no contract registered", the exact confusion #231 is about.
    let donation = selected.and_then(|peer| app.donations.for_peer(&peer.id));
    let full = peer_detail_lines(&app.money, selected, detail_source, donation, false);
    let detail = if full.len() as u16 + 2 <= available {
        full
    } else {
        peer_detail_lines(&app.money, selected, detail_source, donation, true)
    };
    let detail_height = (detail.len() as u16 + 2).min(available);
    let split = Layout::vertical([
        Constraint::Min(MIN_TABLE_HEIGHT),
        Constraint::Length(detail_height),
    ])
    .split(area);

    let peers = app.peers.items.iter().map(|peer| {
        Row::new(vec![
            Cell::from(peer.id.clone()),
            Cell::from(peer.uris.clone()),
            Cell::from(app.money.format_raw(&peer.balance)),
            Cell::from(peer.reputation_score.clone()).style(Style::default().fg(good()).bold()),
            Cell::from(match peer.proof_ids.len() {
                0 => "none".to_string(),
                1 => shorten(&peer.proof_ids[0], 18),
                n => format!("{n} announced"),
            }),
        ])
    });
    let peer_table = Table::new(
        peers,
        [
            Constraint::Length(30),
            Constraint::Length(24),
            Constraint::Length(13),
            Constraint::Length(7),
            Constraint::Min(20),
        ],
    )
    .header(header_row(vec![
        "Peer ID",
        "Endpoints",
        "Our balance",
        "Rep",
        "Reputation proofs",
    ]))
    .block(section_block(
        format!(" PEERS • {} connected ", app.peers.items.len()),
        accent(),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = split[0];
    frame.render_stateful_widget(peer_table, split[0], &mut app.peers.state);

    draw_card(frame, split[1], "SELECTED PEER", detail, accent());
}

/// The clients page: who pays us, and what they are running here.
fn draw_clients(frame: &mut Frame, app: &mut App, area: Rect) {
    const MIN_TABLE_HEIGHT: u16 = 6;
    let available = area.height.saturating_sub(MIN_TABLE_HEIGHT);
    let selected = app.clients.selected();
    let detail_source = app.client_detail.as_ref();
    let full = client_detail_lines(&app.money, selected, detail_source, false);
    let detail = if full.len() as u16 + 2 <= available {
        full
    } else {
        client_detail_lines(&app.money, selected, detail_source, true)
    };
    let detail_height = (detail.len() as u16 + 2).min(available);
    let split = Layout::vertical([
        Constraint::Min(MIN_TABLE_HEIGHT),
        Constraint::Length(detail_height),
    ])
    .split(area);

    let clients = app.clients.items.iter().map(|client| {
        Row::new(vec![
            Cell::from(client.id.clone()),
            Cell::from(app.money.format_raw(&client.balance)),
            Cell::from(client.last_usage.clone()),
            // A balance that never moves is the flag doing its job, not a bug.
            Cell::from(if client.unmetered { "never charged" } else { "" })
                .style(Style::default().fg(muted())),
        ])
    });
    let client_table = Table::new(
        clients,
        [
            Constraint::Min(38),
            Constraint::Length(24),
            Constraint::Length(20),
            Constraint::Length(14),
        ],
    )
    .header(header_row(vec![
        "Client ID",
        "Balance",
        "Last usage",
        "Metering",
    ]))
    .block(section_block(
        format!(" CLIENTS • {} known ", app.clients.items.len()),
        accent(),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = split[0];
    frame.render_stateful_widget(client_table, split[0], &mut app.clients.state);

    draw_card(frame, split[1], "SELECTED CLIENT", detail, accent());
}

/// The two things a node earns by being up: money, and the network's opinion of it.
///
/// Drawn apart because they are different kinds of quantity. Money is a flow, read
/// over windows. Reputation is a stock -- what is staked now -- and the chain cannot
/// say when it was earned, since a proof re-dates its opinions when it republishes.
/// A window over those dates would measure submission cadence while reading like the
/// money above it.
fn draw_earnings(frame: &mut Frame, app: &mut App, area: Rect) {
    const MIN_OPINIONS_HEIGHT: u16 = 4;
    let notes = reputation_lines(app);
    // What leaves belongs next to what came in: the donation is a share of the money
    // in the card above, and an operator reading one figure needs the other to make
    // sense of it.
    let donations = donation_lines(app);
    // Two borders, the header, the blank line `header_row` puts under it, and a row
    // per payment network -- one placeholder row when there is none.
    let table_height = app.earnings.len().max(1) as u16 + 4;
    let rows = Layout::vertical([
        Constraint::Length(table_height),
        Constraint::Length(donations.len() as u16 + 2),
        Constraint::Length(notes.len() as u16 + 2),
        Constraint::Min(MIN_OPINIONS_HEIGHT),
    ])
    .split(area);

    draw_money_taken_in(frame, app, rows[0]);
    draw_card(frame, rows[1], "DONATIONS PAID OUT", donations, series(1));
    draw_card(frame, rows[2], "REPUTATION HELD ON THIS NODE", notes, accent());
    draw_opinions(frame, app, rows[3]);
}

/// What this node donates, what is waiting to go out, and to whom.
///
/// A non-zero donation default is only honest if the operator can see what it does.
///
/// The two lists mean different things and are drawn separately: funding a wallet
/// costs money, while counting one is a free opinion that weighs other peers'
/// donations when this node routes work. An address in one and not the other is
/// usually an oversight, and is named as such.
fn donation_lines(app: &App) -> Vec<Line<'static>> {
    let donations = &app.donations;
    let mut lines = Vec::new();

    if !donations.is_read() {
        lines.push(Line::from(Span::styled(
            "Reading the donation state…",
            Style::default().fg(muted()),
        )));
        return lines;
    }
    if !donations.error.is_empty() {
        lines.push(Line::from(Span::styled(
            donations.error.clone(),
            Style::default().fg(warn()),
        )));
    }
    if donations.ledgers.is_empty() {
        lines.push(Line::from(Span::styled(
            "No payment network is configured to donate.",
            Style::default().fg(muted()),
        )));
        return lines;
    }

    for ledger in &donations.ledgers {
        lines.push(Line::from(vec![
            Span::styled(
                format!("{}  ", ledger.ledger),
                Style::default().fg(text_colour()).bold(),
            ),
            Span::styled("donating ", Style::default().fg(muted())),
            Span::styled(
                format!("{} ", ledger.percentage),
                Style::default().fg(series(1)).bold(),
            ),
            Span::styled("of what comes in   ", Style::default().fg(muted())),
            Span::styled("paid ", Style::default().fg(muted())),
            Span::styled(
                app.money.format_raw(&ledger.paid_mu.to_string()),
                Style::default().fg(series(2)),
            ),
            Span::styled(
                format!("  in {} transaction(s)", ledger.paid_count),
                Style::default().fg(muted()),
            ),
        ]));

        // Accrued, not yet paid: shown in the asset's own smallest unit, not converted.
        // A debt is incurred at the rate of the moment it was incurred, and rendering
        // it through today's rate would put a number on screen that the node does not
        // owe.
        let owed = if ledger.owed.is_empty() {
            "nothing waiting to go out".to_string()
        } else {
            ledger
                .owed
                .iter()
                .map(|(asset, amount)| format!("{amount} {asset} (smallest unit)"))
                .collect::<Vec<_>>()
                .join("   ")
        };
        lines.push(Line::from(vec![
            Span::styled("     accrued  ", Style::default().fg(muted())),
            Span::styled(owed, Style::default().fg(text_colour())),
        ]));

        lines.extend(donation_wallet_lines("funding ", &ledger.pay_wallets, "nobody funded"));
        lines.extend(donation_wallet_lines("counting", &ledger.credit_wallets, "nobody counted"));
    }

    for warning in &donations.warnings {
        lines.push(Line::from(Span::styled(
            format!("! {warning}"),
            Style::default().fg(warn()),
        )));
    }
    lines
}

/// One line per wallet of a list: where it goes and what share of the list it takes.
///
/// The share, not just the weight: weights are normalised before anything is split,
/// so 70 and 30 pay exactly what 0.7 and 0.3 pay.
fn donation_wallet_lines(
    label: &str,
    wallets: &[DonationWallet],
    empty: &str,
) -> Vec<Line<'static>> {
    if wallets.is_empty() {
        return vec![Line::from(vec![
            Span::styled(format!("     {label}  "), Style::default().fg(muted())),
            Span::styled(empty.to_string(), Style::default().fg(muted())),
        ])];
    }
    wallets
        .iter()
        .map(|wallet| {
            Line::from(vec![
                Span::styled(format!("     {label}  "), Style::default().fg(muted())),
                Span::styled(shorten(&wallet.address, 40), Style::default().fg(text_colour())),
                Span::styled(
                    format!("  share {}", wallet.share),
                    Style::default().fg(muted()),
                ),
                Span::styled(
                    // What has actually reached it, which is what makes the share above
                    // a claim an operator can check rather than take on trust. Nothing
                    // is shown for a counted wallet: this node pays it nothing.
                    if wallet.paid.is_empty() {
                        String::new()
                    } else {
                        format!(
                            "  paid {}",
                            wallet
                                .paid
                                .iter()
                                .map(|(asset, amount)| format!("{amount} {asset}"))
                                .collect::<Vec<_>>()
                                .join(", ")
                        )
                    },
                    Style::default().fg(good()),
                ),
                Span::styled(
                    if wallet.in_other_list {
                        String::new()
                    } else {
                        "  (not in the other list)".to_string()
                    },
                    Style::default().fg(warn()),
                ),
            ])
        })
        .collect()
}

/// What came in, per payment network, over each window.
///
/// One row per network rather than a total: two networks are two balances in two
/// places, and a sum would name a figure the operator cannot spend.
fn draw_money_taken_in(frame: &mut Frame, app: &App, area: Rect) {
    let refused: u128 = app.earnings.iter().map(|entry| entry.refused).sum();
    let title = if refused > 0 {
        // Money a client tried to pay and this node could not validate, so no balance
        // was credited for it. Named in the title rather than given a column: it is
        // rare, and when it is not rare it is the first thing here worth reading.
        format!(
            " MONEY TAKEN IN • {} refused ",
            app.money.format_raw(&refused.to_string())
        )
    } else {
        " MONEY TAKEN IN ".to_string()
    };

    let mut rows: Vec<Row> = app
        .earnings
        .iter()
        .map(|entry| {
            Row::new(vec![
                Cell::from(entry.ledger.clone()),
                money_cell(&app.money, entry.day),
                money_cell(&app.money, entry.week),
                money_cell(&app.money, entry.month),
                money_cell(&app.money, entry.year),
                money_cell(&app.money, entry.total),
            ])
        })
        .collect();
    if rows.is_empty() {
        // A node nobody has paid has earned zero over every window, which is a
        // measurement -- so the row is drawn with the zeros rather than left out, and
        // named for why it has no payment network of its own.
        rows.push(
            Row::new(
                std::iter::once(Cell::from("nothing paid in yet"))
                    .chain((0..5).map(|_| money_cell(&app.money, 0)))
                    .collect::<Vec<_>>(),
            )
            .style(Style::default().fg(muted())),
        );
    }

    frame.render_widget(
        Table::new(
            rows,
            [
                Constraint::Min(20),
                Constraint::Length(12),
                Constraint::Length(12),
                Constraint::Length(12),
                Constraint::Length(12),
                Constraint::Length(12),
            ],
        )
        .header(header_row(vec![
            "Network",
            "Last day",
            "Last week",
            "Last month",
            "Last year",
            "All time",
        ]))
        .block(section_block(title, accent())),
        area,
    );
}

/// One amount, in the display unit, greyed when there is nothing in it.
///
/// A real zero, not a `—`: the catalogue was read and nothing came in over that
/// window, which is a measurement and not a gap.
fn money_cell(money: &Money, amount: u128) -> Cell<'static> {
    let cell = Cell::from(money.format_raw(&amount.to_string()));
    if amount == 0 {
        cell.style(Style::default().fg(muted()))
    } else {
        cell
    }
}

/// What is staked on this node, what it cost the proofs staking it, and where the
/// figures were read from.
///
/// Short lines on purpose: the card is drawn at a fixed height computed from the line
/// count, so a line that wrapped would be a line that vanished.
fn reputation_lines(app: &App) -> Vec<Line<'static>> {
    let reputation = &app.reputation;
    let mut lines = Vec::new();

    if !reputation.is_read() {
        lines.push(Line::from(Span::styled(
            "Reading the chain…",
            Style::default().fg(muted()),
        )));
    } else {
        let standing = &reputation.standing;
        lines.push(Line::from(vec![
            Span::styled(
                format!("+{} ", format_share(standing.positive)),
                Style::default().fg(good()).bold(),
            ),
            Span::styled("for   ", Style::default().fg(muted())),
            Span::styled(
                format!("−{} ", format_share(standing.negative)),
                Style::default().fg(if standing.negative > 0.0 { bad() } else { muted() }),
            ),
            Span::styled("against   ", Style::default().fg(muted())),
            Span::styled(
                format!("net {}", format_signed_share(standing.net())),
                Style::default().fg(if standing.net() < 0.0 { bad() } else { good() }),
            ),
            Span::styled(
                format!("   {}", proof_count(standing)),
                Style::default().fg(muted()),
            ),
        ]));
        // The half a share cannot give. Minting a proof costs nothing, so a share is
        // only as meaningful as what the proof staking it had to give up — and the
        // reputation contract makes that ERG unrecoverable, by its owner too.
        lines.push(Line::from(vec![
            Span::styled("backed by ", Style::default().fg(muted())),
            Span::styled(
                format_erg(standing.positive_backing),
                Style::default().fg(good()),
            ),
            Span::styled(" for / ", Style::default().fg(muted())),
            Span::styled(
                format_erg(standing.negative_backing),
                Style::default().fg(if standing.negative_backing > 0.0 { bad() } else { muted() }),
            ),
            Span::styled(" against, sunk and unrecoverable", Style::default().fg(muted())),
        ]));
    }

    // Both halves of the reading in one line, and the reason this block has no windows
    // beside a money table that does — otherwise the first thing an operator asks is
    // where they went.
    lines.push(Line::from(Span::styled(
        "a share of what each proof assigned · no windows: a republish re-dates it all",
        Style::default().fg(muted()),
    )));

    if !reputation.own.is_empty() {
        // An operator whose own proof stakes everything on itself would otherwise
        // wonder where that stake went.
        lines.push(Line::from(Span::styled(
            format!(
                "this node's own proof stakes {} on itself, left out above",
                reputation
                    .own
                    .iter()
                    .map(|opinion| {
                        format!(
                            "{}{}",
                            if opinion.positive { "+" } else { "−" },
                            format_share(opinion.weight)
                        )
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Style::default().fg(muted()),
        )));
    }

    lines.push(Line::from(Span::styled(
        format!(
            "node {}   proof {}",
            shorten(nonempty(&reputation.node_id, "unknown"), 20),
            shorten(
                nonempty(
                    reputation.own_proof_ids.first().map_or("", String::as_str),
                    "none published",
                ),
                20,
            ),
        ),
        Style::default().fg(muted()),
    )));

    if !reputation.error.is_empty() {
        // The previous figures stay on screen; this says they may be stale and why.
        lines.push(Line::from(Span::styled(
            format!("chain not read: {}", reputation.error),
            Style::default().fg(warn()),
        )));
    }

    lines
}

/// Every proof that has staked something on this node.
fn draw_opinions(frame: &mut Frame, app: &mut App, area: Rect) {
    if app.opinions.items.is_empty() {
        frame.render_widget(
            Paragraph::new(vec![Line::from(Span::styled(
                if app.reputation.is_read() {
                    "No proof has staked anything on this node. Reputation arrives when \
                     another node publishes an opinion about this one."
                } else {
                    "Once the chain is read, every proof that has staked something on \
                     this node is listed here."
                },
                Style::default().fg(muted()),
            ))])
            .wrap(Wrap { trim: true })
            .block(section_block(" WHO STAKES ON THIS NODE ", accent())),
            area,
        );
        return;
    }

    let now = app
        .reputation
        .read_at
        .unwrap_or_else(|| unix_now().unwrap_or(0));
    let rows = app.opinions.items.iter().map(|opinion| {
        Row::new(vec![
            Cell::from(format!(
                "{}{}",
                if opinion.positive { "+" } else { "−" },
                format_share(opinion.weight)
            ))
            .style(Style::default().fg(if opinion.positive { good() } else { bad() })),
            // What that share cost whoever published it. A proof sitting at the
            // min-box value has had nothing sacrificed into it, which is what tells a
            // cheap opinion from an expensive one.
            Cell::from(format_erg(opinion.backed_nanoerg)).style(Style::default().fg(
                if opinion.backed_nanoerg > 0.0 {
                    accent()
                } else {
                    muted()
                },
            )),
            // Long enough to identify the proof on an explorer, with the ellipsis
            // making it plain that it is not the whole id.
            Cell::from(shorten(&opinion.proof_id, 40)),
            Cell::from(format_age(opinion.published_at, now)),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(10),
            Constraint::Length(13),
            Constraint::Min(26),
            Constraint::Length(10),
        ],
    )
    .header(header_row(vec![
        "Stake",
        "Backed by",
        "Proof",
        "Published",
    ]))
    .block(section_block(
        format!(
            " WHO STAKES ON THIS NODE • {} opinions ",
            app.opinions.items.len()
        ),
        accent(),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("▸ ");
    app.list_area = area;
    frame.render_stateful_widget(table, area, &mut app.opinions.state);
}

/// A sunk-cost figure, in ERG.
///
/// Never routed through [`Money`]: MU is this node's unit of account for what it
/// charges, and ERG somebody else has burned into their own proof is not a balance of
/// ours to denominate — the same line the Overview's wallet card draws. ERG ↔ nanoERG
/// is fixed by the Ergo protocol, so the divisor is a constant and not a setting.
fn format_erg(nanoerg: f64) -> String {
    format!("{:.6} ERG", nanoerg / 1e9)
}

/// A share of a proof, as the percentage of it that it is.
fn format_share(share: f64) -> String {
    format!("{:.3}%", share * 100.0)
}

/// The same, signed, for a net figure where the direction is the point.
fn format_signed_share(share: f64) -> String {
    format!(
        "{}{}",
        if share < 0.0 { "−" } else { "+" },
        format_share(share.abs())
    )
}

fn proof_count(totals: &ReputationTotals) -> String {
    match (totals.positive_proofs, totals.negative_proofs) {
        (0, 0) => "no proof stakes anything here".to_string(),
        (1, 0) => "1 proof, for".to_string(),
        (0, 1) => "1 proof, against".to_string(),
        (positive, 0) => format!("{positive} proofs, all for"),
        (0, negative) => format!("{negative} proofs, all against"),
        (positive, negative) => format!("{positive} for, {negative} against"),
    }
}

/// How long ago an opinion was published, to the day.
///
/// `—` when the box could not be dated, which is the one case where nothing can be
/// said: an undated opinion still counts towards the standing total, and is in no
/// window (see `published_since` on the Python side).
fn format_age(published_at: Option<i64>, now: i64) -> String {
    let Some(published_at) = published_at else {
        return "—".to_string();
    };
    match (now - published_at).max(0) / 86_400 {
        0 => "today".to_string(),
        days => format!("{days}d ago"),
    }
}

/// Everything the Clients page knows about the selected client.
///
/// Deliberately nothing about *who* they are: a client id has no link back to a peer
/// (`peer.remote_client_id` is our id inside a remote peer, issue #178), so the card
/// shows what this client did here and nothing inferred.
fn client_detail_lines(
    money: &Money,
    client: Option<&Client>,
    detail: Option<&ClientDetail>,
    compact: bool,
) -> Vec<Line<'static>> {
    let Some(client) = client else {
        return vec![Line::from(Span::styled(
            "Select a client to inspect its deposits, instances and payments.",
            Style::default().fg(muted()),
        ))];
    };

    let mut lines = vec![metric_line("Client", client.id.clone())];
    if !compact {
        lines.push(metric_line("Balance", money.format_raw(&client.balance)));
        lines.push(metric_line(
            "Last usage",
            nonempty(&client.last_usage, "—").to_string(),
        ));
        if client.unmetered {
            lines.push(Line::from(Span::styled(
                "Never charged (unmetered): one of this node's own dev clients.",
                Style::default().fg(muted()),
            )));
        }
    }

    // A stale card is worse than none: the selection can move between the load and
    // the frame, and a payment shown under the wrong client is a lie about money.
    let Some(detail) = detail.filter(|detail| detail.client_id == client.id) else {
        return lines;
    };

    if compact {
        lines.push(metric_line(
            "History",
            format!(
                "{} deposit token(s) • {} instance(s) • {} payment(s)",
                detail.deposits.len(),
                detail.instances.len(),
                detail.payments.len()
            ),
        ));
        return lines;
    }

    lines.push(Line::from(""));
    lines.extend(payment_lines(
        money,
        &detail.payments,
        "Payments received",
        "Nothing received from this client yet.",
    ));

    if !detail.deposits.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("Deposit tokens ({})", detail.deposits.len()),
            Style::default().fg(accent()).bold(),
        )));
        for deposit in &detail.deposits {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(status_color(&deposit.status))),
                Span::styled(
                    format!("{:<10}", deposit.status.clone()),
                    Style::default().fg(status_color(&deposit.status)),
                ),
                Span::styled(
                    format!("{}  {}", deposit.created_at.clone(), shorten(&deposit.id, 20)),
                    Style::default().fg(text_colour()),
                ),
            ]));
        }
    }

    if !detail.instances.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("Instances started here ({})", detail.instances.len()),
            Style::default().fg(accent()).bold(),
        )));
        for instance in &detail.instances {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(good())),
                Span::styled(
                    nonempty(&instance.name, "unnamed").to_string(),
                    Style::default().fg(text_colour()).bold(),
                ),
                Span::styled(
                    format!("  {}", shorten(&instance.id, 24)),
                    Style::default().fg(muted()),
                ),
            ]));
        }
    }

    lines
}

/// A payment history as card lines, shared by the peer and client cards.
fn payment_lines(
    money: &Money,
    payments: &[PaymentRow],
    title: &str,
    empty: &str,
) -> Vec<Line<'static>> {
    if payments.is_empty() {
        return vec![Line::from(Span::styled(
            empty.to_string(),
            Style::default().fg(muted()),
        ))];
    }

    let mut lines = vec![Line::from(Span::styled(
        format!("{title} ({})", payments.len()),
        Style::default().fg(accent()).bold(),
    ))];
    for payment in payments {
        // The transaction id when there is one -- a simulated contract settles nothing
        // on a chain -- otherwise the deposit token, which is what identifies the
        // payment on the receiving side.
        let reference = if payment.tx_id.is_empty() {
            format!("token {}", shorten(nonempty(&payment.deposit_token, "—"), 16))
        } else {
            format!("tx {}", shorten(&payment.tx_id, 16))
        };
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(status_color(&payment.status))),
            Span::styled(
                format!("{:<20}", payment.created_at.clone()),
                Style::default().fg(muted()),
            ),
            Span::styled(
                format!("{:>14}  ", money.format_raw(&payment.amount)),
                Style::default().fg(text_colour()).bold(),
            ),
            Span::styled(
                format!("{:<14}", payment.status.clone()),
                Style::default().fg(status_color(&payment.status)),
            ),
            Span::styled(reference, Style::default().fg(muted())),
        ]));
    }
    lines
}

/// Reputation history as card lines: what moved the score, and why.
fn reputation_event_lines(events: &[ReputationEvent]) -> Vec<Line<'static>> {
    if events.is_empty() {
        return vec![Line::from(Span::styled(
            "No reputation event recorded yet.".to_string(),
            Style::default().fg(muted()),
        ))];
    }

    let mut lines = vec![Line::from(Span::styled(
        format!("Reputation history ({})", events.len()),
        Style::default().fg(accent()).bold(),
    ))];
    for event in events {
        let color = if event.amount < 0 { bad() } else { good() };
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(color)),
            Span::styled(
                format!("{:<20}", event.created_at.clone()),
                Style::default().fg(muted()),
            ),
            Span::styled(
                format!("{:>+6}  ", event.amount),
                Style::default().fg(color).bold(),
            ),
            // Stored as `payment_unacknowledged`; read as "payment unacknowledged".
            Span::styled(
                format!("{:<26}", event.reason.replace('_', " ")),
                Style::default().fg(text_colour()),
            ),
            Span::styled(
                event
                    .score_after
                    .map(|score| format!("→ {score}"))
                    .unwrap_or_default(),
                Style::default().fg(muted()),
            ),
        ]));
    }
    lines
}

/// Colour for a payment or deposit status: the ones that mean "money moved and
/// nothing came of it" have to stand out from the ones that worked.
fn status_color(status: &str) -> Color {
    match status {
        "communicated" | "accepted" | "payed" => good(),
        "unacknowledged" | "rejected" => bad(),
        _ => warn(),
    }
}

/// Full breakdown of the peer highlighted in the peers table: identity, balance,
/// reputation, and every payment contract it has registered. Previously reachable
/// only through a raw sqlite query (issue #231).
///
/// `compact` collapses each contract onto one line for terminals too short for the
/// full card.
fn peer_detail_lines(
    money: &Money,
    peer: Option<&Peer>,
    detail: Option<&PeerDetail>,
    donation: Option<(f64, f64)>,
    compact: bool,
) -> Vec<Line<'static>> {
    let Some(peer) = peer else {
        return vec![Line::from(Span::styled(
            "Select a peer to inspect its endpoints, reputation and payment contracts.",
            Style::default().fg(muted()),
        ))];
    };

    // The full id is worth repeating even in compact mode: the table truncates it.
    let mut lines = vec![metric_line("Peer", peer.id.clone())];
    if !compact {
        lines.push(metric_line(
            "Endpoints",
            nonempty(&peer.uris, "—").to_string(),
        ));
        lines.push(metric_line("Our balance", money.format_raw(&peer.balance)));
        // Our id inside *their* node, which is what an operator needs when reading
        // the other side's logs. Not a client of ours -- see the doc on the field.
        lines.push(metric_line(
            "Our client id there",
            nonempty(&peer.remote_client_id, "not registered").to_string(),
        ));
        // The score is ours, first-hand, keyed by this peer's public key. The proofs
        // below are the peer's own published opinions about other nodes, as it
        // announced them -- unverified here (issue #281).
        lines.push(metric_line(
            "Reputation",
            format!(
                "{}  •  {}",
                peer.reputation_score,
                match peer.proof_ids.len() {
                    0 => "no proof announced".to_string(),
                    n => format!("{n} proof(s) announced"),
                }
            ),
        ));
        for proof_id in &peer.proof_ids {
            lines.push(Line::from(vec![
                Span::styled("      proof  ", Style::default().fg(muted())),
                Span::styled(shorten(proof_id, 46), Style::default().fg(text_colour())),
            ]));
        }
        // What this peer's on-chain donations earn it here, and only here: the credit
        // is computed with *this* node's list of whose contributions it recognises, so
        // it is this node's opinion and not a property of the peer. A peer with none
        // is not being penalised -- the term is a bonus only.
        lines.push(metric_line(
            "Donation credit",
            match donation {
                Some((bonus, term)) => format!(
                    "{bonus:.4}  •  +{term:.4} to its score (beats a price up to {:.0}% higher)",
                    (term.exp() - 1.0) * 100.0
                ),
                None => "none counted by this node".to_string(),
            },
        ));
        lines.push(Line::from(""));
    }

    // Same guard as the client card: the selection can move between the load and the
    // frame, and payments shown under the wrong peer would be a lie about money.
    let history = detail.filter(|detail| detail.peer_id == peer.id);
    if let Some(history) = history {
        if compact {
            lines.push(metric_line(
                "History",
                format!(
                    "{} payment(s) • {} reputation event(s)",
                    history.payments.len(),
                    history.events.len()
                ),
            ));
        } else {
            lines.extend(payment_lines(
                money,
                &history.payments,
                "Payments made to this peer",
                "Nothing paid to this peer yet.",
            ));
            lines.push(Line::from(""));
            lines.extend(reputation_event_lines(&history.events));
            lines.push(Line::from(""));
        }
    }

    if peer.contracts.is_empty() {
        lines.push(Line::from(Span::styled(
            "No payment method registered for this peer.",
            Style::default().fg(warn()),
        )));
        return lines;
    }

    lines.push(Line::from(Span::styled(
        format!("Payment methods ({})", peer.contracts.len()),
        Style::default().fg(accent()).bold(),
    )));
    for contract in &peer.contracts {
        // The asset names the money: on Ergo one contract is paid in ERG and in every
        // token at the same address, so the ledger alone would label two different
        // rates identically. A 64-hex token id is shortened; a symbol is not.
        let asset = shorten(nonempty(&contract.asset, &contract.ledger.to_uppercase()), 12);
        if compact {
            lines.push(Line::from(vec![
                Span::styled("  ● ", Style::default().fg(good())),
                Span::styled(contract.ledger.clone(), Style::default().fg(good()).bold()),
                Span::styled(
                    format!(
                        "  {}  {}  {}  1 {} = {} MU",
                        asset,
                        shorten(&contract.contract_hash, 14),
                        shorten(nonempty(&contract.address, "—"), 14),
                        asset,
                        nonempty(&contract.mu_per_unit, "—")
                    ),
                    Style::default().fg(text_colour()),
                ),
            ]));
            continue;
        }
        lines.push(Line::from(vec![
            Span::styled("  ● ", Style::default().fg(good())),
            Span::styled(contract.ledger.clone(), Style::default().fg(good()).bold()),
            Span::styled(
                format!("  {}  contract {}", asset, shorten(&contract.contract_hash, 24)),
                Style::default().fg(text_colour()),
            ),
        ]));
        lines.push(Line::from(vec![
            Span::styled("      address  ", Style::default().fg(muted())),
            Span::styled(
                shorten(nonempty(&contract.address, "—"), 46),
                Style::default().fg(text_colour()),
            ),
        ]));
        lines.push(Line::from(vec![
            Span::styled("      rate     ", Style::default().fg(muted())),
            Span::styled(
                // What this peer says one unit of its ledger buys in ITS MU. This is
                // what makes a price it quotes convertible into money we understand,
                // so it is stated as an equation rather than as a bare number.
                format!("1 {} = {} MU", asset, nonempty(&contract.mu_per_unit, "—")),
                Style::default().fg(text_colour()),
            ),
        ]));
    }
    lines
}

/// The pricing page: what this node charges, as bars you can nudge.
///
/// Recurring and one-off prices get separate charts: on a shared axis a build price
/// three orders of magnitude above a tunnel-open one flattens the whole group. Exact
/// figures are in the table below, which is also where the selection lives.
/// The CELL page: the node drawn as a cell, and its policies as levers inside it.
///
/// The anatomy is load-bearing rather than decoration: what an operator is looking
/// for ("can anyone reach me?") is found by asking which part of a cell would be
/// responsible for it.
///
/// Two layouts, one cursor: wide terminals get the grid, narrow ones an accordion.
/// The keys behave identically in both.
fn draw_cell(frame: &mut Frame, app: &mut App, area: Rect) {
    let document = app.config_document.clone();
    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(6)]).split(area);
    draw_profile_bar(frame, app, rows[0]);

    app.cell.organelle_areas.clear();
    app.cell.lever_areas.clear();

    // The membrane: everything inside it is this node, everything outside is not.
    let membrane = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(muted()))
        .title(Span::styled(
            " MEMBRANE · inside vs outside ",
            Style::default().fg(muted()),
        ))
        .title_alignment(Alignment::Center);
    let inside = membrane.inner(rows[1]);
    frame.render_widget(membrane, rows[1]);

    // The grid's widest band is four organelles across, and each one needs room for a
    // label, a value and a gap; below ~28 columns apiece the boxes are narrower than
    // their own titles and the grid stops being readable. Narrower terminals get the
    // accordion, which shows one organelle at a time whatever the count.
    if inside.width >= 112 && inside.height >= 16 {
        draw_cell_grid(frame, app, inside, document.as_ref());
    } else {
        draw_cell_accordion(frame, app, inside, document.as_ref());
    }
}

/// Which posture this node is closest to, and how to see where it differs.
fn draw_profile_bar(frame: &mut Frame, app: &App, area: Rect) {
    let report = app.cell_profile();
    let colour = if report.deviations.is_empty() { good() } else { warn() };
    let line = Line::from(vec![
        Span::styled(" closest profile ", Style::default().fg(muted())),
        Span::styled(report.summary(), Style::default().fg(colour).bold()),
        Span::styled(
            format!(" ({}/{} keys)", report.matched(), report.total),
            Style::default().fg(muted()),
        ),
        Span::styled(
            if area.width >= 92 {
                "   p apply a profile · d what differs"
            } else {
                ""
            },
            Style::default().fg(muted()),
        ),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

/// The outward-facing organelles above, the self-preserving ones below, and the
/// nucleus banded across the middle.
///
/// The nucleus is central because its loss is the only permanent one: a mnemonic is
/// not recoverable, and a row of it beside "keep failures for 7 days" would read as
/// equally routine.
///
/// The rows hold three and four: the split is "outwards or inwards", not "how do
/// these divide evenly".
fn draw_cell_grid(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let nucleus_height = (Organelle::Nucleus.levers().len() as u16 + 3).min(area.height / 3);
    let bands = Layout::vertical([
        Constraint::Min(5),
        Constraint::Length(nucleus_height),
        Constraint::Min(5),
    ])
    .split(area);
    let top = Layout::horizontal([
        Constraint::Ratio(1, 3),
        Constraint::Ratio(1, 3),
        Constraint::Ratio(1, 3),
    ])
    .split(bands[0]);
    let bottom = Layout::horizontal([
        Constraint::Ratio(1, 4),
        Constraint::Ratio(1, 4),
        Constraint::Ratio(1, 4),
        Constraint::Ratio(1, 4),
    ])
    .split(bands[2]);

    let placement = [
        (Organelle::Channels, top[0]),
        (Organelle::Ribosomes, top[1]),
        (Organelle::Vesicles, top[2]),
        (Organelle::Nucleus, bands[1]),
        (Organelle::Immune, bottom[0]),
        (Organelle::Wall, bottom[1]),
        (Organelle::Mitochondria, bottom[2]),
        (Organelle::Vacuole, bottom[3]),
    ];
    for (organelle, box_area) in placement {
        draw_organelle(frame, app, organelle, box_area, document);
    }
}

/// One column, with only the focused organelle open.
///
/// A grid squeezed into a narrow terminal clips the values, which on this page are
/// the whole content. The rest collapse to a summary line each.
fn draw_cell_accordion(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let focused = app.cell.organelle();
    let constraints: Vec<Constraint> = Organelle::ALL
        .iter()
        .map(|organelle| {
            if *organelle == focused {
                // Whatever the collapsed rows leave: a box shorter than its levers
                // would hide the ones at the bottom with no way to reach them.
                Constraint::Min(focused.levers().len() as u16 + 2)
            } else {
                Constraint::Length(1)
            }
        })
        .collect();
    let rows = Layout::vertical(constraints).split(area);
    for (organelle, row) in Organelle::ALL.iter().zip(rows.iter()) {
        if *organelle == focused {
            draw_organelle(frame, app, *organelle, *row, document);
        } else {
            draw_collapsed_organelle(frame, app, *organelle, *row, document);
        }
    }
}

/// One organelle's box: its levers, each with the position it is currently in.
fn draw_organelle(
    frame: &mut Frame,
    app: &mut App,
    organelle: Organelle,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let index = Organelle::ALL
        .iter()
        .position(|candidate| *candidate == organelle)
        .unwrap_or(0);
    let focused = app.cell.organelle == index;
    let colour = organelle_colour(organelle);
    app.cell.organelle_areas.push((index, area));

    let block = Block::bordered()
        .border_type(if organelle == Organelle::Nucleus {
            // The nucleus is the one part of the cell whose loss cannot be undone.
            BorderType::Double
        } else {
            BorderType::Rounded
        })
        .border_style(Style::default().fg(if focused { colour } else { muted() }))
        .title(Line::from(vec![
            Span::styled(
                format!(" {} ", organelle.title()),
                Style::default().fg(colour).bold(),
            ),
            Span::styled(
                format!("· {} ", organelle.subtitle()),
                Style::default().fg(muted()),
            ),
        ]));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height == 0 {
        return;
    }

    let levers = organelle.levers();
    let rows = Layout::vertical(
        (0..inner.height)
            .map(|_| Constraint::Length(1))
            .collect::<Vec<_>>(),
    )
    .split(inner);

    // A box's share of the band can be shorter than its lever list, and a lever drawn
    // nowhere cannot be operated. So the box scrolls: the selected row is always
    // drawn. Unfocused boxes start at the first lever.
    let visible = rows.len();
    let offset = if focused && visible > 0 && app.cell.lever >= visible {
        (app.cell.lever + 1).saturating_sub(visible)
    } else {
        0
    };
    for (row, (lever_index, lever)) in rows
        .iter()
        .zip(levers.iter().enumerate().skip(offset))
    {
        let selected = focused && app.cell.lever == lever_index;
        // The absolute index, not the position in this window: it is what a mouse
        // click resolves to a lever, and a scrolled box would otherwise select the
        // wrong one.
        app.cell.lever_areas.push((index, lever_index, *row));
        frame.render_widget(
            Paragraph::new(lever_line(lever, document, selected, row.width)),
            *row,
        );
    }
}

/// A collapsed organelle: its name, and enough of its state to know whether to open
/// it. A row that said only "IMMUNE" would make the operator open all six to find
/// the one they want.
fn draw_collapsed_organelle(
    frame: &mut Frame,
    app: &mut App,
    organelle: Organelle,
    area: Rect,
    document: Option<&serde_yaml::Value>,
) {
    let index = Organelle::ALL
        .iter()
        .position(|candidate| *candidate == organelle)
        .unwrap_or(0);
    app.cell.organelle_areas.push((index, area));
    let summary = organelle
        .levers()
        .iter()
        .take(2)
        .map(|lever| cell::status(lever, document).label(lever))
        .collect::<Vec<_>>()
        .join(" · ");
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("  ", Style::default()),
            Span::styled(
                format!("{:<14}", organelle.title()),
                Style::default().fg(organelle_colour(organelle)),
            ),
            Span::styled(summary, Style::default().fg(muted())),
        ])),
        area,
    );
}

/// One lever row: what it decides on the left, where it is set on the right.
fn lever_line(
    lever: &Lever,
    document: Option<&serde_yaml::Value>,
    selected: bool,
    width: u16,
) -> Line<'static> {
    let status = cell::status(lever, document);
    let marker = match &status {
        // A filled marker is a position this page named; a hollow one is a value it
        // is only reporting. `custom` and `not set` are called out because both mean
        // "this page cannot describe what your node is doing here".
        LeverStatus::State(_) => "●",
        LeverStatus::Value(_) => "·",
        LeverStatus::Custom | LeverStatus::Unset => "⁓",
        LeverStatus::Link => "→",
    };
    let value = status.label(lever);
    let value_colour = match &status {
        LeverStatus::Custom | LeverStatus::Unset => warn(),
        LeverStatus::Link => accent(),
        _ if lever.warning.is_some() => bad(),
        _ => text_colour(),
    };
    // The label is padded to a fixed column so the values line up down the box: a
    // ragged right edge on eight rows is what makes a panel hard to scan.
    let label_width = (width.saturating_sub(14)).clamp(8, 18) as usize;
    let label = shorten(lever.label, label_width);
    Line::from(vec![
        Span::styled(
            if selected { "▸" } else { " " },
            Style::default().fg(accent()).bold(),
        ),
        Span::styled(
            format!("{label:<label_width$} "),
            if selected {
                Style::default().fg(text_colour()).bold()
            } else {
                Style::default().fg(muted())
            },
        ),
        Span::styled(format!("{marker} "), Style::default().fg(value_colour)),
        Span::styled(value, Style::default().fg(value_colour)),
    ])
}

fn organelle_colour(organelle: Organelle) -> Color {
    match organelle {
        Organelle::Channels => accent(),
        Organelle::Ribosomes => series(0),
        Organelle::Vesicles => series(1),
        Organelle::Nucleus => warn(),
        Organelle::Immune => bad(),
        Organelle::Wall => series(3),
        Organelle::Mitochondria => good(),
        Organelle::Vacuole => muted(),
    }
}

/// The profile picker: the postures, ordered from the most closed to the most open,
/// with how far this node already is from each.
fn draw_profile_popup(frame: &mut Frame, app: &App) {
    let profiles = cell::profiles();
    let area = centered_rect(70, profiles.len() as u16 * 2 + 6, frame.size());
    frame.render_widget(Clear, area);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(accent()))
        .style(Style::default().fg(text_colour()).bg(popup_background()))
        .title(Span::styled(
            " APPLY A PROFILE ",
            Style::default().fg(accent()).bold(),
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let mut lines: Vec<Line> = Vec::new();
    for (index, profile) in profiles.iter().enumerate() {
        let report = cell::report(profile, app.config_document.as_ref());
        let selected = index == app.cell.profile;
        let distance = if report.deviations.is_empty() {
            "you are here".to_string()
        } else {
            format!("{} keys would change", report.deviations.len())
        };
        lines.push(Line::from(vec![
            Span::styled(
                if selected { "▸ " } else { "  " },
                Style::default().fg(accent()).bold(),
            ),
            Span::styled(
                format!("{:<18}", profile.label),
                if selected {
                    Style::default().fg(text_colour()).bold()
                } else {
                    Style::default().fg(muted())
                },
            ),
            Span::styled(
                distance,
                Style::default().fg(if report.deviations.is_empty() { good() } else { muted() }),
            ),
        ]));
        lines.push(Line::from(Span::styled(
            format!("    {}", profile.blurb),
            Style::default().fg(muted()),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "A profile sets policy only — never an identity, a wallet or a path.",
        Style::default().fg(muted()),
    )));
    lines.push(Line::from(Span::styled(
        "⏎ see exactly what changes  ·  Esc cancel",
        Style::default().fg(warn()),
    )));
    frame.render_widget(
        Paragraph::new(lines).style(Style::default().fg(text_colour()).bg(popup_background())),
        inner,
    );
}

/// The hours this node takes work in, drawn as the day it is.
///
/// The bar answers what two scalar fields cannot: the open stretch is one run of
/// blocks whether or not it crosses midnight, and a marker says where now is.
///
/// A cursor along it also makes an unusable hour inexpressible, where the editors it
/// replaces took `25:00` happily and let the node refuse to start on it.
fn draw_schedule(frame: &mut Frame, app: &mut App, area: Rect) {
    let schedule = app.schedule();
    let now = app.now_minute;
    // Fixed lines regardless of window count: the enabled toggle, `+ add window`, the
    // duration, one of (flip countdown | off note), `at closing:`, and the dev-client
    // exemption note -- six -- plus one row per window (or one placeholder line when
    // there are none), plus the block's own two border rows.
    let summary_height = 8 + schedule.windows.len().max(1) as u16;
    let rows = Layout::vertical([
        Constraint::Length(11),
        Constraint::Length(summary_height),
        Constraint::Min(3),
    ])
    .split(area);

    draw_day_bar(frame, rows[0], app, &schedule, now);
    draw_schedule_summary(frame, rows[1], app, &schedule, now);
    draw_schedule_help(frame, rows[2], app);
}

fn draw_day_bar(
    frame: &mut Frame,
    area: Rect,
    app: &App,
    schedule: &schedule::Schedule,
    now: u16,
) {
    let dirty = app.schedule_is_dirty();
    let title = if dirty {
        " THE WORKING DAY • edited, not applied ".to_string()
    } else {
        " THE WORKING DAY ".to_string()
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(if dirty { warn() } else { muted() }))
        .title(title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 24 || inner.height < 5 {
        return;
    }

    // As many whole cells per hour as the width allows, up to a quarter-hour each. A
    // whole number of them keeps every hour the same width -- an axis where some hours
    // are wider than others is a chart that lies about where the edges are -- and the
    // bar then spans the pane instead of stopping halfway across it.
    let per_hour = (inner.width / 24).clamp(1, 4);
    let width = per_hour * 24;
    let per_slot = 60 / per_hour;

    let mut ticks = String::new();
    let mut axis = String::new();
    for slot in 0..width {
        let minute = slot * per_slot;
        axis.push(if minute % 180 == 0 { '┼' } else { '─' });
        if minute % 180 == 0 {
            ticks.push_str(&format!("{:02}", minute / 60));
        }
        while ticks.chars().count() < (slot + 1) as usize {
            ticks.push(' ');
        }
    }
    // Closes the day. Without it the axis stops at 21 and the last three hours read as
    // if they were outside the chart.
    if inner.width > width {
        axis.push('┤');
        ticks.push_str("24");
    }

    let mut bar: Vec<Span> = Vec::new();
    for slot in 0..width {
        let minute = slot * per_slot;
        let open = schedule.contains(minute);
        let is_now = now >= minute && now < minute + per_slot;
        let (glyph, colour) = match (open, is_now) {
            (true, true) => ('█', good()),
            (true, false) => ('█', if dirty { warn() } else { accent() }),
            (false, true) => ('▒', bad()),
            (false, false) => ('░', muted()),
        };
        bar.push(Span::styled(glyph.to_string(), Style::default().fg(colour)));
    }

    // The marker sits under the bar rather than inside it: a cell that showed "now"
    // instead of open/closed would hide the one thing the bar is for at the one hour
    // the operator cares about most.
    let mut marker = String::new();
    let now_slot = (now / per_slot).min(width.saturating_sub(1));
    for _ in 0..now_slot {
        marker.push(' ');
    }
    marker.push('▲');

    let open_now = schedule.contains(now);
    let state = if open_now { "OPEN" } else { "CLOSED" };
    let state_colour = if open_now { good() } else { bad() };

    let mut lines = vec![
        Line::from(Span::styled(ticks, Style::default().fg(muted()))),
        Line::from(Span::styled(axis, Style::default().fg(muted()))),
        Line::from(bar),
        Line::from(Span::styled(marker, Style::default().fg(state_colour))),
        Line::from(vec![
            Span::styled(
                format!("now {} · ", schedule::format_clock(now)),
                Style::default().fg(muted()),
            ),
            Span::styled(
                state,
                Style::default().fg(state_colour).add_modifier(Modifier::BOLD),
            ),
        ]),
    ];

    // On the same axis as the window above, in the same pane. Two charts in two panes
    // would be two axes the eye has to line up by hand, and lining them up is the whole
    // point: an operator should be able to see that they are closed through their own
    // busiest stretch (issue #337).
    lines.extend(demand_lines(&app.demand, app.demand_days, per_hour, width));
    frame.render_widget(Paragraph::new(lines), inner);
}

/// The demand rows drawn beneath the working day, on its axis.
///
/// Sparklines rather than numbers: the shape against the window above is what
/// matters. Refusals get their own row -- an hour closed through that work arrived in
/// anyway is the one worth reconsidering.
fn demand_lines(
    demand: &DemandByHour,
    days: u16,
    per_hour: u16,
    width: u16,
) -> Vec<Line<'static>> {
    if demand.is_empty() {
        return vec![Line::from(Span::styled(
            format!(
                "No demand recorded yet — the last {days} days will appear here as the node runs."
            ),
            Style::default().fg(muted()),
        ))];
    }

    // Eight levels, so a busy hour and a quiet one are told apart by height rather than
    // by reading a legend.
    const LEVELS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
    let spark = |series: &[u32; 24], colour: Color| -> Line<'static> {
        let peak = series.iter().copied().max().unwrap_or(0).max(1);
        let mut spans: Vec<Span> = Vec::new();
        for slot in 0..width {
            let hour = (slot / per_hour) as usize;
            let value = series[hour.min(23)];
            let glyph = if value == 0 {
                ' '
            } else {
                // Scaled so the busiest hour is full height and anything non-zero is
                // visible: an hour with one instance must not round away to nothing.
                let level = ((value as usize * LEVELS.len()) / (peak as usize + 1)).min(7);
                LEVELS[level]
            };
            spans.push(Span::styled(glyph.to_string(), Style::default().fg(colour)));
        }
        Line::from(spans)
    };

    let mut lines = vec![spark(&demand.held, series(0))];
    let refused = demand.total_refused();
    if refused > 0 {
        lines.push(spark(&demand.refused, bad()));
        lines.push(Line::from(vec![
            Span::styled("peak ", Style::default().fg(muted())),
            Span::styled(
                format!("{} held", demand.peak_held()),
                Style::default().fg(series(0)),
            ),
            Span::styled(" · ", Style::default().fg(muted())),
            Span::styled(
                format!("{refused} refused for being closed"),
                Style::default().fg(bad()),
            ),
            Span::styled(
                format!(" · last {days} days"),
                Style::default().fg(muted()),
            ),
        ]));
    } else {
        lines.push(Line::from(vec![
            Span::styled("peak ", Style::default().fg(muted())),
            Span::styled(
                format!("{} held", demand.peak_held()),
                Style::default().fg(series(0)),
            ),
            Span::styled(
                format!(" · nothing refused for being closed · last {days} days"),
                Style::default().fg(muted()),
            ),
        ]));
    }
    lines
}

/// One line per window, a toggle line each for enforcement and closing behaviour, and
/// an `+ add window` line.
///
/// Every one is a mouse target whose position is recorded as it is laid out
/// (`app.schedule_*_area(s)`) -- the same record-while-drawing pattern the CELL page
/// uses.
fn draw_schedule_summary(
    frame: &mut Frame,
    area: Rect,
    app: &mut App,
    schedule: &schedule::Schedule,
    now: u16,
) {
    app.schedule_edge_areas.clear();
    app.schedule_remove_areas.clear();

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(muted()))
        .title(" THE HOURS ");
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let selected_window = app
        .schedule_selected
        .min(schedule.windows.len().saturating_sub(1));
    let selected_edge = app.schedule_edge;
    let mut lines: Vec<Line> = Vec::new();

    // schedule: ON/OFF -- toggled by `w` or a click.
    let enabled_text = if schedule.enabled {
        " schedule: ON "
    } else {
        " schedule: OFF "
    };
    app.schedule_enabled_area = Rect::new(
        inner.x,
        inner.y + lines.len() as u16,
        enabled_text.chars().count() as u16,
        1,
    );
    lines.push(Line::from(Span::styled(
        enabled_text,
        Style::default()
            .fg(if schedule.enabled { good() } else { muted() })
            .add_modifier(Modifier::BOLD),
    )));

    if schedule.windows.is_empty() {
        lines.push(Line::from(Span::styled(
            "No windows yet.",
            Style::default().fg(muted()),
        )));
    } else {
        for (index, window) in schedule.windows.iter().enumerate() {
            let row = inner.y + lines.len() as u16;
            let is_selected_window = index == selected_window;
            let mut spans: Vec<Span> = Vec::new();
            let mut x = inner.x;

            let marker = if is_selected_window { "› " } else { "  " };
            x += marker.chars().count() as u16;
            spans.push(Span::styled(marker, Style::default().fg(accent())));

            let label = format!("W{} ", index + 1);
            x += label.chars().count() as u16;
            spans.push(Span::styled(label, Style::default().fg(muted())));

            for edge in [schedule::Edge::Start, schedule::Edge::End] {
                let minute = match edge {
                    schedule::Edge::Start => window.start,
                    schedule::Edge::End => window.end,
                };
                let text = format!(" {} {} ", edge.label(), schedule::format_clock(minute));
                let width = text.chars().count() as u16;
                let highlighted = is_selected_window && edge == selected_edge;
                let style = if highlighted {
                    Style::default().fg(inverse_text()).bg(accent())
                } else {
                    Style::default().fg(accent())
                };
                app.schedule_edge_areas
                    .push((index, edge, Rect::new(x, row, width, 1)));
                spans.push(Span::styled(text, style));
                x += width;
                spans.push(Span::raw(" "));
                x += 1;
            }

            let remove_text = "[x]";
            app.schedule_remove_areas.push((
                index,
                Rect::new(x, row, remove_text.chars().count() as u16, 1),
            ));
            spans.push(Span::styled(remove_text, Style::default().fg(bad())));

            if window.is_empty() {
                spans.push(Span::styled(
                    "  empty — move an edge",
                    Style::default().fg(muted()),
                ));
            } else if window.wraps() {
                spans.push(Span::styled(
                    "  one window through midnight",
                    Style::default().fg(muted()),
                ));
            }

            lines.push(Line::from(spans));
        }
    }

    let add_text = " + add window ";
    app.schedule_add_area = Rect::new(
        inner.x,
        inner.y + lines.len() as u16,
        add_text.chars().count() as u16,
        1,
    );
    lines.push(Line::from(Span::styled(
        add_text,
        Style::default().fg(accent()).add_modifier(Modifier::BOLD),
    )));

    // The span the windows describe, whether or not it is being enforced: a line
    // reading "opens 09:00 · closes 18:00 · 24 h a day" while switched off contradicts
    // itself, so what is and is not enforced is said on its own line below.
    let hours_preview = schedule::Schedule {
        enabled: true,
        ..schedule.clone()
    };
    if hours_preview.always_open() {
        // No non-empty window at all: always open regardless of the switch.
        lines.push(Line::from(Span::styled(
            "Always open: every window is empty, so nothing is refused.",
            Style::default().fg(warn()),
        )));
    } else {
        lines.push(Line::from(Span::styled(
            format!("{} a day", hours_preview.open_duration()),
            Style::default().fg(muted()),
        )));
        if !schedule.enabled {
            lines.push(Line::from(Span::styled(
                "Always open: the schedule is off. Press `w` (or click above) to enforce these hours.",
                Style::default().fg(warn()),
            )));
        } else if let Some(minutes) = schedule.minutes_until_flip(now) {
            let verb = if schedule.contains(now) { "closes" } else { "opens" };
            lines.push(Line::from(Span::styled(
                format!("{verb} in {}h {:02}m", minutes / 60, minutes % 60),
                Style::default().fg(muted()),
            )));
        }
    }

    app.schedule_on_close_area = Rect::new(inner.x, inner.y + lines.len() as u16, inner.width, 1);
    lines.push(Line::from(match schedule.on_close {
        schedule::OnClose::Refuse => vec![
            Span::styled("at closing: ", Style::default().fg(muted())),
            Span::styled("refuse", Style::default().fg(good())),
            Span::styled(
                " — new work only. What runs keeps running and keeps being charged.",
                Style::default().fg(muted()),
            ),
        ],
        schedule::OnClose::Stop => vec![
            Span::styled("at closing: ", Style::default().fg(muted())),
            Span::styled("stop", Style::default().fg(bad()).add_modifier(Modifier::BOLD)),
            Span::styled(
                " — running instances are destroyed mid-flight and refunded.",
                Style::default().fg(muted()),
            ),
        ],
    }));
    lines.push(Line::from(Span::styled(
        "Work from a dev client is exempt either way, so `nodo execute` and the core services keep working.",
        Style::default().fg(muted()),
    )));

    frame.render_widget(Paragraph::new(lines), inner);
}

fn draw_schedule_help(frame: &mut Frame, area: Rect, app: &App) {
    let dirty = app.schedule_is_dirty();
    let mut lines = vec![Line::from(Span::styled(
        "←/→ move the selected edge   ↑/↓ switch edge   [/] switch window   a add   d remove   w on/off   c what closing does",
        Style::default().fg(muted()),
    ))];
    lines.push(Line::from(Span::styled(
        "Click an edge to select it, [x] to remove a window, + add window, or the on/off and closing lines — the mouse reaches everything here.",
        Style::default().fg(muted()),
    )));
    lines.push(Line::from(if dirty {
        vec![
            Span::styled("Enter", Style::default().fg(warn()).add_modifier(Modifier::BOLD)),
            Span::styled(
                " applies it (backup, write, restart, and revert if the node does not come back)   ",
                Style::default().fg(muted()),
            ),
            Span::styled("Esc", Style::default().fg(warn())),
            Span::styled(" discards", Style::default().fg(muted())),
        ]
    } else {
        vec![Span::styled(
            "Nothing to apply: this is the schedule the node is running.",
            Style::default().fg(muted()),
        )]
    }));
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(muted()))
                .title(" EDIT "),
        ),
        area,
    );
}

fn draw_pricing(frame: &mut Frame, app: &mut App, area: Rect) {
    let columns = Layout::horizontal([Constraint::Percentage(62), Constraint::Percentage(38)])
        .split(area);
    let left = Layout::vertical([
        Constraint::Percentage(50),
        Constraint::Percentage(50),
    ])
    .split(columns[0]);

    let selected = app.prices.state_id.clone();
    draw_price_bars(
        frame,
        left[0],
        PriceChart {
            title: " RECURRING • charged while held (log scale) ",
            recurring: true,
            color: accent(),
        },
        &app.prices.items,
        selected.as_deref(),
        &app.money,
    );
    draw_price_bars(
        frame,
        left[1],
        PriceChart {
            title: " ONE-OFF • charged per event (log scale) ",
            recurring: false,
            color: series(1),
        },
        &app.prices.items,
        selected.as_deref(),
        &app.money,
    );

    // 13 rows fit the card's tallest state (a price selected, plus the worked
    // example on the last line); anything shorter silently clips the example.
    let right = Layout::vertical([Constraint::Length(13), Constraint::Min(4)]).split(columns[1]);
    draw_money_card(frame, app, right[0]);
    draw_price_table(frame, app, right[1]);
}

/// Which half of the price vector a chart draws, and how it looks.
struct PriceChart<'a> {
    title: &'a str,
    /// Recurring prices are charged for as long as a resource is held; one-off ones
    /// price an event.
    recurring: bool,
    color: Color,
}

/// One vertical bar per price in the requested group.
fn draw_price_bars(
    frame: &mut Frame,
    area: Rect,
    chart: PriceChart,
    prices: &[PriceEntry],
    selected: Option<&str>,
    money: &Money,
) {
    let PriceChart {
        title,
        recurring,
        color,
    } = chart;
    // Node-wide prices only. A per-arch override is a variation on the price above it,
    // not a fourth resource, and charting both would show "RAM" three times and read as
    // three times the memory revenue. The table below carries every per-arch figure.
    let group: Vec<&PriceEntry> = prices
        .iter()
        .filter(|entry| entry.recurring == recurring && entry.arch.is_none())
        .collect();
    if group.is_empty() {
        return;
    }

    // BarChart draws the amount inside the bar, so a zero-height bar prints nothing
    // and "free" has to ride in the always-rendered label -- otherwise a price
    // deliberately set to nothing looks like a missing feature.
    let bars: Vec<Bar> = group
        .iter()
        .map(|entry| {
            let highlighted = selected == Some(entry.key);
            Bar::default()
                .value(log_bar_value(entry.mu))
                .text_value(money.format_raw(&entry.mu.to_string()))
                .label(Line::from(if entry.mu == 0 {
                    format!("{} free", entry.short)
                } else {
                    entry.short.to_string()
                }))
                .style(Style::default().fg(if highlighted { text_colour() } else { color }))
                .value_style(if highlighted {
                    Style::default().fg(inverse_text()).bg(text_colour()).bold()
                } else {
                    Style::default().fg(inverse_text()).bg(color)
                })
        })
        .collect();

    let width = ((area.width.saturating_sub(4)) / group.len().max(1) as u16).clamp(3, 14);
    let chart = BarChart::default()
        .block(section_block(title.to_string(), color))
        .data(BarGroup::default().bars(&bars))
        .bar_width(width.saturating_sub(1).max(1))
        .bar_gap(1)
        .label_style(Style::default().fg(muted()));
    frame.render_widget(chart, area);
}

/// A linear height flattens a price 1000x below its neighbour to nothing (#381),
/// which defeats what the chart is for. `ln(1 + mu)` fits that range into the rows
/// available, at the cost of the chart not being linear -- which is why the title
/// says "log scale". Only the height is scaled; the labels carry the real number.
fn log_bar_value(mu: u64) -> u64 {
    ((1.0 + mu as f64).ln() * 1_000_000.0).round() as u64
}

/// What the numbers on the bars actually mean: the display unit, the ledger rate, the
/// scarcity ceiling that bounds every price, and a worked example.
fn draw_money_card(frame: &mut Frame, app: &App, area: Rect) {
    let money = &app.money;
    let hourly = app.reference_hourly_mu();
    let selected = app.prices.selected();

    let mut lines = vec![
        metric_line("Unit", format!("{} ({})", money.symbol, money.unit_name)),
        metric_line("1 MU", {
            // What one MU is worth on the ledger. This is the piece that makes a price
            // in MU mean anything to a peer, so it is stated rather than implied.
            let nanoerg = 1.0 / money.mu_per_nanoerg;
            format!("{nanoerg} nanoERG")
        }),
        metric_line(
            "Scarcity",
            format!(
                "x1 .. x{} (curve {})",
                app.scarcity.max_multiplier, app.scarcity.curve
            ),
        ),
        Line::from(""),
    ];

    if let Some(entry) = selected {
        lines.push(Line::from(Span::styled(
            entry.config_label(),
            Style::default().fg(text_colour()).bold(),
        )));
        lines.push(metric_line("Price", format!("{} MU {}", entry.mu, entry.per)));
        lines.push(metric_line("That is", money.format_mu(entry.mu)));
        lines.push(metric_line(
            "At max load",
            money.format_mu(entry.mu.saturating_mul(app.scarcity.max_multiplier)),
        ));

        // Why this page mentions the virtualizer at all: a memory price is not what
        // the node earns per GiB it commits. The node boots the VM larger than the
        // service declared and absorbs the difference, so a price set without that in
        // view under-recovers by an amount that differs per architecture.
        if let (Some(arch), Some((effective, multiplier))) =
            (entry.arch, app.effective_memory_mu(entry))
        {
            if let Some(reserve) = app.reserve_for(arch) {
                lines.push(Line::from(""));
                lines.push(metric_line(
                    "Guest kernel",
                    format!("+{} MiB +{:.0}%", reserve.fixed_mib, reserve.ratio * 100.0),
                ));
                // "You set X, you keep Y" -- said in the same unit as the price above
                // it, because the operator is choosing X and cares about Y.
                lines.push(metric_line(
                    "Node earns",
                    format!("{} MU /GiB committed", effective.round() as u64),
                ));
                lines.push(metric_line(
                    "…on 1GiB",
                    format!("{:.0}% of the price set", 100.0 / multiplier),
                ));
            }
        }
    }

    lines.push(Line::from(""));
    // Approximate on purpose: the node charges per manager tick and truncates each one
    // to whole MU, so an hour of ticks comes to a hair less than the hourly price.
    lines.push(metric_line("1h example", "256MiB+1vCPU+10GiB"));
    lines.push(Line::from(Span::styled(
        format!("{:<12}~ {}", "", money.format_mu(hourly)),
        Style::default().fg(good()),
    )));

    draw_card(frame, area, "MONEY", lines, good());
}

fn draw_price_table(frame: &mut Frame, app: &mut App, area: Rect) {
    let money = app.money.clone();
    let rows: Vec<Row> = app
        .prices
        .items
        .iter()
        .map(|entry| {
            // A per-arch row config.yaml does not set shows the scalar it inherits,
            // not a price of its own -- the difference between "arm64 costs this" and
            // "arm64 has its own rate", which editing it is what creates.
            let amount = if entry.inherited {
                format!("{} (inherited)", money.format_mu(entry.mu))
            } else {
                money.format_mu(entry.mu)
            };
            let row = Row::new(vec![entry.short.clone(), entry.mu.to_string(), amount]);
            if entry.arch.is_some() {
                row.style(Style::default().fg(muted()))
            } else {
                row
            }
        })
        .collect();
    let table = Table::new(
        rows,
        [
            Constraint::Length(12),
            Constraint::Percentage(40),
            Constraint::Percentage(48),
        ],
    )
    .header(header_row(vec!["Price", "MU", money.symbol.as_str()]))
    .block(section_block(
        " PRICES • +/- adjust, e exact ".to_string(),
        warn(),
    ))
    .highlight_style(selected_style())
    .highlight_symbol("> ");
    app.list_area = area;
    frame.render_stateful_widget(table, area, &mut app.prices.state);
}

/// The ENERGY page: the `energy:` block as a list of decisions with their reasons
/// beside them (issue #395).
///
/// Two columns, and the right one is the point: the keys are on Config already, but
/// Config cannot show the paragraph saying `IDLE_WATTS` must be *measured* rather
/// than guessed.
///
/// Rows record where they were drawn (`energy_row_areas`) so the mouse can find
/// them: three bordered sections is not a geometry a generic hit test can retrace.
fn draw_energy(frame: &mut Frame, app: &mut App, area: Rect) {
    let columns = Layout::horizontal([Constraint::Percentage(58), Constraint::Percentage(42)])
        .split(area);

    let entries = crate::energy::entries();
    // One block per section, each as tall as the rows it holds plus its border and
    // its one-line blurb. Sized to content rather than split evenly, so a section of
    // two rows does not get the same height as one of seven and print five blank
    // lines to fill it.
    let mut sections: Vec<(crate::energy::EnergySection, Vec<usize>)> = Vec::new();
    for (index, (section, _)) in entries.iter().enumerate() {
        match sections.last_mut() {
            Some((last, rows)) if last == section => rows.push(index),
            _ => sections.push((*section, vec![index])),
        }
    }
    let constraints: Vec<Constraint> = sections
        .iter()
        .map(|(_, rows)| Constraint::Length(rows.len() as u16 + 3))
        .chain(std::iter::once(Constraint::Min(0)))
        .collect();
    let panes = Layout::vertical(constraints).split(columns[0]);

    app.energy_row_areas.clear();
    // `list_area` stays zero: this page is three blocks rather than one table, and a
    // generic row hit test over it would land on the wrong key.
    for (pane, (section, rows)) in panes.iter().zip(sections.iter()) {
        draw_energy_section(frame, app, *pane, *section, rows);
    }

    draw_energy_help(frame, app, columns[1]);
}

/// One band of the ENERGY page, and the rows in it.
fn draw_energy_section(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    section: crate::energy::EnergySection,
    rows: &[usize],
) {
    let entries = crate::energy::entries();
    let colour = match section {
        crate::energy::EnergySection::Metering => accent(),
        crate::energy::EnergySection::Model => warn(),
        crate::energy::EnergySection::Sources => series(1),
    };
    let block = section_block(format!(" {} ", section.title()), colour);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height == 0 {
        return;
    }

    let mut lines: Vec<Line<'static>> = vec![Line::from(Span::styled(
        section.blurb(),
        Style::default().fg(muted()).italic(),
    ))];
    for (offset, index) in rows.iter().enumerate() {
        let (_, entry) = &entries[*index];
        let selected = app.energy_selected == *index;
        // `(not set)` rather than a blank: a key this installation's config predates
        // and a key deliberately set to the empty string are different facts, and
        // SMART_PLUG_URL can legitimately be the second.
        let value = app
            .energy_value(entry)
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "(not set)".to_string());
        let marker = if selected { "> " } else { "  " };
        let key_style = if selected {
            selected_style()
        } else {
            Style::default().fg(text_colour())
        };
        lines.push(Line::from(vec![
            Span::styled(format!("{marker}{:<26}", entry.key()), key_style),
            Span::styled(" ", Style::default()),
            Span::styled(value, Style::default().fg(energy_value_colour(entry, app))),
        ]));
        // The row's own screen rectangle, for the mouse. The blurb takes the first
        // inner line, so rows start one below it.
        let y = inner.y + 1 + offset as u16;
        if y < inner.y + inner.height {
            app.energy_row_areas
                .push((*index, Rect::new(inner.x, y, inner.width, 1)));
        }
    }
    frame.render_widget(Paragraph::new(lines), inner);
}

/// Green for a source that is switched on, muted for one that is not.
///
/// "Which of these five is measuring anything" is what the page is most often opened
/// to answer, and it is otherwise five `false`s to read one at a time.
fn energy_value_colour(entry: &crate::energy::EnergyEntry, app: &App) -> Color {
    match app.energy_value(entry) {
        Some(value) if value == "true" => good(),
        Some(value) if value == "false" || value.is_empty() || value == "0" => muted(),
        Some(_) => text_colour(),
        None => muted(),
    }
}

/// The right-hand panel: what the selected key is, what it is set to, and why it
/// matters. The last of those is the whole reason this page exists rather than a
/// bookmark into the Config tree.
fn draw_energy_help(frame: &mut Frame, app: &App, area: Rect) {
    let Some(entry) = app.selected_energy() else {
        frame.render_widget(
            Paragraph::new("No setting selected.")
                .block(section_block(" ABOUT ".to_string(), accent())),
            area,
        );
        return;
    };
    let value = app
        .energy_value(entry)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "(not set)".to_string());
    let lines = vec![
        Line::from(Span::styled(
            entry.label,
            Style::default().fg(text_colour()).bold(),
        )),
        Line::from(Span::styled(entry.path, Style::default().fg(muted()))),
        Line::from(""),
        Line::from(vec![
            Span::styled("now: ", Style::default().fg(muted())),
            Span::styled(value, Style::default().fg(accent()).bold()),
        ]),
        Line::from(""),
        Line::from(Span::styled(entry.help, Style::default().fg(text_colour()))),
        Line::from(""),
        Line::from(Span::styled(
            "Written through the same backup, yq write, restart and revert as every \
             other change (see the TUI README).",
            Style::default().fg(muted()).italic(),
        )),
    ];
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: true })
            .block(section_block(" ABOUT ".to_string(), accent())),
        area,
    );
}

fn draw_config(frame: &mut Frame, app: &mut App, area: Rect) {
    let needle = app.config_filter.to_lowercase();
    // Owns its Strings (`'static`), so it doesn't borrow `app` and the tree state
    // can be mutated (pre-selection, render) alongside it.
    let items = build_config_tree(&app.config_all, &needle);

    // On first entry nothing is selected yet; land on the first top-level section
    // so the selection highlight (and later Enter/e) has a target.
    if app.config_tree_state.selected().is_empty() {
        if let Some(first) = items.first() {
            app.config_tree_state
                .select(vec![first.identifier().clone()]);
        }
    }

    let title = if app.config_filter.is_empty() {
        format!(
            " CONFIGURATION • {} values • all sections ",
            app.config_all.len()
        )
    } else {
        let matches = app
            .config_all
            .iter()
            .filter(|entry| {
                entry.path.to_lowercase().contains(&needle)
                    || (!entry.secret && entry.value.to_lowercase().contains(&needle))
            })
            .count();
        format!(
            " CONFIGURATION • {} values • filter \"{}\" • {} match ",
            app.config_all.len(),
            app.config_filter,
            matches
        )
    };

    let tree = Tree::new(&items)
        .expect("config tree identifiers are unique within each section")
        .block(section_block(title, warn()))
        .highlight_style(selected_style())
        .node_closed_symbol("▸ ")
        .node_open_symbol("▾ ")
        .node_no_children_symbol("· ");
    frame.render_stateful_widget(tree, area, &mut app.config_tree_state);
}

/// Build the collapsible configuration tree from the flat, document-ordered
/// [`ConfigEntry`] list: mappings and sequences become branches, scalars leaves.
///
/// The structure comes from each entry's `path_segments`, so this only shapes how
/// the unchanged data model is drawn. `needle` (lowercased, empty for no filter)
/// highlights matches without removing anything.
fn build_config_tree(entries: &[ConfigEntry], needle: &str) -> Vec<TreeItem<'static, String>> {
    // An ordered intermediate tree: children stay in document order, and a node
    // either carries a scalar (`leaf`) or has children, never both.
    #[derive(Default)]
    struct Node {
        children: Vec<(String, Node)>,
        leaf: Option<usize>,
    }
    fn child_mut<'a>(node: &'a mut Node, token: &str) -> &'a mut Node {
        if let Some(pos) = node.children.iter().position(|(t, _)| t == token) {
            &mut node.children[pos].1
        } else {
            node.children.push((token.to_string(), Node::default()));
            &mut node.children.last_mut().unwrap().1
        }
    }

    let mut root = Node::default();
    for (index, entry) in entries.iter().enumerate() {
        let mut cursor = &mut root;
        for segment in &entry.path_segments {
            let token = segment_token(segment);
            cursor = child_mut(cursor, &token);
        }
        cursor.leaf = Some(index);
    }

    fn convert(
        token: &str,
        node: Node,
        parent_path: &str,
        entries: &[ConfigEntry],
        needle: &str,
    ) -> TreeItem<'static, String> {
        let display_path = join_config_path(parent_path, token);
        match node.leaf {
            Some(index) => {
                let entry = &entries[index];
                let highlighted = !needle.is_empty()
                    && (entry.path.to_lowercase().contains(needle)
                        || (!entry.secret && entry.value.to_lowercase().contains(needle)));
                TreeItem::new_leaf(token.to_string(), config_leaf_line(entry, highlighted))
            }
            None => {
                let count = node.children.len();
                let highlighted = !needle.is_empty() && display_path.to_lowercase().contains(needle);
                let children = node
                    .children
                    .into_iter()
                    .map(|(child_token, child)| {
                        convert(&child_token, child, &display_path, entries, needle)
                    })
                    .collect::<Vec<_>>();
                TreeItem::new(
                    token.to_string(),
                    config_branch_line(token, count, highlighted),
                    children,
                )
                .expect("config tree identifiers are unique within each section")
            }
        }
    }

    root.children
        .into_iter()
        .map(|(token, node)| convert(&token, node, "", entries, needle))
        .collect()
}

/// Reconstruct the dotted display path (`a.b[1].c`) from a parent path and one
/// more token, matching `config_path_display` so highlight tests hit the same
/// strings the flat table used to show.
fn join_config_path(parent: &str, token: &str) -> String {
    if parent.is_empty() {
        token.to_string()
    } else if token.starts_with('[') {
        format!("{parent}{token}")
    } else {
        format!("{parent}.{token}")
    }
}

/// A scalar leaf: `key: value  [type]`, with the value masked exactly as the
/// table did (via [`ConfigEntry::display_value`]). The key is reverse-video when
/// it matches the active filter.
fn config_leaf_line(entry: &ConfigEntry, highlighted: bool) -> Line<'static> {
    let key = entry
        .path_segments
        .last()
        .map(segment_token)
        .unwrap_or_default();
    let key_style = if highlighted {
        Style::default().fg(inverse_text()).bg(warn()).bold()
    } else {
        Style::default().fg(text_colour())
    };
    Line::from(vec![
        Span::styled(key, key_style),
        Span::styled(": ", Style::default().fg(muted())),
        Span::styled(entry.display_value(), Style::default().fg(accent())),
        Span::styled(
            format!("  [{}]", entry.value_type),
            Style::default().fg(muted()),
        ),
    ])
}

/// A section branch: its name plus the number of direct children, highlighted
/// (reverse-video) when the section's path matches the active filter.
fn config_branch_line(token: &str, count: usize, highlighted: bool) -> Line<'static> {
    let name_style = if highlighted {
        Style::default().fg(inverse_text()).bg(warn()).bold()
    } else {
        Style::default().fg(warn()).bold()
    };
    Line::from(vec![
        Span::styled(token.to_string(), name_style),
        Span::styled(format!("  ({count})"), Style::default().fg(muted())),
    ])
}

fn draw_logs(frame: &mut Frame, app: &App, area: Rect) {
    let split =
        Layout::horizontal([Constraint::Percentage(68), Constraint::Percentage(32)]).split(area);
    let node_text = visible_tail(&app.node_logs, split[0].height.saturating_sub(2) as usize);
    frame.render_widget(
        Paragraph::new(node_text)
            .block(section_block(" NODE LOG • app.log ", text_colour()))
            .style(Style::default().fg(muted()))
            .wrap(Wrap { trim: false }),
        split[0],
    );
    let action_text = visible_tail(&app.app_logs, split[1].height.saturating_sub(2) as usize);
    frame.render_widget(
        Paragraph::new(action_text)
            .block(section_block(" TUI ACTIONS ", accent()))
            .style(Style::default().fg(muted()))
            .wrap(Wrap { trim: false }),
        split[1],
    );
}

fn draw_footer(frame: &mut Frame, app: &App, area: Rect) {
    // The KyA gate owns the footer too: the keys that matter while it is up are its
    // own, and a page's shortcuts printed underneath an unanswered question would be
    // advertising keys that deliberately do nothing (issue #395).
    if app.awaiting_kya() {
        let lines = vec![
            Line::from(Span::styled(
                "Accepting is required to run this node.",
                Style::default().fg(warn()),
            )),
            Line::from(Span::styled(
                "y accept · n decline · ↑↓ scroll",
                Style::default().fg(muted()),
            )),
        ];
        frame.render_widget(Paragraph::new(lines).alignment(Alignment::Center), area);
        return;
    }
    // Page-local keys only. The navigation keys are the same everywhere and are
    // printed on their own line below, rather than repeated twelve times with
    // twelve chances to fall out of step -- which is what "tab/shift+tab cycle" did
    // on every one of these strings before the groups existed.
    let controls = match app.page() {
        Page::Overview => "r refresh  \u{2022}  q quit",
        Page::Instances => "\u{2191}/\u{2193} select  \u{2022}  g tree/flat  \u{2022}  k kill  \u{2022}  r refresh  \u{2022}  q quit",
        Page::Services => {
            "\u{2191}/\u{2193} select  \u{2022}  e execute  \u{2022}  i details  \u{2022}  d delete  \u{2022}  q quit"
        }
        Page::Peers => {
            "\u{2191}/\u{2193} select  \u{2022}  +/- reputation  \u{2022}  c connect  \u{2022}  d forget  \u{2022}  q quit"
        }
        Page::Clients => {
            "\u{2191}/\u{2193} select  \u{2022}  + credit  \u{2022}  - debit  \u{2022}  r refresh  \u{2022}  q quit"
        }
        Page::Earnings => "\u{2191}/\u{2193} select an opinion  \u{2022}  r re-read the chain  \u{2022}  q quit",
        Page::Cell => {
            "\u{2192}/\u{2190} organelle  \u{2022}  \u{2191}/\u{2193} lever  \u{2022}  \u{23ce} change  \u{2022}  e keys behind it  \u{2022}  p profiles  \u{2022}  d deviations  \u{2022}  n router guide"
        }
        Page::Pricing => {
            "\u{2191}/\u{2193} select  \u{2022}  +/- adjust 10%  \u{2022}  e exact value  \u{2022}  r refresh  \u{2022}  q quit"
        }
        // The one page that keeps `[`/`]` for itself: they switch which window the
        // arrows act on, which is why the group keys except it.
        Page::Schedule => {
            "\u{2192}/\u{2190} move edge 30m  \u{2022}  \u{2191}/\u{2193} which edge  \u{2022}  [/] window  \u{2022}  w on/off  \u{2022}  c closing  \u{2022}  \u{23ce} apply  \u{2022}  esc discard"
        }
        Page::Energy => "\u{2191}/\u{2193} select  \u{2022}  \u{23ce} / e edit  \u{2022}  r refresh  \u{2022}  q quit",
        Page::Config => {
            "\u{2191}/\u{2193} select  \u{2022}  \u{2192}/\u{2190} branch  \u{2022}  \u{23ce} toggle  \u{2022}  e edit  \u{2022}  a add  \u{2022}  d remove  \u{2022}  / filter  \u{2022}  q quit"
        }
        Page::Logs => "r refresh  \u{2022}  q quit",
    };
    // How to get anywhere, said once. SCHEDULE is the exception that has to be named
    // where it applies: a footer advertising `[/] group` on the one page where those
    // keys do something else would be advertising the wrong thing.
    let navigation = if app.page() == Page::Schedule {
        "1-5 group  \u{2022}  tab/shift+tab page in group  \u{2022}  click either row"
    } else {
        "[/] or 1-5 group  \u{2022}  tab/shift+tab page in group  \u{2022}  click either row"
    };
    let lines = vec![
        Line::from(Span::styled(controls, Style::default().fg(muted()))),
        Line::from(vec![
            Span::styled(navigation, Style::default().fg(muted())),
            Span::raw("   "),
            Span::styled(app.status.clone(), Style::default().fg(warn())),
        ]),
    ];
    frame.render_widget(Paragraph::new(lines).alignment(Alignment::Center), area);
}

/// Body lines and hint text for the `EditConfig` popup, one variant per
/// [`EditKind`]: a checkbox, a steppable number, a cyclable enum picker, or the
/// original freeform text field (also used outside config editing, e.g. Connect
/// and the filter box).
fn edit_popup_body(app: &App) -> (Vec<Line<'static>>, String) {
    if app.input_mode == InputMode::AddConfigItem {
        // The quoting note is not decoration: a leading `*` is YAML's alias
        // indicator, so `*.example.com` unquoted is rejected as invalid YAML rather
        // than stored. A `*` anywhere else needs no quoting.
        return (
            vec![Line::from(app.input.clone())],
            "Enter appends • Esc cancels • a YAML literal, so quote a leading *: \"*.example.com\""
                .to_string(),
        );
    }
    if app.input_mode != InputMode::EditConfig {
        return (
            vec![Line::from(app.input.clone())],
            "Enter saves • Esc cancels • Ctrl+U clears".to_string(),
        );
    }

    match &app.edit_kind {
        EditKind::Bool => {
            let checked = app.input.trim() == "true";
            let label = if checked { "[x] true" } else { "[ ] false" };
            (
                vec![Line::from(Span::styled(
                    label,
                    Style::default().fg(text_colour()).bold(),
                ))],
                "Space / ←/→ toggles • Enter saves • Esc cancels".to_string(),
            )
        }
        EditKind::Number => (
            vec![Line::from(app.input.clone())],
            "↑/↓ adjust by 1 • type to overwrite • Enter saves • Esc cancels".to_string(),
        ),
        EditKind::Enum(options) => {
            let current = app.input.trim();
            let lines = options
                .iter()
                .map(|option| {
                    let selected = option == current;
                    let marker = if selected { "▸ " } else { "  " };
                    Line::from(Span::styled(
                        format!("{marker}{option}"),
                        if selected {
                            Style::default().fg(accent()).bold()
                        } else {
                            Style::default().fg(text_colour())
                        },
                    ))
                })
                .collect();
            (lines, "↑/↓ cycle • Enter saves • Esc cancels".to_string())
        }
        EditKind::Text => {
            let secret_hint = if app.edit_config_secret {
                " • existing secret hidden; blank keeps it, type \"\" to clear"
            } else {
                ""
            };
            let display = if app.edit_config_secret {
                "•".repeat(app.input.chars().count())
            } else {
                app.input.clone()
            };
            (
                vec![Line::from(display)],
                format!("Enter saves • Esc cancels • Ctrl+U clears{secret_hint}"),
            )
        }
    }
}

/// Break `text` into lines of at most `width` characters, on word boundaries.
///
/// Characters, not bytes: splitting a multi-byte one produces a replacement glyph.
/// An over-long word is left ragged rather than cut mid-token, because a truncated
/// path is one the operator might paste.
fn wrapped(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return vec![text.to_string()];
    }
    let mut lines: Vec<String> = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        let would_be = if current.is_empty() {
            word.chars().count()
        } else {
            current.chars().count() + 1 + word.chars().count()
        };
        if !current.is_empty() && would_be > width {
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
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn draw_input_popup(frame: &mut Frame, app: &App) {
    let (mut content, hint) = edit_popup_body(app);
    // Why a config edit may not stick, said before the value is typed. Never the
    // file: it is the *restart* the transaction owes a serving node, so a key the
    // node re-reads from disk (`energy.*`) owes none and shows no hint.
    let root_hint = app.config_write_root_hint().filter(|_| {
        matches!(
            app.input_mode,
            InputMode::EditConfig | InputMode::AddConfigItem
        )
    });
    // Measured against the popup's own inner width, so the box grows by however
    // many rows the warning actually takes on this terminal instead of by a guess.
    let probe = centered_rect(72, 3, frame.size());
    let hint_rows = root_hint
        .map(|hint| wrapped(hint, probe.width.saturating_sub(2) as usize).len() as u16)
        .unwrap_or(0);
    let height = (content.len() as u16 + 2).max(3) + 2 + hint_rows;
    let area = centered_rect(72, height, frame.size());
    frame.render_widget(Clear, area);
    content.push(Line::from(Span::styled(hint, Style::default().fg(muted()))));
    if let Some(root_hint) = root_hint {
        // Wrapped here rather than through `Paragraph::wrap`, which would also
        // reflow the value being edited -- a YAML literal broken across lines is a
        // different literal, and this popup is where an operator checks what they
        // typed.
        for line in wrapped(root_hint, area.width.saturating_sub(2) as usize) {
            content.push(Line::from(Span::styled(line, Style::default().fg(warn()))));
        }
    }
    let popup = Paragraph::new(content)
        .block(
            Block::bordered()
                .title(Span::styled(
                    format!(" {} ", app.input_title),
                    Style::default().fg(accent()).bold(),
                ))
                .border_style(Style::default().fg(accent())),
        )
        .style(Style::default().fg(text_colour()).bg(popup_background()));
    frame.render_widget(popup, area);
}

fn draw_confirm_popup(frame: &mut Frame, app: &App) {
    let area = centered_rect(60, 6, frame.size());
    frame.render_widget(Clear, area);
    let content = vec![
        Line::from(Span::styled(
            app.input_title.clone(),
            Style::default().fg(text_colour()).bold(),
        )),
        Line::from(""),
        Line::from(Span::styled(
            "y confirms • n / Esc cancels",
            Style::default().fg(muted()),
        )),
    ];
    let popup = Paragraph::new(content)
        .alignment(Alignment::Center)
        .block(
            Block::bordered()
                .title(Span::styled(" CONFIRM ", Style::default().fg(warn()).bold()))
                .border_style(Style::default().fg(warn())),
        )
        .style(Style::default().fg(text_colour()).bg(popup_background()));
    frame.render_widget(popup, area);
}

fn draw_details_popup(frame: &mut Frame, app: &App) {
    let (title, text, scroll, lines) = match &app.details {
        Some(details) => (
            details.title.clone(),
            details.lines.join("\n"),
            details.scroll as u16,
            details.lines.len() as u16,
        ),
        None => (String::new(), String::new(), 0, 0),
    };
    let confirming = app.input_mode == InputMode::ConfirmWrites;
    let gating = app.input_mode == InputMode::AcceptKya;
    // A diff of five keys in a box of thirty rows reads as if something is missing,
    // so the overlay is sized to what it holds. The KyA gate is the exception and
    // takes everything: it is not covering a page, it *is* the screen (issue #395).
    let height = if gating {
        // Everything but the footer, which carries the two keys that answer it: an
        // overlay that covered its own instructions would be the one thing on screen
        // and still not say what to press.
        frame.size().height.saturating_sub(4).max(8)
    } else {
        (lines + 2).clamp(8, frame.size().height.saturating_sub(4).max(8))
    };
    let area = centered_rect(if gating { 94 } else { 80 }, height, frame.size());
    frame.render_widget(Clear, area);
    let keys = if gating {
        "y accept · n decline · ↑↓ scroll"
    } else if confirming {
        "y apply • n cancel • ↑/↓ scroll"
    } else {
        "↑/↓ scroll • Esc close"
    };
    let colour = if confirming || gating { warn() } else { accent() };
    let popup = Paragraph::new(text)
        .scroll((scroll, 0))
        .wrap(Wrap { trim: false })
        .block(
            Block::bordered()
                .title(Span::styled(
                    format!(" {title} • {keys} "),
                    Style::default().fg(colour).bold(),
                ))
                .border_style(Style::default().fg(colour)),
        )
        .style(Style::default().fg(text_colour()).bg(popup_background()));
    frame.render_widget(popup, area);
}

fn centered_rect(percent_x: u16, height: u16, area: Rect) -> Rect {
    let vertical = Layout::vertical([
        Constraint::Fill(1),
        Constraint::Length(height.min(area.height)),
        Constraint::Fill(1),
    ])
    .split(area);
    Layout::horizontal([
        Constraint::Percentage((100 - percent_x) / 2),
        Constraint::Percentage(percent_x),
        Constraint::Percentage((100 - percent_x) / 2),
    ])
    .split(vertical[1])[1]
}

fn header_row(labels: Vec<&str>) -> Row<'static> {
    Row::new(
        labels
            .into_iter()
            .map(|label| Cell::from(label.to_string()))
            .collect::<Vec<_>>(),
    )
    .style(Style::default().fg(accent()).bold())
    .bottom_margin(1)
}

fn section_block(title: impl Into<String>, color: Color) -> Block<'static> {
    Block::bordered()
        .title(Span::styled(
            title.into(),
            Style::default().fg(color).bold(),
        ))
        .border_style(Style::default().fg(color))
}

fn selected_style() -> Style {
    Style::default().fg(inverse_text()).bg(accent()).bold()
}

fn nonempty<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.trim().is_empty() {
        fallback
    } else {
        value
    }
}

fn visible_tail(lines: &[String], count: usize) -> String {
    let start = lines.len().saturating_sub(count);
    lines[start..].join("\n")
}

#[cfg(test)]
mod tests {

    /// The power line has to say which of four different measurements it is showing.
    /// A package counter, a rail, the supply's input and a plug at the wall are not
    /// interchangeable, and naming one of them for another misreads by whatever the
    /// missing parts of the machine draw.
    mod power_line {
        use super::super::node_power_line;
        use crate::app::NodeEnergy;

        fn line(watts: Option<f64>, backend: &str, is_floor: bool) -> String {
            node_power_line(&NodeEnergy {
                watts,
                backend: backend.to_string(),
                is_floor,
                ..NodeEnergy::default()
            })
        }

        #[test]
        fn a_partial_source_is_named_and_marked_a_floor() {
            assert_eq!(line(Some(12.0), "rapl", true), "12 W · rapl floor");
            assert_eq!(line(Some(9.0), "hwmon", true), "9.00 W · hwmon floor");
        }

        #[test]
        fn a_combined_source_keeps_the_names_of_its_parts() {
            assert_eq!(line(Some(150.0), "rapl+nvml", true), "150 W · rapl+nvml floor");
        }

        #[test]
        fn a_whole_machine_source_is_not_called_a_floor() {
            assert_eq!(line(Some(200.0), "ipmi", false), "200 W · ipmi");
            assert_eq!(line(Some(180.0), "smart_plug", false), "180 W · smart_plug");
        }

        #[test]
        fn the_estimate_says_it_is_one() {
            assert_eq!(line(Some(90.0), "model", false), "90 W · model estimate");
        }

        #[test]
        fn no_sample_is_a_dash_and_never_a_zero() {
            assert_eq!(line(None, "rapl", true), "—");
            assert_eq!(line(f64::NAN.into(), "rapl", true), "—");
        }
    }

    /// The working day has to be legible as a day. Drawn rather than described,
    /// because the layout is the feature: a bar spanning half the pane or an axis
    /// stopping at 21 is not a mistake reading the code catches.
    mod schedule_page {
        use super::super::draw_schedule;
        use crate::app::{App, Page};
        use ratatui::{backend::TestBackend, Terminal};

        fn screen(width: u16, height: u16, config: &str, now: u16) -> String {
            let mut app = App::new();
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Schedule)
                .unwrap();
            app.config_document = Some(serde_yaml::from_str(config).unwrap());
            app.now_minute = now;
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_schedule(frame, &mut app, frame.size()))
                .unwrap();
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

        const NIGHT: &str = "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '22:00'\n      END: '06:00'\n  ON_CLOSE: refuse\n";

        /// A month of demand: busy through the evening, refused work at 20:00 and
        /// 21:00 -- the hours this node is closed through.
        fn evening_demand() -> crate::app::DemandByHour {
            let mut demand = crate::app::DemandByHour::default();
            for (hour, held) in [(9, 1), (12, 2), (17, 4), (18, 6), (19, 7), (20, 8), (21, 5), (23, 2)] {
                demand.held[hour] = held;
            }
            demand.refused[20] = 11;
            demand.refused[21] = 4;
            demand
        }

        fn screen_with_demand(config: &str, now: u16, demand: crate::app::DemandByHour) -> String {
            let mut app = App::new();
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Schedule)
                .unwrap();
            app.config_document = Some(serde_yaml::from_str(config).unwrap());
            app.now_minute = now;
            app.demand = demand;
            let backend = TestBackend::new(100, 26);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_schedule(frame, &mut app, frame.size()))
                .unwrap();
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

        /// The row of demand blocks, and the row of refusals under it.
        fn demand_rows(screen: &str) -> Vec<&str> {
            screen
                .lines()
                .filter(|line| {
                    line.chars()
                        .any(|glyph| ('▁'..='█').contains(&glyph) && glyph != '█')
                        || (line.contains('█') && !line.contains('░'))
                })
                .collect()
        }

        #[test]
        fn a_node_with_no_history_says_so_instead_of_drawing_a_flat_line() {
            // A flat bar would read as "nobody ever asked for anything", which is a
            // different claim from "this has not been recorded yet".
            let screen = screen_with_demand(NIGHT, 12 * 60, crate::app::DemandByHour::default());
            assert!(screen.contains("No demand recorded yet"), "{screen}");
        }

        #[test]
        fn demand_is_drawn_on_the_same_axis_as_the_window() {
            // The entire point: an operator has to be able to see that they are closed
            // through their own busiest hours. Two charts on two axes would leave that
            // to be lined up by eye.
            let screen = screen_with_demand(NIGHT, 12 * 60, evening_demand());
            let rows = demand_rows(&screen);
            assert!(!rows.is_empty(), "no demand row on screen:\n{screen}");

            let per_hour = 4;
            let held = cells(rows[0]);
            let column = |hour: usize| held[hour * per_hour];
            assert_eq!(column(3), ' ', "03:00 had no demand: {held:?}");
            assert_ne!(column(20), ' ', "20:00 was the busiest hour: {held:?}");
            assert_eq!(
                held.len(),
                cells(bar(&screen)).len(),
                "the demand row and the window are not the same width"
            );
        }

        #[test]
        fn the_busiest_hour_is_full_height_and_a_quiet_one_is_still_visible() {
            let screen = screen_with_demand(NIGHT, 12 * 60, evening_demand());
            let held = cells(demand_rows(&screen)[0]);
            let per_hour = 4;
            assert_eq!(held[20 * per_hour], '█', "peak hour");
            // One instance out of a peak of eight must not round away to nothing.
            assert_eq!(held[9 * per_hour], '▁', "quiet hour");
        }

        #[test]
        fn work_refused_for_being_closed_gets_its_own_row_and_a_total() {
            let screen = screen_with_demand(NIGHT, 12 * 60, evening_demand());
            assert!(
                screen.contains("15 refused for being closed"),
                "the cost of the operator's own hours is not stated:\n{screen}"
            );
            assert!(screen.contains("peak 8 held"), "{screen}");
            assert_eq!(
                demand_rows(&screen).len(),
                2,
                "refusals should be a row of their own:\n{screen}"
            );
        }

        #[test]
        fn nothing_refused_is_said_plainly_rather_than_left_blank() {
            let mut demand = evening_demand();
            demand.refused = [0; 24];
            let screen = screen_with_demand(NIGHT, 12 * 60, demand);
            assert!(screen.contains("nothing refused for being closed"), "{screen}");
            assert_eq!(demand_rows(&screen).len(), 1, "an empty row was drawn:\n{screen}");
        }
        const DAY: &str = "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '09:00'\n      END: '18:00'\n  ON_CLOSE: stop\n";

        /// The bar of open/closed blocks, whatever row it landed on.
        fn bar(screen: &str) -> &str {
            screen
                .lines()
                .find(|line| line.contains('█') || line.contains('░'))
                .unwrap_or_else(|| panic!("no day bar on screen:\n{screen}"))
        }

        /// A drawn row without the pane border, so a column index is an hour rather
        /// than an hour plus one. Indexing straight into the screen line reads the
        /// cell next door, which only shows where neighbouring cells differ.
        fn cells(line: &str) -> Vec<char> {
            line.chars().filter(|glyph| *glyph != '│').collect()
        }

        #[test]
        fn the_axis_spans_the_whole_day() {
            let screen = screen(100, 24, NIGHT, 12 * 60);
            // 24 closes the day: an axis stopping at 21 reads as if the last three
            // hours were outside the chart.
            for hour in ["00", "03", "06", "09", "12", "15", "18", "21", "24"] {
                assert!(screen.contains(hour), "{hour} missing from the axis:\n{screen}");
            }
        }

        #[test]
        fn the_bar_spans_the_pane_rather_than_stopping_halfway() {
            let screen = screen(100, 24, DAY, 12 * 60);
            let blocks = bar(&screen)
                .chars()
                .filter(|glyph| *glyph == '█' || *glyph == '░' || *glyph == '▒')
                .count();
            // A whole number of cells per hour, as wide as the pane allows: 96 of the
            // 98 usable columns at this width, not 48 of them.
            assert!(blocks >= 96, "the bar is only {blocks} cells wide:\n{screen}");
            assert_eq!(blocks % 24, 0, "hours are not all the same width: {blocks}");
        }

        #[test]
        fn a_day_shift_is_open_in_the_middle_and_closed_at_both_ends() {
            let bar = cells(bar(&screen(100, 24, DAY, 12 * 60)));
            let open_at = |hour: usize| bar[hour * 4];
            assert_eq!(open_at(3), '░', "03:00 should be closed");
            assert_eq!(open_at(12), '█', "12:00 should be open");
            assert_eq!(open_at(20), '░', "20:00 should be closed");
        }

        #[test]
        fn a_night_shift_is_open_at_both_ends_and_says_it_is_one_window() {
            let screen = screen(100, 24, NIGHT, 23 * 60);
            let bar = cells(bar(&screen));
            let per_hour = 4;
            assert_eq!(bar[2 * per_hour], '█', "02:00 open");
            assert_eq!(bar[12 * per_hour], '░', "12:00 closed");
            // Drawn on a straight 00→24 axis the night shift is two runs of blocks, so
            // the one thing the picture cannot say has to be written down.
            assert!(
                screen.contains("one window through midnight"),
                "nothing says the two ends are one window:\n{screen}"
            );
        }

        #[test]
        fn the_marker_says_whether_it_is_open_right_now() {
            let open = screen(100, 24, NIGHT, 23 * 60 + 40);
            assert!(open.contains("now 23:40"), "{open}");
            assert!(open.contains("OPEN"), "{open}");
            assert!(open.contains("closes in"), "{open}");

            let closed = screen(100, 24, NIGHT, 12 * 60);
            assert!(closed.contains("CLOSED"), "{closed}");
            assert!(closed.contains("opens in"), "{closed}");
        }

        #[test]
        fn a_window_that_is_off_says_so_next_to_the_hours_it_is_not_enforcing() {
            let screen = screen(
                100,
                24,
                "activity_window:\n  ENABLED: false\n  WINDOWS:\n    - START: '09:00'\n      END: '18:00'\n",
                11 * 60,
            );
            assert!(screen.contains("the schedule is off"), "{screen}");
            // The duration line still names the hours a switched-off window would
            // enforce.
            assert!(screen.contains("9 h a day"), "{screen}");
            assert!(!screen.contains("24 h a day"), "{screen}");
        }

        #[test]
        fn stopping_at_closing_time_is_spelled_out() {
            let screen = screen(100, 24, DAY, 12 * 60);
            assert!(screen.contains("destroyed mid-flight"), "{screen}");
        }

        #[test]
        fn a_narrow_terminal_still_draws_a_day() {
            // One cell per hour is the floor; below that the pane is left empty rather
            // than drawing an axis whose hours are different widths.
            let screen = screen(40, 24, DAY, 12 * 60);
            assert!(screen.contains("00"), "{screen}");
            let blocks = bar(&screen)
                .chars()
                .filter(|glyph| *glyph == '█' || *glyph == '░' || *glyph == '▒')
                .count();
            assert_eq!(blocks, 24, "expected one cell per hour:\n{screen}");
        }
    }

    /// The cell is a panel an operator reads to decide something, so what matters is
    /// that the decision and where it is currently set are both legibly on screen --
    /// at both layouts, and without a secret leaking into either.
    mod cell_page {
        use super::super::{draw_cell, lever_line};
        use crate::app::App;
        use crate::cell::{self, LeverKind};
        use ratatui::{backend::TestBackend, Terminal};

        fn screen(width: u16, height: u16, config: &str) -> String {
            let mut app = App::new();
            // The page derives every lever from the cached document, the same one
            // the refresh sweep reloads, so the fixture goes there.
            app.config_document = Some(serde_yaml::from_str(config).unwrap());
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_cell(frame, &mut app, frame.size()))
                .unwrap();
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

        const RENTING: &str = "client:\n  ACCEPT_NEW_DEPOSITS: true\nnetwork:\n  GATEWAY_PORT: 58443\n  DISABLE_EXPOSE_OUTSIDE: true\ncosts:\n  ALLOW_DEBT: false\nidentity:\n  MNEMONIC: \"abandon abandon ability\"\n";

        /// Wide enough for the grid: every organelle is drawn, so the operator can
        /// see the whole cell without hunting through collapsed sections.
        #[test]
        fn the_grid_shows_every_organelle() {
            let screen = screen(140, 40, RENTING);
            for organelle in cell::Organelle::ALL {
                assert!(
                    screen.contains(organelle.title()),
                    "{} is missing from the grid:\n{screen}",
                    organelle.title()
                );
            }
            assert!(screen.contains("MEMBRANE"), "the membrane frames the page");
        }

        /// An organelle can hold more levers than its share of the band has rows --
        /// WALL is the first that does. A lever drawn nowhere cannot be operated, so
        /// the box scrolls to the selection rather than clipping the tail.
        #[test]
        fn a_lever_past_the_bottom_of_its_box_is_still_drawn() {
            let wall = cell::Organelle::ALL
                .iter()
                .position(|organelle| *organelle == cell::Organelle::Wall)
                .unwrap();
            let last = cell::Organelle::Wall.levers().len() - 1;
            let label = cell::Organelle::Wall.levers()[last].label;

            let mut app = App::new();
            app.config_document = Some(serde_yaml::from_str(RENTING).unwrap());
            let backend = TestBackend::new(120, 30);
            let mut terminal = Terminal::new(backend).unwrap();

            let render = |app: &mut App, terminal: &mut Terminal<TestBackend>| {
                terminal
                    .draw(|frame| draw_cell(frame, app, frame.size()))
                    .unwrap();
                let buffer = terminal.backend().buffer().clone();
                (0..buffer.area.height)
                    .map(|row| {
                        (0..buffer.area.width)
                            .map(|column| buffer.get(column, row).symbol())
                            .collect::<String>()
                    })
                    .collect::<Vec<_>>()
                    .join("\n")
            };

            // Guards the test: the last lever really is off the bottom to begin with,
            // or scrolling to it would prove nothing.
            let unscrolled = render(&mut app, &mut terminal);
            assert!(
                !unscrolled.contains(label),
                "{label} fits without scrolling, so this test proves nothing:\n{unscrolled}"
            );

            app.cell.organelle = wall;
            app.cell.lever = last;
            let scrolled = render(&mut app, &mut terminal);
            assert!(
                scrolled.contains(label),
                "{label} is unreachable in the grid:\n{scrolled}"
            );
        }

        /// A lever is only useful if its current position is visible: "outside work"
        /// with no "open" beside it is a question with no answer.
        #[test]
        fn a_levers_current_position_is_on_screen_beside_it() {
            let screen = screen(140, 40, RENTING);
            assert!(screen.contains("outside work"));
            assert!(screen.contains("open"), "the position is shown:\n{screen}");
            assert!(screen.contains("58443"), "a scalar shows its value:\n{screen}");
        }

        /// The one thing this page must never do. The mnemonic is in the file it
        /// reads, and the nucleus row that reports it says only whether it is set.
        #[test]
        fn a_secret_never_reaches_the_screen() {
            for (width, height) in [(140, 40), (80, 24)] {
                let screen = screen(width, height, RENTING);
                assert!(
                    !screen.contains("abandon"),
                    "the identity mnemonic leaked at {width}x{height}:\n{screen}"
                );
            }
        }

        /// A narrow terminal cannot show six boxes of rows, so it collapses to one
        /// column -- and still names every organelle, so nothing becomes unreachable.
        #[test]
        fn a_narrow_terminal_collapses_to_one_column_without_losing_an_organelle() {
            let screen = screen(80, 24, RENTING);
            for organelle in cell::Organelle::ALL {
                assert!(
                    screen.contains(organelle.title()),
                    "{} is unreachable at 80x24:\n{screen}",
                    organelle.title()
                );
            }
        }

        /// A combination the catalogue cannot name is called out rather than shown
        /// as one of the named positions. Showing the nearest one would have the
        /// page misreport the policy the node is running.
        #[test]
        fn an_unnamed_combination_is_marked_rather_than_rounded() {
            let lever = cell::lever("descendants").unwrap();
            let document: serde_yaml::Value = serde_yaml::from_str(
                "workload_admission:\n  POLICY: full\n  ON_UNSATISFIABLE: reject\n",
            )
            .unwrap();
            let line = lever_line(lever, Some(&document), false, 30);
            let text: String = line
                .spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect();
            assert!(text.contains("custom"), "unnamed state reads as: {text}");
            assert!(!text.contains("strict") && !text.contains("lenient"));
        }

        /// Every lever the page can draw has a label short enough to survive the
        /// narrowest box the grid produces, or its value is pushed off the row.
        #[test]
        fn every_lever_label_fits_the_narrowest_box() {
            for lever in cell::levers() {
                assert!(
                    lever.label.chars().count() <= 18,
                    "{} has a label too long for a box",
                    lever.id
                );
            }
        }

        /// Every lever says what it decides and what happens if you change it. The
        /// questions and consequences are the page's whole reason to exist over the
        /// raw keys, so an empty one is a bug rather than a style slip.
        #[test]
        fn every_lever_explains_itself() {
            for lever in cell::levers() {
                assert!(!lever.question.is_empty(), "{} asks nothing", lever.id);
                assert!(
                    lever.consequence.len() > 30,
                    "{} does not say what changing it does",
                    lever.id
                );
                if let LeverKind::Link(_) = lever.kind {
                    continue;
                }
                assert!(!lever.paths().is_empty(), "{} writes nothing", lever.id);
            }
        }
    }

    /// The bars are an editor, so what matters is that every price is actually on
    /// screen -- including a free one, and one three orders of magnitude below its
    /// neighbour, which on a linear scale would round to no bar at all.
    mod pricing {
        use super::super::{accent, draw_price_bars, log_bar_value, PriceChart};
        use crate::app::{Money, PriceEntry};
        use ratatui::{backend::TestBackend, Terminal};

        fn entry(key: &'static str, short: &'static str, mu: u64, recurring: bool) -> PriceEntry {
            PriceEntry {
                id: key.to_string(),
                key,
                short: short.to_string(),
                per: "per unit",
                recurring,
                arch: None,
                inherited: false,
                mu,
            }
        }

        /// A per-architecture override of `key`, as `get_prices` builds one.
        fn arch_entry(
            key: &'static str,
            short: &str,
            arch: &'static str,
            mu: u64,
            inherited: bool,
        ) -> PriceEntry {
            PriceEntry {
                id: format!("{arch}/{key}"),
                key,
                short: short.to_string(),
                per: "per unit",
                recurring: true,
                arch: Some(arch),
                inherited,
                mu,
            }
        }

        fn render(prices: &[PriceEntry], selected: Option<&str>) -> String {
            let mut terminal = Terminal::new(TestBackend::new(60, 14)).unwrap();
            terminal
                .draw(|frame| {
                    draw_price_bars(
                        frame,
                        frame.size(),
                        PriceChart {
                            title: " RECURRING ",
                            recurring: true,
                            color: accent(),
                        },
                        prices,
                        selected,
                        &Money::default(),
                    );
                })
                .unwrap();
            terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect()
        }

        #[test]
        fn every_price_gets_a_labelled_bar() {
            let prices = vec![
                entry("RAM_MU_PER_GIB_HOUR", "RAM", 1_000_000, true),
                entry("CPU_MU_PER_VCPU_HOUR", "CPU", 4_000_000, true),
                entry("DISK_MU_PER_GIB_HOUR", "DISK", 100_000, true),
            ];
            let text = render(&prices, Some("CPU"));
            for label in ["RAM", "CPU", "DISK"] {
                assert!(text.contains(label), "missing {label} in:\n{text}");
            }
        }

        #[test]
        fn a_free_resource_is_labelled_rather_than_missing() {
            // A bar of height zero is indistinguishable from an absent feature, so a
            // price of zero says so in words.
            let prices = vec![entry("NET_MU_PER_GIB", "NET", 0, true)];
            let text = render(&prices, None);
            assert!(text.contains("NET"));
            assert!(text.contains("free"), "expected 'free' in:\n{text}");
        }

        #[test]
        fn a_price_dwarfed_by_its_neighbour_still_shows_its_amount() {
            // BarChart still prints the amount for any non-zero value regardless of
            // height, so the price does not vanish even in the degenerate case --
            // which is precisely why a price of exactly zero needs the `free` label
            // instead, rather than relying on this.
            let prices = vec![
                entry("BUILD_MU", "BUILD", 10_000_000, true),
                entry("TUNNEL_OPEN_MU", "TUNNEL", 10_000, true),
            ];
            let text = render(&prices, None);
            assert!(text.contains("TUNNEL"), "missing label in:\n{text}");
            assert!(text.contains("0.00001"), "missing amount in:\n{text}");
        }

        #[test]
        fn a_price_dwarfed_by_its_neighbour_still_gets_a_meaningful_bar() {
            // On a linear scale TUNNEL is 1/1000th of BUILD's height -- rounded away
            // to nothing on any terminal. The whole point of the log scale is that it
            // no longer is: it should read as comparable in magnitude, not vanishing.
            let build = log_bar_value(10_000_000);
            let tunnel = log_bar_value(10_000);
            assert!(tunnel > 0, "a non-zero price must not log-scale to a zero bar");
            let ratio = tunnel as f64 / build as f64;
            assert!(
                ratio > 0.5,
                "TUNNEL should render at more than half of BUILD's height, was {ratio}"
            );
        }

        #[test]
        fn log_bar_value_is_monotonic_and_maps_zero_to_zero() {
            assert_eq!(log_bar_value(0), 0, "a free price must still draw no bar");
            assert!(log_bar_value(1) < log_bar_value(1_000));
            assert!(log_bar_value(1_000) < log_bar_value(1_000_000));
        }

        #[test]
        fn the_table_carries_every_exact_figure() {
            // The chart is for proportion; this is the part that must never lose a
            // number, however small, and it shows both the raw MU and the display unit.
            let mut app = crate::app::App::default();
            app.money = Money::default();
            app.prices = crate::app::StatefulList::with_items(vec![
                entry("BUILD_MU", "BUILD", 10_000_000, false),
                entry("TUNNEL_OPEN_MU", "TUNNEL", 10_000, false),
            ]);

            let mut terminal = Terminal::new(TestBackend::new(70, 8)).unwrap();
            terminal
                .draw(|frame| super::super::draw_price_table(frame, &mut app, frame.size()))
                .unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect();

            assert!(text.contains("10000000"), "raw MU missing in:\n{text}");
            assert!(text.contains("0.01 ERG"), "display unit missing in:\n{text}");
            assert!(text.contains("10000"), "the small price is missing in:\n{text}");
            assert!(text.contains("0.00001 ERG"), "its amount is missing in:\n{text}");
        }

        #[test]
        fn the_other_group_is_not_drawn() {
            let prices = vec![
                entry("RAM_MU_PER_GIB_HOUR", "RAM", 1_000_000, true),
                entry("BUILD_MU", "BUILD", 10_000_000, false),
            ];
            let text = render(&prices, None);
            assert!(text.contains("RAM"));
            assert!(!text.contains("BUILD"));
        }

        #[test]
        fn per_arch_overrides_do_not_get_their_own_bar() {
            // A per-arch memory price is a variation on the memory price, not a fourth
            // resource. Charting both would draw "RAM" three times and read as three
            // times the memory revenue. The table is what carries the per-arch figures.
            let prices = vec![
                entry("RAM_MU_PER_GIB_HOUR", "RAM", 1_000_000, true),
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·amd64", "linux/amd64", 1_000_000, true),
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·arm64", "linux/arm64", 1_400_000, false),
            ];
            let text = render(&prices, None);
            assert!(text.contains("RAM"), "the node-wide bar is missing in:\n{text}");
            assert!(
                !text.contains("amd64") && !text.contains("arm64"),
                "a per-arch override was charted in:\n{text}"
            );
        }

        #[test]
        fn a_price_an_arch_only_inherits_says_so() {
            // "arm64 costs this" and "arm64 has been given its own rate" are different
            // facts, and the second is the one an edit creates. An operator has to be
            // able to tell which they are looking at before they nudge it -- otherwise
            // the page shows a per-arch pricing policy the node does not have.
            let mut app = crate::app::App::default();
            app.money = Money::default();
            app.prices = crate::app::StatefulList::with_items(vec![
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·amd64", "linux/amd64", 1_000_000, true),
                arch_entry("RAM_MU_PER_GIB_HOUR", "RAM·arm64", "linux/arm64", 1_400_000, false),
            ]);

            let mut terminal = Terminal::new(TestBackend::new(80, 8)).unwrap();
            terminal
                .draw(|frame| super::super::draw_price_table(frame, &mut app, frame.size()))
                .unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect();

            assert!(text.contains("amd64"), "the arch row is missing in:\n{text}");
            assert!(
                text.contains("inherited"),
                "an inherited price is not marked in:\n{text}"
            );
            // The configured one must NOT be, or the marker means nothing.
            assert_eq!(
                text.matches("inherited").count(),
                1,
                "a configured per-arch price was marked inherited in:\n{text}"
            );
        }

        /// What the operator is actually deciding when they set a memory price.
        ///
        /// The node boots a guest larger than declared and absorbs the difference, so
        /// a memory price earns less per GiB of host RAM than it says, by a different
        /// amount per architecture. These pin that the page says so.
        mod overhead_guidance {
            use crate::app::{App, GuestKernelReserve, Money, PriceEntry, StatefulList};
            use ratatui::{backend::TestBackend, Terminal};

            const GIB: u64 = 1024 * 1024 * 1024;

            fn app_with(arch: &'static str, mu: u64, reserve: GuestKernelReserve) -> App {
                let mut app = App::default();
                app.money = Money::default();
                app.guest_kernel_reserves = vec![(arch, reserve)];
                app.prices = StatefulList::with_items(vec![PriceEntry {
                    id: format!("{arch}/RAM_MU_PER_GIB_HOUR"),
                    key: "RAM_MU_PER_GIB_HOUR",
                    short: "RAM".to_string(),
                    per: "per GiB-hour",
                    recurring: true,
                    arch: Some(arch),
                    inherited: false,
                    mu,
                }]);
                app.prices.state.select(Some(0));
                app.prices.state_id = Some(format!("{arch}/RAM_MU_PER_GIB_HOUR"));
                app
            }

            #[test]
            fn the_reserve_matches_the_nodes_own_model() {
                // The same arithmetic as `limits.guest_kernel_reserve_bytes`: a fixed
                // part plus a share of the guest. If these drift, the page advises the
                // operator about an overhead the node does not actually apply.
                let reserve = GuestKernelReserve {
                    fixed_mib: 40,
                    ratio: 0.05,
                };
                // Rounded UP, like the node's `math.ceil`: a reserve short by a page is
                // still a guest that can be OOM-killed below its declared ceiling, and
                // rounding is the one place that is free to get right.
                assert_eq!(
                    reserve.bytes_for(GIB),
                    40 * 1024 * 1024 + (GIB as f64 * 0.05).ceil() as u64,
                    "the fixed and proportional parts must both apply"
                );
                assert!(
                    reserve.bytes_for(GIB) > 40 * 1024 * 1024 + GIB / 20,
                    "the proportional part must round up, not truncate"
                );
                assert_eq!(reserve.bytes_for(0), 0, "nothing is reserved for nothing");
            }

            #[test]
            fn a_costlier_arch_earns_the_node_less_at_the_same_price() {
                // This is the whole argument for per-arch pricing in one assertion: the
                // same number in config.yaml is not the same amount of money, because
                // the RAM the node has to commit to honour it differs per arch.
                let amd64 = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                let arm64 = app_with(
                    "linux/arm64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 32,
                        ratio: 0.05,
                    },
                );
                let (amd_effective, amd_multiplier) = amd64
                    .effective_memory_mu(amd64.prices.selected().unwrap())
                    .unwrap();
                let (arm_effective, _) = arm64
                    .effective_memory_mu(arm64.prices.selected().unwrap())
                    .unwrap();

                assert!(
                    amd_effective < arm_effective,
                    "amd64 reserves more, so the same price must earn less: \
                     amd64={amd_effective} arm64={arm_effective}"
                );
                // And never more than the price itself -- the node cannot earn more
                // than it charges by committing extra RAM.
                assert!(amd_effective < 1_000_000.0);
                assert!(amd_multiplier > 1.0);
            }

            #[test]
            fn the_suggested_price_recovers_the_overhead_exactly() {
                // The guidance has to be actionable, not just descriptive: a price set
                // to the suggestion must earn the target per GiB of host RAM committed.
                let reserve = GuestKernelReserve {
                    fixed_mib: 40,
                    ratio: 0.05,
                };
                let app = app_with("linux/amd64", 1_000_000, reserve);
                let suggested = app.suggested_memory_mu("linux/amd64", 1_000_000).unwrap();
                assert!(
                    suggested > 1_000_000,
                    "covering an overhead cannot cost less than not covering it"
                );

                let mut priced = app_with("linux/amd64", suggested, reserve);
                priced.prices.state.select(Some(0));
                let (effective, _) = priced
                    .effective_memory_mu(priced.prices.selected().unwrap())
                    .unwrap();
                assert!(
                    (effective - 1_000_000.0).abs() <= 1.0,
                    "a price set to the suggestion should earn the target, got {effective}"
                );
            }

            #[test]
            fn an_unpriced_arch_is_not_guessed_at() {
                // No measurement, no advice. Inventing an overhead figure for an
                // architecture nodo has never characterised would have the operator
                // price against a number nobody measured.
                let app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                assert!(app.reserve_for("linux/riscv64").is_none());
                assert!(app.suggested_memory_mu("linux/riscv64", 1_000_000).is_none());
            }

            #[test]
            fn the_node_wide_price_is_not_given_a_per_arch_figure() {
                // The scalar price applies to every arch, so there is no single
                // overhead to quote against it. Picking one arch's would be a
                // guess presented as a fact.
                let mut app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                app.prices = StatefulList::with_items(vec![PriceEntry {
                    id: "RAM_MU_PER_GIB_HOUR".to_string(),
                    key: "RAM_MU_PER_GIB_HOUR",
                    short: "RAM".to_string(),
                    per: "per GiB-hour",
                    recurring: true,
                    arch: None,
                    inherited: false,
                    mu: 1_000_000,
                }]);
                assert!(app
                    .effective_memory_mu(app.prices.items.first().unwrap())
                    .is_none());
            }

            #[test]
            fn a_non_memory_price_carries_no_overhead() {
                // Only memory has a per-arch cost to recover: the node hands a guest
                // the vCPUs and the image it asked for whatever arch it is.
                let mut app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                app.prices = StatefulList::with_items(vec![PriceEntry {
                    id: "linux/amd64/CPU_MU_PER_VCPU_HOUR".to_string(),
                    key: "CPU_MU_PER_VCPU_HOUR",
                    short: "CPU".to_string(),
                    per: "per vCPU-hour",
                    recurring: true,
                    arch: Some("linux/amd64"),
                    inherited: false,
                    mu: 4_000_000,
                }]);
                assert!(app
                    .effective_memory_mu(app.prices.items.first().unwrap())
                    .is_none());
            }

            #[test]
            fn the_money_card_states_what_the_node_keeps() {
                // The operator has to be able to read the consequence off the screen,
                // not derive it. A page that shows only the price teaches nothing about
                // the overhead it has to cover.
                let mut app = app_with(
                    "linux/amd64",
                    1_000_000,
                    GuestKernelReserve {
                        fixed_mib: 40,
                        ratio: 0.05,
                    },
                );
                let mut terminal = Terminal::new(TestBackend::new(46, 16)).unwrap();
                terminal
                    .draw(|frame| super::super::super::draw_money_card(frame, &app, frame.size()))
                    .unwrap();
                let text: String = terminal
                    .backend()
                    .buffer()
                    .content()
                    .iter()
                    .map(|cell| cell.symbol())
                    .collect();

                assert!(
                    text.contains("Guest kernel"),
                    "the overhead is not named in:\n{text}"
                );
                assert!(
                    text.contains("40"),
                    "the arch's own reserve is not shown in:\n{text}"
                );
                assert!(
                    text.contains("Node earns"),
                    "what the price actually earns is not shown in:\n{text}"
                );
                let _ = &mut app;
            }
        }
    }

    /// The instances page has to answer "is this instance using what it was given?".
    /// These tests pin the two halves of that: a live figure appears next to its
    /// allocation, and an instance we cannot see into says so instead of reading idle.
    mod instances {
        use super::super::{cpu_detail, cpu_load_color, good, muted, net_detail, warn};
        use crate::app::{Instance, InstanceUsage};

        fn instance(vcpus: Option<f64>, usage: InstanceUsage) -> Instance {
            Instance {
                id: "8f4e2c".to_string(),
                name: "worker".to_string(),
                ip: "10.0.0.7:4040".to_string(),
                service: "builder".to_string(),
                balance: "1000".to_string(),
                virtualizer: "ch".to_string(),
                memory_limit: 1 << 30,
                disk_limit: 10 << 30,
                vcpus,
                usage,
                location: "local".to_string(),
                father_id: String::new(),
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
                energy_watts: None,
                energy_share: None,
            }
        }

        /// A raw percentage is ambiguous on its own: 180% is nearly idle on 4 vCPUs and
        /// impossible on 1. The card carries the allowance so the figure can be judged.
        #[test]
        fn the_cpu_line_states_the_allowance_the_percentage_is_measured_against() {
            let usage = InstanceUsage {
                cpu_percent: Some(182.4),
                ..InstanceUsage::default()
            };
            let detail = cpu_detail(&instance(Some(2.0), usage.clone()));
            assert!(detail.contains("182%"), "{detail}");
            assert!(detail.contains("200%"), "{detail}");
            assert!(detail.contains("2.00 vCPU"), "{detail}");

            // No quota recorded: say so rather than inventing a denominator.
            let unbounded = cpu_detail(&instance(None, usage));
            assert!(unbounded.contains("182%"), "{unbounded}");
            assert!(unbounded.contains("no vCPU quota"), "{unbounded}");
        }

        #[test]
        fn an_unreadable_cpu_reads_as_unknown_in_both_the_text_and_the_colour() {
            let blind = instance(Some(2.0), InstanceUsage::default());
            assert!(cpu_detail(&blind).contains('—'), "{}", cpu_detail(&blind));
            assert_eq!(cpu_load_color(&blind), muted());
        }

        /// The colour is the at-a-glance signal for oversubscription, so it has to turn
        /// on the allowance rather than on a flat percentage.
        #[test]
        fn the_cpu_colour_warns_only_near_the_instances_own_allowance() {
            let at = |percent: f64, vcpus: f64| {
                cpu_load_color(&instance(
                    Some(vcpus),
                    InstanceUsage {
                        cpu_percent: Some(percent),
                        ..InstanceUsage::default()
                    },
                ))
            };
            // 95% of one core is nearly saturated; the same figure on four cores is not.
            assert_eq!(at(95.0, 1.0), warn());
            assert_eq!(at(95.0, 4.0), good());
            assert_eq!(at(390.0, 4.0), warn());
        }

        #[test]
        fn the_net_line_shows_rates_and_the_totals_they_accumulate() {
            let detail = net_detail(&instance(
                Some(1.0),
                InstanceUsage {
                    net_rx_bytes: Some(3 << 30),
                    net_tx_bytes: Some(512 << 20),
                    net_rx_rate: Some(1024.0 * 1024.0),
                    net_tx_rate: Some(2048.0),
                    ..InstanceUsage::default()
                },
            ));
            assert!(detail.contains("1.0 MiB/s"), "{detail}");
            assert!(detail.contains("2.0 KiB/s"), "{detail}");
            assert!(detail.contains("3.0 GiB"), "{detail}");
            assert!(detail.contains("512.0 MiB"), "{detail}");

            let blind = net_detail(&instance(Some(1.0), InstanceUsage::default()));
            assert!(!blind.contains('0'), "a missing tap is not silence: {blind}");
        }
    }

    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    /// The whole point of the page is the live column, so it has to survive the draw at
    /// the sizes an operator actually uses — including 80 columns, where the row is far
    /// wider than the terminal and ratatui has to truncate it.
    #[test]
    fn the_instances_table_shows_live_usage_beside_the_allocation() {
        let usage = crate::app::InstanceUsage {
            memory_current: Some(412 << 20),
            cpu_percent: Some(143.0),
            net_rx_rate: Some(1024.0 * 1024.0),
            net_tx_rate: Some(2048.0),
            ..crate::app::InstanceUsage::default()
        };
        let mut app = App::new();
        app.instances_grouped = false;
        app.instances.refresh(vec![Instance {
            id: "8f4e2c".to_string(),
            name: "worker".to_string(),
            ip: "10.0.0.7:4040".to_string(),
            service: "builder".to_string(),
            balance: "1000".to_string(),
            virtualizer: "ch".to_string(),
            memory_limit: 1 << 30,
            disk_limit: 10 << 30,
            vcpus: Some(2.0),
            usage,
            location: "local".to_string(),
            father_id: String::new(),
            // 1e6 MU/s → 6e7 MU/min, 3.6e9 MU/h; in the default ERG unit (1e9 MU) the
            // Burn/h column reads "3.6 ERG", exercising the rate through format_mu.
            mu_per_minute: Some(60_000_000.0),
            mu_per_hour: Some(3_600_000_000.0),
            consumption_samples: Some(12),
            consumption_age_secs: Some(45.0),
            energy_watts: Some(12.0),
            energy_share: Some(0.3),
        }]);
        app.instances.state.select(Some(0));
        app.instances.state_id = Some("8f4e2c".to_string());

        let mut terminal = Terminal::new(TestBackend::new(160, 40)).unwrap();
        terminal
            .draw(|frame| draw_instances(frame, &mut app, frame.size()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        let text: String = buffer.content().iter().map(|cell| cell.symbol()).collect();

        assert!(text.contains("CPU%"), "missing the CPU column header");
        assert!(text.contains("143%"), "missing the live CPU reading");
        // Used and allocated in the same cell, so no division is left to the operator.
        assert!(text.contains("412M / 1.0G"), "missing RAM used/allocated");
        assert!(text.contains("1.0M / 2.0K"), "missing the net rates");
        // And the detail card explains what the 143% is a fraction of.
        assert!(text.contains("2.00 vCPU"), "missing the vCPU allowance");
        // The burn rate is a column of its own and a detail line: 3.6e9 MU/h renders
        // as "3.6 ERG" in the default unit, and the card states what it is built from.
        assert!(text.contains("Burn/h"), "missing the burn-rate column header");
        assert!(text.contains("3.6 ERG"), "missing the per-hour burn rate");
        assert!(text.contains("12 samples"), "missing the burn-rate sample count");
        assert!(text.contains("12 W"), "missing attributed watts");
        assert!(text.contains("30% of node"), "missing energy share");
    }

    /// The EARNINGS page has to answer "is this machine worth leaving on" without
    /// either half of the answer being mistakable for the other: money is money and a
    /// share of somebody's proof is not, and neither is a number the page may invent
    /// when it has not been read.
    mod earnings_page {
        use super::super::draw_earnings;
        use crate::app::{
            App, LedgerEarnings, NodeOpinion, NodeReputation, Page, ReputationTotals,
        };
        use ratatui::{backend::TestBackend, Terminal};

        const NOW: i64 = 1_800_000_000;
        const DAY: i64 = 86_400;

        /// One opinion, shaped as mainnet's really are: a proof that has assigned a
        /// couple of dozen tokens out of ~1e8, staking one of them here.
        fn opinion(
            proof: &str,
            amount: u128,
            assigned_amount: u128,
            positive: bool,
            age_days: i64,
            burned_nanoerg: f64,
        ) -> NodeOpinion {
            let weight = amount as f64 / assigned_amount as f64;
            NodeOpinion {
                box_id: format!("box-{proof}"),
                proof_id: proof.to_string(),
                owner: "0008cd0392aabbcc".to_string(),
                amount,
                assigned_amount,
                weight,
                positive,
                published_at: Some(NOW - age_days * DAY),
                burned_nanoerg,
                backed_nanoerg: weight * burned_nanoerg,
            }
        }

        fn totals(positive: f64, negative: f64) -> ReputationTotals {
            ReputationTotals {
                positive,
                negative,
                positive_proofs: u32::from(positive > 0.0),
                negative_proofs: u32::from(negative > 0.0),
                // 10 ERG behind the supporter, the min-box floor behind the detractor.
                positive_backing: positive * 10e9,
                negative_backing: negative * 1e6,
            }
        }

        /// A node that has been paid over Ergo and vouched for by one proof, opposed
        /// by another, with its own self-opinion set aside.
        fn earning_node() -> App {
            let mut app = App::new();
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Earnings)
                .unwrap();
            app.earnings = vec![LedgerEarnings {
                ledger: "ergo".to_string(),
                day: 0,
                week: 2_000_000_000,
                month: 7_500_000_000,
                year: 12_000_000_000,
                total: 12_000_000_000,
                refused: 500_000_000,
            }];
            let reputation = NodeReputation {
                node_id: "ed6df5dfbea1f0932dc7fdd25d0f0543f6086ef110fc888f1acd5c89af4c84b8"
                    .to_string(),
                own_proof_ids: vec!["aa".repeat(32)],
                standing: totals(0.5, 0.125),
                opinions: vec![
                    // A proof with 10 ERG sunk into it, and one sitting at the
                    // min-box floor: the same page has to tell them apart.
                    opinion("f3b61c2e", 1, 2, true, 3, 10e9),
                    opinion("9d0a4471", 1, 8, false, 20, 1e6),
                ],
                own: vec![opinion(&"aa".repeat(32), 1, 1, true, 40, 1e6)],
                read_at: Some(NOW),
                error: String::new(),
            };
            app.opinions.refresh(reputation.opinions.clone());
            app.reputation = reputation;
            app
        }

        fn screen(app: &mut App, width: u16, height: u16) -> String {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal
                .draw(|frame| draw_earnings(frame, app, frame.size()))
                .unwrap();
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

        #[test]
        fn money_is_shown_per_network_and_per_window() {
            let screen = screen(&mut earning_node(), 120, 24);
            assert!(screen.contains("Last week"), "missing the window columns");
            assert!(screen.contains("ergo"), "missing the payment network");
            // Raw MU rendered in the display unit, ERG by default: 12e9 MU is 12 ERG.
            assert!(screen.contains("12 ERG"), "missing the all-time figure:\n{screen}");
            assert!(screen.contains("7.5 ERG"), "missing the month figure:\n{screen}");
        }

        #[test]
        fn a_refused_deposit_is_named_rather_than_folded_into_the_total() {
            // Money a client tried to pay and this node could not validate. Adding it
            // to what was earned would report income that never arrived.
            let screen = screen(&mut earning_node(), 120, 24);
            assert!(
                screen.contains("0.5 ERG refused"),
                "the refused deposit is not on the page:\n{screen}"
            );
        }

        #[test]
        fn reputation_keeps_what_is_for_and_against_apart() {
            // A stake against is not a smaller stake for, so the two never collapse
            // into one figure — the net is stated as well, never instead.
            let screen = screen(&mut earning_node(), 120, 24);
            assert!(screen.contains("+50.000%"), "missing the stake in favour:\n{screen}");
            assert!(screen.contains("−12.500%"), "missing the stake against:\n{screen}");
            assert!(screen.contains("net +37.500%"), "missing the net:\n{screen}");
            assert!(screen.contains("1 for, 1 against"), "missing the proof count:\n{screen}");
        }

        #[test]
        fn money_is_windowed_and_reputation_is_not() {
            // The point of drawing them apart: money is a flow, so the windows mean
            // something; the network's opinion is a stock the chain cannot date, so a
            // window over it would read like the money above it and mean nothing of
            // the kind. The page says why rather than leaving a gap.
            let screen = screen(&mut earning_node(), 120, 26);
            let money = screen
                .lines()
                .find(|line| line.trim_start_matches('│').starts_with("ergo"))
                .expect("no money row");
            assert!(money.contains("7.5 ERG"), "{screen}");
            assert!(
                !screen.contains("reputation · for"),
                "reputation is being windowed again:\n{screen}"
            );
            assert!(
                screen.contains("no windows"),
                "the missing windows are unexplained:\n{screen}"
            );
        }

        #[test]
        fn a_window_nothing_came_in_over_reads_zero_rather_than_a_dash() {
            // The catalogue was read and nothing arrived that day, which is a
            // measurement and not a gap.
            let screen = screen(&mut earning_node(), 120, 26);
            let money = screen
                .lines()
                .find(|line| line.trim_start_matches('│').starts_with("ergo"))
                .expect("no money row");
            assert!(money.contains("0 ERG"), "the quiet day is not stated:\n{screen}");
        }

        #[test]
        fn every_opinion_is_listed_as_a_share_rather_than_as_raw_token_counts() {
            // Raw counts cannot be read: what "1 token" is worth depends entirely on
            // how many that proof has assigned, so the share is the only figure that
            // means anything on its own.
            let screen = screen(&mut earning_node(), 120, 30);
            assert!(screen.contains("f3b61c2e"), "missing the supporting proof:\n{screen}");
            assert!(screen.contains("9d0a4471"), "missing the opposing proof:\n{screen}");
            assert!(screen.contains("+50.000%"), "missing the share:\n{screen}");
            assert!(!screen.contains(" of 2"), "raw counts are back:\n{screen}");
            assert!(screen.contains("3d ago"), "missing when it was published:\n{screen}");
        }

        #[test]
        fn what_a_share_cost_its_publisher_is_shown_beside_it() {
            // A share alone cannot be read: minting a proof is free, so 50% of a proof
            // with 10 ERG sunk into it and 12.5% of one sitting at the min-box floor
            // are worlds apart, and the page has to say which is which.
            let screen = screen(&mut earning_node(), 120, 30);
            assert!(screen.contains("Backed by"), "missing the backing column:\n{screen}");
            let row = |needle: &str| {
                screen.lines().find(|line| line.contains(needle)).unwrap().to_string()
            };
            // 50% of a 10 ERG proof: 5 ERG of unrecoverable value stands behind us.
            assert!(row("f3b61c2e").contains("5.000000 ERG"), "{screen}");
            // 12.5% of a proof that cost 0.001 ERG to exist: next to nothing.
            assert!(row("9d0a4471").contains("0.000125 ERG"), "{screen}");
            // And the standing total, in the same unit.
            assert!(
                screen.contains("backed by") && screen.contains("sunk and unrecoverable"),
                "the totals do not state the backing:\n{screen}"
            );
        }

        #[test]
        fn backing_is_shown_in_erg_and_never_through_the_display_unit() {
            // MU is what this node charges in; ERG somebody else burned into their own
            // proof is not a balance of ours to denominate. Reading it in MU would
            // make one node's sacrifice look like another node's price list.
            let mut app = earning_node();
            app.money = crate::app::Money {
                unit_name: "mu".to_string(),
                symbol: "MU".to_string(),
                mu_per_unit: 1.0,
                mu_per_unit_pow10: Some(0),
                decimals: 0,
                mu_per_nanoerg: 1.0,
            };
            let screen = screen(&mut app, 120, 30);
            assert!(screen.contains("5.000000 ERG"), "backing left the ERG scale:\n{screen}");
        }

        #[test]
        fn our_own_proof_is_shown_but_said_to_be_excluded() {
            // Otherwise an operator holding a proof that stakes everything on itself
            // would wonder why the page says nobody has staked anything on them.
            let screen = screen(&mut earning_node(), 120, 30);
            assert!(
                screen.contains("own proof stakes") && screen.contains("left out above"),
                "the node's own stake is unexplained:\n{screen}"
            );
        }

        /// A node with nothing recorded either way. `App::new()` reads the real
        /// catalogue, so both halves are cleared: the machine running the tests may
        /// well have been paid.
        fn fresh_node() -> App {
            let mut app = App::new();
            app.tabs.index = Page::ALL
                .iter()
                .position(|page| *page == Page::Earnings)
                .unwrap();
            app.earnings.clear();
            app.reputation = NodeReputation::default();
            app.opinions.refresh(Vec::new());
            app
        }

        #[test]
        fn an_unread_chain_says_so_instead_of_showing_zeros() {
            // A node whose explorer is unreachable has *unknown* reputation, and a
            // page reading "+0.000% from no proof" would be a claim about the network.
            let screen = screen(&mut fresh_node(), 120, 24);
            assert!(screen.contains("Reading the chain"), "no reading notice:\n{screen}");
            assert!(!screen.contains("no proof"), "claimed a verdict it has not read:\n{screen}");
        }

        #[test]
        fn a_failed_read_keeps_the_last_figures_and_says_why() {
            let mut app = earning_node();
            app.reputation.error = "ergo: explorer unreachable".to_string();
            let screen = screen(&mut app, 120, 30);
            assert!(screen.contains("+50.000%"), "the last standing was dropped:\n{screen}");
            assert!(
                screen.contains("chain not read: ergo: explorer unreachable"),
                "the failure is not reported:\n{screen}"
            );
        }

        #[test]
        fn a_node_nobody_has_paid_reads_zero_rather_than_losing_the_row() {
            // Without the placeholder the reputation rows would sit alone under money
            // headings, which reads as though the money figures were the reputation.
            let screen = screen(&mut fresh_node(), 120, 24);
            let row = screen
                .lines()
                .find(|line| line.contains("nothing paid in yet"))
                .unwrap_or_else(|| panic!("no money row at all:\n{screen}"));
            assert_eq!(row.matches("0 ERG").count(), 5, "{screen}");
        }
    }

    #[test]
    fn every_page_renders_at_common_terminal_sizes() {
        for (width, height) in [(80, 24), (140, 40)] {
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            let mut app = App::new();
            for index in 0..Page::ALL.len() {
                app.tabs.index = index;
                terminal.draw(|frame| render(&mut app, frame)).unwrap();
            }
        }
    }

    fn peer_with(contracts: Vec<crate::app::PeerContract>) -> Peer {
        Peer {
            id: "f3b61c2e-aaaa-bbbb-cccc-ddddeeeeffff".to_string(),
            uris: "10.0.0.4:8080".to_string(),
            // Raw MU, as the catalogue stores it; formatting happens at draw time.
            balance: "1000".to_string(),
            remote_client_id: "cli-9f2a".to_string(),
            proof_ids: Vec::new(),
            reputation_score: "7".to_string(),
            contracts,
        }
    }

    fn ergo_contract() -> crate::app::PeerContract {
        crate::app::PeerContract {
            ledger: "ergo".to_string(),
            contract_hash: "1c691f72deadbeef".to_string(),
            asset: "ERG".to_string(),
            address: "0008cd0392aabbcc".to_string(),
            mu_per_unit: "1000000000".to_string(),
        }
    }

    /// A second method on the SAME contract, differing only in its asset. This is what
    /// an Ergo token is: one script, one address, another currency and another rate.
    fn token_contract() -> crate::app::PeerContract {
        crate::app::PeerContract {
            ledger: "ergo".to_string(),
            contract_hash: "1c691f72deadbeef".to_string(),
            asset: "ab".repeat(32),
            address: "0008cd0392aabbcc".to_string(),
            mu_per_unit: "20000000".to_string(),
        }
    }

    mod wallets_card {
        use super::super::draw_ergo;
        use crate::app::{App, LedgerWallet};
        use ratatui::{backend::TestBackend, Terminal};

        fn wallet(ledger: &str, address: &str, balance: Option<f64>, unit: &str,
                  cold: &str) -> LedgerWallet {
            LedgerWallet {
                ledger: ledger.to_string(),
                address: address.to_string(),
                balance,
                unit: unit.to_string(),
                cold_address: cold.to_string(),
            }
        }

        fn rendered(wallets: Vec<LedgerWallet>) -> String {
            let mut app = App::default();
            app.node_info.wallets = wallets;
            let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
            terminal
                .draw(|frame| draw_ergo(frame, &app, frame.size()))
                .unwrap();
            terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect::<String>()
        }

        #[test]
        fn one_block_per_payment_system_and_never_a_total() {
            let screen = rendered(vec![
                wallet("ergo", "9walletaddr", Some(1.25), "ERG", "9coldaddr"),
                wallet("bitcoin", "bc1qxyz", Some(0.5), "BTC", ""),
            ]);

            assert!(screen.contains("ERGO"), "{screen}");
            assert!(screen.contains("BITCOIN"), "{screen}");
            // Each figure in its own money. Adding them up would name an amount the
            // operator cannot spend: they are on different chains.
            assert!(screen.contains("1.25 ERG"), "{screen}");
            assert!(screen.contains("0.5 BTC"), "{screen}");
            assert!(!screen.contains("Total"), "{screen}");
        }

        #[test]
        fn a_cold_wallet_is_shown_only_where_there_is_one() {
            let screen = rendered(vec![
                wallet("ergo", "9walletaddr", Some(1.25), "ERG", "9coldaddr"),
                wallet("bitcoin", "bc1qxyz", Some(0.5), "BTC", ""),
            ]);
            assert!(screen.contains("9coldaddr"), "{screen}");
            // Sweeping nowhere is the default; a "not configured" line per contract
            // would be most of the card.
            // The line prefix, not the word: "9coldaddr" contains "cold" too.
            assert_eq!(screen.matches("  cold ").count(), 1, "{screen}");
        }

        #[test]
        fn a_node_nobody_can_pay_says_so_rather_than_drawing_an_empty_card() {
            let screen = rendered(Vec::new());
            assert!(screen.contains("No payment system configured"), "{screen}");
        }

        #[test]
        fn a_balance_that_could_not_be_read_is_not_shown_as_zero() {
            // Zero would read as an empty wallet, which is a different fact from a
            // wallet this node could not reach.
            let screen = rendered(vec![wallet("ergo", "9walletaddr", None, "ERG", "")]);
            assert!(screen.contains("—"), "{screen}");
            assert!(!screen.contains("0 ERG"), "{screen}");
        }
    }

    mod donations_card {
        use super::super::donation_lines;
        use super::rendered;
        use crate::app::{App, DonationWallet, LedgerDonations, NodeDonations};

        fn wallet(address: &str, weight: &str, share: &str, in_other: bool) -> DonationWallet {
            DonationWallet {
                address: address.to_string(),
                weight: weight.to_string(),
                share: share.to_string(),
                in_other_list: in_other,
                paid: Vec::new(),
            }
        }

        /// The same wallet, with something having actually reached it.
        fn paid_wallet(address: &str, share: &str, paid: &[(&str, &str)]) -> DonationWallet {
            DonationWallet {
                paid: paid
                    .iter()
                    .map(|(asset, amount)| (asset.to_string(), amount.to_string()))
                    .collect(),
                ..wallet(address, share, share, true)
            }
        }

        fn donating_node() -> App {
            let mut app = App::default();
            app.donations = NodeDonations {
                ledgers: vec![LedgerDonations {
                    ledger: "ergo".to_string(),
                    percentage: "0.02".to_string(),
                    owed: vec![("ERG".to_string(), "1200000.5".to_string())],
                    paid_mu: 41_000_000,
                    paid_count: 2,
                    pay_wallets: vec![
                        wallet("9gGZaaaaaaaaaaaaaaaa", "0.7", "0.7", true),
                        wallet("9fXXbbbbbbbbbbbbbbbb", "0.3", "0.3", false),
                    ],
                    credit_wallets: vec![wallet("9gGZaaaaaaaaaaaaaaaa", "1", "1", true)],
                }],
                donation_weight: 0.3,
                peers: Default::default(),
                warnings: vec!["ledgers.ergo: you fund 9fXX but do not count it.".to_string()],
                read_at: Some(1_800_000_000),
                error: String::new(),
            };
            app
        }

        #[test]
        fn the_card_says_what_was_paid_and_what_is_still_owed() {
            let text = rendered(donation_lines(&donating_node()));

            assert!(text.contains("donating 0.02"), "{text}");
            assert!(text.contains("in 2 transaction(s)"), "{text}");
            // The accrued debt is shown in the asset's own smallest unit, fraction
            // included, and never converted: it was incurred at the rate of the moment
            // it was incurred.
            assert!(text.contains("1200000.5 ERG"), "{text}");
        }

        #[test]
        fn what_has_reached_each_funded_wallet_is_shown() {
            // A share is a claim; this is the only thing on the card that can check it.
            // A wallet given a small share should be able to show that something
            // arrived -- and before the payout kept a per-wallet ledger, nothing had.
            let mut app = donating_node();
            app.donations.ledgers[0].pay_wallets = vec![
                paid_wallet("9big", "0.999", &[("ERG", "3959899899")]),
                paid_wallet("9small", "0.001", &[("ERG", "3267327")]),
            ];
            let text = rendered(donation_lines(&app));

            assert!(text.contains("paid 3959899899 ERG"));
            assert!(text.contains("paid 3267327 ERG"));
        }

        #[test]
        fn a_wallet_that_has_never_been_paid_shows_no_figure_at_all() {
            // Rather than "0", which reads as a payout that went wrong rather than as
            // one that has not happened yet. Counted wallets show nothing either: this
            // node pays them nothing. Asserted on the wallet renderer rather than on
            // the whole card, whose own header carries a "paid <total>" of its own.
            let unpaid = rendered(super::super::donation_wallet_lines(
                "funding",
                &[wallet("9nobody", "1", "1", true)],
                "nobody",
            ));
            assert!(!unpaid.contains("paid"));

            let paid = rendered(super::super::donation_wallet_lines(
                "funding",
                &[paid_wallet("9somebody", "1", &[("ERG", "1000000")])],
                "nobody",
            ));
            assert!(paid.contains("paid 1000000 ERG"));
        }

        #[test]
        fn each_asset_is_shown_separately() {
            // A credit in nanoERG says nothing about what a wallet has had in a token,
            // and the two cannot be added up.
            let mut app = donating_node();
            app.donations.ledgers[0].pay_wallets = vec![paid_wallet(
                "9both",
                "1",
                &[("ERG", "1000000"), ("abababab", "4200")],
            )];
            let text = rendered(donation_lines(&app));

            assert!(text.contains("1000000 ERG"));
            assert!(text.contains("4200 abababab"));
        }

        #[test]
        fn an_address_in_one_list_and_not_the_other_is_named() {
            let text = rendered(donation_lines(&donating_node()));

            assert!(text.contains("(not in the other list)"), "{text}");
            assert!(text.contains("you fund 9fXX but do not count it."), "{text}");
        }

        #[test]
        fn a_report_that_has_not_come_back_does_not_read_as_donating_nothing() {
            let app = App::default();
            let text = rendered(donation_lines(&app));

            assert!(text.contains("Reading the donation state"), "{text}");
            assert!(!text.contains("No payment network"), "{text}");
        }

        #[test]
        fn a_node_that_donates_to_nobody_says_so_rather_than_drawing_an_empty_card() {
            let mut app = App::default();
            app.donations = NodeDonations {
                read_at: Some(1),
                ..Default::default()
            };
            let text = rendered(donation_lines(&app));

            assert!(text.contains("No payment network is configured to donate."), "{text}");
        }
    }

    fn rendered(lines: Vec<Line<'static>>) -> String {
        lines
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn peer_detail_prompts_when_nothing_is_selected() {
        let text = rendered(peer_detail_lines(&Money::default(), None, None, None, false));
        assert!(text.contains("Select a peer"));
    }

    #[test]
    fn peer_detail_shows_ledger_contract_address_and_price() {
        // The whole point of issue #231: these four facts were only reachable
        // through a raw sqlite query before.
        let text = rendered(peer_detail_lines(&Money::default(), Some(&peer_with(vec![ergo_contract()])), None, None, false));
        assert!(text.contains("Payment methods (1)"));
        assert!(text.contains("ergo"));
        assert!(text.contains("1c691f72deadbeef"));
        assert!(text.contains("0008cd0392aabbcc"));
        // The rate reads as an equation: what one unit of that ledger buys in MU.
        assert!(text.contains("1 ERG = 1000000000 MU"));
    }

    #[test]
    fn peer_detail_lists_every_contract_instance() {
        // A peer with several instances used to get silently truncated to one.
        let second = crate::app::PeerContract {
            ledger: "simulator".to_string(),
            contract_hash: "abc123".to_string(),
            asset: "SIM".to_string(),
            address: "sim-address".to_string(),
            mu_per_unit: "500".to_string(),
        };
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer_with(vec![ergo_contract(), second])),
            None,
            None,
            false,
        ));
        assert!(text.contains("Payment methods (2)"));
        assert!(text.contains("ergo"));
        assert!(text.contains("simulator"));
        assert!(text.contains("sim-address"));
    }

    #[test]
    fn peer_detail_tells_two_assets_of_one_contract_apart() {
        // Keyed by the contract alone these two rows are the same row twice, at two
        // different rates -- and an operator reading "1 ERG = 20000000 MU" would see
        // this node's ERG rate as the token's.
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer_with(vec![ergo_contract(), token_contract()])),
            None,
            None,
            false,
        ));
        assert!(text.contains("Payment methods (2)"));
        assert!(text.contains("1 ERG = 1000000000 MU"));
        // The id is shortened for the width, so match its head rather than all 64.
        assert!(text.contains("ababab"));
        assert!(text.contains("= 20000000 MU"));
    }

    #[test]
    fn peer_detail_says_so_when_no_contract_is_registered() {
        // Must stay distinguishable from "peer charges through something we
        // don't render", which is exactly what the old hardcoded lookup did.
        let text = rendered(peer_detail_lines(&Money::default(), Some(&peer_with(vec![])), None, None, false));
        assert!(text.contains("No payment method registered"));
    }

    fn payment(status: &str, tx_id: &str, amount: &str) -> PaymentRow {
        PaymentRow {
            created_at: "2026-01-02 10:00:00".to_string(),
            amount: amount.to_string(),
            status: status.to_string(),
            tx_id: tx_id.to_string(),
            deposit_token: "token-1".to_string(),
        }
    }

    fn peer_history(peer_id: &str) -> PeerDetail {
        PeerDetail {
            peer_id: peer_id.to_string(),
            payments: vec![payment("unacknowledged", "abcdef0123456789", "2000")],
            events: vec![ReputationEvent {
                created_at: "2026-01-02 10:00:01".to_string(),
                amount: -100,
                reason: "payment_unacknowledged".to_string(),
                score_after: Some(-93),
            }],
        }
    }

    fn a_client() -> Client {
        Client {
            id: "client-1".to_string(),
            balance: "500".to_string(),
            last_usage: "1700000000".to_string(),
            unmetered: true,
        }
    }

    #[test]
    fn service_detail_shows_its_reputation_and_what_moved_it() {
        let service = Service {
            id: "service-1".to_string(),
            tag: "demo".to_string(),
            size_bytes: 1024,
        };
        let detail = ServiceDetail {
            service_id: service.id.clone(),
            score: Some(-90),
            events: vec![ReputationEvent {
                created_at: "2026-01-02 10:00:00".to_string(),
                amount: -100,
                reason: "instance_lost".to_string(),
                score_after: Some(-90),
            }],
        };

        let text = rendered(service_detail_lines(Some(&service), Some(&detail)));

        assert!(text.contains("Reputation"), "{text}");
        assert!(text.contains("-90"), "{text}");
        assert!(text.contains("instance lost"), "{text}");
    }

    #[test]
    fn a_service_never_scored_says_so_rather_than_reading_as_zero() {
        let service = Service {
            id: "service-1".to_string(),
            tag: "demo".to_string(),
            size_bytes: 1024,
        };
        let detail = ServiceDetail {
            service_id: service.id.clone(),
            score: None,
            events: Vec::new(),
        };

        let text = rendered(service_detail_lines(Some(&service), Some(&detail)));

        assert!(text.contains("not scored yet"), "{text}");
    }

    #[test]
    fn peer_detail_shows_what_we_paid_and_why_the_score_moved() {
        let peer = peer_with(vec![ergo_contract()]);
        let history = peer_history(&peer.id);
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer),
            Some(&history),
            None,
            false,
        ));

        assert!(text.contains("Payments made to this peer (1)"), "{text}");
        assert!(text.contains("unacknowledged"), "{text}");
        // The reason is stored with underscores and read as words.
        assert!(text.contains("payment unacknowledged"), "{text}");
        assert!(text.contains("-100"), "{text}");
        assert!(text.contains("→ -93"), "{text}");
    }

    #[test]
    fn a_peer_with_no_history_says_so_rather_than_showing_an_empty_card() {
        let peer = peer_with(vec![ergo_contract()]);
        let history = PeerDetail {
            peer_id: peer.id.clone(),
            payments: Vec::new(),
            events: Vec::new(),
        };
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer),
            Some(&history),
            None,
            false,
        ));

        assert!(text.contains("Nothing paid to this peer yet."), "{text}");
        assert!(text.contains("No reputation event recorded yet."), "{text}");
    }

    #[test]
    fn history_loaded_for_another_peer_is_never_shown_under_this_one() {
        // The selection can move between the load and the frame. A payment rendered
        // under the wrong peer is a lie about money, so the id has to match.
        let peer = peer_with(vec![ergo_contract()]);
        let history = peer_history("some-other-peer");
        let text = rendered(peer_detail_lines(
            &Money::default(),
            Some(&peer),
            Some(&history),
            None,
            false,
        ));

        assert!(!text.contains("Payments made to this peer"), "{text}");
        assert!(!text.contains("payment unacknowledged"), "{text}");
    }

    #[test]
    fn client_detail_shows_deposits_instances_and_payments() {
        let client = a_client();
        let detail = ClientDetail {
            client_id: client.id.clone(),
            deposits: vec![crate::app::DepositToken {
                id: "token-1".to_string(),
                status: "payed".to_string(),
                created_at: "2026-01-03 09:59:00".to_string(),
            }],
            instances: vec![crate::app::ClientInstance {
                id: "instance-1".to_string(),
                name: "demo".to_string(),
            }],
            payments: vec![payment("accepted", "", "750")],
        };

        let text = rendered(client_detail_lines(
            &Money::default(),
            Some(&client),
            Some(&detail),
            false,
        ));

        assert!(text.contains("Payments received (1)"), "{text}");
        assert!(text.contains("Deposit tokens (1)"), "{text}");
        assert!(text.contains("Instances started here (1)"), "{text}");
        assert!(text.contains("demo"), "{text}");
        // An incoming payment has no transaction id; the token identifies it.
        assert!(text.contains("token token-1"), "{text}");
        // And the reason its balance never moves.
        assert!(text.contains("Never charged"), "{text}");
    }

    #[test]
    fn clients_page_renders_the_client_detail_card() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Clients).unwrap();
        app.clients.items = vec![a_client()];
        app.clients.state.select(Some(0));
        app.client_detail = Some(ClientDetail {
            client_id: "client-1".to_string(),
            deposits: Vec::new(),
            instances: Vec::new(),
            payments: vec![payment("accepted", "", "750")],
        });
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();

        assert!(screen.contains("CLIENTS • 1 known"), "{screen}");
        assert!(screen.contains("SELECTED CLIENT"), "{screen}");
        assert!(screen.contains("Payments received (1)"), "{screen}");
        // Peers are a page of their own now, not a pane on this one.
        assert!(!screen.contains("Reputation proof"), "{screen}");
    }

    #[test]
    fn peers_page_renders_the_peer_detail_card() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Peers).unwrap();
        app.peers.items = vec![peer_with(vec![ergo_contract()])];
        app.peers.state.select(Some(0));
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("SELECTED PEER"));
        assert!(screen.contains("Payment methods (1)"));
        // The table itself stays lean -- no contract columns were added to it.
        assert!(screen.contains("Reputation proof"));
        assert!(!screen.contains("Ledger  "));
    }

    #[test]
    fn a_short_terminal_keeps_both_the_peers_table_and_the_contracts() {
        // Regression: a fixed-height detail card pushed the peers table off an
        // 80x24 screen entirely, and clipped the contracts out of the card --
        // leaving it looking exactly like a peer with nothing registered.
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Peers).unwrap();
        let second = crate::app::PeerContract {
            ledger: "simulator".to_string(),
            contract_hash: "abc123def456".to_string(),
            asset: "SIM".to_string(),
            address: "sim-address".to_string(),
            mu_per_unit: "500".to_string(),
        };
        app.peers.items = vec![peer_with(vec![ergo_contract(), second])];
        app.peers.state.select(Some(0));
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("PEERS • 1 connected"));
        assert!(screen.contains("Payment methods (2)"));
        assert!(screen.contains("ergo"));
        assert!(screen.contains("simulator"));
    }

    #[test]
    fn grouped_instance_tree_renders() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Instances).unwrap();
        app.instances_grouped = true;
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("DEPENDENCY TREE"));
    }

    #[test]
    fn confirm_popup_shows_prompt_and_choices() {
        let backend = TestBackend::new(100, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.input_mode = InputMode::Confirm;
        app.input_title = "Delete service demo? (y/N)".to_string();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("Delete service demo?"));
        assert!(screen.contains("y confirms"));
    }

    #[test]
    fn details_popup_renders_inspect_output() {
        use crate::app::DetailsView;
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.input_mode = InputMode::Details;
        app.details = Some(DetailsView {
            title: "Service abcdef".to_string(),
            lines: vec!["tag: demo".to_string(), "size: 42 bytes".to_string()],
            scroll: 0,
        });
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("Service abcdef"));
        assert!(screen.contains("tag: demo"));
    }

    #[test]
    fn secret_editor_never_renders_plaintext() {
        let backend = TestBackend::new(100, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.input_mode = InputMode::EditConfig;
        app.edit_config_secret = true;
        app.input_title = "Edit wallet mnemonic".to_string();
        app.input = "these words must stay hidden".to_string();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(!screen.contains("these words"));
        assert!(screen.contains("••••"));
    }

    #[test]
    fn ergo_wallet_card_shows_reputation_proof() {
        let backend = TestBackend::new(140, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut app = App::new();
        app.node_info.reputation_proof = "rep-proof-xyz".to_string();
        app.node_info.wallets = vec![crate::app::LedgerWallet {
            ledger: "ergo".to_string(),
            address: "9walletaddr".to_string(),
            balance: Some(1.25),
            unit: "ERG".to_string(),
            cold_address: String::new(),
        }];
        app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Overview).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        // Proof lives in the wallets card, not the NETWORK summary card.
        assert!(screen.contains("WALLETS"));
        assert!(screen.contains("rep-proof-xyz"));
        assert_eq!(screen.matches("rep-proof-xyz").count(), 1);
    }

    /// Who started an instance whose parent this node does not run itself. The tree
    /// nests instance-under-instance already; a client parent had nowhere to show, so
    /// an instance a client started read as one with no parent at all (issue #277).
    mod external_parents {
        use crate::app::{App, Client, Instance, InstanceUsage};
        use ratatui::{backend::TestBackend, Terminal};

        fn instance(id: &str, father: &str) -> Instance {
            Instance {
                id: id.to_string(),
                name: format!("inst-{id}"),
                ip: "10.0.0.7:4040".to_string(),
                service: "builder".to_string(),
                balance: "1000".to_string(),
                virtualizer: "ch".to_string(),
                memory_limit: 1 << 30,
                disk_limit: 10 << 30,
                vcpus: Some(1.0),
                usage: InstanceUsage::default(),
                location: "local".to_string(),
                father_id: father.to_string(),
                mu_per_minute: None,
                mu_per_hour: None,
                consumption_samples: None,
                consumption_age_secs: None,
                energy_watts: None,
                energy_share: None,
            }
        }

        fn tree_text(instances: Vec<Instance>, clients: Vec<&str>) -> String {
            let mut app = App::new();
            app.instances_grouped = true;
            app.instances.refresh(instances);
            app.clients.refresh(
                clients
                    .into_iter()
                    .map(|id| Client {
                        id: id.to_string(),
                        balance: "0".to_string(),
                        last_usage: String::new(),
                        unmetered: false,
                    })
                    .collect(),
            );
            let mut terminal = Terminal::new(TestBackend::new(120, 12)).unwrap();
            terminal
                .draw(|frame| super::super::draw_instances_tree(frame, &app, frame.size()))
                .unwrap();
            terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .map(|cell| cell.symbol())
                .collect()
        }

        #[test]
        fn a_root_started_by_a_client_names_the_client() {
            let text = tree_text(vec![instance("aaa", "client-42")], vec!["client-42"]);
            assert!(text.contains("client client-42"), "{text}");
        }

        #[test]
        fn a_father_this_node_cannot_resolve_is_flagged_rather_than_hidden() {
            let text = tree_text(vec![instance("aaa", "ghost-7")], vec!["client-42"]);
            assert!(text.contains("ghost-7"), "{text}");
            assert!(text.contains("unknown"), "{text}");
        }

        /// The nesting already says who the parent is, so repeating it on the child
        /// would both duplicate it and mislabel a perfectly ordinary parent "unknown".
        #[test]
        fn a_child_nested_under_its_parent_carries_no_parent_label() {
            let text = tree_text(
                vec![instance("aaa", ""), instance("bbb", "aaa")],
                vec!["client-42"],
            );
            assert!(text.contains("inst-bbb"), "{text}");
            assert!(!text.contains("unknown"), "{text}");
        }
    }

    /// Our id inside a remote peer, which is what the other side's logs call us. The
    /// CLI has always printed it; the TUI never carried the column at all (issue #277).
    #[test]
    fn the_peer_card_shows_our_client_id_on_that_peer() {
        let peer = peer_with(vec![]);
        let lines = peer_detail_lines(&Money::default(), Some(&peer), None, None, false);
        let text: String = lines
            .iter()
            .flat_map(|line| line.spans.iter().map(|span| span.content.to_string()))
            .collect();
        assert!(text.contains("cli-9f2a"), "{text}");

        let unregistered = Peer {
            remote_client_id: String::new(),
            ..peer
        };
        let text: String = peer_detail_lines(&Money::default(), Some(&unregistered), None, None, false)
            .iter()
            .flat_map(|line| line.spans.iter().map(|span| span.content.to_string()))
            .collect();
        assert!(text.contains("not registered"), "{text}");
    }

    /// The mouse hit tests read off a *real* frame: the row arithmetic in `app.rs`
    /// retraces widget internals (border, header, the header's bottom margin, the tab
    /// padding and dividers), and nothing but a render can confirm it still matches.
    mod mouse_clicks {
        use super::*;

        fn app_with_peers() -> App {
            let mut app = App::new();
            app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Peers).unwrap();
            app.peers.refresh(
                ["peer-aaa", "peer-bbb", "peer-ccc"]
                    .into_iter()
                    .map(|id| Peer {
                        id: id.to_string(),
                        uris: "10.0.0.4:8080".to_string(),
                        balance: "1000".to_string(),
                        remote_client_id: String::new(),
                        proof_ids: Vec::new(),
                        reputation_score: "0".to_string(),
                        contracts: Vec::new(),
                    })
                    .collect(),
            );
            app
        }

        /// Renders a frame and returns the screen as one string per terminal row.
        fn draw(app: &mut App) -> Vec<String> {
            let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
            terminal.draw(|frame| render(app, frame)).unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..buffer.area.height)
                .map(|y| {
                    (0..buffer.area.width)
                        .map(|x| buffer.get(x, y).symbol())
                        .collect()
                })
                .collect()
        }

        #[test]
        fn a_click_lands_on_the_row_under_the_pointer() {
            let mut app = app_with_peers();
            let screen = draw(&mut app);
            // Whatever row the third peer actually printed on -- not where the
            // arithmetic thinks it should be.
            let y = screen
                .iter()
                .position(|row| row.contains("peer-ccc"))
                .expect("the peers table should have rendered every peer") as u16;

            app.click_at(4, y);
            assert_eq!(
                app.peers.selected().map(|peer| peer.id.as_str()),
                Some("peer-ccc"),
                "clicked row {y} of:\n{}",
                screen.join("\n")
            );
        }

        #[test]
        fn a_click_on_the_header_or_the_border_selects_nothing() {
            let mut app = app_with_peers();
            let screen = draw(&mut app);
            let header = screen
                .iter()
                .position(|row| row.contains("Peer ID"))
                .expect("header") as u16;
            app.click_at(4, header);
            assert!(app.peers.selected().is_none(), "header click selected a row");
        }

        #[test]
        fn a_click_on_a_tab_opens_that_page() {
            let mut app = app_with_peers();
            let screen = draw(&mut app);
            // The tab bar is the first rows of the frame; find CLIENTS in it.
            let (y, x) = screen
                .iter()
                .enumerate()
                .find_map(|(y, row)| {
                    // Byte offset -> terminal column: the dividers between tabs are
                    // multi-byte boxdrawing characters, so the two are not the same.
                    row.find("CLIENTS")
                        .map(|byte| (y as u16, row[..byte].chars().count() as u16))
                })
                .expect("the tab bar should list every page");

            app.click_at(x, y);
            assert_eq!(app.page(), Page::Clients);
        }

        /// Every page is reachable with the mouse across the two rows (issue #395).
        ///
        /// Read off a real render rather than from the arithmetic: one wrong offset
        /// means a click landing on the neighbouring tab.
        ///
        /// Each page takes two clicks now — its group, then the page. That is the
        /// trade the grouping makes, stated rather than implied.
        #[test]
        fn every_page_is_reachable_through_its_group() {
            for page in Page::ALL {
                let mut app = App::new();

                // Row 1: open the group.
                let screen = draw(&mut app);
                let (gx, gy) = screen
                    .iter()
                    .enumerate()
                    .take(3)
                    .find_map(|(y, row)| {
                        row.find(page.group().title())
                            .map(|byte| (row[..byte].chars().count() as u16, y as u16))
                    })
                    .unwrap_or_else(|| {
                        panic!(
                            "{} is not on the group row:\n{}",
                            page.group().title(),
                            screen.join("\n")
                        )
                    });
                app.click_at(gx, gy);
                assert_eq!(app.tabs.group(), page.group());

                // Row 2: pick the page. A group of one draws no second row, and
                // opening it has already landed on its only page.
                if page.group().pages().len() == 1 {
                    assert_eq!(app.page(), page);
                    continue;
                }
                let screen = draw(&mut app);
                let (px, py) = screen
                    .iter()
                    .enumerate()
                    .take(5)
                    .find_map(|(y, row)| {
                        row.find(page.title())
                            .map(|byte| (row[..byte].chars().count() as u16, y as u16))
                    })
                    .unwrap_or_else(|| {
                        panic!("{} is not on the page row:\n{}", page.title(), screen.join("\n"))
                    });

                // Both ends of the title, so a hit test off by one in either
                // direction fails rather than being saved by clicking the middle.
                for column in [px, px + page.title().chars().count() as u16 - 1] {
                    app.click_at(column, py);
                    assert_eq!(
                        app.page(),
                        page,
                        "clicking column {column} of {} opened {:?}",
                        page.title(),
                        app.page()
                    );
                }
            }
        }

        /// The top row shows the five GROUPS and not the twelve pages: an operator
        /// orienting themselves reads five labels, not twelve titles of which the
        /// last was cut off at 80 columns.
        #[test]
        fn the_top_row_shows_groups_rather_than_every_page() {
            let mut app = App::new();
            let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer();
            let bar: String = (0..buffer.area.width)
                .map(|x| buffer.get(x, 1).symbol())
                .collect();

            for group in PageGroup::ALL {
                assert!(bar.contains(group.title()), "{} not in: {bar}", group.title());
            }
            // And the pages that are NOT the open group's are not up here. PRICING
            // belongs to SETTINGS, which is closed on a fresh start.
            assert!(!bar.contains("PRICING"), "{bar}");
            assert!(!bar.contains("INSTANCES"), "{bar}");
        }

        /// ...and the second row shows that group's pages, and only that group's.
        #[test]
        fn the_second_row_shows_only_the_open_groups_pages() {
            let mut app = App::new();
            app.tabs.select_group(PageGroup::Settings);
            let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer();
            let row: String = (0..buffer.area.width)
                .map(|x| buffer.get(x, 3).symbol())
                .collect();

            for page in PageGroup::Settings.pages() {
                assert!(row.contains(page.title()), "{} not in: {row}", page.title());
            }
            // A page from another group has no business on this row: the whole
            // problem being solved is that everything was visible at once.
            assert!(!row.contains("INSTANCES"), "{row}");
            assert!(!row.contains("CLIENTS"), "{row}");
        }

        /// A group with one page draws no second row: a single already-selected
        /// title says nothing and costs the page below it a line.
        #[test]
        fn a_single_page_group_spends_no_row_on_itself() {
            let mut app = App::new();
            app.tabs.select_group(PageGroup::Status);
            let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer();
            let row: String = (0..buffer.area.width)
                .map(|x| buffer.get(x, 3).symbol())
                .collect();

            // Row 3 is the first row of the page itself, not a tab row.
            assert!(!row.trim().is_empty(), "the page should start here: {row}");
            assert_eq!(app.page_tabs_area, ratatui::layout::Rect::ZERO);
        }

        /// The ENERGY page answers the mouse: its rows are three separate bordered
        /// sections rather than one table, so `click_energy` reads the areas the draw
        /// path recorded. A click has to land on the key it looks like it landed on.
        #[test]
        fn clicking_an_energy_row_selects_that_key() {
            let mut app = App::new();
            app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Energy).unwrap();
            let screen = draw(&mut app);
            let (x, y) = find(&screen, "NVML_ENABLED");

            app.click_at(x, y);

            assert_eq!(
                app.selected_energy().map(|entry| entry.path),
                Some("energy.NVML_ENABLED"),
                "clicked ({x},{y}) of:\n{}",
                screen.join("\n")
            );
        }

        /// A schedule with two windows, on the SCHEDULE page, ready to be clicked on.
        /// Every one of the SCHEDULE page's elements has to answer the mouse, not only
        /// the ones a table's `list_area` already covered — `click_at` had no arm for
        /// this page at all before, so a click here used to do nothing.
        fn app_on_schedule_with_two_windows() -> App {
            let mut app = App::new();
            app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Schedule).unwrap();
            app.config_document = serde_yaml::from_str(
                "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '22:00'\n      END: '06:00'\n    - START: '12:00'\n      END: '13:00'\n  ON_CLOSE: refuse\n",
            )
            .ok();
            app
        }

        fn find(screen: &[String], text: &str) -> (u16, u16) {
            screen
                .iter()
                .enumerate()
                .find_map(|(y, row)| row.find(text).map(|byte| (row[..byte].chars().count() as u16, y as u16)))
                .unwrap_or_else(|| panic!("{text:?} not found on screen:\n{}", screen.join("\n")))
        }

        #[test]
        fn clicking_an_edge_selects_its_window_and_edge() {
            let mut app = app_on_schedule_with_two_windows();
            let screen = draw(&mut app);
            let (x, y) = find(&screen, "closes 13:00");

            app.click_at(x + 1, y);
            assert_eq!(app.schedule_selected, 1, "the second window's row was clicked");
            assert_eq!(app.schedule_edge, crate::schedule::Edge::End);
        }

        #[test]
        fn clicking_remove_deletes_that_window() {
            let mut app = app_on_schedule_with_two_windows();
            let screen = draw(&mut app);
            // `[x]` sits on the same row as the window it removes, past its end edge.
            let (_, y) = find(&screen, "closes 13:00");
            let row = &screen[y as usize];
            let remove_x = row.find("[x]").map(|byte| row[..byte].chars().count() as u16).unwrap();

            app.click_at(remove_x, y);
            assert_eq!(app.schedule().windows.len(), 1);
            assert_eq!(
                app.schedule().windows[0].start,
                crate::schedule::parse_clock("22:00").unwrap()
            );
        }

        #[test]
        fn clicking_add_window_appends_one() {
            let mut app = app_on_schedule_with_two_windows();
            let screen = draw(&mut app);
            let (x, y) = find(&screen, "+ add window");

            app.click_at(x + 1, y);
            assert_eq!(app.schedule().windows.len(), 3);
        }

        #[test]
        fn clicking_the_schedule_line_toggles_it_on_and_off() {
            let mut app = app_on_schedule_with_two_windows();
            let screen = draw(&mut app);
            let (x, y) = find(&screen, "schedule: ON");

            app.click_at(x + 1, y);
            assert!(!app.schedule().enabled);
        }

        #[test]
        fn clicking_the_closing_line_swaps_what_it_does() {
            let mut app = app_on_schedule_with_two_windows();
            let screen = draw(&mut app);
            let (x, y) = find(&screen, "at closing:");

            app.click_at(x + 1, y);
            assert_eq!(app.schedule().on_close, crate::schedule::OnClose::Stop);
        }
    }

    /// What the ENERGY page has to put on screen (issue #395).
    ///
    /// The page is not "the energy keys, again": every one of them is already on the
    /// Config tree. What it adds is the sentence beside each key, and the point of
    /// these tests is that the sentence is actually drawn — a page that lost its help
    /// panel in a layout change would still look perfectly reasonable.
    mod energy_page {
        use super::*;

        fn draw_energy_at(width: u16, height: u16) -> (App, Vec<String>) {
            let mut app = App::new();
            app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Energy).unwrap();
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer().clone();
            let screen = (0..buffer.area.height)
                .map(|y| {
                    (0..buffer.area.width)
                        .map(|x| buffer.get(x, y).symbol())
                        .collect::<String>()
                })
                .collect();
            (app, screen)
        }

        #[test]
        fn the_sections_and_their_keys_are_drawn() {
            let (_, screen) = draw_energy_at(140, 40);
            let text = screen.join("\n");

            for expected in [
                "METERING",
                "MODEL FALLBACK",
                "MEASURED SOURCES",
                "ENABLED",
                "PRICE_PER_KWH",
                "IDLE_WATTS",
                "SMART_PLUG_URL",
                "NVML_ENABLED",
                "EXTERNAL_TIMEOUT_SECONDS",
            ] {
                assert!(text.contains(expected), "{expected} missing from:\n{text}");
            }
        }

        /// The reason the page exists. A key with no explanation beside it is a key
        /// that belonged on the Config tree.
        #[test]
        fn the_selected_key_gets_its_explanation() {
            let mut app = App::new();
            app.tabs.index = Page::ALL.iter().position(|p| *p == Page::Energy).unwrap();
            app.energy_selected = crate::energy::entries()
                .iter()
                .position(|(_, entry)| entry.path == "energy.IDLE_WATTS")
                .unwrap();

            let mut terminal = Terminal::new(TestBackend::new(140, 40)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer();
            let text: String = (0..buffer.area.height)
                .map(|y| {
                    (0..buffer.area.width)
                        .map(|x| buffer.get(x, y).symbol())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n");

            assert!(text.contains("energy.IDLE_WATTS"), "{text}");
            // The warning that is the whole reason this key needs prose: measure it.
            assert!(text.contains("MEASURE"), "{text}");
        }

        /// Small terminals must not panic, same as every other page.
        #[test]
        fn it_renders_at_the_common_sizes() {
            for (width, height) in [(80, 24), (140, 40)] {
                draw_energy_at(width, height);
            }
        }
    }

    /// The KyA gate, as it is drawn (issue #395).
    ///
    /// The state machine is pinned in `app::tests::kya_gate`; what is checked here is
    /// that the operator can actually see the question and the two keys that answer
    /// it — a gate whose footer said "q quit" would be telling them the wrong thing
    /// about what `q` does.
    mod kya_overlay {
        use super::*;

        fn screen_of(app: &mut App) -> String {
            let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
            terminal.draw(|frame| render(app, frame)).unwrap();
            let buffer = terminal.backend().buffer();
            (0..buffer.area.height)
                .map(|y| {
                    (0..buffer.area.width)
                        .map(|x| buffer.get(x, y).symbol())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n")
        }

        /// The gate is raised against a directory this test owns, so the developer's
        /// own `storage/.acceptedkya` (or absence of one) cannot decide the outcome.
        fn gated_app() -> App {
            let dir = std::env::temp_dir().join("nodo-tui-kya-render");
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(dir.join("storage")).unwrap();
            let mut app = App::default();
            app.paths.storage = dir.join("storage");
            app.with_kya_gate()
        }

        #[test]
        fn the_document_and_its_two_keys_are_on_screen() {
            let mut app = gated_app();
            let text = screen_of(&mut app);

            assert!(app.awaiting_kya());
            assert!(text.contains("KNOW YOUR ASSUMPTIONS"), "{text}");
            assert!(text.contains("y accept"), "{text}");
            assert!(text.contains("n decline"), "{text}");
        }

        /// The footer must not advertise a page's shortcuts underneath an unanswered
        /// question: those keys deliberately do nothing while the gate is up, and `q`
        /// in particular means something different here.
        #[test]
        fn the_footer_belongs_to_the_gate_rather_than_to_the_page_behind_it() {
            let mut app = gated_app();
            let text = screen_of(&mut app);

            assert!(text.contains("required to run this node"), "{text}");
            assert!(
                !text.contains("tab/shift+tab cycle"),
                "the page's controls are still being offered:\n{text}"
            );
        }
    }
}

#[cfg(test)]
mod energy_preview {
    //! Prints the ENERGY page and the KyA gate once, so a change to either layout is
    //! visible in the test output rather than only in a terminal nobody in CI has.
    //! `cargo test -- --nocapture energy_preview` renders them (issue #395).
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    fn print(app: &mut App) {
        let mut terminal = Terminal::new(TestBackend::new(140, 32)).unwrap();
        terminal.draw(|frame| render(app, frame)).unwrap();
        let buffer = terminal.backend().buffer();
        for y in 0..buffer.area.height {
            let row: String = (0..buffer.area.width)
                .map(|x| buffer.get(x, y).symbol())
                .collect();
            println!("{row}");
        }
    }

    #[test]
    fn preview() {
        let mut app = App::new();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Energy)
            .unwrap();
        app.energy_selected = crate::energy::entries()
            .iter()
            .position(|(_, entry)| entry.path == "energy.IDLE_WATTS")
            .unwrap();
        print(&mut app);
    }

    #[test]
    fn kya_preview() {
        let dir = std::env::temp_dir().join("nodo-tui-kya-preview");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("storage")).unwrap();
        let mut app = App::default();
        app.paths.storage = dir.join("storage");
        let mut app = app.with_kya_gate();
        print(&mut app);
    }
}

#[cfg(test)]
mod pricing_preview {
    //! Prints the pricing page once so a change to the layout is visible in the test
    //! output. `cargo test -- --nocapture pricing_preview` renders it.
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    fn preview() {
        let mut app = App::new();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Pricing)
            .unwrap();
        app.prices.state.select(Some(1));
        app.prices.state_id = Some("CPU_MU_PER_VCPU_HOUR".to_string());

        let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer();
        for y in 0..buffer.area.height {
            let row: String = (0..buffer.area.width)
                .map(|x| buffer.get(x, y).symbol())
                .collect();
            println!("{row}");
        }
    }
}

#[cfg(test)]
mod config_tree {
    //! The Config page is a collapsible tree, so the properties that matter are
    //! that sections start collapsed, expanding reveals the nested (indented)
    //! scalars, and the `/` filter *expands and highlights* matches instead of
    //! hiding everything else.
    use super::*;
    use crate::app::ConfigPathSegment;
    use ratatui::buffer::Buffer;
    use ratatui::{backend::TestBackend, Terminal};

    fn entry(path: &str, value: &str, value_type: &str, secret: bool) -> ConfigEntry {
        ConfigEntry {
            path: path.to_string(),
            path_segments: path
                .split('.')
                .map(|key| ConfigPathSegment::Key(key.to_string()))
                .collect(),
            value: value.to_string(),
            edit_value: value.to_string(),
            value_type: value_type.to_string(),
            secret,
        }
    }

    fn render_buffer(app: &mut App) -> Buffer {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal
            .draw(|frame| draw_config(frame, app, frame.size()))
            .unwrap();
        terminal.backend().buffer().clone()
    }

    fn rows_of(buffer: &Buffer) -> Vec<String> {
        (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer.get(x, y).symbol())
                    .collect::<String>()
            })
            .collect()
    }

    /// Leading spaces of the row containing `needle`, after the left border — the
    /// tree indents each level by two, so a deeper node has a larger value.
    fn indent_of(rows: &[String], needle: &str) -> usize {
        let row = rows
            .iter()
            .find(|row| row.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle} in:\n{}", rows.join("\n")));
        let body = row.trim_start_matches('│');
        body.len() - body.trim_start().len()
    }

    #[test]
    fn sections_start_collapsed_and_expand_to_reveal_indented_nested_values() {
        let mut app = App::default();
        app.config_all = vec![
            entry("virtualizers.ch.MIN_MEM_MIB", "512", "number", false),
            entry("virtualizers.ch.MAX_MEM_MIB", "2048", "number", false),
            entry("network.GATEWAY_PORT", "5000", "number", false),
        ];

        // Collapsed: the top-level sections show, their nested scalars do not.
        let screen = rows_of(&render_buffer(&mut app)).join("\n");
        assert!(screen.contains("virtualizers"), "{screen}");
        assert!(screen.contains("network"), "{screen}");
        assert!(
            !screen.contains("MIN_MEM_MIB"),
            "a collapsed tree must hide nested leaves:\n{screen}"
        );

        // Expand the branch and its child mapping: the nested scalar now renders,
        // carrying its value, and is indented deeper than its parent section.
        app.config_tree_state.open(vec!["virtualizers".to_string()]);
        app.config_tree_state
            .open(vec!["virtualizers".to_string(), "ch".to_string()]);
        let rows = rows_of(&render_buffer(&mut app));
        let screen = rows.join("\n");
        assert!(screen.contains("MIN_MEM_MIB"), "{screen}");
        assert!(screen.contains("512"), "{screen}");
        assert!(
            indent_of(&rows, "MIN_MEM_MIB") > indent_of(&rows, "virtualizers"),
            "nested leaf should be indented deeper than its section:\n{screen}"
        );
    }

    #[test]
    fn filter_expands_and_highlights_the_match_without_hiding_context() {
        let mut app = App::default();
        app.config_all = vec![
            entry("virtualizers.ch.MIN_MEM_MIB", "512", "number", false),
            entry("virtualizers.ch.MAX_MEM_MIB", "2048", "number", false),
            entry("network.GATEWAY_PORT", "5000", "number", false),
        ];

        app.config_filter = "mem".to_string();
        app.apply_config_filter();

        let buffer = render_buffer(&mut app);
        let screen = rows_of(&buffer).join("\n");

        // Both matches' ancestors were opened, so the nested leaves are revealed...
        assert!(screen.contains("MIN_MEM_MIB"), "{screen}");
        assert!(screen.contains("MAX_MEM_MIB"), "{screen}");
        // ...and the unrelated section is still on screen (filter expands, not hides).
        assert!(
            screen.contains("network"),
            "filter must keep non-matching sections visible for context:\n{screen}"
        );
        // The title reports a match count, not a shrunken row count.
        assert!(screen.contains("2 match"), "{screen}");

        // A match that isn't the (selected) first one is highlighted with the
        // filter colour — the selected row carries the selection style instead,
        // which is why the assertion looks at a second, non-selected match.
        let has_highlight = (0..buffer.area.height).any(|y| {
            (0..buffer.area.width).any(|x| {
                let cell = buffer.get(x, y);
                cell.symbol() != " " && cell.style().bg == Some(warn())
            })
        });
        assert!(has_highlight, "expected a non-selected filter match to be highlighted");
    }

    /// Prints the Config page once so a layout change is visible in the test
    /// output. `cargo test -- --nocapture config_tree::preview` renders it.
    #[test]
    fn preview() {
        let mut app = App::default();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Config)
            .unwrap();
        app.config_all = vec![
            entry("main.MAIN_DIR", "/var/lib/nodo", "string", false),
            entry("virtualizers.ch.MIN_MEM_MIB", "512", "number", false),
            entry("virtualizers.ch.MAX_MEM_MIB", "2048", "number", false),
            entry("ledgers.ergo.NODE_URL", "http://localhost:9053", "string", false),
            entry("ledgers.ergo.WALLET_MNEMONIC", "word word word", "string", true),
            entry("network.GATEWAY_PORT", "5000", "number", false),
            entry("core_services.packer", "Qm…packer", "string", false),
        ];
        app.config_tree_state.open(vec!["virtualizers".to_string()]);
        app.config_tree_state
            .open(vec!["virtualizers".to_string(), "ch".to_string()]);
        app.config_tree_state.open(vec!["ledgers".to_string()]);
        app.config_tree_state
            .open(vec!["ledgers".to_string(), "ergo".to_string()]);
        app.config_tree_state.select(vec![
            "virtualizers".to_string(),
            "ch".to_string(),
            "MIN_MEM_MIB".to_string(),
        ]);

        for row in rows_of(&render_buffer(&mut app)) {
            println!("{row}");
        }
    }

}

#[cfg(test)]
mod cell_preview {
    use super::{draw_cell, render};
    use crate::app::{App, InputMode, Page};
    use ratatui::{backend::TestBackend, Terminal};

    fn with_example_config() -> App {
        let mut app = App::new();
        let example = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../config.example.yaml");
        app.config_document =
            serde_yaml::from_str(&std::fs::read_to_string(example).unwrap()).ok();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Cell)
            .unwrap();
        app
    }

    fn dump(app: &mut App, width: u16, height: u16, title: &str) {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        println!("--- {title} ---");
        for row in 0..buffer.area.height {
            let line: String = (0..buffer.area.width)
                .map(|column| buffer.get(column, row).symbol())
                .collect();
            println!("{line}");
        }
    }

    /// Prints the two overlays for eyeballing:
    /// `cargo test cell_overlays -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn cell_overlays() {
        let mut app = with_example_config();
        app.open_profile_picker();
        dump(&mut app, 120, 32, "profile picker");

        let mut app = with_example_config();
        app.cell.profile = 1;
        app.submit_profile_selection();
        dump(&mut app, 120, 32, "profile diff");

        let mut app = with_example_config();
        app.cell.organelle = 4;
        app.cell.lever = 0;
        app.toggle_selected_lever();
        dump(&mut app, 120, 32, "one lever, several keys");
        assert_eq!(app.input_mode, InputMode::ConfirmWrites);
    }

    /// Prints the CELL page for eyeballing during development:
    /// `cargo test cell_preview -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn preview() {
        for (width, height) in [(120, 30), (80, 24)] {
            let mut app = App::new();
            // The shipped defaults, so the preview shows real values rather than a
            // page of "not set".
            let example = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../../config.example.yaml");
            app.config_document =
                serde_yaml::from_str(&std::fs::read_to_string(example).unwrap()).ok();
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal
                .draw(|frame| draw_cell(frame, &mut app, frame.size()))
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            println!("--- {width}x{height} ---");
            for row in 0..buffer.area.height {
                let line: String = (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect();
                println!("{line}");
            }
        }
    }
}

#[cfg(test)]
/// The ACTION REQUIRED banner on OVERVIEW.
///
/// Both conditions were detected already and written only to `storage/app.log`. A
/// node that cannot serve and one that cannot be paid both looked, from this screen,
/// exactly like a healthy one.
mod alert_banner {
    use super::{alert_banner_height, render};
    use crate::alerts::OperatorAlert;
    use crate::app::{App, Page};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn overview_with(alerts: Vec<OperatorAlert>) -> String {
        let mut app = App::new();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Overview)
            .unwrap();
        app.alerts.set(alerts);
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
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

    /// The real messages, not shortened ones. The wrapping these provoke on an
    /// ordinary terminal is exactly what the height arithmetic has to get right, so a
    /// fixture with a convenient one-line summary would test the easy case and miss
    /// the bug that truncated the second alert.
    fn port_alert() -> OperatorAlert {
        OperatorAlert {
            key: "gateway_port_firewall",
            summary: "TCP 52285 must be open in the host firewall before this node can \
                      serve. See /opt/nodo/.gateway_notice for the exact command."
                .to_string(),
        }
    }

    fn java_alert() -> OperatorAlert {
        OperatorAlert {
            key: "java_missing",
            summary: "Java is not installed, so this node cannot settle payments or \
                      publish reputation. Install it with `sudo /bin/bash \
                      /opt/nodo/bash/install_java.sh /opt/nodo`."
                .to_string(),
        }
    }

    /// The point of the whole change: the firewall instruction reaches the screen
    /// the operator leaves open, instead of only the log file.
    #[test]
    fn the_gateway_port_alert_is_drawn_above_the_cards() {
        let screen = overview_with(vec![port_alert()]);

        assert!(screen.contains("ACTION REQUIRED"), "{screen}");
        assert!(screen.contains("TCP 52285"), "{screen}");

        let banner = screen
            .lines()
            .position(|line| line.contains("ACTION REQUIRED"))
            .expect("a banner line");
        // The card's own border, not merely the string "NODE": the banner's
        // own title contains it too, and a test that matched that would pass
        // while asserting nothing.
        let node_card = screen
            .lines()
            .position(|line| line.contains("\u{250c} NODE \u{2500}"))
            .expect("the NODE card");
        // Above, not beside: a warning under the fold is a warning nobody has
        // scrolled to.
        assert!(banner < node_card, "banner at {banner}, NODE at {node_card}:\n{screen}");
    }

    #[test]
    fn the_java_alert_names_the_consequence_rather_than_the_symptom() {
        let screen = overview_with(vec![java_alert()]);

        // "Java is not installed" is a fact about the machine. "cannot settle
        // payments" is the thing the operator is losing by it, and it is the
        // reason this is on the front page at all: nothing crashes without Java,
        // the node simply stops being payable and looks fine doing it.
        assert!(screen.contains("cannot settle payments"), "{screen}");
    }

    #[test]
    fn both_alerts_get_their_own_line() {
        let screen = overview_with(vec![port_alert(), java_alert()]);

        assert_eq!(screen.matches("ACTION REQUIRED").count(), 2, "{screen}");
    }

    /// A healthy node's OVERVIEW is exactly the page it was before this existed.
    /// Space permanently reserved for a warning is space that stops carrying one.
    #[test]
    fn a_healthy_node_gets_no_banner_and_no_reserved_space() {
        let screen = overview_with(Vec::new());

        assert!(!screen.contains("ACTION REQUIRED"), "{screen}");
        assert!(!screen.contains("THIS NODE NEEDS YOU"), "{screen}");
    }

    /// The banner is sized to its contents, so the cards below keep their own
    /// heights rather than being squeezed by a fixed strip.
    #[test]
    fn the_banner_is_as_tall_as_it_needs_to_be_and_no_taller() {
        let mut app = App::new();
        assert_eq!(alert_banner_height(&app, 200), 0);

        app.alerts.set(vec![port_alert()]);
        assert_eq!(alert_banner_height(&app, 200), 3);

        app.alerts.set(vec![port_alert(), java_alert()]);
        assert_eq!(alert_banner_height(&app, 200), 4);
    }

    /// ...and it grows when the messages wrap, which they do on any ordinary
    /// terminal. A fixed row per alert let the paragraph produce more than the box
    /// was sized for, so a node with both problems showed only the first.
    #[test]
    fn a_wrapped_alert_gets_the_rows_it_actually_needs() {
        let mut app = App::new();
        app.alerts.set(vec![port_alert(), java_alert()]);

        let wide = alert_banner_height(&app, 200);
        let narrow = alert_banner_height(&app, 90);

        assert!(narrow > wide, "narrow {narrow} should exceed wide {wide}");
    }

    /// The real test of the above: on a terminal where both messages wrap, both are
    /// still fully on screen.
    #[test]
    fn both_alerts_survive_a_terminal_narrow_enough_to_wrap_them() {
        let mut app = App::new();
        app.alerts.set(vec![port_alert(), java_alert()]);
        app.tabs.select_page(Page::Overview);
        let mut terminal = Terminal::new(TestBackend::new(90, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let screen: String = (0..buffer.area.height)
            .map(|row| {
                (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");

        assert_eq!(screen.matches("ACTION REQUIRED").count(), 2, "{screen}");
        // The tail of each message, so a banner that merely started both but
        // truncated one fails here.
        assert!(screen.contains("exact command"), "{screen}");
        assert!(screen.contains("install_java.sh"), "{screen}");
    }

    /// It is on OVERVIEW and nowhere else. A banner repeated on twelve pages is
    /// a banner that becomes part of the furniture.
    #[test]
    fn the_banner_belongs_to_overview() {
        let mut app = App::new();
        app.alerts.set(vec![port_alert()]);
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Logs)
            .unwrap();
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let screen: String = (0..buffer.area.height)
            .map(|row| {
                (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");

        assert!(!screen.contains("ACTION REQUIRED"), "{screen}");
    }
}


/// Why a config edit may not stick, said before the value is typed.
///
/// Never the file: config.yaml is world-writable by install.sh. What needs root is
/// the *restart* the transaction owes a serving node (`nodo daemon restart` ->
/// systemctl, refused under a non-zero euid), so a key the node re-reads from disk
/// owes none and shows no hint.
#[cfg(test)]
mod config_write_root_hint {
    use super::render;
    use crate::app::{App, EditKind, InputMode, Page};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    /// An open editor pointed at `key`, on a node in `service_status`.
    fn app_editing(key: &str, service_status: &str) -> App {
        let mut app = App::new();
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Config)
            .unwrap();
        app.node_info.service_status = service_status.to_string();
        app.input_mode = InputMode::EditConfig;
        app.input_title = format!("Edit {key}");
        app.input = "0.21".to_string();
        app.edit_kind = EditKind::Number;
        app.edit_config_path = Some(crate::cell::path_segments(key));
        app
    }

    /// A restart key: `ConfigManager` reads it once at start-up, so the node and
    /// the file disagree until it restarts.
    fn app_editing_a_restart_key(service_status: &str) -> App {
        app_editing("low_demand.CPU_MAX_PERCENT", service_status)
    }

    fn app_editing_the_kwh_price(service_status: &str) -> App {
        let mut app = app_editing("energy.PRICE_PER_KWH", service_status);
        app.tabs.index = Page::ALL
            .iter()
            .position(|page| *page == Page::Energy)
            .unwrap();
        app
    }

    fn screen(app: &mut App) -> String {
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(app, frame)).unwrap();
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

    /// The whole point of the fix: the operator is told *before* typing, and told
    /// the real reason, instead of watching a value be written and put back.
    #[test]
    fn an_unprivileged_edit_against_a_serving_node_is_warned_about_up_front() {
        let mut app = app_editing_a_restart_key("running");
        // The test process is not root on CI or on a developer's machine; if it
        // somehow is, the hint is correctly absent and there is nothing to assert.
        if !app.config_write_needs_root() {
            return;
        }

        let screen = screen(&mut app);

        assert!(screen.contains("needs root"), "{screen}");
        // And it names the actual cause, because "needs root" on its own is what
        // sent the operator looking at the key and at the file permissions.
        assert!(screen.contains("daemon restart"), "{screen}");
    }

    /// A stopped node owes no restart, so the same edit is unprivileged -- and this
    /// is exactly the asymmetry that made the requirement look arbitrary.
    #[test]
    fn the_same_edit_against_a_stopped_node_needs_nothing() {
        let mut app = app_editing_a_restart_key("not running");

        assert!(!app.config_write_needs_root());
        assert!(!screen(&mut app).contains("needs root"));
    }

    /// The kWh price needs nothing, on a serving node, as an unprivileged process:
    /// the node re-reads the `energy:` block itself, so the write is the whole
    /// change and there is no restart to be refused.
    #[test]
    fn editing_the_kwh_price_on_a_serving_node_needs_no_root() {
        let mut app = app_editing_the_kwh_price("running");

        assert!(!app.config_write_needs_root());
        assert!(!screen(&mut app).contains("needs root"));
    }

    /// And it really is the key that decides, not the page it was edited from: the
    /// same key reached through the raw Config tree answers the same way.
    #[test]
    fn a_live_key_is_live_from_whichever_page_it_is_edited() {
        let energy = app_editing_the_kwh_price("running");
        let config = app_editing("energy.PRICE_PER_KWH", "running");

        assert!(!energy.config_write_needs_root());
        assert!(!config.config_write_needs_root());
    }

    /// The distinction is between keys the node re-reads and keys it does not, so
    /// a restart key and a live key must not answer the same way on a serving node.
    #[test]
    fn a_restart_key_and_a_live_key_are_told_apart() {
        let restart = app_editing_a_restart_key("running");
        let live = app_editing_the_kwh_price("running");

        if !restart.config_write_needs_root() {
            return; // running as root; there is nothing to distinguish.
        }
        assert!(!live.config_write_needs_root());
    }

    /// The hint belongs to config editing. The Connect box and the Config filter go
    /// nowhere near `apply_config_change`, and a root warning on them would be a
    /// claim that is simply false.
    #[test]
    fn the_hint_is_not_shown_on_modals_that_write_nothing() {
        let mut app = app_editing_the_kwh_price("running");
        app.input_mode = InputMode::FilterConfig;
        app.input_title = "Filter".to_string();

        assert!(!screen(&mut app).contains("needs root"));
    }
}

/// Prints the two-row tab bar for each group, for the PR description.
/// `cargo test -p tui two_level_tab_bar_preview -- --ignored --nocapture`
#[cfg(test)]
mod two_level_tab_bar_preview {
    use super::render;
    use crate::app::{App, PageGroup};
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    #[ignore]
    fn preview() {
        for group in PageGroup::ALL {
            let mut app = App::new();
            app.tabs.select_group(group);
            let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer().clone();
            println!("--- {} open ---", group.title());
            for row in 0..5 {
                let line: String = (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect();
                println!("{}", line.trim_end());
            }
            println!();
        }
    }
}

/// Themes reach the screen (issue #395).
///
/// The property that matters is that switching a theme changes what is drawn, which
/// is only true if every colour goes through it.
///
/// Serialised by a mutex: the theme is process-global and `cargo test` runs these on
/// threads, so two racing would each see the other's palette.
#[cfg(test)]
mod themes {
    use super::render;
    use crate::app::{App, Page};
    use crate::theme::{self, Theme, DARK, LIGHT, MONO, UBUNTU};
    use ratatui::backend::TestBackend;
    use ratatui::style::Color;
    use ratatui::Terminal;
    use std::sync::Mutex;

    static SERIAL: Mutex<()> = Mutex::new(());

    /// Every foreground colour actually painted on a full render of `page`.
    fn colours_drawn(theme: Theme, page: Page) -> Vec<Color> {
        let _guard = SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let restore = theme::current();
        theme::set_current(theme);

        let mut app = App::new();
        app.tabs.select_page(page);
        let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let mut colours: Vec<Color> = Vec::new();
        for y in 0..buffer.area.height {
            for x in 0..buffer.area.width {
                let colour = buffer.get(x, y).fg;
                if !colours.contains(&colour) {
                    colours.push(colour);
                }
            }
        }

        theme::set_current(restore);
        colours
    }

    /// Every background colour actually painted on a full render of `page`, with
    /// how many cells carry each.
    fn backgrounds_drawn(theme: Theme, page: Page) -> Vec<(Color, usize)> {
        let _guard = SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let restore = theme::current();
        theme::set_current(theme);

        let mut app = App::new();
        app.tabs.select_page(page);
        let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let mut counts: Vec<(Color, usize)> = Vec::new();
        for y in 0..buffer.area.height {
            for x in 0..buffer.area.width {
                let colour = buffer.get(x, y).bg;
                match counts.iter_mut().find(|(seen, _)| *seen == colour) {
                    Some((_, count)) => *count += 1,
                    None => counts.push((colour, 1)),
                }
            }
        }

        theme::set_current(restore);
        counts
    }

    /// The theme colours the *background*, not just the text on it: a single cell
    /// left at `Reset` shows the terminal's own colour through the console.
    ///
    /// `mono` is excluded by construction and asserted separately below.
    #[test]
    fn every_cell_carries_the_themed_background() {
        for theme in [UBUNTU, DARK, LIGHT] {
            for page in Page::ALL {
                let counts = backgrounds_drawn(theme, page);
                assert!(
                    !counts.iter().any(|(colour, _)| *colour == Color::Reset),
                    "{} leaves cells unpainted on {:?}: {counts:?}",
                    theme.name,
                    page
                );
                let themed = counts
                    .iter()
                    .find(|(colour, _)| *colour == theme.background)
                    .map(|(_, count)| *count)
                    .unwrap_or(0);
                // The frame fill is the majority of the screen; anything else is a
                // widget that chose its own background for a reason.
                assert!(
                    themed > (120 * 30) / 2,
                    "{} paints its background on only {themed} cells of {:?}",
                    theme.name,
                    page
                );
            }
        }
    }

    /// The mono theme is the one that keeps the terminal's background, because the
    /// terminal's colours are the thing it exists not to override.
    #[test]
    fn the_mono_theme_leaves_the_terminal_background_alone() {
        assert_eq!(MONO.background, Color::Reset);
        let counts = backgrounds_drawn(MONO, Page::Overview);
        assert!(
            counts.iter().any(|(colour, _)| *colour == Color::Reset),
            "{counts:?}"
        );
    }

    /// A popup paints its own background over the frame's, with nothing showing
    /// through between the two: `Clear` resets the cells it covers, so a popup that
    /// did not repaint would be a terminal-coloured hole in a themed console.
    #[test]
    fn a_popup_repaints_every_cell_it_covers() {
        for mode in [
            crate::app::InputMode::EditConfig,
            crate::app::InputMode::Confirm,
            crate::app::InputMode::Details,
            crate::app::InputMode::PickProfile,
        ] {
            let _guard = SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let restore = theme::current();
            theme::set_current(UBUNTU);

            let mut app = App::new();
            app.input_mode = mode;
            app.input_title = "Edit energy.PRICE_PER_KWH".to_string();
            app.input = "0.21".to_string();
            app.details = Some(crate::app::DetailsView {
                title: "Details".to_string(),
                lines: vec!["one".to_string(), "two".to_string()],
                scroll: 0,
            });
            let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
            let buffer = terminal.backend().buffer().clone();

            let mut unpainted = Vec::new();
            for y in 0..buffer.area.height {
                for x in 0..buffer.area.width {
                    if buffer.get(x, y).bg == Color::Reset {
                        unpainted.push((x, y));
                    }
                }
            }

            theme::set_current(restore);

            assert!(
                unpainted.is_empty(),
                "{mode:?} leaves {} cells showing the terminal through",
                unpainted.len()
            );
        }
    }

    /// The default really is the Ubuntu palette, on screen and not merely in a
    /// struct: the orange accent and the white text are what the issue asks for.
    #[test]
    fn the_default_theme_paints_the_ubuntu_palette() {
        let colours = colours_drawn(UBUNTU, Page::Overview);

        assert!(
            colours.contains(&Color::Rgb(0xE9, 0x54, 0x20)),
            "no Ubuntu orange in {colours:?}"
        );
        assert!(
            colours.contains(&Color::Rgb(0xFF, 0xFF, 0xFF)),
            "no Ubuntu white in {colours:?}"
        );
        // And none of the old hard-coded palette survives on this page.
        assert!(!colours.contains(&Color::Cyan), "{colours:?}");
    }

    /// Switching the theme changes what is painted. The whole point.
    #[test]
    fn switching_the_theme_changes_the_colours_on_screen() {
        let ubuntu = colours_drawn(UBUNTU, Page::Overview);
        let dark = colours_drawn(DARK, Page::Overview);

        assert_ne!(ubuntu, dark);
        assert!(dark.contains(&Color::Cyan), "{dark:?}");
        assert!(!dark.contains(&Color::Rgb(0xE9, 0x54, 0x20)), "{dark:?}");
    }

    /// Every page, not just OVERVIEW. A colour left hard-coded on some page nobody
    /// checked is exactly the failure mode this refactor exists to prevent, and it
    /// would be invisible on the one page a test happened to render.
    #[test]
    fn no_page_keeps_a_hard_coded_colour_from_the_old_palette() {
        for page in Page::ALL {
            let colours = colours_drawn(UBUNTU, page);

            for stale in [Color::Cyan, Color::LightBlue, Color::LightMagenta] {
                assert!(
                    !colours.contains(&stale),
                    "{:?} still paints {stale:?}, so it is not going through the theme",
                    page
                );
            }
        }
    }

    /// The mono theme paints no hue on any page. Not decoration: anything it cannot
    /// express is something the interface was saying with colour *alone*, and that is
    /// a thing it should not be doing.
    #[test]
    fn the_mono_theme_paints_no_hue_on_any_page() {
        for page in Page::ALL {
            for colour in colours_drawn(MONO, page) {
                assert!(
                    matches!(
                        colour,
                        Color::Reset
                            | Color::White
                            | Color::Gray
                            | Color::DarkGray
                            | Color::Black
                    ),
                    "{:?} paints {colour:?} under the mono theme",
                    page
                );
            }
        }
    }

    /// No theme paints text in the colour of the background under it.
    ///
    /// Catches a foreground chosen against a dark terminal and left to sit on a
    /// light one. Now that the frame has a background, this holds for every theme
    /// rather than only where the comparison was against `Reset`.
    #[test]
    fn no_theme_paints_text_in_the_colour_behind_it() {
        for theme in [UBUNTU, DARK, LIGHT, MONO] {
            for page in Page::ALL {
                let _guard = SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                let restore = theme::current();
                theme::set_current(theme);

                let mut app = App::new();
                app.tabs.select_page(page);
                let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
                terminal.draw(|frame| render(&mut app, frame)).unwrap();
                let buffer = terminal.backend().buffer().clone();

                let mut invisible = Vec::new();
                for y in 0..buffer.area.height {
                    for x in 0..buffer.area.width {
                        let cell = buffer.get(x, y);
                        // A blank carries no text, so its foreground says nothing
                        // about legibility.
                        if cell.symbol().trim().is_empty() {
                            continue;
                        }
                        // `Reset` on `Reset` is the terminal's own pair, which is
                        // legible by definition -- and is what `mono` asks for.
                        if cell.fg == cell.bg && cell.bg != Color::Reset {
                            invisible.push((x, y, cell.symbol().to_string()));
                        }
                    }
                }

                theme::set_current(restore);

                assert!(
                    invisible.is_empty(),
                    "{} paints invisible text on {page:?}: {invisible:?}",
                    theme.name
                );
            }
        }
    }

    /// Popups are themed too. They paint their own background over whatever they
    /// cover, so a popup that kept a hard-coded black would be a black box in the
    /// middle of a light terminal.
    #[test]
    fn a_popup_takes_its_background_from_the_theme() {
        let _guard = SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let restore = theme::current();
        theme::set_current(LIGHT);

        let mut app = App::new();
        app.input_mode = crate::app::InputMode::EditConfig;
        app.input_title = "Edit ui.THEME".to_string();
        app.input = "light".to_string();
        let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let backgrounds: Vec<Color> = (0..buffer.area.height)
            .flat_map(|y| (0..buffer.area.width).map(move |x| (x, y)))
            .map(|(x, y)| buffer.get(x, y).bg)
            .collect();

        theme::set_current(restore);

        assert!(backgrounds.contains(&Color::White), "the popup is not themed");
        assert!(!backgrounds.contains(&Color::Black), "a hard-coded black background survived");
    }

    /// The CONFIG page offers the theme as a picker rather than as free text, and
    /// every name it offers resolves. A picker listing a value that silently falls
    /// back to something else would be lying about what it does.
    #[test]
    fn the_config_page_offers_every_theme_by_name() {
        let options = crate::app::known_enum_values("ui.THEME").expect("a picker");

        assert_eq!(options, theme::NAMES);
        for name in options {
            let resolved = Theme::by_name(name);
            assert!(
                *name == "default" || resolved.name == *name,
                "{name} resolves to {}",
                resolved.name
            );
        }
    }
}


/// Prints the new OVERVIEW, for the PR description.
/// `cargo test -p tui overview_preview -- --ignored --nocapture`
#[cfg(test)]
mod overview_preview {
    use super::render;
    use crate::app::{App, LedgerEarnings, NodeEnergy, Page};
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    #[ignore]
    fn preview() {
        let mut app = App::new();
        app.tabs.select_page(Page::Overview);
        app.earnings = vec![LedgerEarnings {
            ledger: "ergo".to_string(),
            day: 120_000_000,
            week: 940_000_000,
            month: 3_600_000_000,
            year: 21_000_000_000,
            total: 24_500_000_000,
            refused: 0,
        }];
        app.node_energy = NodeEnergy {
            watts: Some(84.0),
            price_per_kwh: 0.21,
            currency: "EUR".to_string(),
            backend: "rapl".to_string(),
            is_floor: true,
        };
        app.config_document = serde_yaml::from_str(
            "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '08:00'\n      END: '20:00'\n  ON_CLOSE: refuse\n",
        )
        .ok();
        app.now_minute = 9 * 60 + 30;

        let mut terminal = Terminal::new(TestBackend::new(116, 26)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        for row in 0..buffer.area.height {
            let line: String = (0..buffer.area.width)
                .map(|column| buffer.get(column, row).symbol())
                .collect();
            println!("{}", line.trim_end());
        }
    }
}

/// The OVERVIEW summary panels that replaced the CPU and MEMORY history charts
/// (issue #395).
///
/// The sparklines charted the same two numbers HOST CAPACITY gauges directly above
/// them. These three each summarise a page that is otherwise a whole tab away.
#[cfg(test)]
mod overview_summaries {
    use super::render;
    use crate::app::{App, LedgerEarnings, NodeEnergy, Page};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn overview(app: &mut App) -> String {
        app.tabs.select_page(Page::Overview);
        let mut terminal = Terminal::new(TestBackend::new(150, 30)).unwrap();
        terminal.draw(|frame| render(app, frame)).unwrap();
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

    fn earning_node() -> App {
        let mut app = App::new();
        app.earnings = vec![
            LedgerEarnings {
                ledger: "ergo".to_string(),
                day: 120_000_000,
                week: 940_000_000,
                month: 3_600_000_000,
                year: 21_000_000_000,
                total: 24_500_000_000,
                refused: 0,
            },
            LedgerEarnings {
                ledger: "bitcoin".to_string(),
                day: 80_000_000,
                week: 60_000_000,
                month: 400_000_000,
                year: 1_000_000_000,
                total: 1_500_000_000,
                refused: 0,
            },
        ];
        app
    }

    /// The two charts are gone, and nothing was left behind referring to them.
    #[test]
    fn the_cpu_and_memory_history_charts_are_gone() {
        let screen = overview(&mut App::new());

        assert!(!screen.contains("CPU HISTORY"), "{screen}");
        assert!(!screen.contains("MEMORY HISTORY"), "{screen}");
        // HOST CAPACITY stays: it is where those two numbers belong, and it is the
        // reason the charts were redundant rather than merely unloved.
        assert!(screen.contains("HOST CAPACITY"), "{screen}");
    }

    #[test]
    fn the_three_summaries_are_drawn_in_their_place() {
        let screen = overview(&mut App::new());

        for panel in ["EARNINGS", "SCHEDULE", "ENERGY"] {
            assert!(screen.contains(panel), "no {panel} panel:\n{screen}");
        }
    }

    /// Summed across payment networks, which the EARNINGS page does not do: there a
    /// total would be money the operator cannot spend as one sum, but the question
    /// here is "is this node earning at all".
    #[test]
    fn earnings_are_totalled_across_payment_networks() {
        let screen = overview(&mut earning_node());

        // 0.12 + 0.08 ERG for the day.
        assert!(screen.contains("0.2 ERG"), "{screen}");
        // And the count is named, so the figure is not read as one balance.
        assert!(screen.contains("2 payment networks"), "{screen}");
    }

    /// A node nobody has paid has earned zero, which is a measurement rather than a
    /// gap -- and the panel says so instead of drawing an empty box.
    #[test]
    fn a_node_nobody_has_paid_says_so() {
        let screen = overview(&mut App::new());

        assert!(screen.contains("Nothing paid in yet"), "{screen}");
    }

    /// Refused deposits are named rather than folded in: money a client tried to pay
    /// and this node could not validate is the operator's problem to see.
    #[test]
    fn refused_deposits_take_the_place_of_the_network_count() {
        let mut app = earning_node();
        app.earnings[0].refused = 500_000_000;

        let screen = overview(&mut app);

        assert!(screen.contains("Refused"), "{screen}");
    }

    fn scheduled_node(config: &str, now: u16) -> App {
        let mut app = App::new();
        app.config_document = serde_yaml::from_str(config).ok();
        app.now_minute = now;
        app
    }

    /// "When does this flip" is the fact the SCHEDULE page exists to answer, and the
    /// one worth having without navigating: a node about to stop taking work in
    /// twenty minutes is worth knowing about before it does.
    #[test]
    fn the_schedule_panel_says_whether_it_is_open_and_when_that_changes() {
        let screen = overview(&mut scheduled_node(
            "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '08:00'\n      END: '20:00'\n  ON_CLOSE: refuse\n",
            9 * 60 + 30,
        ));

        assert!(screen.contains("OPEN"), "{screen}");
        // Readable, not "630 minutes": an operator deciding whether they have time
        // before the node closes should not be doing division.
        assert!(screen.contains("10h 30m"), "{screen}");
        assert!(screen.contains("12h a day"), "{screen}");
    }

    #[test]
    fn a_closed_node_counts_down_to_opening_instead() {
        let screen = overview(&mut scheduled_node(
            "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '08:00'\n      END: '20:00'\n  ON_CLOSE: stop\n",
            7 * 60,
        ));

        assert!(screen.contains("CLOSED"), "{screen}");
        assert!(screen.contains("Opens in"), "{screen}");
        assert!(screen.contains("1h"), "{screen}");
    }

    /// Hours that are not enforced are a different state from "open right now", and
    /// a countdown on a node with no schedule would be inventing one.
    #[test]
    fn a_node_with_no_enforced_hours_says_that_rather_than_open() {
        let screen = overview(&mut scheduled_node(
            "activity_window:\n  ENABLED: false\n",
            12 * 60,
        ));

        assert!(screen.contains("not enforced"), "{screen}");
        assert!(!screen.contains("Closes in"), "{screen}");
    }

    /// The panel reads `schedule()`, which is the draft while one is being edited --
    /// so it agrees with the SCHEDULE page rather than with disk. That is right, and
    /// it has to be labelled: a summary quietly previewing an uncommitted change
    /// would be reporting a schedule the node is not enforcing.
    #[test]
    fn an_unapplied_schedule_edit_is_shown_but_named_as_unapplied() {
        let mut app = scheduled_node(
            "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '08:00'\n      END: '20:00'\n  ON_CLOSE: refuse\n",
            9 * 60,
        );
        let mut draft = app.schedule_saved();
        draft.windows[0].end = 18 * 60;
        app.schedule_draft = Some(draft);

        let screen = overview(&mut app);

        assert!(screen.contains("unapplied edit"), "{screen}");
        assert!(screen.contains("10h a day"), "{screen}");
    }

    /// The energy source is named because it is the difference between a reading and
    /// a guess, and the two are otherwise drawn identically. A `floor` misses
    /// whatever the counter does not cover -- a discrete GPU, most of the board --
    /// and is not the machine's consumption.
    #[test]
    fn the_energy_panel_qualifies_a_partial_reading() {
        let mut app = App::new();
        app.node_energy = NodeEnergy {
            watts: Some(84.0),
            price_per_kwh: 0.21,
            currency: "EUR".to_string(),
            backend: "rapl".to_string(),
            is_floor: true,
        };

        let screen = overview(&mut app);

        assert!(screen.contains("84 W"), "{screen}");
        assert!(screen.contains("0.0176 EUR/h"), "{screen}");
        assert!(screen.contains("a floor"), "{screen}");
    }

    /// An estimate from two coefficients the operator may never have measured is
    /// drawn exactly like a measurement unless it is labelled one.
    #[test]
    fn a_modelled_wattage_is_called_an_estimate() {
        let mut app = App::new();
        app.node_energy = NodeEnergy {
            watts: Some(60.0),
            price_per_kwh: 0.0,
            currency: String::new(),
            backend: "model".to_string(),
            is_floor: false,
        };

        let screen = overview(&mut app);

        assert!(screen.contains("not measured"), "{screen}");
        // Zero is the honest default rather than a missing value: a cost from
        // somebody else's tariff is a number nobody can act on.
        assert!(screen.contains("no tariff set"), "{screen}");
    }

    #[test]
    fn an_unmeasured_node_says_unmeasured_rather_than_zero() {
        let screen = overview(&mut App::new());

        assert!(screen.contains("unmeasured"), "{screen}");
        assert!(!screen.contains("0 W"), "{screen}");
    }

    /// The panels summarise; they do not fetch. A second data path is a second thing
    /// that can be stale, and an operator comparing the front page with the EARNINGS
    /// tab has no way to tell which of the two is wrong.
    #[test]
    fn the_summaries_agree_with_the_pages_they_summarise() {
        let mut app = earning_node();
        app.config_document = serde_yaml::from_str(
            "activity_window:\n  ENABLED: true\n  WINDOWS:\n    - START: '08:00'\n      END: '20:00'\n  ON_CLOSE: refuse\n",
        )
        .ok();
        app.now_minute = 9 * 60;

        let front = overview(&mut app);

        // The same `app.earnings` the EARNINGS page reads, and the same
        // `app.schedule()` the SCHEDULE page reads -- asserted by reading the page
        // and finding the same figures rather than by trusting the call sites.
        app.tabs.select_page(Page::Earnings);
        let mut terminal = Terminal::new(TestBackend::new(150, 30)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let earnings_page: String = (0..buffer.area.height)
            .map(|row| {
                (0..buffer.area.width)
                    .map(|column| buffer.get(column, row).symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");

        // The per-network figures the page shows add up to the total the front page
        // shows. Both derive from one `app.earnings`, which is the property.
        assert!(front.contains("0.2 ERG"), "{front}");
        assert!(earnings_page.contains("0.12 ERG"), "{earnings_page}");
        assert!(earnings_page.contains("0.08 ERG"), "{earnings_page}");
    }

    /// The page still fits a small terminal. Three panels where there were two is
    /// more to lay out, and OVERVIEW is the page most likely to be left open in a
    /// split pane.
    #[test]
    fn the_overview_still_renders_on_a_small_terminal() {
        for (width, height) in [(80, 24), (100, 30), (200, 50)] {
            let mut app = earning_node();
            app.tabs.select_page(Page::Overview);
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| render(&mut app, frame)).unwrap();
        }
    }
}

/// Prints the ACTION REQUIRED banner, for the PR description.
/// `cargo test -p tui banner_preview -- --ignored --nocapture`
#[cfg(test)]
mod banner_preview {
    use super::render;
    use crate::alerts::OperatorAlert;
    use crate::app::{App, Page};
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    #[ignore]
    fn preview() {
        let mut app = App::new();
        app.tabs.select_page(Page::Overview);
        app.alerts.set(vec![
            OperatorAlert {
                key: "gateway_port_firewall",
                summary: "TCP 52285 must be open in the host firewall before this node can \
                          serve. See /opt/nodo/.gateway_notice for the exact command."
                    .to_string(),
            },
            OperatorAlert {
                key: "java_missing",
                summary: "Java is not installed, so this node cannot settle payments or \
                          publish reputation. Install it with `sudo /bin/bash \
                          /opt/nodo/bash/install_java.sh /opt/nodo`."
                    .to_string(),
            },
        ]);
        let mut terminal = Terminal::new(TestBackend::new(116, 12)).unwrap();
        terminal.draw(|frame| render(&mut app, frame)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        for row in 0..9 {
            let line: String = (0..buffer.area.width)
                .map(|column| buffer.get(column, row).symbol())
                .collect();
            println!("{}", line.trim_end());
        }
    }
}
