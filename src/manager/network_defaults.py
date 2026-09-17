"""Operator-provided seeds shared by all communication-domain resolvers."""
from urllib.parse import urlparse
import socket

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

DEFAULT_INSTANCES_KEY = "service_networks.default_instances"


def configured_endpoints(tag, config=None):
    config = config if config is not None else ConfigManager()
    mapping = config.get(DEFAULT_INSTANCES_KEY, {}) or {}
    if not isinstance(mapping, dict):
        logger(f"[NETWORK] {DEFAULT_INSTANCES_KEY} must be a tag-to-URI mapping; ignored.")
        return []
    entries = mapping.get(tag, [])
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        return []
    return list(dict.fromkeys(e.strip() for e in entries if isinstance(e, str) and e.strip()))


def endpoint_addresses(endpoints):
    """Resolve valid URI seeds to IPv4/port pairs; a bad seed cannot hide good ones.

    Arbitrary schemes need an explicit port; only http/https have inferred ports.
    PoW uses its own URL verifier instead: these are seeds, not a validation bypass.
    """
    found = []
    for endpoint in endpoints:
        try:
            parsed = urlparse(endpoint if "://" in endpoint else "//" + endpoint)
            port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
            if not parsed.hostname or not port or parsed.username or parsed.password:
                continue
            for info in socket.getaddrinfo(parsed.hostname, port, socket.AF_INET, socket.SOCK_STREAM):
                address = (info[4][0], port)
                if address not in found:
                    found.append(address)
        except (ValueError, OSError):
            continue
    return found
