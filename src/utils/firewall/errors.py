"""Exception types shared by the whole firewall package.

Its own module so that ``rules`` (the leaf data model) and ``backends`` (which
imports it) can raise the same hierarchy without a cycle. A malformed rule is a
firewall failure as far as a caller is concerned, so ``RuleError`` is both a
``FirewallError`` and a ``ValueError``.
"""


class FirewallError(Exception):
    """A netfilter operation failed."""


class FirewallUnavailable(FirewallError):
    """Neither nft nor iptables can be used on this host."""


class RuleError(FirewallError, ValueError):
    """A rule that could not be built. Never rendered, never applied."""
