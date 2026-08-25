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
3. ``_build_network_resolution`` in the virtualizer -- defence in depth. Reaching
   it means 1 and 2 were bypassed, so it aborts the launch instead of quietly
   dropping the network.

The policy applies to every service, including the core services the node starts
for itself (packer, source-application, low-demand-fallback). Their egress is still
egress from this node, and exempting them would make ``blacklist: ["*"]`` a claim
the node does not keep; an operator who needs one of them whitelists what it needs.
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

    ``policy`` defaults to whatever ``config.yaml`` currently says, read through the
    shared :class:`ConfigManager` -- which reloads the file when it changes on disk,
    so an edited policy takes effect without a restart.
    """
    resolved = policy if policy is not None else NetworkPolicy.from_config()
    rejection = resolved.check(networks=networks, subject=subject)
    if rejection is not None:
        raise NetworkPolicyRejection(rejection)
