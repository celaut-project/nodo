"""The address an instance is published at on this host's own interface.

`nodo execute` normally hands back the instance's internal address, which is only
meaningful on the machine the node runs on (#437). Some hosts put the operator's own
tools outside the network namespace the node runs in -- a node inside a VM, driven
from the machine that hosts the VM, is the usual case -- and there the internal
address answers nothing the operator can use. ``network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE``
covers that: instances started by this node's own local clients are also published
on a port of the host interface, and that address is what `execute` prints.

The address is taken from explicit configuration only -- ``network.PUBLIC_IP``, then
``network.EXTERNAL_INTERFACE``, then the interface of the default IPv4 route -- and
never from the caller's address. The CLI talks to its own gateway over loopback, so
falling back to the caller would advertise ``127.0.0.1`` and look like it worked.
A loopback, link-local or unspecified address is refused instead: the exposure fails
loudly and the instance stays internal.

This only ever publishes on the host's own interface. Reaching an instance from
anywhere else is still `nodo tunnel`'s job (docs/TUNNELING.md).
"""
import ipaddress
from typing import Optional

import netifaces as ni


HOST_EXPOSURE_KEY = "network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE"


class HostInterfaceUnresolved(RuntimeError):
    """No usable (non-loopback, non-link-local) host address could be resolved."""


def _unusable_reason(ip: str) -> Optional[str]:
    """Why ``ip`` cannot be advertised, or None when it can. Non-IP text (a DNS name) passes."""
    candidate = str(ip).split("%", 1)[0].strip()
    if candidate.lower() == "localhost":
        return "it is loopback"
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if address.is_loopback:
        return "it is loopback"
    if address.is_link_local:
        return "it is link-local"
    if address.is_unspecified:
        return "it is unspecified"
    return None


def interface_ip(interface: str) -> str:
    """First advertisable address on ``interface``, IPv4 before IPv6."""
    try:
        addresses = ni.ifaddresses(interface)
    except (ValueError, OSError) as e:
        raise HostInterfaceUnresolved(f"interface {interface!r} does not exist ({e})") from e
    for family in (ni.AF_INET, ni.AF_INET6):
        for entry in addresses.get(family, []):
            candidate = str(entry.get("addr", "")).split("%", 1)[0]
            if candidate and _unusable_reason(candidate) is None:
                return candidate
    raise HostInterfaceUnresolved(
        f"interface {interface!r} has no address other than loopback/link-local"
    )


def _default_route_interface() -> str:
    try:
        route = ni.gateways().get("default", {}).get(ni.AF_INET)
    except Exception:
        return ""
    return str(route[1]) if route and len(route) > 1 else ""


def resolve_host_interface_ip(public_ip: str = "", external_interface: str = "") -> str:
    """The address to publish host-exposed instances at, or ``HostInterfaceUnresolved``.

    ``public_ip`` and ``external_interface`` are ``network.PUBLIC_IP`` and
    ``network.EXTERNAL_INTERFACE``; the first one set wins, and an unusable value is an
    error rather than a reason to try the next source -- the operator named it.
    """
    public_ip = str(public_ip or "").strip()
    if public_ip:
        reason = _unusable_reason(public_ip)
        if reason:
            raise HostInterfaceUnresolved(f"network.PUBLIC_IP={public_ip} cannot be advertised: {reason}")
        return public_ip

    external_interface = str(external_interface or "").strip()
    if external_interface:
        return interface_ip(external_interface)

    default_interface = _default_route_interface()
    if default_interface:
        return interface_ip(default_interface)

    raise HostInterfaceUnresolved(
        "network.PUBLIC_IP and network.EXTERNAL_INTERFACE are empty and there is no default route"
    )


def resolve_from_config(get) -> str:
    """``resolve_host_interface_ip`` fed from a config getter (``ConfigManager.get``)."""
    return resolve_host_interface_ip(
        public_ip=get("network.PUBLIC_IP", ""),
        external_interface=get("network.EXTERNAL_INTERFACE", ""),
    )
