"""``nodo help`` -- the command catalogue, grouped by what the operator is doing.

The catalogue is an ordered list of groups; the printer is a pure function of it,
the same split ``src/commands/nat_guide.py`` documents. So the wording can be
tested without a node, and a new command is one line in the group it belongs to.

Two things read the catalogue: ``nodo help``, which prints all of it, and the bare
``nodo``, which prints only the :data:`QUICK_START` commands with their same
descriptions -- so the two can never drift apart.

Keep it in sync with the ``match sys.argv[1]`` dispatch in ``nodo.py`` and with
``COMMANDS`` in ``src/commands/completion.py``; ``tests/test_help.py`` fails when
a command is added to one and not the others.
"""

from typing import List, Optional, Tuple

Entry = Tuple[str, str]
Group = Tuple[str, Optional[str], List[Entry]]

# (title, aside, [(usage, what it does)]). Ordered as an operator meets them: the
# node itself, then the services it holds, running them, the network around it,
# the money that moves, and last the commands only a developer needs.
GROUPS: List[Group] = [
    (
        "This node",
        None,
        [
            ("tui", "status, peers and the config editor"),
            ("info", "this node's address, state and balances"),
            ("doctor", "check, and fix, what stops it serving"),
            ("daemon start|status|stop|restart", "control the nodo.service systemd unit"),
            ("logs", "follow this node's log"),
            ("envs", "print the effective config.yaml"),
            ("nat-guide", "reach this node from the Internet"),
            ("firewall-compat status|apply|remove", "rules a coexisting firewall must keep"),
            ("completion bash|zsh|install", "tab-completion for commands and ids"),
            ("update", "update nodo itself"),
            ("help", "this list"),
        ],
    ),
    (
        "Services",
        "the local registry: what this node can run",
        [
            ("pack <project dir>", "package a project (a dir, or a git URL)"),
            ("download <url> [-o <dir>]", "fetch a published service from its manifest"),
            ("import <path>", "read a packaged '.celaut' file in"),
            ("export <service> <path>", "write it out as a file (--raw for the tree)"),
            ("publish <service>", "offer a service to the rest of the network"),
            ("services", "list what the registry holds"),
            ("inspect <service>", "show a service's spec and metadata"),
            ("tag <service> <new tag>", "name a service, so its id stays optional"),
            ("remove <service>", "drop a service from the registry"),
            ("integrity [<service>] [--fix]", "check the stored blocks against their hashes"),
        ],
    ),
    (
        "Running services",
        None,
        [
            ("estimate <service>", "what an execution would cost, before paying for it"),
            ("execute <service>", "run it (--remote, --name <name>, -e <key> <value>)"),
            ("instances [<search>]", "list what is running (--grouped by parent)"),
            ("observe <instance>", "watch an instance live (--save <path> records it)"),
            ("tunnel <instance> <slot>", "reach its port from here (--udp, --listen, --peer)"),
            ("kill <instance>", "stop one instance"),
            ("burnall", "stop every instance, parents first (--yes)"),
            ("prune", "reclaim orphaned runtime dirs (--all, --dry-run)"),
        ],
    ),
    (
        "The network",
        None,
        [
            ("peers", "list the peers this node knows"),
            ("connect <ip:port>", "introduce this node to a peer"),
            ("disconnect <peer>", "forget a peer"),
            ("clients", "list the clients that use this node"),
            ("reputation [<peer>]", "what the network stakes on us (--json)"),
            ("verify_reputation <peer>", "validate a peer's reputation proof and ownership"),
            ("submit_reputation", "publish this node's reputation proof on-chain"),
            ("sync_reputation_proof", "reconcile the proof id with what the chain holds"),
        ],
    ),
    (
        "Money",
        "amounts in ui.DISPLAY_UNIT, ERG by default",
        [
            ("donations", "who we fund, who we count (--json)"),
            ("increase_deposit <instance> <amount>", "fund a running instance"),
            ("decrease_deposit <instance> <amount>", "take funds back out of it"),
            ("increase_peer_deposit <peer> <amount>", "top up what a peer holds for us"),
            ("credit_client <client> <amount>", "give a client balance on this node"),
            ("debit_client <client> <amount>", "take that balance back"),
            ("pay <peer> <amount> [--payment-method]",
             "pay a peer in an asset it accepts"),
            ("tx_history", "payments made and received"),
        ],
    ),
    (
        "Development",
        None,
        [
            ("serve", "run the node in the foreground"),
            ("migrate", "recreate the database from scratch"),
            ("test <test name>", "run one test from tests/"),
            ("ggconf <repository path>", "gateway config for a local project"),
            ("force_execution <peer> <service>", "no balancer, no fallback: it must run there"),
            ("local_builder <buildctl args>", "talk to nodo's rootless BuildKit builder"),
            ("storage:prune_blocks", "drop blocks nothing references"),
            ("prune_containers", "sweep dead VMs without waiting"),
            ("refresh_clients", "settle client and peer deposits now"),
            ("refresh_ergo_nodes", "refresh the list of Ergo nodes"),
        ],
    ),
]

# What a bare `nodo` shows: the shortest path from an empty node to a running
# service. Names only -- the descriptions come from GROUPS.
QUICK_START = ["tui", "pack", "download", "publish", "execute", "instances", "observe"]

# Where the prose behind each of these lives, installed alongside nodo.
LONG_FORM = "docs/USAGE.md"

# Descriptions start here, unless a group's own usages are wider.
MIN_DESCRIPTION_COLUMN = 26
INDENT = "  "


def entries() -> List[Entry]:
    """Every documented ``(usage, description)``, in catalogue order."""
    return [entry for _, _, group_entries in GROUPS for entry in group_entries]


def commands() -> List[str]:
    """Every documented command name, in catalogue order."""
    return [usage.split()[0] for usage, _ in entries()]


def _entry(command: str) -> Entry:
    for usage, description in entries():
        if usage.split()[0] == command:
            return usage, description
    raise KeyError(f"{command} is not in the help catalogue")


def render_help() -> str:
    """Compose the whole catalogue. Pure: reads nothing but :data:`GROUPS`."""
    lines: List[str] = [
        "nodo -- your node on the Celaut network",
        "",
        "Usage: nodo <command> [arguments]",
        "",
        "A <service> is a service id or a tag. An <instance> is the token that",
        "'execute' printed for it, or the --name it was given.",
    ]

    for title, aside, group_entries in GROUPS:
        lines.append("")
        lines.append(f"{title}  ({aside})" if aside else title)
        # One description column per group, so a single long usage does not push
        # every other group's text off to the right.
        column = max(MIN_DESCRIPTION_COLUMN, max(len(usage) for usage, _ in group_entries) + 2)
        for usage, description in group_entries:
            lines.append(f"{INDENT}{usage.ljust(column)}{description}")

    lines.append("")
    lines.append(f"The long form of every command is in {LONG_FORM}.")
    return "\n".join(lines) + "\n"


def render_quick_start() -> str:
    """The bare-``nodo`` greeting: a few commands, described as in :func:`render_help`."""
    lines = [
        "Welcome to Nodo -- your node on the Celaut network.",
        "",
        "Getting started",
    ]
    quick = [_entry(command) for command in QUICK_START]
    column = max(MIN_DESCRIPTION_COLUMN, max(len(usage) for usage, _ in quick) + 2)
    for usage, description in quick:
        lines.append(f"{INDENT}{usage.ljust(column)}{description}")
    lines.append("")
    lines.append("Run 'nodo help' for every command.")
    return "\n".join(lines) + "\n"


def print_help() -> None:
    print(render_help(), flush=True)


def print_quick_start() -> None:
    print(render_quick_start(), flush=True)
