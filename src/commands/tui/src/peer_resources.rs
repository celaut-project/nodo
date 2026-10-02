//! What each peer announced it can run, per architecture, and the sum of it all
//! (issue #455).
//!
//! A peer's `Peer.resources` (#459) is one `ArchitectureResources` per architecture
//! it can boot: the most one instance of that architecture could be granted there --
//! cores as a CFS pair, memory, disk -- plus its measured per-core `benchmark`
//! scores. The node keeps the signed announcement verbatim in `peer.advertisement`,
//! and the TUI already decodes that blob for the reputation proofs, so this reads the
//! same bytes in the same pass rather than asking for anything new.
//!
//! The Overview total is an OPTIMISTIC UPPER BOUND, never availability:
//!
//! * it is each peer's announced ceiling summed, per architecture -- cores on amd64
//!   and cores on arm64 are different things, so they are never added together;
//! * a ceiling is what the machine *has*, not what is free on it right now, and one
//!   instance per peer is assumed to be able to take all of it;
//! * benchmark scores are not summed or maxed into it: they are per-core rates, and
//!   "N cores, best score X" would read as N cores each at X, which no peer said.
//!   They are shown per peer, in its detail card, where they mean what they say;
//! * this node's own *announcement* is in it -- what `nodo resources --json` reports,
//!   the same `Peer.resources` this node signs for its peers -- so the total is what
//!   the node can reach, itself included. An announcement beside announcements: the
//!   live machine is still HOST CAPACITY's, and is never added to them.

use crate::app::protos;
use std::collections::BTreeMap;

/// The architecture aliases the node normalises a tag through
/// (`src/utils/arch_guard.py::ARCH_ALIASES`), so `x86_64` from one peer and
/// `linux/amd64` from another land in the same row of the total.
const ARCH_ALIASES: [(&str, &str); 7] = [
    ("linux/amd64", "linux/amd64"),
    ("amd64", "linux/amd64"),
    ("x86_64", "linux/amd64"),
    ("linux/arm64", "linux/arm64"),
    ("arm64", "linux/arm64"),
    ("arm_64", "linux/arm64"),
    ("aarch64", "linux/arm64"),
];

/// The canonical tag the first recognised entry of `tags` names -- the same rule as
/// `arch_guard.arch_from_tags`. An architecture nodo has no table for keeps its own
/// first tag (lower-cased), so it is still shown and still summed only with itself;
/// no tags at all reads as `unspecified`.
pub fn canonical_arch(tags: &[String]) -> String {
    for tag in tags {
        let normalized = tag.trim().to_lowercase();
        if let Some((_, canonical)) = ARCH_ALIASES.iter().find(|(alias, _)| *alias == normalized) {
            return canonical.to_string();
        }
    }
    tags.iter()
        .map(|tag| tag.trim().to_lowercase())
        .find(|tag| !tag.is_empty())
        .unwrap_or_else(|| "unspecified".to_string())
}

/// `linux/amd64` as `amd64`: the OS prefix is the same on every architecture nodo
/// boots, and a narrow card has no room to repeat it.
pub fn short_arch(arch: &str) -> &str {
    arch.strip_prefix("linux/").unwrap_or(arch)
}

/// One architecture a peer announced, read off its `ArchitectureResources`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ArchOffer {
    /// Canonical tag (see [`canonical_arch`]).
    pub arch: String,
    /// `cpu_quota / cpu_period` in thousandths of a core, so the total is integer
    /// arithmetic and two halves sum to exactly one. `None` when the pair is missing
    /// or either half is 0 -- "not stated", never 0 cores.
    pub millicores: Option<u64>,
    pub mem_bytes: Option<u64>,
    pub disk_bytes: Option<u64>,
    /// Measured per-core scores, by key, in the order the peer sent them. Entries
    /// with no value are dropped: they say nothing.
    pub benchmark: Vec<(String, u64)>,
}

impl ArchOffer {
    /// Whether the peer left any of cores, memory or disk unstated for this
    /// architecture. Such a limit adds nothing to the total, which therefore
    /// understates that column -- worth saying beside the figure.
    pub fn is_partial(&self) -> bool {
        self.millicores.is_none() || self.mem_bytes.is_none() || self.disk_bytes.is_none()
    }
}

/// What a peer's stored announcement says about its resources.
#[derive(Debug, Clone, PartialEq, Default)]
pub enum Announced {
    /// No announcement stored, or one with no `resources` -- a node that predates
    /// #459, or one choosing not to say. Contributes nothing, and is counted.
    #[default]
    Undeclared,
    /// The stored bytes are not a `Peer` this build can decode.
    Unreadable,
    Declared(Vec<ArchOffer>),
}

/// `cpu_quota / cpu_period` in millicores, or `None` when either half is absent or
/// zero. u128 inside, so a hostile quota near `u64::MAX` cannot overflow the
/// multiplication; the result saturates rather than wraps.
pub fn millicores(period: Option<u64>, quota: Option<u64>) -> Option<u64> {
    match (period, quota) {
        (Some(period), Some(quota)) if period > 0 && quota > 0 => {
            let milli = (quota as u128 * 1000) / period as u128;
            Some(u64::try_from(milli).unwrap_or(u64::MAX))
        }
        _ => None,
    }
}

/// An absent value and an explicit 0 both mean "not stated" for a limit.
fn stated(value: Option<u64>) -> Option<u64> {
    value.filter(|value| *value > 0)
}

/// The resources a decoded `Peer` announced.
pub fn announced_resources(peer: &protos::Peer) -> Announced {
    if peer.resources.is_empty() {
        return Announced::Undeclared;
    }
    let mut offers: Vec<ArchOffer> = Vec::new();
    for entry in &peer.resources {
        let arch = canonical_arch(
            entry
                .architecture
                .as_ref()
                .map(|architecture| architecture.tags.as_slice())
                .unwrap_or(&[]),
        );
        // One row per architecture: a peer that (wrongly) lists one twice under two
        // aliases does not get to count its machine twice. The first entry wins.
        if offers.iter().any(|offer| offer.arch == arch) {
            continue;
        }
        let resources = entry.resources.clone().unwrap_or_default();
        offers.push(ArchOffer {
            arch,
            millicores: millicores(resources.cpu_period, resources.cpu_quota),
            mem_bytes: stated(resources.mem_limit),
            disk_bytes: stated(resources.disk_space),
            benchmark: resources
                .benchmark
                .iter()
                .filter_map(|entry| entry.value.map(|value| (entry.key.clone(), value)))
                .collect(),
        });
    }
    Announced::Declared(offers)
}

/// What an already-decoded `peer.advertisement` says. NULL (`None`) is a peer we
/// hold no announcement for, which says nothing -- undeclared, not unreadable.
pub fn from_decoded(decoded: Option<&Result<protos::Peer, prost::DecodeError>>) -> Announced {
    match decoded {
        None => Announced::Undeclared,
        Some(Ok(peer)) => announced_resources(peer),
        Some(Err(_)) => Announced::Unreadable,
    }
}

/// [`from_decoded`] straight from a `peer.advertisement` blob.
pub fn decode_advertisement(bytes: Option<&[u8]>) -> Announced {
    use prost::Message;
    from_decoded(bytes.map(protos::Peer::decode).as_ref())
}

/// This node's own announcement, as `nodo resources --json` last reported it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct OwnResources {
    /// `None` until the first report lands.
    pub announced: Option<Announced>,
    /// Why the last read failed; the previous announcement, if any, stays.
    pub error: String,
}

/// The report line of `nodo resources --json`: `{"peer": "<base64 Peer>"}`, the
/// serialized `Peer` carrying only `resources`, so it is decoded exactly like a
/// peer's stored advertisement. `{"error": ...}` is the command's own failure.
pub fn parse_own_resources(line: &str) -> Result<Announced, String> {
    use base64::Engine;
    let report: serde_json::Value =
        serde_json::from_str(line).map_err(|error| format!("Unreadable resources report: {error}"))?;
    if let Some(error) = report.get("error").and_then(|error| error.as_str()) {
        return Err(error.to_string());
    }
    let encoded = report
        .get("peer")
        .and_then(|peer| peer.as_str())
        .ok_or_else(|| "Resources report has no announcement".to_string())?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|error| format!("Unreadable resources report: {error}"))?;
    Ok(decode_advertisement(Some(&bytes)))
}

/// One architecture's row of the total.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ArchTotal {
    /// Nodes that announced this architecture at all: peers, plus this node when
    /// its own announcement is in the total.
    pub peers: usize,
    pub millicores: u64,
    pub mem_bytes: u64,
    pub disk_bytes: u64,
    /// Of `peers`, how many left cores, memory or disk unstated (see
    /// [`ArchOffer::is_partial`]).
    pub partial: usize,
}

/// The optimistic upper bound on what this node could reach through its peers.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct PotentialResources {
    /// Per canonical architecture, in tag order so the card does not reshuffle.
    pub per_arch: BTreeMap<String, ArchTotal>,
    /// Peers only; this node is [`PotentialResources::own_included`].
    pub declared: usize,
    pub undeclared: usize,
    pub unreadable: usize,
    /// Whether this node's own announced ceilings are in `per_arch`.
    pub own_included: bool,
}

impl PotentialResources {
    pub fn peers(&self) -> usize {
        self.declared + self.undeclared + self.unreadable
    }
}

/// Sum every peer's announced ceilings, per architecture. Saturating: a sum of
/// announcements is a sum of claims, and one absurd claim must not wrap the total
/// round to a small, believable number.
pub fn aggregate<'a>(announcements: impl IntoIterator<Item = &'a Announced>) -> PotentialResources {
    let mut total = PotentialResources::default();
    for announced in announcements {
        match announced {
            Announced::Undeclared => total.undeclared += 1,
            Announced::Unreadable => total.unreadable += 1,
            Announced::Declared(offers) => {
                total.declared += 1;
                add_offers(&mut total, offers);
            }
        }
    }
    total
}

/// [`aggregate`] with this node's own announcement added in, when it declared one:
/// what this node can reach, itself included. Its own silence is not a peer's, so it
/// is never counted as undeclared or unreadable -- the card says it is missing instead.
pub fn aggregate_with_own<'a>(
    own: Option<&Announced>,
    peers: impl IntoIterator<Item = &'a Announced>,
) -> PotentialResources {
    let mut total = aggregate(peers);
    if let Some(Announced::Declared(offers)) = own {
        add_offers(&mut total, offers);
        total.own_included = true;
    }
    total
}

fn add_offers(total: &mut PotentialResources, offers: &[ArchOffer]) {
    for offer in offers {
        let row = total.per_arch.entry(offer.arch.clone()).or_default();
        row.peers += 1;
        row.millicores = row.millicores.saturating_add(offer.millicores.unwrap_or(0));
        row.mem_bytes = row.mem_bytes.saturating_add(offer.mem_bytes.unwrap_or(0));
        row.disk_bytes = row.disk_bytes.saturating_add(offer.disk_bytes.unwrap_or(0));
        if offer.is_partial() {
            row.partial += 1;
        }
    }
}

/// Millicores as cores: `12`, `2.5`, `0.25`. Never rounds a fraction up to a whole
/// core, and drops the trailing zeros a quota of exactly N cores would print.
pub fn format_cores(millicores: u64) -> String {
    let whole = millicores / 1000;
    let fraction = millicores % 1000;
    if fraction == 0 {
        return whole.to_string();
    }
    let digits = format!("{fraction:03}");
    format!("{whole}.{}", digits.trim_end_matches('0'))
}

/// A benchmark score for reading: `1.2M int ops/s`, `8.4 GiB/s @ 64 MiB`. A key this
/// build does not know is shown as it is, never hidden -- the peer said it.
pub fn format_benchmark(key: &str, value: u64) -> (String, String) {
    if let Some(set) = key
        .strip_prefix("mem_bandwidth_")
        .and_then(|rest| rest.strip_suffix("_bytes_per_sec"))
    {
        return (
            format!("mem bw @ {}", working_set_label(set)),
            format!("{}/s", crate::app::format_bytes(value)),
        );
    }
    let label = match key {
        "int_ops_per_sec" => "int ops/s",
        "flt_ops_per_sec" => "float ops/s",
        "sha256_hashes_per_sec" => "sha256/s",
        other => return (other.to_string(), value.to_string()),
    };
    (label.to_string(), format_count(value))
}

/// `64mib` as `64 MiB`; anything else unchanged.
fn working_set_label(set: &str) -> String {
    for (suffix, unit) in [("kib", "KiB"), ("mib", "MiB"), ("gib", "GiB")] {
        if let Some(number) = set.strip_suffix(suffix) {
            if !number.is_empty() && number.chars().all(|c| c.is_ascii_digit()) {
                return format!("{number} {unit}");
            }
        }
    }
    set.to_string()
}

/// A count in SI steps: `950`, `12.3k`, `4.5M`, `1.2G`.
fn format_count(value: u64) -> String {
    const UNITS: [&str; 5] = ["", "k", "M", "G", "T"];
    let mut scaled = value as f64;
    let mut unit = 0;
    while scaled >= 1000.0 && unit < UNITS.len() - 1 {
        scaled /= 1000.0;
        unit += 1;
    }
    if unit == 0 {
        value.to_string()
    } else {
        format!("{scaled:.1}{}", UNITS[unit])
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use prost::Message;

    pub(crate) fn entry(tags: &[&str], resources: protos::Sysresources) -> protos::ArchitectureResources {
        protos::ArchitectureResources {
            architecture: Some(protos::service::container::Architecture {
                tags: tags.iter().map(|tag| tag.to_string()).collect(),
                ..Default::default()
            }),
            resources: Some(resources),
        }
    }

    pub(crate) fn sys(cores: u64, mem: u64, disk: u64) -> protos::Sysresources {
        protos::Sysresources {
            cpu_period: Some(100_000),
            cpu_quota: Some(cores * 100_000),
            mem_limit: Some(mem),
            disk_space: Some(disk),
            ..Default::default()
        }
    }

    pub(crate) fn advertisement(resources: Vec<protos::ArchitectureResources>) -> Vec<u8> {
        protos::Peer { resources, ..Default::default() }.encode_to_vec()
    }

    const GIB: u64 = 1 << 30;

    #[test]
    fn aliases_land_on_one_canonical_architecture() {
        let tags = |list: &[&str]| list.iter().map(|tag| tag.to_string()).collect::<Vec<_>>();
        assert_eq!(canonical_arch(&tags(&["linux/amd64", "amd64", "x86_64"])), "linux/amd64");
        assert_eq!(canonical_arch(&tags(&["X86_64"])), "linux/amd64");
        assert_eq!(canonical_arch(&tags(&["something", "aarch64"])), "linux/arm64");
        assert_eq!(canonical_arch(&tags(&["RISCV64"])), "riscv64");
        assert_eq!(canonical_arch(&[]), "unspecified");
        assert_eq!(short_arch("linux/arm64"), "arm64");
        assert_eq!(short_arch("riscv64"), "riscv64");
    }

    #[test]
    fn cores_come_from_the_cfs_pair_without_overflow() {
        assert_eq!(millicores(Some(100_000), Some(400_000)), Some(4000));
        assert_eq!(millicores(Some(100_000), Some(50_000)), Some(500));
        assert_eq!(millicores(Some(3), Some(1)), Some(333));
        // Either half missing or zero is "not stated", not zero cores.
        assert_eq!(millicores(None, Some(100_000)), None);
        assert_eq!(millicores(Some(100_000), None), None);
        assert_eq!(millicores(Some(0), Some(100_000)), None);
        assert_eq!(millicores(Some(100_000), Some(0)), None);
        // quota * 1000 overflows u64; the u128 path does not, and the result saturates.
        assert_eq!(millicores(Some(1), Some(u64::MAX)), Some(u64::MAX));
        assert_eq!(millicores(Some(u64::MAX), Some(u64::MAX)), Some(1000));
    }

    #[test]
    fn cores_print_without_rounding_up() {
        assert_eq!(format_cores(12_000), "12");
        assert_eq!(format_cores(2_500), "2.5");
        assert_eq!(format_cores(250), "0.25");
        assert_eq!(format_cores(333), "0.333");
        assert_eq!(format_cores(0), "0");
    }

    #[test]
    fn decoding_tells_undeclared_from_unreadable() {
        assert_eq!(decode_advertisement(None), Announced::Undeclared);
        // An empty blob is a valid, empty `Peer`: it says nothing.
        assert_eq!(decode_advertisement(Some(&[])), Announced::Undeclared);
        assert_eq!(
            decode_advertisement(Some(&advertisement(Vec::new()))),
            Announced::Undeclared
        );
        assert_eq!(decode_advertisement(Some(&[0xff, 0xff, 0xff])), Announced::Unreadable);
        let declared = decode_advertisement(Some(&advertisement(vec![entry(
            &["linux/amd64", "x86_64"],
            protos::Sysresources {
                benchmark: vec![
                    protos::Uint64KeyValue { key: "int_ops_per_sec".into(), value: Some(500_000) },
                    protos::Uint64KeyValue { key: "flt_ops_per_sec".into(), value: None },
                ],
                ..sys(4, 8 * GIB, 100 * GIB)
            },
        )])));
        assert_eq!(
            declared,
            Announced::Declared(vec![ArchOffer {
                arch: "linux/amd64".into(),
                millicores: Some(4000),
                mem_bytes: Some(8 * GIB),
                disk_bytes: Some(100 * GIB),
                benchmark: vec![("int_ops_per_sec".into(), 500_000)],
            }])
        );
    }

    #[test]
    fn a_repeated_architecture_counts_once_per_peer() {
        let announced = decode_advertisement(Some(&advertisement(vec![
            entry(&["linux/amd64"], sys(4, GIB, GIB)),
            entry(&["x86_64"], sys(64, GIB, GIB)),
        ])));
        let Announced::Declared(offers) = announced else { panic!("declared") };
        assert_eq!(offers.len(), 1);
        assert_eq!(offers[0].millicores, Some(4000));
    }

    #[test]
    fn totals_are_per_architecture_and_count_the_silent() {
        let ads = [
            decode_advertisement(Some(&advertisement(vec![
                entry(&["linux/amd64"], sys(8, 16 * GIB, 500 * GIB)),
                entry(&["linux/arm64"], sys(2, 4 * GIB, 50 * GIB)),
            ]))),
            decode_advertisement(Some(&advertisement(vec![entry(&["x86_64"], sys(4, 32 * GIB, GIB))]))),
            decode_advertisement(Some(&advertisement(vec![entry(
                &["aarch64"],
                // Only cores stated: memory and disk add nothing, and it is flagged.
                protos::Sysresources { cpu_period: Some(100_000), cpu_quota: Some(150_000), ..Default::default() },
            )]))),
            decode_advertisement(None),
            decode_advertisement(Some(&advertisement(Vec::new()))),
            decode_advertisement(Some(b"\xff\xff")),
        ];
        let total = aggregate(&ads);
        assert_eq!((total.declared, total.undeclared, total.unreadable), (3, 2, 1));
        assert_eq!(total.peers(), 6);
        // amd64 cores never add to arm64 cores.
        assert_eq!(
            total.per_arch["linux/amd64"],
            ArchTotal { peers: 2, millicores: 12_000, mem_bytes: 48 * GIB, disk_bytes: 501 * GIB, partial: 0 }
        );
        assert_eq!(
            total.per_arch["linux/arm64"],
            ArchTotal { peers: 2, millicores: 3_500, mem_bytes: 4 * GIB, disk_bytes: 50 * GIB, partial: 1 }
        );
        assert_eq!(total.per_arch.len(), 2);
    }

    #[test]
    fn totals_saturate_instead_of_wrapping() {
        let huge = Announced::Declared(vec![ArchOffer {
            arch: "linux/amd64".into(),
            millicores: Some(u64::MAX),
            mem_bytes: Some(u64::MAX),
            disk_bytes: Some(u64::MAX - 1),
            benchmark: Vec::new(),
        }]);
        let total = aggregate([&huge, &huge]);
        let row = &total.per_arch["linux/amd64"];
        assert_eq!((row.millicores, row.mem_bytes, row.disk_bytes), (u64::MAX, u64::MAX, u64::MAX));
    }

    #[test]
    fn this_node_adds_its_own_rows_but_is_never_a_silent_peer() {
        let peer = decode_advertisement(Some(&advertisement(vec![entry(&["linux/amd64"], sys(8, 16 * GIB, GIB))])));
        let own = decode_advertisement(Some(&advertisement(vec![
            entry(&["x86_64"], sys(4, 8 * GIB, GIB)),
            entry(&["linux/arm64"], sys(2, 4 * GIB, GIB)),
        ])));
        let total = aggregate_with_own(Some(&own), [&peer]);
        assert!(total.own_included);
        assert_eq!((total.declared, total.undeclared, total.unreadable), (1, 0, 0));
        assert_eq!(
            total.per_arch["linux/amd64"],
            ArchTotal { peers: 2, millicores: 12_000, mem_bytes: 24 * GIB, disk_bytes: 2 * GIB, partial: 0 }
        );
        assert_eq!(total.per_arch["linux/arm64"].peers, 1);

        // Not read yet, or nothing announced: the peers' total, flagged as such.
        for own in [None, Some(&Announced::Undeclared), Some(&Announced::Unreadable)] {
            let total = aggregate_with_own(own, [&peer]);
            assert!(!total.own_included);
            assert_eq!(total, aggregate([&peer]));
        }
    }

    #[test]
    fn the_own_report_decodes_like_a_stored_advertisement() {
        use base64::Engine;
        let bytes = advertisement(vec![entry(&["linux/amd64"], sys(4, GIB, GIB))]);
        let line = format!(
            r#"{{"peer": "{}", "read_at": 1}}"#,
            base64::engine::general_purpose::STANDARD.encode(&bytes)
        );
        assert_eq!(parse_own_resources(&line), Ok(decode_advertisement(Some(&bytes))));
        assert_eq!(parse_own_resources(r#"{"peer": ""}"#), Ok(Announced::Undeclared));
        assert_eq!(parse_own_resources(r#"{"error": "no psutil"}"#), Err("no psutil".to_string()));
        assert!(parse_own_resources(r#"{"peer": "%%"}"#).is_err());
        assert!(parse_own_resources("not json").is_err());
    }

    #[test]
    fn nothing_announced_is_an_empty_total() {
        let total = aggregate(std::iter::empty());
        assert_eq!(total, PotentialResources::default());
    }

    #[test]
    fn benchmarks_read_in_their_own_units() {
        assert_eq!(
            format_benchmark("int_ops_per_sec", 1_234_567),
            ("int ops/s".to_string(), "1.2M".to_string())
        );
        assert_eq!(
            format_benchmark("mem_bandwidth_64mib_bytes_per_sec", 8 * GIB),
            ("mem bw @ 64 MiB".to_string(), "8.0 GiB/s".to_string())
        );
        assert_eq!(
            format_benchmark("mem_bandwidth_1gib_bytes_per_sec", 512 << 20),
            ("mem bw @ 1 GiB".to_string(), "512.0 MiB/s".to_string())
        );
        assert_eq!(format_benchmark("sha256_hashes_per_sec", 950), ("sha256/s".to_string(), "950".to_string()));
        // An unknown key is shown verbatim, not dropped.
        assert_eq!(format_benchmark("gpu_tflops", 7), ("gpu_tflops".to_string(), "7".to_string()));
    }
}
