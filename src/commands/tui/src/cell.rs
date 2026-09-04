//! The CELL page's catalogue: the policies an operator actually chooses between,
//! and what each choice writes into `config.yaml`.
//!
//! The Config page edits the whole YAML tree, ordered by where a key lives in the
//! file. That is the right tool for someone who already knows which key they want,
//! and no help at all to someone who has just installed nodo and wants to rent out
//! their machine. This module is the other half: a closed catalogue of *decisions*,
//! each one named by the question it answers rather than by the key it writes.
//!
//! Everything here is one primitive at three scales:
//!
//! * a **lever state** is a small set of writes (one to five keys),
//! * a **profile** is a large one (a whole posture, twenty-odd keys),
//! * and the Config page is the same thing at one key.
//!
//! Nothing records which state or profile is active. It is **derived** by reading
//! `config.yaml` and asking which set of writes it already satisfies. A stored
//! marker would start lying the moment someone nudged one key on the Config page;
//! a derived one cannot. It also makes the deviation report free, and that report
//! is the most instructive thing on the page: "you match CAUTIOUS RENTER except
//! for these two keys" teaches an operator their own configuration.
//!
//! A lever whose keys match no state at all reads as `custom` and is never
//! silently rounded to the nearest one.

use crate::app::{ConfigPathSegment, Page};
use serde_yaml::Value;

/// A region of the cell. Not decoration: the anatomy carries the grouping, because
/// what a membrane, a nucleus and a lysosome each do to a cell is what these groups
/// of settings each do to a node.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Organelle {
    /// Ion channels: which ports and addresses let anything through.
    Channels,
    /// Ribosomes: what work this node produces.
    Ribosomes,
    /// Secretory vesicles: what this node sends out into the network.
    Vesicles,
    /// The nucleus: identity and wallets — the only losses here are permanent.
    Nucleus,
    /// The immune system: what this node refuses to trust.
    Immune,
    /// The cell wall: how much of the host machine this node may occupy, and at what
    /// hours. Everything else on this page decides what the node does; this decides how
    /// much of somebody's PC it is allowed to do it with.
    Wall,
    /// Mitochondria: energy, which for a node means money.
    Mitochondria,
    /// The vacuole: storage and housekeeping.
    Vacuole,
}

impl Organelle {
    /// Reading order, and the order they are laid out in: the outward-facing three
    /// above the nucleus, the self-preserving four below it.
    pub const ALL: [Organelle; 8] = [
        Organelle::Channels,
        Organelle::Ribosomes,
        Organelle::Vesicles,
        Organelle::Nucleus,
        Organelle::Immune,
        Organelle::Wall,
        Organelle::Mitochondria,
        Organelle::Vacuole,
    ];

    pub fn title(self) -> &'static str {
        match self {
            Organelle::Channels => "CHANNELS",
            Organelle::Ribosomes => "RIBOSOMES",
            Organelle::Vesicles => "VESICLES",
            Organelle::Nucleus => "NUCLEUS",
            Organelle::Immune => "IMMUNE",
            Organelle::Wall => "WALL",
            Organelle::Mitochondria => "MITOCHONDRIA",
            Organelle::Vacuole => "VACUOLE",
        }
    }

    /// What the organelle governs, in the operator's words. Rendered beside the
    /// title, because "CHANNELS" alone tells nobody anything.
    pub fn subtitle(self) -> &'static str {
        match self {
            Organelle::Channels => "reach",
            Organelle::Ribosomes => "work",
            Organelle::Vesicles => "voice",
            Organelle::Nucleus => "identity & wallet",
            Organelle::Immune => "trust",
            Organelle::Wall => "footprint & hours",
            Organelle::Mitochondria => "money",
            Organelle::Vacuole => "upkeep",
        }
    }

    /// The levers in this organelle, in catalogue order.
    pub fn levers(self) -> Vec<&'static Lever> {
        LEVERS.iter().filter(|lever| lever.organelle == self).collect()
    }
}

/// One named position of a lever, and the keys it writes to get there.
#[derive(Debug, Clone, Copy)]
pub struct LeverState {
    pub label: &'static str,
    pub writes: &'static [(&'static str, &'static str)],
}

/// How a lever is operated.
#[derive(Debug, Clone, Copy)]
pub enum LeverKind {
    /// Cycles through named positions. Enter moves to the next one.
    Cycle(&'static [LeverState]),
    /// A single key with no natural set of positions (a port, a percentage, a
    /// credit). Enter opens the ordinary value editor.
    Scalar {
        path: &'static str,
        /// Appended to the value when shown, e.g. "/tcp" or "%". Empty for none.
        unit: &'static str,
    },
    /// Not a setting: the page that owns this properly. Prices are the case —
    /// they have a whole page with bars and a worked example, and a second,
    /// cruder editor for them here would only be a way to disagree with it.
    Link(Page),
}

/// One decision, named by the question it answers.
#[derive(Debug, Clone, Copy)]
pub struct Lever {
    /// Stable id, so a selection survives a refresh.
    pub id: &'static str,
    pub organelle: Organelle,
    /// Short label for the row.
    pub label: &'static str,
    /// The question this lever answers, spelled out for the detail line.
    pub question: &'static str,
    /// What changing it actually does — the sentence that goes in the status bar.
    /// This is where a lever earns its place over the raw key.
    pub consequence: &'static str,
    pub kind: LeverKind,
    /// Shown in red beside the value. For the settings that can quietly cost the
    /// operator money or break a running node.
    pub warning: Option<&'static str>,
    /// A secret is never rendered in plaintext, on this page or in its editor.
    pub secret: bool,
}

impl Lever {
    /// Every config key this lever can write, deduplicated, in first-seen order.
    /// What `e` lists, and what the "writes N keys" hint counts.
    pub fn paths(&self) -> Vec<&'static str> {
        let mut paths: Vec<&'static str> = Vec::new();
        match self.kind {
            LeverKind::Cycle(states) => {
                for state in states {
                    for (path, _) in state.writes {
                        if !paths.contains(path) {
                            paths.push(path);
                        }
                    }
                }
            }
            LeverKind::Scalar { path, .. } => paths.push(path),
            LeverKind::Link(_) => {}
        }
        paths
    }
}

/// What `config.yaml` currently says a lever is set to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LeverStatus {
    /// The document satisfies this state's writes exactly.
    State(usize),
    /// A scalar lever's value, as written in the file.
    Value(String),
    /// The keys are set to a combination no state describes. Reported as such
    /// rather than rounded to the closest one: a panel that guesses here would
    /// misreport the node's own policy.
    Custom,
    /// The keys this lever reads are absent from the file.
    Unset,
    /// A link, which has no value of its own.
    Link,
}

impl LeverStatus {
    /// How the status reads on the row.
    pub fn label(&self, lever: &Lever) -> String {
        match self {
            LeverStatus::State(index) => match lever.kind {
                LeverKind::Cycle(states) => states
                    .get(*index)
                    .map(|state| state.label.to_string())
                    .unwrap_or_else(|| "?".to_string()),
                _ => "?".to_string(),
            },
            LeverStatus::Value(value) => {
                if lever.secret {
                    return if value.is_empty() || value == "null" || value == "auto" {
                        "not set".to_string()
                    } else {
                        "•••••• set".to_string()
                    };
                }
                if value.is_empty() {
                    return "not set".to_string();
                }
                // The unit belongs to a measurement, so a sentinel does not get one:
                // "auto/tcp" reads as a port called auto rather than as "not assigned
                // yet", which is what `auto` means for the gateway port.
                let unit = match lever.kind {
                    LeverKind::Scalar { unit, .. } if value.parse::<f64>().is_ok() => unit,
                    _ => "",
                };
                format!("{value}{unit}")
            }
            LeverStatus::Custom => "custom".to_string(),
            LeverStatus::Unset => "not set".to_string(),
            LeverStatus::Link => match lever.kind {
                LeverKind::Link(page) => format!("{} page", page.title()),
                _ => "→".to_string(),
            },
        }
    }
}

/// A single key that a pending write would change.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Change {
    pub path: String,
    /// The value on disk. `None` when the key is absent and would be created.
    pub from: Option<String>,
    pub to: String,
}

// --- The catalogue ---------------------------------------------------------
//
// Every path here is checked against the real config.example.yaml by
// `every_lever_writes_a_key_that_exists`, so a key renamed in the node fails CI
// rather than an operator's node.

/// Levers that turn several keys at once are the reason this page exists: the
/// decision an operator is making ("can I be found from the internet?") is spread
/// across three blocks of the file, and setting one of the three is worse than
/// setting none.
static LEVERS: &[Lever] = &[
    // --- CHANNELS · reach --------------------------------------------------
    Lever {
        id: "front-door",
        organelle: Organelle::Channels,
        label: "front door",
        question: "Which port do peers and guests reach this node on?",
        consequence: "Changing it drops the proof that the port is reachable, so the next start re-probes it.",
        kind: LeverKind::Scalar {
            path: "network.GATEWAY_PORT",
            unit: "/tcp",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "findable",
        organelle: Organelle::Channels,
        label: "findable outside",
        question: "Should nodes on the internet be able to find and reach me?",
        consequence: "Publishes this node's address on its reputation proof. Either way the router still needs a forward for the gateway port: press n for the steps.",
        // Three positions rather than two, because publishing an address and keeping
        // a hostname pointed at it are separately answerable: a node on a static
        // address wants the first and not the second, and DDNS without a hostname
        // and token configured is a manager that logs an error every interval.
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "no",
                writes: &[
                    ("ddns.ENABLED", "false"),
                    ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
                ],
            },
            LeverState {
                label: "address",
                writes: &[
                    ("ddns.ENABLED", "false"),
                    ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "true"),
                ],
            },
            LeverState {
                label: "+ hostname",
                writes: &[
                    ("ddns.ENABLED", "true"),
                    ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "true"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "ddns-domain",
        organelle: Organelle::Channels,
        label: "ddns hostname",
        question: "Which hostname should keep pointing at this node?",
        consequence: "deSEC only, e.g. my-node.dedyn.io. Without it, and its token, findable-outside has nothing to publish to.",
        kind: LeverKind::Scalar {
            path: "ddns.DOMAIN",
            unit: "",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "ddns-token",
        organelle: Organelle::Channels,
        label: "ddns token",
        question: "The provider API token for that hostname.",
        consequence: "A bearer credential for the DNS record. Stored in config.yaml and never shown here.",
        kind: LeverKind::Scalar {
            path: "ddns.TOKEN",
            unit: "",
        },
        warning: None,
        secret: true,
    },
    Lever {
        id: "instance-ports",
        organelle: Organelle::Channels,
        label: "instance ports",
        question: "May an instance get a port of its own on this host?",
        consequence: "Tunnel-only keeps every service reachable through the gateway alone, so the router needs one forward instead of a range.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "own port",
                writes: &[("network.DISABLE_EXPOSE_OUTSIDE", "false")],
            },
            LeverState {
                label: "tunnel only",
                writes: &[("network.DISABLE_EXPOSE_OUTSIDE", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "lan-peers",
        organelle: Organelle::Channels,
        label: "lan-only peers",
        question: "Are my peers on this same network, with no public addresses?",
        consequence: "Announces this node's LAN addresses. Noise to a remote peer — one connect timeout per probe — and right for an all-on-one-LAN deployment.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "no",
                writes: &[("network.ANNOUNCE_PRIVATE_ADDRESSES", "false")],
            },
            LeverState {
                label: "yes",
                writes: &[("network.ANNOUNCE_PRIVATE_ADDRESSES", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "public-ip",
        organelle: Organelle::Channels,
        label: "public address",
        question: "Which address should I advertise, if auto-detection gets it wrong?",
        consequence: "Empty auto-detects from the default outbound interface. Set it when this node is behind NAT and the detected address is private.",
        kind: LeverKind::Scalar {
            path: "network.PUBLIC_IP",
            unit: "",
        },
        warning: None,
        secret: false,
    },
    // --- RIBOSOMES · work --------------------------------------------------
    Lever {
        id: "outside-work",
        organelle: Organelle::Ribosomes,
        label: "outside work",
        question: "Do I take paid work from other nodes at all?",
        consequence: "Closed refuses every new deposit, so nobody can acquire MU here. Balances already held keep being spent — this only stops new demand.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "closed",
                writes: &[("client.ACCEPT_NEW_DEPOSITS", "false")],
            },
            LeverState {
                label: "open",
                writes: &[("client.ACCEPT_NEW_DEPOSITS", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "foreign-arch",
        organelle: Organelle::Ribosomes,
        label: "foreign arch",
        question: "Do I run services built for the other CPU architecture?",
        consequence: "Emulated execution is an order of magnitude slower than native. Off stops this node advertising the foreign arch at all.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "off",
                writes: &[("virtualizers.qemu.ENABLE", "false")],
            },
            LeverState {
                label: "on",
                writes: &[("virtualizers.qemu.ENABLE", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "descendants",
        organelle: Organelle::Ribosomes,
        label: "descendants",
        question: "What if a service declares children this node has nowhere to run?",
        consequence: "Strict refuses the launch at the first group that fits nowhere. Lenient logs it and starts anyway, deferring the failure.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "strict",
                writes: &[
                    ("workload_admission.POLICY", "fail_fast"),
                    ("workload_admission.ON_UNSATISFIABLE", "reject"),
                ],
            },
            LeverState {
                label: "lenient",
                writes: &[
                    ("workload_admission.POLICY", "full"),
                    ("workload_admission.ON_UNSATISFIABLE", "warn"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "spare-capacity",
        organelle: Organelle::Ribosomes,
        label: "spare capacity",
        question: "Should idle capacity run the opportunistic fallback service?",
        consequence: "Only governs that fallback. A PAID workload still starts whatever the host load is — nothing here caps what a client may rent.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "off",
                writes: &[("low_demand.ENABLED", "false")],
            },
            LeverState {
                label: "on",
                writes: &[("low_demand.ENABLED", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "idle-cpu",
        organelle: Organelle::Ribosomes,
        label: "idle below",
        question: "How quiet must the host be before the fallback runs?",
        consequence: "Host CPU share the fallback needs to be under. Paid work ignores it.",
        kind: LeverKind::Scalar {
            path: "low_demand.CPU_MAX_PERCENT",
            unit: "%",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "dev-internal",
        organelle: Organelle::Ribosomes,
        label: "dev is internal",
        question: "Do my own dev instances count as internal wherever they run?",
        consequence: "Turn it off when this node is driven over SSH, so a dev instance is judged by where it really is rather than by its name.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "yes",
                writes: &[("network.CONSIDER_DEV_AS_INTERNAL", "true")],
            },
            LeverState {
                label: "no",
                writes: &[("network.CONSIDER_DEV_AS_INTERNAL", "false")],
            },
        ]),
        warning: None,
        secret: false,
    },
    // --- VESICLES · voice --------------------------------------------------
    Lever {
        id: "delegate",
        organelle: Organelle::Vesicles,
        label: "delegate work",
        question: "May this node ask a peer to run something it cannot run itself, and pay for it?",
        consequence: "Never makes such a request fail instead of being delegated. Without auto-pay it is delegated only to a peer already funded, so no ERG leaves this node until `nodo pay` says so.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "never",
                writes: &[
                    ("network.DELEGATE_EXECUTION", "false"),
                    ("deposits.AUTOMATIC_REFILL", "false"),
                ],
            },
            LeverState {
                label: "no auto-pay",
                writes: &[
                    ("network.DELEGATE_EXECUTION", "true"),
                    ("deposits.AUTOMATIC_REFILL", "false"),
                ],
            },
            LeverState {
                label: "and auto-pay",
                writes: &[
                    ("network.DELEGATE_EXECUTION", "true"),
                    ("deposits.AUTOMATIC_REFILL", "true"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "tunnel-policy",
        organelle: Organelle::Vesicles,
        label: "tunnel delegated",
        question: "How is a delegated service handed back to my client?",
        consequence: "auto tunnels only when the peer is unreachable from here; never hands over the peer's own address, which fails for a client on another network.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "auto",
                writes: &[("network.DELEGATION_TUNNEL_POLICY", "auto")],
            },
            LeverState {
                label: "always",
                writes: &[("network.DELEGATION_TUNNEL_POLICY", "always")],
            },
            LeverState {
                label: "never",
                writes: &[("network.DELEGATION_TUNNEL_POLICY", "never")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "introduce",
        organelle: Organelle::Vesicles,
        label: "introduce myself",
        question: "Do I announce myself to a peer that connects to me?",
        consequence: "On, a peer that reaches this node learns how to reach it back without being told separately.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "off",
                writes: &[("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false")],
            },
            LeverState {
                label: "on",
                writes: &[("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "announcements",
        organelle: Organelle::Vesicles,
        label: "announcements",
        question: "Do announcements carry the written-out protocol spec?",
        consequence: "Full adds ~5 KB per address. On a ledger that is storage rent for as long as the proof exists; over gRPC the bytes are transient.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "lean",
                writes: &[
                    ("communication.SHARE_PROSE_ON_GET_PEER_INFO", "true"),
                    ("communication.SHARE_PROSE_ON_LEDGER", "false"),
                ],
            },
            LeverState {
                label: "full",
                writes: &[
                    ("communication.SHARE_PROSE_ON_GET_PEER_INFO", "true"),
                    ("communication.SHARE_PROSE_ON_LEDGER", "true"),
                ],
            },
            LeverState {
                label: "silent",
                writes: &[
                    ("communication.SHARE_PROSE_ON_GET_PEER_INFO", "false"),
                    ("communication.SHARE_PROSE_ON_LEDGER", "false"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    // --- NUCLEUS · identity & wallet ---------------------------------------
    Lever {
        id: "identity-mnemonic",
        organelle: Organelle::Nucleus,
        label: "identity",
        question: "The one mnemonic behind this node's name.",
        consequence: "BACK IT UP. Changing it makes every peer see a brand-new node, orphaning the deposits and the reputation recorded against the old one.",
        kind: LeverKind::Scalar {
            path: "identity.MNEMONIC",
            unit: "",
        },
        warning: Some("irreplaceable"),
        secret: true,
    },
    Lever {
        id: "wallet-mnemonic",
        organelle: Organelle::Nucleus,
        label: "ergo wallet",
        question: "The wallet this node is paid into and publishes its proofs from.",
        consequence: "BACK IT UP. Losing it means losing the funds and the on-chain history. It is not the node's identity, so replacing it leaves the node's name and peers intact.",
        kind: LeverKind::Scalar {
            path: "ledgers.ergo.WALLET_MNEMONIC",
            unit: "",
        },
        warning: Some("holds funds"),
        secret: true,
    },
    Lever {
        id: "cold-wallet",
        organelle: Organelle::Nucleus,
        label: "cold wallet",
        question: "Where should earnings above the hot-wallet limit be swept?",
        consequence: "A public address, never a mnemonic. Empty disables sweeping, leaving everything in the wallet the node signs with.",
        kind: LeverKind::Scalar {
            path: "ledgers.ergo.payments.COLD_WALLET",
            unit: "",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "hot-limit",
        organelle: Organelle::Nucleus,
        label: "keep hot",
        question: "How much ERG stays in the operational wallet?",
        consequence: "Everything above this is swept to the cold wallet, once the minimum transfer is also met.",
        kind: LeverKind::Scalar {
            path: "ledgers.ergo.payments.HOT_WALLET_LIMITS",
            unit: " ERG",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "simulate-payments",
        organelle: Organelle::Nucleus,
        label: "payments",
        question: "Are payments real, or simulated?",
        consequence: "Simulated means this node believes it is being paid and no money moves. For development only — a node left like this earns nothing while looking healthy.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "REAL",
                writes: &[("general_flags.SIMULATE_PAYMENTS", "false")],
            },
            LeverState {
                label: "SIMULATED",
                writes: &[("general_flags.SIMULATE_PAYMENTS", "true")],
            },
        ]),
        warning: Some("earns nothing when simulated"),
        secret: false,
    },
    // --- IMMUNE · trust ----------------------------------------------------
    Lever {
        id: "service-egress",
        organelle: Organelle::Immune,
        label: "service egress",
        question: "May a service someone else wrote reach the internet from my machine?",
        consequence: "Nothing refuses every service that declares a network — including this node's OWN core services (packer, source-application), whose egress is egress from here too.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "anything",
                writes: &[
                    ("service_networks.blacklist", "[]"),
                    ("service_networks.whitelist", "[]"),
                ],
            },
            LeverState {
                label: "nothing",
                writes: &[
                    ("service_networks.blacklist", "[\"*\"]"),
                    ("service_networks.whitelist", "[]"),
                ],
            },
        ]),
        warning: Some("\"nothing\" also refuses core services"),
        secret: false,
    },
    Lever {
        id: "child-isolation",
        organelle: Organelle::Immune,
        label: "child isolation",
        question: "Are the children of my internal instances exposed on this host?",
        consequence: "Isolated keeps a child off the host's ports. Exposed publishes it, reachable from the internet subject to the host firewall.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "isolated",
                writes: &[("network.ISOLATE_INTERNAL_CHILDREN", "true")],
            },
            LeverState {
                label: "exposed",
                writes: &[("network.ISOLATE_INTERNAL_CHILDREN", "false")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "verify-imports",
        organelle: Organelle::Immune,
        label: "verify imports",
        question: "Do I check that a service is what its hash says it is?",
        consequence: "Checks on import, and runs the integrity pass at every start. Costs time on a large registry; catches a service that arrived corrupted or altered.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "on import",
                writes: &[
                    ("misc.VALIDATE_ON_IMPORT", "true"),
                    ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
                ],
            },
            LeverState {
                label: "always",
                writes: &[
                    ("misc.VALIDATE_ON_IMPORT", "true"),
                    ("hashing.CHECK_INTEGRITY_ON_SERVE", "true"),
                ],
            },
            LeverState {
                label: "never",
                writes: &[
                    ("misc.VALIDATE_ON_IMPORT", "false"),
                    ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "device-nodes",
        organelle: Organelle::Immune,
        label: "device nodes",
        question: "May a service create device nodes in its own filesystem?",
        consequence: "Deny is the safe answer and the default. An allowlist opens only the entries listed under virtualizers.ch.SECURITY -- to every service, unless it is restricted to the trusted service ids listed there.",
        // The trusted-only restriction is inert while the policy is deny, so the
        // safe position states it too: that way returning to deny from an open
        // allowlist cannot leave the restriction switched off behind it.
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "deny",
                writes: &[
                    ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "deny"),
                    ("virtualizers.ch.SECURITY.REQUIRE_TRUSTED_SERVICE_FOR_DEVICES", "true"),
                ],
            },
            LeverState {
                label: "trusted only",
                writes: &[
                    ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "allowlist"),
                    ("virtualizers.ch.SECURITY.REQUIRE_TRUSTED_SERVICE_FOR_DEVICES", "true"),
                ],
            },
            LeverState {
                label: "any service",
                writes: &[
                    ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "allowlist"),
                    ("virtualizers.ch.SECURITY.REQUIRE_TRUSTED_SERVICE_FOR_DEVICES", "false"),
                ],
            },
        ]),
        warning: Some("\"any service\" opens the allowlist to anyone"),
        secret: false,
    },
    Lever {
        id: "manifest-arch",
        organelle: Organelle::Immune,
        label: "manifest arch",
        question: "Do I believe a service manifest about which architecture it is for?",
        consequence: "Verify inspects the binary instead of trusting the declaration. Trusting a wrong one wastes a build and boots a guest that cannot run.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "verify",
                writes: &[("builder.TRUST_METADATA_ARCHITECTURE", "false")],
            },
            LeverState {
                label: "trust",
                writes: &[("builder.TRUST_METADATA_ARCHITECTURE", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    // --- WALL · footprint & hours ------------------------------------------
    //
    // Nothing else on this page can say "not with my whole machine". Scarcity pricing
    // makes a busy host expensive and `spare capacity` gates only the opportunistic
    // fallback: neither of them ever refuses a paid workload. These do.
    Lever {
        id: "host-ceiling",
        organelle: Organelle::Wall,
        label: "host ceiling",
        question: "Is there a hard cap on how much of this machine nodo may hold?",
        consequence: "The master switch for the four ceilings below. Off, a client may rent every core and every byte the host has, whatever it costs them.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "off",
                writes: &[("host_limits.ENABLED", "false")],
            },
            LeverState {
                label: "on",
                writes: &[("host_limits.ENABLED", "true")],
            },
        ]),
        warning: None,
        secret: false,
    },
    // The three shares are cycles rather than typed numbers because the decision is
    // "roughly how much of my PC", not a figure anyone measures. Any other value set on
    // the Config page still applies -- the row simply reads `custom`, which is the whole
    // point of deriving the state instead of storing it.
    Lever {
        id: "cpu-ceiling",
        organelle: Organelle::Wall,
        label: "cpu ceiling",
        question: "How much of this machine's CPU may all instances add up to?",
        consequence: "Compared against the sum of every instance's CPU quota, so it caps what they can take rather than what they happen to be using.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "no cap",
                writes: &[("host_limits.MAX_CPU_SHARE", "0")],
            },
            LeverState {
                label: "a quarter",
                writes: &[("host_limits.MAX_CPU_SHARE", "0.25")],
            },
            LeverState {
                label: "half",
                writes: &[("host_limits.MAX_CPU_SHARE", "0.5")],
            },
            LeverState {
                label: "most",
                writes: &[("host_limits.MAX_CPU_SHARE", "0.8")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "ram-ceiling",
        organelle: Organelle::Wall,
        label: "ram ceiling",
        question: "How much of this machine's memory may all instances add up to?",
        consequence: "The one an operator feels first: memory handed to an instance is memory the person at this PC does not have.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "no cap",
                writes: &[("host_limits.MAX_RAM_SHARE", "0")],
            },
            LeverState {
                label: "a quarter",
                writes: &[("host_limits.MAX_RAM_SHARE", "0.25")],
            },
            LeverState {
                label: "half",
                writes: &[("host_limits.MAX_RAM_SHARE", "0.5")],
            },
            LeverState {
                label: "most",
                writes: &[("host_limits.MAX_RAM_SHARE", "0.8")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "disk-ceiling",
        organelle: Organelle::Wall,
        label: "disk ceiling",
        question: "How much of the storage filesystem may all instances add up to?",
        consequence: "Measured against the filesystem's total size, not its free space, so a disk something else filled does not quietly raise this node's allowance.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "no cap",
                writes: &[("host_limits.MAX_DISK_SHARE", "0")],
            },
            LeverState {
                label: "a quarter",
                writes: &[("host_limits.MAX_DISK_SHARE", "0.25")],
            },
            LeverState {
                label: "half",
                writes: &[("host_limits.MAX_DISK_SHARE", "0.5")],
            },
            LeverState {
                label: "most",
                writes: &[("host_limits.MAX_DISK_SHARE", "0.8")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "net-per-day",
        organelle: Organelle::Wall,
        label: "net per day",
        question: "How much tunnelled traffic may this node relay in a day?",
        consequence: "Counts both directions, resets at local midnight, and survives a restart. Spent, no tunnel opens and the open ones close. 0 is unlimited.",
        kind: LeverKind::Scalar {
            path: "host_limits.MAX_NET_GIB_PER_DAY",
            unit: " GiB/day",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "net-rate",
        organelle: Organelle::Wall,
        label: "net rate",
        question: "How fast may tunnelled traffic flow, across every tunnel at once?",
        consequence: "Shapes the relay by making it wait, so a transfer over the ceiling gets slower and still finishes. Only tunnelled traffic: an instance on a port of its own is not shaped. 0 is unlimited.",
        kind: LeverKind::Scalar {
            path: "host_limits.MAX_NET_MIB_PER_SECOND",
            unit: " MiB/s",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "after-hours",
        organelle: Organelle::Wall,
        label: "after hours",
        question: "What happens outside the hours this node works?",
        consequence: "Both positions refuse new work from outside. \"stop\" also destroys what is already running, mid-flight, and refunds what its balance still holds. Work from a dev client is exempt either way, so `nodo execute` and the core services keep working.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "always open",
                writes: &[
                    ("activity_window.ENABLED", "false"),
                    ("activity_window.ON_CLOSE", "refuse"),
                ],
            },
            LeverState {
                label: "refuse work",
                writes: &[
                    ("activity_window.ENABLED", "true"),
                    ("activity_window.ON_CLOSE", "refuse"),
                ],
            },
            LeverState {
                label: "stop work",
                writes: &[
                    ("activity_window.ENABLED", "true"),
                    ("activity_window.ON_CLOSE", "stop"),
                ],
            },
        ]),
        warning: Some("\"stop\" kills running instances"),
        secret: false,
    },
    Lever {
        id: "open-from",
        organelle: Organelle::Wall,
        label: "open from",
        question: "From what time of day does this node take work?",
        consequence: "Local time, HH:MM. Equal to \"open until\" means always open, so setting the hours is what makes after-hours mean anything.",
        kind: LeverKind::Scalar {
            path: "activity_window.START",
            unit: "",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "open-until",
        organelle: Organelle::Wall,
        label: "open until",
        question: "Until what time of day does this node take work?",
        consequence: "Local time, HH:MM, and exclusive. Earlier than \"open from\" is the night shift: 22:00 to 06:00 is one window, open all night.",
        kind: LeverKind::Scalar {
            path: "activity_window.END",
            unit: "",
        },
        warning: None,
        secret: false,
    },
    // --- MITOCHONDRIA · money ----------------------------------------------
    Lever {
        id: "prices",
        organelle: Organelle::Mitochondria,
        label: "prices",
        question: "What does this node charge per resource?",
        consequence: "Edited on the PRICING page, which shows each price as a bar against the others and what the node really keeps once the guest kernel reserve is paid for.",
        kind: LeverKind::Link(Page::Pricing),
        warning: None,
        secret: false,
    },
    Lever {
        id: "scarcity",
        organelle: Organelle::Mitochondria,
        label: "scarcity",
        question: "Do my prices rise as the node fills up?",
        consequence: "The only automatic protection this node has against selling more than it can comfortably give: as a resource runs out, its price climbs towards the ceiling.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "flat",
                writes: &[
                    ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
                    ("pricing.SCARCITY_CURVE", "1.0"),
                ],
            },
            LeverState {
                label: "linear",
                writes: &[
                    ("pricing.SCARCITY_MAX_MULTIPLIER", "10"),
                    ("pricing.SCARCITY_CURVE", "1.0"),
                ],
            },
            LeverState {
                label: "steep",
                writes: &[
                    ("pricing.SCARCITY_MAX_MULTIPLIER", "10"),
                    ("pricing.SCARCITY_CURVE", "2.0"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "free-while-calm",
        organelle: Organelle::Mitochondria,
        label: "free while calm",
        question: "Should work be free while the node is not busy?",
        consequence: "Charges nothing while EVERY resource sits below this share of capacity. How you run a node that is free until it is actually needed.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "never",
                writes: &[("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0")],
            },
            LeverState {
                label: "under 50%",
                writes: &[("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.5")],
            },
            LeverState {
                label: "under 80%",
                writes: &[("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.8")],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "welcome-credit",
        organelle: Organelle::Mitochondria,
        label: "welcome credit",
        question: "How much MU does a brand-new client start with?",
        consequence: "Given away per client, once each. 0 gives nothing.",
        kind: LeverKind::Scalar {
            path: "free_tier.CREDIT_MU_PER_NEW_CLIENT",
            unit: " MU",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "instance-debt",
        organelle: Organelle::Mitochondria,
        label: "instance debt",
        question: "May an instance keep running once its balance is empty?",
        consequence: "An empty balance is the ONLY thing that reaps an instance nobody stops. Allowing debt means a service that leaks children leaks them until the node restarts.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "never",
                writes: &[("costs.ALLOW_DEBT", "false")],
            },
            LeverState {
                label: "allowed",
                writes: &[("costs.ALLOW_DEBT", "true")],
            },
        ]),
        warning: Some("debt disables the only reaper"),
        secret: false,
    },
    Lever {
        id: "initial-runtime",
        organelle: Organelle::Mitochondria,
        label: "funded for",
        question: "How long is a new instance funded when the client asks for no balance?",
        consequence: "The amount is the price of the resources it requested, for this long.",
        kind: LeverKind::Scalar {
            path: "deposits.INITIAL_RUNTIME_HOURS",
            unit: " h",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "display-unit",
        organelle: Organelle::Mitochondria,
        label: "display unit",
        question: "What do I want to read prices and balances in?",
        consequence: "Purely presentational: changing it never changes what anybody is charged.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "erg",
                writes: &[("ui.DISPLAY_UNIT", "erg")],
            },
            LeverState {
                label: "mu",
                writes: &[("ui.DISPLAY_UNIT", "mu")],
            },
        ]),
        warning: None,
        secret: false,
    },
    // --- VACUOLE · upkeep --------------------------------------------------
    Lever {
        id: "debug-mode",
        organelle: Organelle::Vacuole,
        label: "debug mode",
        question: "Do I want everything logged while I work out what is wrong?",
        consequence: "Turns on debug, in-memory and tunnel logs, guest serial capture and failure retention in one go. Noisy: tunnel logs alone are a line per connection and per billed MiB.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "off",
                writes: &[
                    ("logs.DEBUG_MODE", "false"),
                    ("logs.MEMORY_LOGS", "false"),
                    ("logs.TUNNEL_LOGS", "false"),
                ],
            },
            LeverState {
                label: "on",
                writes: &[
                    ("logs.DEBUG_MODE", "true"),
                    ("logs.MEMORY_LOGS", "true"),
                    ("logs.TUNNEL_LOGS", "true"),
                    ("virtualizers.ch.SERIAL_MODE", "file"),
                    ("virtualizers.ch.CONSERVE_RUNTIME_DIR_ON_FAILURE", "true"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
    Lever {
        id: "keep-failures",
        organelle: Organelle::Vacuole,
        label: "keep failures",
        question: "How long does a failed launch stay on disk for inspection?",
        consequence: "Nothing reclaims them automatically: `nodo prune` drops the ones past this window.",
        kind: LeverKind::Scalar {
            path: "virtualizers.ch.FAILURE_RETENTION_DAYS",
            unit: " days",
        },
        warning: None,
        secret: false,
    },
    Lever {
        id: "downloads",
        organelle: Organelle::Vacuole,
        label: "downloads",
        question: "What happens to a service file after it is downloaded?",
        consequence: "Import-and-drop keeps the registry as the single copy. Keeping the file doubles the disk a downloaded service costs.",
        kind: LeverKind::Cycle(&[
            LeverState {
                label: "keep file",
                writes: &[
                    ("publisher.KEEP_DOWNLOADED_FILE", "true"),
                    ("publisher.AUTO_IMPORT_SERVICE_ON_DOWNLOAD", "true"),
                ],
            },
            LeverState {
                label: "import only",
                writes: &[
                    ("publisher.KEEP_DOWNLOADED_FILE", "false"),
                    ("publisher.AUTO_IMPORT_SERVICE_ON_DOWNLOAD", "true"),
                ],
            },
        ]),
        warning: None,
        secret: false,
    },
];

/// Every lever, in catalogue order.
pub fn levers() -> &'static [Lever] {
    LEVERS
}

/// Find a lever by its stable id.
pub fn lever(id: &str) -> Option<&'static Lever> {
    LEVERS.iter().find(|lever| lever.id == id)
}

// --- Profiles --------------------------------------------------------------

/// A whole posture, as a set of writes. The same primitive as a lever state, at
/// the scale of the entire cell.
///
/// A profile deliberately touches **only policy**. None of them writes an
/// identity, a wallet, a path, a binary location or a core-service id: a posture
/// is a decision about how this node behaves, never about who it is or where its
/// things live.
#[derive(Debug, Clone, Copy)]
pub struct Profile {
    pub id: &'static str,
    pub label: &'static str,
    /// One line: who this is for.
    pub blurb: &'static str,
    pub writes: &'static [(&'static str, &'static str)],
}

/// Ordered from the most closed posture to the most open, so the list reads as a
/// dial rather than a menu.
static PROFILES: &[Profile] = &[
    Profile {
        id: "just-me",
        label: "JUST ME",
        blurb: "I run my own things here. Nothing from outside, nothing spent outside.",
        writes: &[
            ("client.ACCEPT_NEW_DEPOSITS", "false"),
            ("network.DISABLE_EXPOSE_OUTSIDE", "true"),
            ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
            ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
            ("network.DELEGATE_EXECUTION", "false"),
            ("deposits.AUTOMATIC_REFILL", "false"),
            ("ddns.ENABLED", "false"),
            ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
            ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false"),
            ("service_networks.blacklist", "[]"),
            ("service_networks.whitelist", "[]"),
            ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
            ("pricing.SCARCITY_CURVE", "1.0"),
            ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
            ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "0"),
            ("costs.ALLOW_DEBT", "false"),
            ("misc.VALIDATE_ON_IMPORT", "true"),
            ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
            ("virtualizers.qemu.ENABLE", "false"),
            ("workload_admission.POLICY", "fail_fast"),
            ("workload_admission.ON_UNSATISFIABLE", "reject"),
            ("low_demand.ENABLED", "false"),
            // A node running its owner's own things on their own PC is exactly what the
            // ceilings are for, even though nothing is rented here: they cap what nodo
            // occupies, and a dev instance occupies it like any other.
            ("host_limits.ENABLED", "true"),
            ("general_flags.SIMULATE_PAYMENTS", "false"),
            ("logs.DEBUG_MODE", "false"),
            ("logs.MEMORY_LOGS", "false"),
            ("logs.TUNNEL_LOGS", "false"),
        ],
    },
    Profile {
        id: "cautious",
        label: "CAUTIOUS RENTER",
        blurb: "I will rent this machine out, but on a short leash.",
        writes: &[
            ("client.ACCEPT_NEW_DEPOSITS", "true"),
            // Tunnel-only: a client still reaches its service through the gateway,
            // and the router needs one forward rather than a range of them.
            ("network.DISABLE_EXPOSE_OUTSIDE", "true"),
            ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
            ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
            ("network.DELEGATE_EXECUTION", "false"),
            ("deposits.AUTOMATIC_REFILL", "false"),
            ("ddns.ENABLED", "false"),
            ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "true"),
            ("network.VERIFY_GATEWAY_REACHABILITY", "true"),
            ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false"),
            ("service_networks.blacklist", "[]"),
            ("service_networks.whitelist", "[]"),
            // Steep, because scarcity is the only automatic brake this node has on
            // renting out more than it can comfortably give.
            ("pricing.SCARCITY_MAX_MULTIPLIER", "10"),
            ("pricing.SCARCITY_CURVE", "2.0"),
            ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
            ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "0"),
            ("costs.ALLOW_DEBT", "false"),
            ("misc.VALIDATE_ON_IMPORT", "true"),
            ("hashing.CHECK_INTEGRITY_ON_SERVE", "true"),
            ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "deny"),
            ("builder.TRUST_METADATA_ARCHITECTURE", "false"),
            ("virtualizers.qemu.ENABLE", "true"),
            ("workload_admission.POLICY", "fail_fast"),
            ("workload_admission.ON_UNSATISFIABLE", "reject"),
            ("low_demand.ENABLED", "false"),
            // The posture this whole organelle was written for: rent the machine out, but
            // not all of it. Scarcity pricing above only makes the last core expensive;
            // this is what stops it being sold at all.
            ("host_limits.ENABLED", "true"),
            ("general_flags.SIMULATE_PAYMENTS", "false"),
            ("logs.DEBUG_MODE", "false"),
            ("logs.MEMORY_LOGS", "false"),
            ("logs.TUNNEL_LOGS", "false"),
        ],
    },
    Profile {
        id: "open-renter",
        label: "OPEN RENTER",
        blurb: "I want this machine earning: reachable, delegating, priced by load.",
        writes: &[
            ("client.ACCEPT_NEW_DEPOSITS", "true"),
            ("network.DISABLE_EXPOSE_OUTSIDE", "false"),
            ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
            ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
            ("network.DELEGATE_EXECUTION", "true"),
            ("deposits.AUTOMATIC_REFILL", "true"),
            // The hostname is left alone: DDNS needs a domain and a token this
            // profile cannot invent, so the CHANNELS lever is where it gets turned on.
            ("ddns.ENABLED", "false"),
            ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "true"),
            ("network.VERIFY_GATEWAY_REACHABILITY", "true"),
            ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "true"),
            ("service_networks.blacklist", "[]"),
            ("service_networks.whitelist", "[]"),
            ("pricing.SCARCITY_MAX_MULTIPLIER", "10"),
            ("pricing.SCARCITY_CURVE", "1.0"),
            ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
            ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "0"),
            ("costs.ALLOW_DEBT", "false"),
            ("misc.VALIDATE_ON_IMPORT", "true"),
            ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
            ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "deny"),
            ("builder.TRUST_METADATA_ARCHITECTURE", "false"),
            ("virtualizers.qemu.ENABLE", "true"),
            ("workload_admission.POLICY", "fail_fast"),
            ("workload_admission.ON_UNSATISFIABLE", "reject"),
            ("low_demand.ENABLED", "true"),
            // No ceiling: this profile is for a machine whose job is earning, where the
            // whole host is the product.
            ("host_limits.ENABLED", "false"),
            ("general_flags.SIMULATE_PAYMENTS", "false"),
            ("logs.DEBUG_MODE", "false"),
            ("logs.MEMORY_LOGS", "false"),
            ("logs.TUNNEL_LOGS", "false"),
        ],
    },
    Profile {
        id: "lan-lab",
        label: "LAN LAB",
        blurb: "A few machines on my own network, sharing capacity for free.",
        writes: &[
            ("client.ACCEPT_NEW_DEPOSITS", "true"),
            ("network.DISABLE_EXPOSE_OUTSIDE", "false"),
            ("network.ISOLATE_INTERNAL_CHILDREN", "false"),
            ("network.ANNOUNCE_PRIVATE_ADDRESSES", "true"),
            ("network.DELEGATE_EXECUTION", "true"),
            ("deposits.AUTOMATIC_REFILL", "false"),
            ("ddns.ENABLED", "false"),
            ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
            ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "true"),
            ("service_networks.blacklist", "[]"),
            ("service_networks.whitelist", "[]"),
            ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
            ("pricing.SCARCITY_CURVE", "1.0"),
            ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.8"),
            ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "1000000"),
            ("costs.ALLOW_DEBT", "false"),
            ("misc.VALIDATE_ON_IMPORT", "true"),
            ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
            ("virtualizers.qemu.ENABLE", "true"),
            ("workload_admission.POLICY", "full"),
            ("workload_admission.ON_UNSATISFIABLE", "warn"),
            ("low_demand.ENABLED", "true"),
            ("host_limits.ENABLED", "false"),
            ("general_flags.SIMULATE_PAYMENTS", "false"),
            ("logs.DEBUG_MODE", "false"),
            ("logs.MEMORY_LOGS", "false"),
            ("logs.TUNNEL_LOGS", "false"),
        ],
    },
    Profile {
        id: "workbench",
        label: "WORKBENCH",
        blurb: "I am developing against this node. Nothing here is real money.",
        writes: &[
            ("client.ACCEPT_NEW_DEPOSITS", "false"),
            ("network.DISABLE_EXPOSE_OUTSIDE", "true"),
            ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
            ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
            ("network.CONSIDER_DEV_AS_INTERNAL", "true"),
            ("network.DELEGATE_EXECUTION", "false"),
            ("deposits.AUTOMATIC_REFILL", "false"),
            ("ddns.ENABLED", "false"),
            ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
            ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false"),
            ("service_networks.blacklist", "[]"),
            ("service_networks.whitelist", "[]"),
            ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
            ("pricing.SCARCITY_CURVE", "1.0"),
            ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
            ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "0"),
            ("costs.ALLOW_DEBT", "true"),
            ("misc.VALIDATE_ON_IMPORT", "true"),
            ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
            ("virtualizers.qemu.ENABLE", "true"),
            ("workload_admission.POLICY", "full"),
            ("workload_admission.ON_UNSATISFIABLE", "warn"),
            ("low_demand.ENABLED", "false"),
            // The one profile that turns this on, and the reason the nucleus shows
            // it in red: a node left simulating payments looks healthy and earns
            // nothing.
            ("host_limits.ENABLED", "false"),
            ("general_flags.SIMULATE_PAYMENTS", "true"),
            ("logs.DEBUG_MODE", "true"),
            ("logs.MEMORY_LOGS", "true"),
            ("logs.TUNNEL_LOGS", "true"),
        ],
    },
];

pub fn profiles() -> &'static [Profile] {
    PROFILES
}

// --- Reading the document --------------------------------------------------

/// Split a dotted catalogue path into the segments the config writer takes.
///
/// No config key contains a dot, so a dot is unambiguously a separator. Indices
/// are not representable on purpose: a lever writes named keys, and a list is
/// written whole (`service_networks.blacklist`) rather than element by element.
pub fn path_segments(path: &str) -> Vec<ConfigPathSegment> {
    path.split('.')
        .map(|key| ConfigPathSegment::Key(key.to_string()))
        .collect()
}

/// The value at a dotted path, or None when any segment of it is missing.
fn value_at<'a>(document: Option<&'a Value>, path: &str) -> Option<&'a Value> {
    let mut value = document?;
    for key in path.split('.') {
        value = value.get(key)?;
    }
    Some(value)
}

/// A catalogue value (which is written as YAML text) as the `Value` it denotes.
///
/// Comparing parsed values rather than strings is what lets one code path handle
/// `true`, `10`, `1.0`, `auto` and `["*"]` alike -- a lever that writes a list is
/// compared exactly the way one that writes a boolean is.
fn parsed(text: &str) -> Value {
    serde_yaml::from_str(text).unwrap_or_else(|_| Value::String(text.to_string()))
}

/// One line for a value, in the shape the operator typed or would type it.
///
/// A list is rendered in flow style (`["*"]`) rather than as serde_yaml's block
/// sequence: a diff line reading `[]  →  - '*'` says the same thing in two
/// different notations, and the flow one is what the catalogue and the editor use.
fn rendered(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "null".to_string(),
        Value::Sequence(items) => {
            let rendered: Vec<String> = items
                .iter()
                .map(|item| match item {
                    Value::String(text) => format!("\"{text}\""),
                    other => rendered(other),
                })
                .collect();
            format!("[{}]", rendered.join(", "))
        }
        other => serde_yaml::to_string(other)
            .map(|text| text.trim_end().replace('\n', " "))
            .unwrap_or_default(),
    }
}

/// True when every write in `writes` is already what the document says.
fn satisfied(writes: &[(&str, &str)], document: Option<&Value>) -> bool {
    writes
        .iter()
        .all(|(path, wanted)| value_at(document, path) == Some(&parsed(wanted)))
}

/// What `config.yaml` currently says this lever is set to.
pub fn status(lever: &Lever, document: Option<&Value>) -> LeverStatus {
    match lever.kind {
        LeverKind::Link(_) => LeverStatus::Link,
        LeverKind::Scalar { path, .. } => match value_at(document, path) {
            Some(value) => LeverStatus::Value(rendered(value)),
            None => LeverStatus::Unset,
        },
        LeverKind::Cycle(states) => {
            if lever.paths().iter().all(|path| value_at(document, path).is_none()) {
                return LeverStatus::Unset;
            }
            states
                .iter()
                .position(|state| satisfied(state.writes, document))
                .map(LeverStatus::State)
                .unwrap_or(LeverStatus::Custom)
        }
    }
}

/// The state Enter should move to: the next one round, or the first when the
/// current combination has no name.
pub fn next_state(lever: &Lever, current: &LeverStatus) -> Option<usize> {
    let LeverKind::Cycle(states) = lever.kind else {
        return None;
    };
    if states.is_empty() {
        return None;
    }
    match current {
        LeverStatus::State(index) => Some((index + 1) % states.len()),
        _ => Some(0),
    }
}

/// Which of `writes` would actually change the file. Only these are shown in a
/// confirmation: a diff padded with keys that already hold the wanted value hides
/// the ones that do not.
pub fn changes(writes: &[(&'static str, &'static str)], document: Option<&Value>) -> Vec<Change> {
    writes
        .iter()
        .filter_map(|(path, wanted)| {
            let wanted_value = parsed(wanted);
            let current = value_at(document, path);
            if current == Some(&wanted_value) {
                return None;
            }
            Some(Change {
                path: (*path).to_string(),
                from: current.map(rendered),
                to: rendered(&wanted_value),
            })
        })
        .collect()
}

/// How closely the document already matches a profile.
#[derive(Debug, Clone)]
pub struct ProfileReport {
    pub profile: &'static Profile,
    /// Keys that differ from the profile. Empty means the node is in this posture.
    pub deviations: Vec<Change>,
    pub total: usize,
}

impl ProfileReport {
    pub fn matched(&self) -> usize {
        self.total - self.deviations.len()
    }

    /// How the profile bar reads: the posture, and how far off it is.
    pub fn summary(&self) -> String {
        match self.deviations.len() {
            0 => format!("{} · exact match", self.profile.label),
            1 => format!("{} · 1 deviation", self.profile.label),
            count => format!("{} · {count} deviations", self.profile.label),
        }
    }
}

pub fn report(profile: &'static Profile, document: Option<&Value>) -> ProfileReport {
    ProfileReport {
        profile,
        deviations: changes(profile.writes, document),
        total: profile.writes.len(),
    }
}

/// The profile this node is closest to, which is what the page names itself after.
///
/// Ties go to the earlier profile, and the catalogue runs from the most closed
/// posture to the most open: a node that is equally far from two of them is
/// described by the more conservative one rather than by whichever happened to be
/// declared first.
pub fn closest_profile(document: Option<&Value>) -> ProfileReport {
    PROFILES
        .iter()
        .map(|profile| report(profile, document))
        .min_by_key(|report| report.deviations.len())
        .expect("the profile catalogue is never empty")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    /// The shipped example config, which is the node's own record of every key it
    /// reads. Checking the catalogue against it is what keeps a lever from
    /// pointing at a key that was renamed on the Python side.
    fn example_config() -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../config.example.yaml");
        let text = fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
        serde_yaml::from_str(&text).expect("config.example.yaml does not parse")
    }

    fn document(yaml: &str) -> Value {
        serde_yaml::from_str(yaml).expect("test fixture does not parse")
    }

    mod catalogue {
        use super::*;

        /// A lever that writes a key the node does not read is a lever that does
        /// nothing, and looks like it worked. The example config is the reference
        /// because it is what the node ships and what an install starts from.
        #[test]
        fn every_lever_writes_a_key_that_exists() {
            let config = example_config();
            for lever in levers() {
                for path in lever.paths() {
                    assert!(
                        value_at(Some(&config), path).is_some(),
                        "lever {} writes {path}, which config.example.yaml does not define",
                        lever.id
                    );
                }
            }
        }

        #[test]
        fn every_profile_writes_a_key_that_exists() {
            let config = example_config();
            for profile in profiles() {
                for (path, _) in profile.writes {
                    assert!(
                        value_at(Some(&config), path).is_some(),
                        "profile {} writes {path}, which config.example.yaml does not define",
                        profile.id
                    );
                }
            }
        }

        /// Two levers writing the same key would fight: setting one would push the
        /// other into `custom` on a decision its owner never touched. One key, one
        /// owning lever -- which is also what makes the derived state trustworthy.
        #[test]
        fn no_key_is_owned_by_two_levers() {
            let mut owner: Vec<(&str, &str)> = Vec::new();
            for lever in levers() {
                for path in lever.paths() {
                    if let Some((_, other)) = owner.iter().find(|(key, _)| *key == path) {
                        panic!("{path} is written by both {other} and {}", lever.id);
                    }
                    owner.push((path, lever.id));
                }
            }
        }

        #[test]
        fn lever_ids_are_unique() {
            let mut seen: Vec<&str> = Vec::new();
            for lever in levers() {
                assert!(!seen.contains(&lever.id), "duplicate lever id {}", lever.id);
                seen.push(lever.id);
            }
        }

        /// Two positions that write identical values are indistinguishable once
        /// written, so the page could never show which one the operator chose.
        #[test]
        fn every_position_of_a_lever_is_distinguishable() {
            for lever in levers() {
                let LeverKind::Cycle(states) = lever.kind else {
                    continue;
                };
                for (index, state) in states.iter().enumerate() {
                    for other in &states[index + 1..] {
                        assert_ne!(
                            state.writes, other.writes,
                            "lever {} has two positions writing the same values",
                            lever.id
                        );
                    }
                }
            }
        }

        /// A position that leaves out a key its sibling sets cannot be recognised
        /// once the sibling has been applied: the leftover key stays, and the lever
        /// reads as whichever position happens to still match. Every position of a
        /// cycle therefore has to speak about every key the cycle owns -- except
        /// where a comment on the lever says why it deliberately does not.
        #[test]
        fn every_position_answers_for_every_key_it_shares() {
            for lever in levers() {
                let LeverKind::Cycle(states) = lever.kind else {
                    continue;
                };
                // `debug-mode` is the documented exception: switching it off must
                // not delete the failure directories the operator turned it on to
                // read, so `off` speaks only for the three log keys.
                if lever.id == "debug-mode" {
                    continue;
                }
                let owned = lever.paths();
                for state in states {
                    for path in &owned {
                        assert!(
                            state.writes.iter().any(|(key, _)| key == path),
                            "lever {}: position {} says nothing about {path}",
                            lever.id,
                            state.label
                        );
                    }
                }
            }
        }

        /// Every organelle has to have something in it, or the layout draws an
        /// empty box the operator cannot do anything with.
        #[test]
        fn every_organelle_holds_at_least_one_lever() {
            for organelle in Organelle::ALL {
                assert!(
                    !organelle.levers().is_empty(),
                    "{} has no levers",
                    organelle.title()
                );
            }
        }

        /// A closed set of values on a lever has to be a set the node accepts.
        /// These mirror the enums documented in config.example.yaml.
        #[test]
        fn enum_positions_write_values_the_node_accepts() {
            let accepted: &[(&str, &[&str])] = &[
                ("network.DELEGATION_TUNNEL_POLICY", &["auto", "always", "never"]),
                ("workload_admission.POLICY", &["fail_fast", "full"]),
                ("workload_admission.ON_UNSATISFIABLE", &["reject", "warn"]),
                ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", &["deny", "allowlist"]),
                ("ui.DISPLAY_UNIT", &["erg", "mu"]),
                ("activity_window.ON_CLOSE", &["refuse", "stop"]),
                ("virtualizers.ch.SERIAL_MODE", &["file", "off", "null", "tty"]),
            ];
            let mut writes: Vec<(&str, &str)> = Vec::new();
            for lever in levers() {
                if let LeverKind::Cycle(states) = lever.kind {
                    for state in states {
                        writes.extend(state.writes.iter().copied());
                    }
                }
            }
            for profile in profiles() {
                writes.extend(profile.writes.iter().copied());
            }
            for (path, value) in writes {
                if let Some((_, values)) = accepted.iter().find(|(key, _)| *key == path) {
                    assert!(
                        values.contains(&value),
                        "{path} is written as {value}, which is not one of {values:?}"
                    );
                }
            }
        }
    }

    mod derived_state {
        use super::*;

        #[test]
        fn a_document_matching_a_position_reads_as_that_position() {
            let lever = lever("outside-work").unwrap();
            let open = document("client:\n  ACCEPT_NEW_DEPOSITS: true\n");
            assert_eq!(status(lever, Some(&open)), LeverStatus::State(1));
            let closed = document("client:\n  ACCEPT_NEW_DEPOSITS: false\n");
            assert_eq!(status(lever, Some(&closed)), LeverStatus::State(0));
        }

        /// The whole point of deriving rather than storing: a combination the
        /// catalogue has no name for is reported as unnamed, not rounded to the
        /// nearest position. Rounding would make the page misreport the policy the
        /// node is actually running.
        #[test]
        fn a_combination_no_position_describes_reads_as_custom() {
            let lever = lever("descendants").unwrap();
            let mixed = document("workload_admission:\n  POLICY: full\n  ON_UNSATISFIABLE: reject\n");
            assert_eq!(status(lever, Some(&mixed)), LeverStatus::Custom);
        }

        #[test]
        fn keys_absent_from_the_file_read_as_unset_rather_than_as_a_position() {
            let lever = lever("outside-work").unwrap();
            assert_eq!(status(lever, Some(&document("client: {}\n"))), LeverStatus::Unset);
            assert_eq!(status(lever, None), LeverStatus::Unset);
        }

        /// A list-valued lever is compared as a value, not as text, so quoting and
        /// flow style in the file cannot make an equal list look different.
        #[test]
        fn a_list_valued_position_is_recognised_whatever_the_yaml_style() {
            let lever = lever("service-egress").unwrap();
            let flow = document("service_networks:\n  blacklist: [\"*\"]\n  whitelist: []\n");
            let block = document("service_networks:\n  blacklist:\n    - '*'\n  whitelist: []\n");
            assert_eq!(status(lever, Some(&flow)), LeverStatus::State(1));
            assert_eq!(status(lever, Some(&block)), LeverStatus::State(1));
        }

        #[test]
        fn a_scalar_lever_reports_the_value_on_disk() {
            let lever = lever("front-door").unwrap();
            let document = document("network:\n  GATEWAY_PORT: 58443\n");
            assert_eq!(
                status(lever, Some(&document)),
                LeverStatus::Value("58443".to_string())
            );
            assert_eq!(status(lever, Some(&document)).label(lever), "58443/tcp");
        }

        /// A secret is a secret on this page too: the row says whether it is set
        /// and never what it is.
        #[test]
        fn a_secret_lever_never_renders_its_value() {
            let lever = lever("identity-mnemonic").unwrap();
            let document = document("identity:\n  MNEMONIC: \"abandon abandon ability\"\n");
            let label = status(lever, Some(&document)).label(lever);
            assert_eq!(label, "•••••• set");
            assert!(!label.contains("abandon"));
        }

        #[test]
        fn cycling_wraps_and_a_custom_combination_starts_over() {
            let lever = lever("tunnel-policy").unwrap();
            assert_eq!(next_state(lever, &LeverStatus::State(0)), Some(1));
            assert_eq!(next_state(lever, &LeverStatus::State(2)), Some(0));
            assert_eq!(next_state(lever, &LeverStatus::Custom), Some(0));
            assert_eq!(next_state(lever, &LeverStatus::Unset), Some(0));
        }

        /// The two decisions the WALL organelle exists for, read back off a document.
        /// Both write more than one key, which is the reason they are levers rather
        /// than something to find on the Config page: an operator who set
        /// `activity_window.ENABLED` and left `ON_CLOSE` alone would have a window
        /// whose closing behaviour they never chose.
        #[test]
        fn the_after_hours_lever_reads_back_what_the_document_says() {
            let lever = lever("after-hours").unwrap();
            let stopping = document(
                "activity_window:\n  ENABLED: true\n  ON_CLOSE: stop\n",
            );
            assert_eq!(status(lever, Some(&stopping)), LeverStatus::State(2));
            assert_eq!(status(lever, Some(&stopping)).label(lever), "stop work");

            let refusing = document(
                "activity_window:\n  ENABLED: true\n  ON_CLOSE: refuse\n",
            );
            assert_eq!(status(lever, Some(&refusing)), LeverStatus::State(1));

            let open = document(
                "activity_window:\n  ENABLED: false\n  ON_CLOSE: refuse\n",
            );
            assert_eq!(status(lever, Some(&open)), LeverStatus::State(0));
        }

        /// A share of 0 is "no cap", not "nothing allowed", and the page has to say so:
        /// a row reading `0` where the operator expects a ceiling is the one misreading
        /// that would have them believe the node is capped when it is wide open.
        #[test]
        fn a_share_of_zero_reads_as_no_cap() {
            let lever = lever("ram-ceiling").unwrap();
            let uncapped = document("host_limits:\n  MAX_RAM_SHARE: 0\n");
            assert_eq!(status(lever, Some(&uncapped)).label(lever), "no cap");
            let half = document("host_limits:\n  MAX_RAM_SHARE: 0.5\n");
            assert_eq!(status(lever, Some(&half)).label(lever), "half");
            // A figure typed on the Config page is reported as unnamed rather than
            // rounded to the nearest position.
            let typed = document("host_limits:\n  MAX_RAM_SHARE: 0.6\n");
            assert_eq!(status(lever, Some(&typed)), LeverStatus::Custom);
        }

        /// The network ceilings are figures, not postures, so they are read straight
        /// off the file with the unit they are quoted in.
        #[test]
        fn the_network_ceilings_report_their_units() {
            let per_day = lever("net-per-day").unwrap();
            let document = document(
                "host_limits:\n  MAX_NET_GIB_PER_DAY: 20\n  MAX_NET_MIB_PER_SECOND: 5\n",
            );
            assert_eq!(status(per_day, Some(&document)).label(per_day), "20 GiB/day");
            let rate = lever("net-rate").unwrap();
            assert_eq!(status(rate, Some(&document)).label(rate), "5 MiB/s");
        }

        #[test]
        fn a_link_has_no_state_to_cycle() {
            let lever = lever("prices").unwrap();
            assert_eq!(status(lever, None), LeverStatus::Link);
            assert_eq!(next_state(lever, &LeverStatus::Link), None);
        }
    }

    mod diffs {
        use super::*;

        #[test]
        fn only_keys_that_would_change_are_listed() {
            let lever = lever("descendants").unwrap();
            let LeverKind::Cycle(states) = lever.kind else {
                panic!("descendants is a cycle");
            };
            let document = document("workload_admission:\n  POLICY: fail_fast\n  ON_UNSATISFIABLE: warn\n");
            let changes = changes(states[0].writes, Some(&document));
            assert_eq!(changes.len(), 1);
            assert_eq!(changes[0].path, "workload_admission.ON_UNSATISFIABLE");
            assert_eq!(changes[0].from.as_deref(), Some("warn"));
            assert_eq!(changes[0].to, "reject");
        }

        /// A key the file has never carried is reported as a creation rather than
        /// as a change from nothing, so the confirmation does not claim a previous
        /// value that never existed.
        #[test]
        fn a_key_absent_from_the_file_is_reported_with_no_previous_value() {
            let lever = lever("outside-work").unwrap();
            let LeverKind::Cycle(states) = lever.kind else {
                panic!("outside-work is a cycle");
            };
            let changes = changes(states[1].writes, Some(&document("client: {}\n")));
            assert_eq!(changes.len(), 1);
            assert_eq!(changes[0].from, None);
        }

        #[test]
        fn a_document_already_in_the_wanted_state_produces_no_changes() {
            let lever = lever("outside-work").unwrap();
            let LeverKind::Cycle(states) = lever.kind else {
                panic!("outside-work is a cycle");
            };
            let document = document("client:\n  ACCEPT_NEW_DEPOSITS: true\n");
            assert!(changes(states[1].writes, Some(&document)).is_empty());
        }
    }

    mod postures {
        use super::*;

        #[test]
        fn profile_ids_and_labels_are_unique() {
            let mut seen: Vec<&str> = Vec::new();
            for profile in profiles() {
                assert!(!seen.contains(&profile.id), "duplicate profile {}", profile.id);
                seen.push(profile.id);
            }
        }

        /// Applying a profile has to leave every lever it touches on a position the
        /// page can name. A profile that pushed a lever into `custom` would leave
        /// the operator looking at a page that cannot describe what it just did.
        #[test]
        fn applying_a_profile_leaves_every_lever_it_touches_on_a_named_position() {
            let mut document = example_config();
            for profile in profiles() {
                let mut applied = document.clone();
                for (path, value) in profile.writes {
                    write_into(&mut applied, path, parsed(value));
                }
                for lever in levers() {
                    let touched = lever
                        .paths()
                        .iter()
                        .any(|path| profile.writes.iter().any(|(key, _)| key == path));
                    if !touched {
                        continue;
                    }
                    let state = status(lever, Some(&applied));
                    assert!(
                        !matches!(state, LeverStatus::Custom),
                        "profile {} leaves lever {} in a state the page cannot name",
                        profile.id,
                        lever.id
                    );
                }
                document = example_config();
            }
        }

        /// A profile applied to a document reports as an exact match afterwards --
        /// the property the deviation report depends on.
        #[test]
        fn a_profile_applied_reports_no_deviations() {
            for profile in profiles() {
                let mut applied = example_config();
                for (path, value) in profile.writes {
                    write_into(&mut applied, path, parsed(value));
                }
                let report = report(profile, Some(&applied));
                assert!(
                    report.deviations.is_empty(),
                    "profile {} still deviates from itself: {:?}",
                    profile.id,
                    report.deviations
                );
                assert_eq!(report.matched(), report.total);
                assert!(report.summary().ends_with("exact match"));
            }
        }

        #[test]
        fn the_closest_profile_is_the_one_with_the_fewest_deviations() {
            let workbench = profiles().iter().find(|p| p.id == "workbench").unwrap();
            let mut applied = example_config();
            for (path, value) in workbench.writes {
                write_into(&mut applied, path, parsed(value));
            }
            // One key off the posture, so it is still the closest one and says so.
            write_into(&mut applied, "logs.TUNNEL_LOGS", Value::Bool(false));
            let report = closest_profile(Some(&applied));
            assert_eq!(report.profile.id, "workbench");
            assert_eq!(report.deviations.len(), 1);
            assert_eq!(report.summary(), "WORKBENCH · 1 deviation");
        }

        /// No profile writes an identity, a wallet, a filesystem path or a
        /// core-service id. A posture decides how the node behaves, never who it is
        /// or where its things live -- and an operator applying one has to be able
        /// to trust that without reading the table.
        #[test]
        fn no_profile_touches_identity_paths_or_wallets() {
            let forbidden = [
                "identity.",
                "ledgers.",
                "main.",
                "dependencies.",
                "core_services.",
                "publisher.TOKEN",
                "virtualizers.ch.KERNEL_PATHS",
                "network.GATEWAY_PORT",
                "network.PUBLIC_IP",
                "ddns.DOMAIN",
                "ddns.TOKEN",
            ];
            for profile in profiles() {
                for (path, _) in profile.writes {
                    for prefix in forbidden {
                        assert!(
                            !path.starts_with(prefix),
                            "profile {} writes {path}, which is not policy",
                            profile.id
                        );
                    }
                }
            }
        }
    }

    /// Set `path` in `document`, creating the intermediate maps a real yq write
    /// would create. Only the tests need this: the app writes through yq.
    fn write_into(document: &mut Value, path: &str, value: Value) {
        let keys: Vec<&str> = path.split('.').collect();
        let mut cursor = document;
        for key in &keys[..keys.len() - 1] {
            if !cursor.is_mapping() {
                *cursor = Value::Mapping(Default::default());
            }
            let map = cursor.as_mapping_mut().expect("just made it a mapping");
            cursor = map
                .entry(Value::String((*key).to_string()))
                .or_insert_with(|| Value::Mapping(Default::default()));
        }
        if !cursor.is_mapping() {
            *cursor = Value::Mapping(Default::default());
        }
        cursor.as_mapping_mut().expect("just made it a mapping").insert(
            Value::String(keys[keys.len() - 1].to_string()),
            value,
        );
    }
}
