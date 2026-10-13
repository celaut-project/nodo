# Profile deck: first-run profile picker in the TUI

Status: phases 1 to 5 (state, data, static render, navigation, animation) are implemented. Phase 6 is not.

## Goal

The first time the user opens the TUI, the TUI asks the user to accept the KyA.
After that, the TUI shows a profile picker. The picker has a cyberpunk, retro-game design.

- Each profile is one full-modal card.
- A character shows the profile. The profile data is on the right side of the card.
- The user moves to the next or the previous card with the keys or with the mouse.
- The cards slide to the left and to the right.
- The user can skip the picker. The SKIP button is always visible.

## Audit: the code as it is

Paths are relative to `src/commands/tui/src`.

### Stack

- The TUI is Rust. It uses `ratatui 0.26` and `crossterm 0.27`.
- `main.rs` runs the loop. It draws, then waits for one `Event`.
- `event.rs` sends a `Tick` event each 250 ms. This is too slow for an animation.
- `app.rs` holds the state. `InputMode` is the modal state (`app.rs:477`).
- `ui.rs` draws the modals. `handler.rs` handles the keys and the mouse.

### KyA gate

- `with_kya_gate()` in `app.rs` opens `InputMode::AcceptKya` if the marker file is absent.
- The marker is `<storage>/.acceptedkya` (`Paths::kya_marker()`, `app.rs:2284`).
- `handler.rs:117-122` handles the answer. `accept_kya()` (`app.rs:3656`) creates the marker.
- The test `main_asks_the_kya_before_it_draws_anything` pins the call order in `main.rs`.

### Profiles

- `cell.rs:1382` defines `Profile { id, label, blurb, writes }`.
- `cell.rs:1392` defines `PROFILES`. There are five: `just-me`, `cautious`, `open-renter`, `lan-lab`, `workbench`.
- `config_edit.py:79` has a copy of the list. `tests/test_commands_config_edit.py` fails if the two lists differ.
- The active profile is not stored. `cell::closest_profile` computes it from `config.yaml`.
- `InputMode::PickProfile` shows a list popup (`draw_profile_popup` in `ui.rs`).
- `submit_profile_selection` then opens `InputMode::ConfirmWrites`. This mode shows the diff and starts the config transaction.
- Without root, the transaction stages the change in `config.pending.yaml`.

## Design

### Rules

- Do not change `Profile` in `cell.rs`. The Python copy and its test stay the same.
- Keep the presentation data in a new module, `profile_deck.rs`.
- Use the existing `ConfirmWrites` flow to write the config. There is no second write path.

### First-run flow

```
start -> KyA marker missing?     -> AcceptKya (y)
      -> picker marker missing?  -> PickProfile (first_run = true)
      -> Enter -> write picker marker -> ConfirmWrites -> apply -> Normal
      -> Esc / SKIP             -> write picker marker, no change -> Normal
```

- The new marker is `<storage>/.profilepicked`. It is an empty file, like `.acceptedkya`.
- The TUI writes the marker when the user chooses a profile, before the diff. If the user then declines the diff, the picker does not show again.
- The picker shows if the marker is absent. This includes users who accepted the KyA before this feature.
- If the user closes the TUI before the choice, the picker shows again at the next start.
- If the TUI cannot write the marker, it shows a status line. The picker shows again at the next start.
- If the user declines the KyA, the TUI quits. This does not change.
- The user can open the deck again from the POLICIES page. Then `first_run = false`. Esc returns and writes nothing.

### Screen

```
╔═[ SELECT YOUR RIG ]══════════════════════════ 2/5 ═╗
║ ◀                                                 ▶ ║
║   character sprite     ┌─ CAUTIOUS RENTER ───────┐  ║
║   (half-block RGB)     │ CLASS: Fixer            │  ║
║                        │ "short leash"           │  ║
║                        │ EXPOSURE  ▮▮▯▯▯         │  ║
║                        │ INCOME    ▮▮▮▯▯         │  ║
║                        │ 28 keys will change     │  ║
║                        └─────────────────────────┘  ║
║  ○ ● ○ ○ ○       [ ENTER: JACK IN ]   [ ESC: SKIP ] ║
╚═════════════════════════════════════════════════════╝
```

- The card uses all the modal area. The sprite takes about 40 % of the width.
- The sprite uses the half-block characters `▀`, `▄` and `█` with RGB colours. One cell shows two vertical pixels.
- The deck has its own neon palette. It does not use the global theme, because it is an identity screen.
- The frame is a double line. The title blinks. A faint scanline pattern shows in the background.

| Profile | Character | Neon colour |
|---|---|---|
| `just-me` | Ronin hermit | green |
| `cautious` | Fixer | yellow |
| `open-renter` | Netrunner trader | magenta |
| `lan-lab` | Ripperdoc technician | cyan |
| `workbench` | Tinkerer hacker | orange |

### Card data

- `profile_deck.rs` has a table `Persona { id, codename, class, tagline, palette, sprite }`.
- A test makes sure each id in `cell::profiles()` has one persona. A new profile without a character fails the test.
- The card shows `label` and `blurb` from `Profile`.
- The card shows five attribute bars: EXPOSURE, INCOME, OPENNESS, ISOLATION and RISK.
- A pure function computes the bars from the `writes` of the profile. For example, EXPOSURE is high when `network.DISABLE_EXPOSE_OUTSIDE` is `false`.
- A test fixes the bars of the five profiles. A change to a profile that changes a bar fails the test.
- The card shows 3 or 4 key traits taken from the `writes`.
- The card shows how many keys change from the current `config.yaml`. It uses `cell::report`.

### Input

| Input | Action |
|---|---|
| Left, Right, Up, Down, `h`, `l`, Tab, Shift+Tab | Previous or next card (wraps) |
| `1` to `9`, Home, End | Go to the card |
| Enter | Choose the profile, open `ConfirmWrites` |
| Esc, `s` | Skip |
| Click on the arrows or on the dots | Previous, next or go to the card (a dot is three cells wide to hit) |
| Click on the card or on the choose button | Choose the profile |
| Mouse wheel (vertical or horizontal) | Previous or next card |

### Animation

- The state is `deck { index, from, started }`. The slide takes about 200 ms with easing.
- The deck renders each card into a temporary `Buffer`. It then copies the buffer with a horizontal offset, cut to the modal area.
- During an animation, the event loop ticks each 33 ms. At rest, it ticks each 250 ms. The idle cost does not change.
- If the user presses a key during a slide, the slide jumps to its end. The new move starts after that.

### Limits and fallbacks

- If the terminal is smaller than about 72x22 cells, the TUI shows the existing list popup.
- If `COLORTERM` does not show truecolor, the sprite uses the 256-colour palette.
- If `NO_COLOR` is set, the sprite is monochrome.
- Without root, the change is staged. The `ConfirmWrites` text already says this.

## Phases

1. **State and flow.** Add `Paths::profile_marker()` and `with_profile_gate()`. Add `first_run` to the picker mode. Add a test for the call order KyA, then picker.
2. **Presentation data.** Add `profile_deck.rs` with personas, sprites and the derived bars. Add the coverage tests.
3. **Static render.** Replace `draw_profile_popup` with the deck. Keep the list as a fallback. Add the deck to `responsive_sweep.rs`.
4. **Navigation.** Add the key and mouse handlers. Register the click zones.
5. **Animation.** Add the adaptive tick and the buffer copy.
6. **Polish and docs.** Add the blink, the scanlines and the colour fallback. Update `src/commands/tui/README.md` and `docs/QUICK_GUIDE.md`.

## Risks

- `app.rs` and `ui.rs` are very large. Put all new code in new modules.
- The five sprites take the most time. Each sprite is about 16x14 cells and is drawn by hand.
- On first run, the node can have no `config.yaml`. Check what the config transaction does in this case before phase 3.
