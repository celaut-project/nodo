"""Which address the node advertises about itself on its reputation proof.

A LAN address must never reach the ledger: it is useless to whoever reads the proof.
"""
from src.utils.network import resolve_public_host


def test_configured_public_ip_wins_over_the_outbound_one():
    assert resolve_public_host("81.2.3.4", "192.168.1.50") == "81.2.3.4"


def test_configured_dns_name_is_advertised_as_is():
    assert resolve_public_host("node.example.org", None) == "node.example.org"


def test_outbound_ip_is_used_when_nothing_is_configured():
    # Node with a directly routable address (a VPS): no configuration needed.
    assert resolve_public_host("", "81.2.3.4") == "81.2.3.4"


def test_private_outbound_ip_is_not_advertised():
    # Behind NAT: publishing 192.168.x.x would tell the network nothing.
    assert resolve_public_host("", "192.168.1.50") is None
    assert resolve_public_host("", "10.0.0.7") is None
    assert resolve_public_host("", "172.16.4.1") is None


def test_loopback_and_link_local_are_not_advertised():
    assert resolve_public_host("", "127.0.0.1") is None
    assert resolve_public_host("", "169.254.10.2") is None


def test_a_configured_private_ip_is_also_rejected():
    # The ledger is public; an explicitly configured LAN address is still a LAN address.
    assert resolve_public_host("192.168.1.50", "81.2.3.4") is None


def test_no_address_at_all():
    assert resolve_public_host("", None) is None
    assert resolve_public_host("   ", "") is None


def test_public_ipv6_is_advertised():
    assert resolve_public_host("", "2001:db8::1") is None  # documentation range, not global
    assert resolve_public_host("2a00:1450:4003:80f::200e", None) == "2a00:1450:4003:80f::200e"
