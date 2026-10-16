"""Operator policy over the communication domains a service may declare.

A service asks for egress by declaring ``Service.Network`` entries (docs/NETWORKS.md).
Two controls already narrow that ask, and neither of them is the operator's:
``filter_networks_with_ancestors`` intersects the request with what every local
ancestor declared, and the per-VM firewall only opens the destinations that were
actually resolved. Nothing let the person running the node say "not from my node".

This module is that statement -- two glob lists in ``config.yaml``, evaluated over
the tags of every network a service declares:

    service_networks:
      blacklist:
        - "*google.com"
      whitelist:
        - "dns:*"
        - "pow:bitcoin"

Semantics:

* **The blacklist wins.** It is evaluated first, over every tag, so a tag that is
  on both lists is rejected and the report names the blacklist.
* **An empty or absent list restricts nothing.** Both empty is the shipped default,
  so a node that never configures this behaves exactly as it did before.
* **Matching is glob** (:mod:`fnmatch`), case-insensitive on every platform:
  patterns and tags are lowercased before comparison, rather than left to
  ``fnmatch``'s platform-dependent case folding. It is glob over the tag and
  nothing more -- ``"google.com"`` does not match ``www.google.com``, so a domain
  is written ``"*google.com"`` when the subdomains are meant too.
* **A service declaring no network is always accepted**: it asked for no domain.
* **Every tag of every declared network has to pass.** A network is not one
  destination, it is as many as it names: ``resolve_network`` walks the tags one by
  one and stops at the first that resolves, and the firewall reads them one by one
  too (the allow-all-egress check looks at single tags). A tag nobody vetted is a
  destination nobody vetted.
* ``blacklist: ["*"]`` therefore refuses every service that declares any tagged
  network, which is how an operator says "nothing beyond this node".

A tag that is empty once stripped, and a network that declares no tag at all, are
skipped rather than judged: they name no destination, the resolver ignores them and
the firewall opens nothing for them.

Enforced at three points, each on its own grounds:

1. ``launch_service`` -- before the balancer, so the rejection covers delegation
   too. A node that refuses to reach a domain itself and then pays a peer to reach
   it has not applied a policy, it has outsourced one.
2. ``GetServiceEstimatedCostIterable`` -- this node does not quote a service it
   would refuse to run, so a peer's balancer never selects it and then fails.
3. ``rootfs.build_network_resolution`` in the virtualizer -- defence in depth.
   Reaching it means 1 and 2 were bypassed, so it aborts the launch instead of
   quietly dropping the network. It judges the network as **declared**, before any
   ``${VAR}`` substitution (#385), so whether a launch is refused never depends on
   which variables the launcher happened to set: a service cannot evade a blacklist
   by leaving one unanswered and having the network deferred out from under the
   check.

The policy applies to every service, including the core services the node starts
for itself (packer, source-application, low-demand-fallback). Their egress is still
egress from this node, and exempting them would make ``blacklist: ["*"]`` a claim
the node does not keep; an operator who needs one of them whitelists what it needs.

**Hidden mode** (``routing.HIDDEN``) adds a second, different refusal: the networks
whose traffic would leave this host directly, and so show its IP address to whoever is
at the other end. It is not about the domain, it is about who reaches it:

* the open egress ``*``, a ``pow:<chain>`` network (the host checks each candidate and
  the guest connects to it), a host name, and a tag with operator seeds
  (``service_networks.default_instances``) are refused **for local execution**;
* a network whose only members are instances this node runs is allowed: that traffic
  stays on the bridge.

With ``routing.tor.SERVICE_EGRESS`` (the default) none of this is refused: the
services' TCP goes through Tor instead (``policy.tor_redirect_rules``), and only a Tor
that is not ready yet makes the launcher skip this node. Without it, a peer can still
run a refused service: the peer's IP is the one that shows, and this node calls the
peer through Tor. So ``enforce_hidden_mode`` is not applied before the
balancer, as the operator lists are. The launcher skips the local candidate, the cost
quote answers that this node does not run the service, and ``rootfs`` checks again.
"""
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

import fnmatch

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager

# The block is `service_networks`, not `networks`: that would be one letter from
# the unrelated `network:` block, which configures this node's own ports and
# addresses. A typo between the two would apply no policy at all while looking
# configured, and that is the one failure mode a control like this must not have.
CONFIG_BLOCK = "service_networks"
BLACKLIST_KEY = f"{CONFIG_BLOCK}.blacklist"
WHITELIST_KEY = f"{CONFIG_BLOCK}.whitelist"

# A `networks:` block carrying these keys is a config written against the name this
# block does not have. It is reported instead of ignored, for the reason above.
_MISTAKEN_BLOCK = "networks"

_env_manager = ConfigManager()

HIDDEN_RULE = "routing.HIDDEN"
# Copies of the constants of the resolvers, kept here so that this module stays cheap
# to import. tests/test_hidden_network_policy.py checks that they agree.
ALL_EGRESS_TAG = "*"
POW_TAG_PREFIX = "pow:"
SEEDS_KEY = "service_networks.default_instances"
SERVICE_EGRESS_KEY = "routing.tor.SERVICE_EGRESS"


class NetworkPolicyConfigError(Exception):
    """The policy itself could not be read, so nothing is evaluated against it.

    Raised rather than falling back to "no restrictions": a list the node failed to
    read is not a list that allowed everything.
    """


class NetworkPolicyRejection(Exception):
    """A service declares a communication domain this node refuses to serve.

    Carries the full report (``str(exc)``) and the :class:`Rejection` that produced
    it, so a caller can log one and forward the other.
    """

    def __init__(self, rejection: "Rejection"):
        self.rejection = rejection
        super().__init__(rejection.report())


@dataclass(frozen=True)
class Rejection:
    """Which tag was refused, by which rule, and against which pattern."""

    tag: str
    network_index: int
    rule: str
    # The blacklist pattern that matched. None for a whitelist miss, where what
    # refused the tag is the absence of a match against `candidates`.
    pattern: Optional[str]
    declared: Tuple[Tuple[str, ...], ...]
    candidates: Tuple[str, ...]
    subject: str = ""
    # Set for a hidden-mode refusal: why this tag would leave the host directly.
    reason: str = ""

    def report(self) -> str:
        """The rejection as the client reads it: what it asked for, and what said no.

        Names every declared network and not just the offending one, because the
        client sent a set and a report about one tag of it does not say whether the
        rest would have passed.
        """
        declared = "\n".join(
            f"    #{index}: {', '.join(tags) if tags else '<no tags>'}"
            for index, tags in enumerate(self.declared, start=1)
        ) or "    <none>"

        if self.pattern is not None:
            matched = f"  pattern:           {self.pattern}"
        else:
            listed = ", ".join(self.candidates) if self.candidates else "<empty>"
            matched = f"  matched none of:   {listed}"

        subject = f" for {self.subject}" if self.subject else ""
        if self.rule == HIDDEN_RULE:
            return (
                f"Hidden mode: this node does not run a service{subject} that reaches "
                f"'{self.tag}' itself.\n"
                f"  declared networks:\n{declared}\n"
                f"  rejected tag:      {self.tag} (network #{self.network_index})\n"
                f"  rule:              {HIDDEN_RULE}\n"
                f"  reason:            {self.reason}\n"
                f"Its traffic would leave this host directly and show its IP address. "
                f"A peer that is not hidden can run it. See docs/CONFIG.md, 'Hidden mode'."
            )
        return (
            f"Network policy: this node refuses to run a service{subject} that reaches "
            f"'{self.tag}'.\n"
            f"  declared networks:\n{declared}\n"
            f"  rejected tag:      {self.tag} (network #{self.network_index})\n"
            f"  rule:              {self.rule}\n"
            f"{matched}\n"
            f"The blacklist is checked first and wins over the whitelist; a non-empty "
            f"whitelist has to cover every tag declared. These lists are the node "
            f"operator's, set in config.yaml under '{CONFIG_BLOCK}:' -- see "
            f"docs/NETWORKS.md."
        )


def _normalize(patterns: Any, key: str) -> Tuple[str, ...]:
    """Read one list from the config as lowercase, non-empty glob patterns.

    A bare string is read as a one-element list (``blacklist: "*"`` means what it
    looks like). Anything else that is not a sequence is a config error rather than
    an empty policy.
    """
    if patterns is None:
        return ()
    if isinstance(patterns, str):
        patterns = [patterns]
    if isinstance(patterns, dict) or not isinstance(patterns, (list, tuple, set)):
        raise NetworkPolicyConfigError(
            f"{key} must be a list of glob patterns (or a single pattern), got "
            f"{type(patterns).__name__}. Nothing is evaluated against a policy this "
            f"node could not read."
        )
    return tuple(
        stripped for stripped in (str(entry).strip().lower() for entry in patterns if entry is not None)
        if stripped
    )


class NetworkPolicy:
    """The two configured lists, and the verdict they give on a set of networks."""

    def __init__(self, blacklist: Any = (), whitelist: Any = ()):
        # The raw config values are accepted as they come (a list, a bare string,
        # None) and normalized here, so `from_config` has nothing left to do but
        # read the block.
        self.blacklist = _normalize(blacklist, BLACKLIST_KEY)
        self.whitelist = _normalize(whitelist, WHITELIST_KEY)

    @classmethod
    def from_config(cls, env_manager: Optional[ConfigManager] = None) -> "NetworkPolicy":
        manager = env_manager if env_manager is not None else _env_manager
        block = manager.get(CONFIG_BLOCK) or {}
        if not isinstance(block, dict):
            raise NetworkPolicyConfigError(
                f"'{CONFIG_BLOCK}:' must be a mapping with 'blacklist' and 'whitelist' "
                f"entries, got {type(block).__name__}."
            )

        mistaken = manager.get(_MISTAKEN_BLOCK)
        if isinstance(mistaken, dict) and ("blacklist" in mistaken or "whitelist" in mistaken):
            raise NetworkPolicyConfigError(
                f"config.yaml has a '{_MISTAKEN_BLOCK}:' block with blacklist/whitelist "
                f"entries. The network policy block is '{CONFIG_BLOCK}:' -- '"
                f"{_MISTAKEN_BLOCK}' is not read, and this node would run with no policy "
                f"while looking configured. Rename the block."
            )

        return cls(
            blacklist=block.get("blacklist"),
            whitelist=block.get("whitelist"),
        )

    @property
    def restricts(self) -> bool:
        """Whether this policy can reject anything at all."""
        return bool(self.blacklist or self.whitelist)

    def describe(self) -> str:
        if not self.restricts:
            return f"{CONFIG_BLOCK}: no restrictions (both lists empty)"
        return (
            f"{CONFIG_BLOCK}: blacklist=[{', '.join(self.blacklist) or '-'}] "
            f"whitelist=[{', '.join(self.whitelist) or '-'}]"
        )

    def check(
            self,
            networks: Sequence[celaut.Service.Network],
            subject: str = "",
    ) -> Optional[Rejection]:
        """The first reason to refuse ``networks``, or None if the policy allows them.

        The blacklist sweep covers every tag before the whitelist sweep begins, so
        "the blacklist wins" holds across networks and not only within one: a
        service whose first network misses the whitelist and whose second is
        blacklisted is reported as blacklisted, which is the stronger statement.
        """
        if not self.restricts:
            return None

        declared = tuple(tuple(network.tags) for network in networks)
        # (network index, tag as declared, tag as matched)
        tags = [
            (index, tag, tag.strip().lower())
            for index, network in enumerate(networks, start=1)
            for tag in network.tags
            if tag and tag.strip()
        ]
        if not tags:
            return None

        for index, tag, normalized in tags:
            for pattern in self.blacklist:
                if fnmatch.fnmatchcase(normalized, pattern):
                    return Rejection(
                        tag=tag, network_index=index, rule=BLACKLIST_KEY,
                        pattern=pattern, declared=declared,
                        candidates=self.blacklist, subject=subject,
                    )

        if self.whitelist:
            for index, tag, normalized in tags:
                if not any(fnmatch.fnmatchcase(normalized, p) for p in self.whitelist):
                    return Rejection(
                        tag=tag, network_index=index, rule=WHITELIST_KEY,
                        pattern=None, declared=declared,
                        candidates=self.whitelist, subject=subject,
                    )

        return None


def enforce_network_policy(
        networks: Sequence[celaut.Service.Network],
        subject: str = "",
        policy: Optional[NetworkPolicy] = None,
) -> None:
    """Raise :class:`NetworkPolicyRejection` if the policy refuses ``networks``.

    ``policy`` defaults to whatever ``config.yaml`` said when this process loaded it,
    read through the shared :class:`ConfigManager`. An edited policy takes effect on
    the next daemon start; `nodo tui` writes the change and restarts in one step.
    """
    resolved = policy if policy is not None else NetworkPolicy.from_config()
    rejection = resolved.check(networks=networks, subject=subject)
    if rejection is not None:
        raise NetworkPolicyRejection(rejection)


def direct_egress_reason(tag: str, env_manager: Optional[ConfigManager] = None) -> Optional[str]:
    """Why traffic for ``tag`` would leave this host directly, or None if it stays local.

    Mirrors the sources ``networks.resolve_network`` uses for a tag, in its order: a
    ``pow:`` domain, the operator's seeds, then a tag shaped like a host name. ``*`` is
    the open egress the firewall grants for that tag alone. A tag that none of these
    answer resolves only to the instances this node runs.
    """
    manager = env_manager if env_manager is not None else _env_manager
    tag = str(tag or "").strip()
    if not tag:
        return None
    if tag == ALL_EGRESS_TAG:
        return "it opens all egress to the Internet"
    if tag.startswith(POW_TAG_PREFIX):
        return "proof-of-work peers are checked from this host and reached directly"
    seeds = manager.get(SEEDS_KEY, {}) or {}
    if isinstance(seeds, dict) and seeds.get(tag):
        return f"the operator's seeds for it ({SEEDS_KEY}) are outside addresses"
    if tag.islower() and "." in tag:
        return "a host name is reached directly"
    return None


def hidden_mode_rejection(
        networks: Sequence[celaut.Service.Network],
        subject: str = "",
        env_manager: Optional[ConfigManager] = None,
) -> Optional[Rejection]:
    """The first network a hidden node cannot reach itself, or None.

    None when the node is not hidden. Every tag counts: the tags of an entry are
    synonyms, and the resolver uses the first one that answers, so a single tag that
    leaves the host is enough to refuse the entry.
    """
    manager = env_manager if env_manager is not None else _env_manager
    if manager.get(HIDDEN_RULE, False) is not True:
        return None
    if manager.get(SERVICE_EGRESS_KEY, True) is not False:
        # The services' TCP goes through Tor (``routing.tor.SERVICE_EGRESS``), so
        # nothing leaves the host directly. Whether Tor is ready is the launcher's
        # question, at launch time: see ``needs_outside_egress``.
        return None
    declared = tuple(tuple(network.tags) for network in networks)
    for index, network in enumerate(networks, start=1):
        for tag in network.tags:
            reason = direct_egress_reason(tag, manager)
            if reason:
                return Rejection(
                    tag=tag, network_index=index, rule=HIDDEN_RULE, pattern=None,
                    declared=declared, candidates=(), subject=subject, reason=reason,
                )
    return None


def needs_outside_egress(
        networks: Sequence[celaut.Service.Network],
        env_manager: Optional[ConfigManager] = None,
) -> Optional[str]:
    """The first tag of ``networks`` whose traffic leaves this host, or None."""
    for network in networks:
        for tag in network.tags:
            if direct_egress_reason(tag, env_manager):
                return tag
    return None


def tor_egress_unready_reason(
        networks: Sequence[celaut.Service.Network],
) -> Optional[str]:
    """Why this node cannot run ``networks`` right now because Tor is not ready, or None.

    In hidden mode with ``routing.tor.SERVICE_EGRESS``, the outside traffic of a service
    goes through Tor. While Tor's ``TransPort`` is not up, a guest started now would get
    no egress. The launcher then skips this node, and the cost quote must refuse for the
    same reason, or a peer selects this node, pays it, and gets the service from a third
    node. One function, so that the two always agree.
    """
    from src.routing.policy import service_egress_via_tor

    if not service_egress_via_tor():
        return None
    from src.routing import registry

    if registry.TOR.transparent_proxy() is not None:
        return None
    outside_tag = needs_outside_egress(networks)
    if not outside_tag:
        return None
    return f"hidden mode sends '{outside_tag}' through Tor, and Tor is not ready"


class HiddenModeRejection(NetworkPolicyRejection):
    """A hidden node refuses to reach a network itself. A peer may still run it."""


def enforce_hidden_mode(
        networks: Sequence[celaut.Service.Network],
        subject: str = "",
) -> None:
    """Raise :class:`HiddenModeRejection` if a hidden node would reach ``networks`` itself."""
    rejection = hidden_mode_rejection(networks=networks, subject=subject)
    if rejection is not None:
        raise HiddenModeRejection(rejection)


def enforce_hidden_resolution(network: celaut.Service.Network, subject: str = "") -> None:
    """A hidden node does not answer ``ResolveNetwork`` for a ``pow:`` domain.

    Answering means checking each candidate's chain state from this host, which shows
    its IP address to every candidate. The other sources only read the config or ask
    the local resolver, and contact nobody.
    """
    if _env_manager.get(HIDDEN_RULE, False) is not True:
        return
    if _env_manager.get(SERVICE_EGRESS_KEY, True) is not False:
        return  # The candidates are checked through Tor (pow_networks._get_json).
    declared = (tuple(network.tags),)
    for tag in network.tags:
        if str(tag).strip().startswith(POW_TAG_PREFIX):
            raise HiddenModeRejection(Rejection(
                tag=tag, network_index=1, rule=HIDDEN_RULE, pattern=None,
                declared=declared, candidates=(), subject=subject,
                reason="proof-of-work peers are checked from this host",
            ))
