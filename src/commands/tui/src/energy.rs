//! The ENERGY page's catalogue: what the machine costs to run, and where that
//! figure is allowed to come from (issue #395).
//!
//! The `energy:` block is editable on the Config page already. This page exists
//! because the keys are meaningless without the comments beside them in
//! `config.example.yaml`, and a YAML tree cannot show a comment: `IDLE_WATTS` must
//! be *measured* rather than guessed, and the five sources are tried in a fixed
//! order rather than being options to tick.
//!
//! Nothing here writes YAML. `Enter` opens the same `EditConfig` popup the Config
//! page opens, so there is one writer and one transaction.
//!
//! Mirrors the `energy:` block of `config.example.yaml`; a key added there and not
//! here is invisible on this page and still reachable on Config.

use crate::app::{ConfigPathSegment, EditKind, Identifiable};

/// One editable key of the `energy:` block.
///
/// `help` is the part that cannot be got anywhere else, and it is deliberately a
/// sentence rather than a restatement of the key name: "Watts at 0% CPU" is what the
/// key is called, and "measure it, do not estimate it" is the thing an operator needs
/// to be told.
#[derive(Debug, Clone)]
pub struct EnergyEntry {
    /// Dotted path into config.yaml, e.g. `energy.IDLE_WATTS`. Also the row id, which
    /// is what keeps a selection on the key the operator picked across a refresh.
    pub path: &'static str,
    /// What the row is called on screen. Short: the key itself is shown beside it.
    pub label: &'static str,
    /// Why this key exists and what setting it wrongly does, in the help panel.
    pub help: &'static str,
    /// Which editor widget `Enter` opens. Fixed per key rather than inferred from the
    /// YAML type, because the type of what is *currently* written is not the type the
    /// key takes: `PRICE_PER_KWH: 0` parses as an integer, and a key whose value is an
    /// empty string (`SMART_PLUG_URL`) says nothing about what belongs in it.
    pub kind: EditKindSpec,
}

/// A description of the editor a key wants, in a form that can live in a `const`.
///
/// [`EditKind`] carries a `Vec<String>` for its enum case and so cannot be built in a
/// static table. This is the same three-way choice plus a closed option set as a
/// `&'static [&'static str]`, converted on demand.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EditKindSpec {
    Bool,
    Number,
    Text,
    Enum(&'static [&'static str]),
}

impl EditKindSpec {
    pub fn to_edit_kind(self) -> EditKind {
        match self {
            EditKindSpec::Bool => EditKind::Bool,
            EditKindSpec::Number => EditKind::Number,
            EditKindSpec::Text => EditKind::Text,
            EditKindSpec::Enum(options) => {
                EditKind::Enum(options.iter().map(|value| value.to_string()).collect())
            }
        }
    }
}

impl Identifiable for EnergyEntry {
    fn id(&self) -> &str {
        self.path
    }
}

impl EnergyEntry {
    /// Where this key lives in config.yaml, as the segments the `yq` writer wants.
    ///
    /// Goes through [`crate::cell::path_segments`] rather than splitting the path
    /// here, so this page's writes are built by the same code every other page's are.
    pub fn config_path(&self) -> Vec<ConfigPathSegment> {
        crate::cell::path_segments(self.path)
    }

    /// The key without its `energy.` prefix, for a table that has already said which
    /// block it is editing.
    pub fn key(&self) -> &'static str {
        self.path.strip_prefix("energy.").unwrap_or(self.path)
    }
}

/// A band of the page: keys that answer the same question, kept together.
///
/// The `energy:` block is not a flat dozen settings. It is "is this on and what is a
/// kWh worth", then "what to assume when nothing can be read", then five mutually
/// exclusive places a real reading can come from. Reading it as one list is how an
/// operator ends up filling in `IDLE_WATTS` on a machine that has a metering plug —
/// harmless, but it means the two numbers on screen come from different worlds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnergySection {
    /// Whether to measure at all, how often, and what a kWh costs.
    Metering,
    /// The idle + linear-in-CPU% estimate, used when nothing can be read.
    Model,
    /// Hardware and endpoints that report real watts.
    Sources,
}

impl EnergySection {
    pub fn title(self) -> &'static str {
        match self {
            EnergySection::Metering => "METERING",
            EnergySection::Model => "MODEL FALLBACK",
            EnergySection::Sources => "MEASURED SOURCES",
        }
    }

    /// One sentence on what the whole band is for, shown above its rows.
    pub fn blurb(self) -> &'static str {
        match self {
            EnergySection::Metering => {
                "Informational only: watts and a currency cost. Never feeds MU pricing."
            }
            EnergySection::Model => {
                "Used only when nothing above can be read. Measure these; do not estimate."
            }
            EnergySection::Sources => {
                "Tried in this order, each before RAPL. All off by default."
            }
        }
    }
}

/// Every row of the ENERGY page, in the order the block is written in
/// `config.example.yaml` — which is also the order the node tries the sources in,
/// and that order is load-bearing (see [`EnergySection::Sources`]).
pub fn entries() -> &'static [(EnergySection, EnergyEntry)] {
    use EditKindSpec::{Bool, Enum, Number, Text};
    use EnergySection::{Metering, Model, Sources};
    &[
        (
            Metering,
            EnergyEntry {
                path: "energy.ENABLED",
                label: "Measure at all",
                help: "Master switch. Off means no samples are taken and the Overview \
                       power and electricity lines read as unmeasured rather than zero. \
                       Nothing here affects what the node charges: energy is reported, \
                       never priced (issue #258).",
                kind: Bool,
            },
        ),
        (
            Metering,
            EnergyEntry {
                path: "energy.SAMPLE_INTERVAL_SECONDS",
                label: "Sample every",
                help: "Seconds between samples. Sampling runs on the maintenance loop, \
                       so a short interval spends that loop's time on reading meters; a \
                       long one averages away the peaks a spiky workload is made of.",
                kind: Number,
            },
        ),
        (
            Metering,
            EnergyEntry {
                path: "energy.PRICE_PER_KWH",
                label: "Price per kWh",
                help: "What a kilowatt-hour costs you, in CURRENCY. 0 means show watts \
                       and omit the money entirely, which is the honest default: a cost \
                       computed from somebody else's tariff is a number nobody can act \
                       on. Persisted with each sample, so changing it does not rewrite \
                       history.",
                kind: Number,
            },
        ),
        (
            Metering,
            EnergyEntry {
                path: "energy.CURRENCY",
                label: "Currency",
                help: "Presentation only. Nothing converts between this and the ledger's \
                       money, or between it and MU — an electricity bill is not paid in \
                       the unit a node earns in.",
                kind: Text,
            },
        ),
        (
            Metering,
            EnergyEntry {
                path: "energy.PRICE_SOURCE",
                label: "Price source",
                help: "Where the kWh price comes from. Only \"fixed\" is implemented, \
                       which is the one that works offline. Listed rather than hidden \
                       because it is the seam a spot-price feed would be added at.",
                kind: Enum(&["fixed"]),
            },
        ),
        (
            Model,
            EnergyEntry {
                path: "energy.IDLE_WATTS",
                label: "Idle watts",
                help: "What the machine pulls at 0% CPU. MEASURE THIS WITH A PLUG-IN \
                       METER: read the wall once with the machine quiet, and that is the \
                       figure. 0 disables the model entirely, which is better than a \
                       guess — a guessed wattage is drawn exactly like a measured one, \
                       and an order of magnitude out on a Raspberry Pi.",
                kind: Number,
            },
        ),
        (
            Model,
            EnergyEntry {
                path: "energy.LOAD_WATTS",
                label: "Extra watts at full load",
                help: "The difference between the quiet reading and the reading with \
                       every core busy — not the busy reading itself. 0 falls back to \
                       the packages' own declared long-term power limit, when sysfs \
                       exposes one.",
                kind: Number,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.SMART_PLUG_URL",
                label: "Smart plug URL",
                help: "The only source that reads the wall socket itself, losses and \
                       all. Shelly Gen1 http://<ip>/meter/0, Shelly Gen2 \
                       http://<ip>/rpc/Switch.GetStatus?id=0, Tasmota \
                       http://<ip>/cm?cmnd=Status%208. It measures whatever is plugged \
                       into it, so a power strip reports the strip, not this machine.",
                kind: Text,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.SMART_PLUG_POWER_PATH",
                label: "Smart plug JSON path",
                help: "Where the watts sit in the JSON the plug answers with: \"power\" \
                       for Shelly Gen1, \"apower\" for Gen2/Plus, \
                       \"StatusSNS.ENERGY.Power\" for Tasmota.",
                kind: Text,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.IPMI_ENABLED",
                label: "Server BMC (IPMI)",
                help: "`ipmitool dcmi power reading`: what the power supply pulls from \
                       the wall, as the board's own management controller sees it. Needs \
                       ipmitool and access to the IPMI device, so there is nothing to \
                       enable on a laptop or a consumer desktop.",
                kind: Bool,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.HWMON_CHIP",
                label: "hwmon chip",
                help: "A /sys/class/hwmon/hwmonN/name. The only power figure Apple \
                       Silicon under Asahi (chip \"macsmc\") and most ARM boards offer. \
                       What a rail covers is a property of the board and cannot be \
                       guessed.",
                kind: Text,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.HWMON_SENSOR",
                label: "hwmon sensor",
                help: "A sensor prefix within that chip: \"power1\" reads microwatts, \
                       \"energy1\" a microjoule counter — prefer the counter. Do not \
                       point it at the battery: that measures discharge and reads zero \
                       on mains.",
                kind: Text,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.NVML_ENABLED",
                label: "GPUs (nvidia-smi)",
                help: "RAPL never sees a discrete GPU, so this one ADDS to a partial \
                       reading instead of replacing it — and is skipped when the figure \
                       already covers the whole machine, which would otherwise count \
                       the GPU twice.",
                kind: Bool,
            },
        ),
        (
            Sources,
            EnergyEntry {
                path: "energy.EXTERNAL_TIMEOUT_SECONDS",
                label: "Source timeout",
                help: "Ceiling, per sample, on the subprocess and HTTP sources above. \
                       They run on the maintenance loop, so this is wall time that loop \
                       gives up when a plug is unreachable or a BMC is slow.",
                kind: Number,
            },
        ),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every key on the page is a key the shipped configuration actually has.
    ///
    /// The failure this prevents is silent: a row for a key that does not exist would
    /// look perfectly ordinary, read as `(not set)`, and on `Enter` write a brand new
    /// key into `energy:` that the node has never heard of.
    #[test]
    fn every_catalogued_key_exists_in_the_example_config() {
        let example = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../config.example.yaml"
        ))
        .expect("config.example.yaml ships with the repository");
        let block = example
            .split_once("\nenergy:\n")
            .expect("config.example.yaml has an energy: block")
            .1;
        // The next top-level key ends the block: a line that starts in column zero.
        let block: String = block
            .lines()
            .take_while(|line| line.starts_with(' ') || line.trim().is_empty())
            .collect::<Vec<_>>()
            .join("\n");

        for (_, entry) in entries() {
            assert!(
                block.contains(&format!("{}:", entry.key())),
                "{} is on the ENERGY page but not in config.example.yaml's energy: block",
                entry.path
            );
        }
    }

    /// And the other direction: a key added to the block is a key this page has to
    /// grow a row for, or it is invisible here.
    #[test]
    fn the_catalogue_covers_every_key_of_the_example_block() {
        let example = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../config.example.yaml"
        ))
        .expect("config.example.yaml ships with the repository");
        let block = example
            .split_once("\nenergy:\n")
            .expect("config.example.yaml has an energy: block")
            .1;
        let catalogued: Vec<&str> = entries().iter().map(|(_, entry)| entry.key()).collect();

        for line in block.lines() {
            if !line.starts_with("  ") {
                break;
            }
            let trimmed = line.trim_start();
            if trimmed.starts_with('#') || trimmed.is_empty() {
                continue;
            }
            let Some((key, _)) = trimmed.split_once(':') else {
                continue;
            };
            assert!(
                catalogued.contains(&key),
                "energy.{key} is in config.example.yaml but has no row on the ENERGY page"
            );
        }
    }

    /// Every path is under `energy.`, which is what the page claims to edit. A stray
    /// path would write somewhere else entirely, through a perfectly ordinary-looking
    /// row.
    #[test]
    fn every_path_is_inside_the_energy_block() {
        for (_, entry) in entries() {
            assert!(
                entry.path.starts_with("energy."),
                "{} is not an energy key",
                entry.path
            );
            assert_eq!(
                entry.config_path().len(),
                2,
                "{} should be exactly energy.<KEY>",
                entry.path
            );
        }
    }

    /// Rows are grouped, and the groups do not interleave: the page draws one block
    /// per section, so a row in the wrong place would be drawn under the wrong
    /// heading.
    #[test]
    fn sections_are_contiguous() {
        let mut seen: Vec<EnergySection> = Vec::new();
        for (section, _) in entries() {
            if seen.last() != Some(section) {
                assert!(
                    !seen.contains(section),
                    "{:?} appears in two separate runs",
                    section
                );
                seen.push(*section);
            }
        }
    }
}
