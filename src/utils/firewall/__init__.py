"""Host firewall handling: backend detection, rule management, real verification."""

from src.utils.firewall.backends import (
    AppliedRule,
    FirewallBackend,
    ForeignRejector,
    InputRule,
    IptablesBackend,
    NftBackend,
    detect_backend,
)
from src.utils.firewall.errors import FirewallError, FirewallUnavailable, RuleError
from src.utils.firewall.rules import Chain, Rule, Verdict
from src.utils.firewall.gateway import (
    GATEWAY_COMMENT_PREFIX,
    GatewayPortUnavailable,
    assign_gateway_port,
    cleanup_legacy_rules,
    ensure_gateway_port_open,
    gateway_comment,
    unassigned_port_error,
)
from src.utils.firewall.reachability import ProbeResult, probe_tcp_from_bridge

__all__ = [
    "AppliedRule",
    "Chain",
    "Rule",
    "RuleError",
    "Verdict",
    "GATEWAY_COMMENT_PREFIX",
    "FirewallBackend",
    "FirewallError",
    "FirewallUnavailable",
    "ForeignRejector",
    "GatewayPortUnavailable",
    "InputRule",
    "IptablesBackend",
    "NftBackend",
    "ProbeResult",
    "assign_gateway_port",
    "cleanup_legacy_rules",
    "detect_backend",
    "ensure_gateway_port_open",
    "gateway_comment",
    "probe_tcp_from_bridge",
    "unassigned_port_error",
]
