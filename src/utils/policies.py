"""Policies: the settings of a service, identified by their ``formal`` bytes (#670).

A setting of a service (a shared directory, a read-only root filesystem) is a
``celaut.Policy``: ``tags`` and ``prose`` for a reader, a ``formal`` that defines the
setting, and a ``value``. Two policies are the same setting only if their ``formal``
fields have the same bytes. ``tags`` and ``prose`` never decide: two authors can use one
tag for two meanings, and for a restriction a different meaning can open what the
author closed.

This module is the one reader of policies. The packer and the node both use it, so the
packer cannot write what a node refuses. The rules, all fail-closed:

* a policy with an empty ``formal`` is invalid (old data in the field number of the old
  ``xattrs`` is read as such a policy, so a service packed in the old form is refused);
* a policy whose ``formal`` the node does not know is refused, and so is a known one
  with a ``value`` that is not valid for it, or in a place where it is not defined;
* in one list two policies do not have the same ``formal``. A sender sorts the list by
  ``formal``; the order does not change the meaning, so a reader does not require it.

The ``formal`` bytes of each setting are in this file and in ``protos/formals/policies/``. A test
pins them to each other. A published ``formal`` never changes: a new meaning is a new
``formal``, therefore a new setting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional

from protos import celaut_pb2 as celaut
from src.identity.node_identity import (
    ComponentFormalError,
    component_formal,
    parse_component_formal,
)

# Places where a policy can be.
PLACE_ENTRY_DIRECTORY = "filesystem.directory"  # a directory entry of a filesystem
PLACE_ENTRY_FILE = "filesystem.file"            # a file, a link or a device entry
PLACE_ROOT = "filesystem.root"                  # the filesystem that Container.filesystem holds
PLACE_NESTED = "filesystem.nested"              # the filesystem of a subdirectory
PLACE_INIT = "init"                             # Container.Init
PLACE_API_SLOT = "api.slot"                     # Service.Api.Slot (#669)

SHARE = "share"
READ_MODE = "read_mode"
ENTRY_METADATA = "entry_metadata"
ACCESS = "access"
TUNNEL_AUTH = "tunnel_auth"
WAKE = "wake"

SHARE_FORMAL: bytes = component_formal({
    "setting": SHARE,
    "place": PLACE_ENTRY_DIRECTORY,
    "keys": "access,env,network,role,tag",
    "role.values": "guest,network,shared",
    "access.default": "rw",
})
READ_MODE_FORMAL: bytes = component_formal({
    "setting": READ_MODE,
    "place": PLACE_ROOT,
    "values": "ro,rw",
    "default": "rw",
})
# The keys that `entry_metadata` defines for `ItemBranch.xattrs`, sorted as in its formal.
ENTRY_METADATA_KEYS = (
    "device.is_block",
    "device.major",
    "device.minor",
    "gid",
    "mode",
    "mtime_ns",
    "uid",
)
ENTRY_METADATA_FORMAL: bytes = component_formal({
    "setting": ENTRY_METADATA,
    "place": PLACE_ROOT,
    "keys": ",".join(ENTRY_METADATA_KEYS),
    "encoding": "base-10 integers as UTF-8 text",
    "entry": "all keys or none",
    "nondevice": "device.is_block, device.major and device.minor are 0",
})

ACCESS_FORMAL: bytes = component_formal({
    "setting": ACCESS,
    "place": PLACE_API_SLOT,
    "values": "any,direct,tunnel",
    "default": "any",
})
TUNNEL_AUTH_FORMAL: bytes = component_formal({
    "setting": TUNNEL_AUTH,
    "place": PLACE_API_SLOT,
    "values": "address,token",
    "default": "token",
})
WAKE_FORMAL: bytes = component_formal({
    "setting": WAKE,
    "place": PLACE_API_SLOT,
    "values": "never,on_tunnel",
    "default": "never",
})

# Values of the settings of a slot (#669).
ACCESS_DIRECT = "direct"
ACCESS_TUNNEL = "tunnel"
ACCESS_ANY = "any"
ACCESS_SLOT_VALUES = (ACCESS_ANY, ACCESS_DIRECT, ACCESS_TUNNEL)
TUNNEL_AUTH_TOKEN = "token"
TUNNEL_AUTH_ADDRESS = "address"
TUNNEL_AUTH_VALUES = (TUNNEL_AUTH_ADDRESS, TUNNEL_AUTH_TOKEN)
WAKE_NEVER = "never"
WAKE_ON_TUNNEL = "on_tunnel"
WAKE_VALUES = (WAKE_NEVER, WAKE_ON_TUNNEL)

# Keys of the value of a `share` policy.
ROLE_SHARED = "shared"
ROLE_GUEST = "guest"
ROLE_NETWORK = "network"
ROLES = (ROLE_GUEST, ROLE_NETWORK, ROLE_SHARED)
ACCESS_RO = "ro"
ACCESS_RW = "rw"
ACCESS_VALUES = (ACCESS_RO, ACCESS_RW)
SHARE_KEYS = frozenset({"access", "env", "network", "role", "tag"})

READ_MODE_RW = "rw"
READ_MODE_RO = "ro"
READ_MODES = (READ_MODE_RW, READ_MODE_RO)

# A tag is matched literally, so what it may contain is pinned down: the match is
# case-sensitive, and the charset excludes anything that could make two tags look alike
# (no whitespace, no control characters, no separators). An env var name is a POSIX one.
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PolicyError(ValueError):
    """A policy that a node does not accept. The message names it and its place."""


def describe(policy: celaut.Policy) -> str:
    """How an error names a policy: by its tags, or by saying that it has none."""
    tags = [t for t in policy.tags if t]
    return "policy " + (", ".join(repr(t) for t in tags) if tags else "(no tags)")


def _text(value: bytes, what: str) -> str:
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{what} is not UTF-8") from None


def parse_share_value(value: bytes) -> Dict[str, str]:
    """The value of a ``share`` policy as ``{key: value}``, or ``ValueError``.

    ``key=value`` lines, sorted by key, UTF-8, joined by a newline, with no empty and
    no repeated key. ``role`` is required. ``network`` is only valid with
    ``role=network``. Another key makes the value not valid.
    """
    try:
        pairs = parse_component_formal(value)
    except ComponentFormalError as e:
        raise ValueError(str(e)) from None
    if component_formal(pairs) != bytes(value):
        raise ValueError(
            "the value is not in canonical form: 'key=value' lines sorted by key, "
            "joined by a newline, with nothing else"
        )
    unknown = sorted(set(pairs) - SHARE_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) {unknown}; the keys are {sorted(SHARE_KEYS)}")
    role = pairs.get("role")
    if role is None:
        raise ValueError("'role' is required")
    if role not in ROLES:
        raise ValueError(f"'role' must be one of {list(ROLES)}, got {role!r}")
    if "network" in pairs and role != ROLE_NETWORK:
        raise ValueError(f"'network' is only valid with role={ROLE_NETWORK}, got role={role}")
    if role == ROLE_NETWORK and not pairs.get("network"):
        raise ValueError(f"role={ROLE_NETWORK} requires 'network': the tag of the network")
    if "access" in pairs and pairs["access"] not in ACCESS_VALUES:
        raise ValueError(f"'access' must be one of {list(ACCESS_VALUES)}, got {pairs['access']!r}")
    for key, pattern in (("tag", TAG_PATTERN), ("env", ENV_NAME_PATTERN), ("network", TAG_PATTERN)):
        if key in pairs and not pattern.match(pairs[key]):
            raise ValueError(f"'{key}' is {pairs[key]!r}, which does not match {pattern.pattern}")
    return pairs


def parse_read_mode_value(value: bytes) -> str:
    """The value of a ``read_mode`` policy: ``"ro"`` or ``"rw"``, or ``ValueError``."""
    text = _text(value, "the value")
    if text not in READ_MODES:
        raise ValueError(f"expected one of {list(READ_MODES)}, got {text!r}")
    return text


def parse_entry_metadata_value(value: bytes) -> bytes:
    """The value of an ``entry_metadata`` policy: empty, or ``ValueError``.

    The setting is a declaration that the tree carries the metadata keys, so it has
    nothing to say in its value. Any content is refused now, because a value that a
    node ignores today would mean something else the day a node reads it.
    """
    if bytes(value):
        raise ValueError("expected an empty value")
    return b""


def _word_parser(values):
    def parse(value: bytes) -> str:
        text = _text(value, "the value")
        if text not in values:
            raise ValueError(f"expected one of {list(values)}, got {text!r}")
        return text
    return parse


@dataclass(frozen=True)
class Setting:
    name: str
    formal: bytes
    places: FrozenSet[str]
    parse: Callable[[bytes], object]


_SETTINGS: Dict[bytes, Setting] = {
    s.formal: s
    for s in (
        Setting(SHARE, SHARE_FORMAL, frozenset({PLACE_ENTRY_DIRECTORY}), parse_share_value),
        Setting(READ_MODE, READ_MODE_FORMAL, frozenset({PLACE_ROOT}), parse_read_mode_value),
        Setting(ENTRY_METADATA, ENTRY_METADATA_FORMAL, frozenset({PLACE_ROOT}),
                parse_entry_metadata_value),
        Setting(ACCESS, ACCESS_FORMAL, frozenset({PLACE_API_SLOT}), _word_parser(ACCESS_SLOT_VALUES)),
        Setting(TUNNEL_AUTH, TUNNEL_AUTH_FORMAL, frozenset({PLACE_API_SLOT}),
                _word_parser(TUNNEL_AUTH_VALUES)),
        Setting(WAKE, WAKE_FORMAL, frozenset({PLACE_API_SLOT}), _word_parser(WAKE_VALUES)),
    )
}


def known_settings() -> List[Setting]:
    return sorted(_SETTINGS.values(), key=lambda s: s.formal)


def check_list(
    policies: Iterable[celaut.Policy],
    place: str,
    where: str,
) -> Dict[str, object]:
    """Validate one ``policies`` list and return ``{setting name: parsed value}``.

    ``where`` names the place in the service for the error (``"directory '/data'"``).
    Raises :class:`PolicyError` for an empty ``formal``, a repeated one, one that is
    not known, a known one in the wrong ``place``, and a value that is not valid.
    """
    out: Dict[str, object] = {}
    seen = set()
    for policy in policies:
        name = describe(policy)
        formal = bytes(policy.formal)
        if not formal:
            raise PolicyError(f"{name} on {where} has an empty formal, so it is not valid")
        if formal in seen:
            raise PolicyError(
                f"{name} on {where} repeats the formal of another policy of the same list"
            )
        seen.add(formal)
        setting = _SETTINGS.get(formal)
        if setting is None:
            raise PolicyError(f"{name} on {where}: this node does not know this setting")
        if place not in setting.places:
            raise PolicyError(
                f"{name} on {where}: the setting '{setting.name}' is not valid in this place "
                f"(valid: {', '.join(sorted(setting.places))})"
            )
        try:
            out[setting.name] = setting.parse(policy.value)
        except ValueError as e:
            raise PolicyError(
                f"{name} on {where}: the value {bytes(policy.value)!r} is not valid for "
                f"'{setting.name}': {e}"
            ) from None
    return out


def make(name: str, value: bytes) -> celaut.Policy:
    """A policy for a known setting, with its ``formal``. For the packer."""
    for setting in _SETTINGS.values():
        if setting.name == name:
            return celaut.Policy(
                tags=[name],
                prose=f"The setting '{name}'. Its definition is protos/formals/policies/{name}.formal.",
                formal=setting.formal,
                value=value,
            )
    raise KeyError(name)


def entry_metadata_declared(filesystem) -> bool:
    """Whether the root filesystem declares ``entry_metadata`` (raises :class:`PolicyError`).

    Pass the Filesystem that ``Container.filesystem`` points at, like ``read_mode``.
    """
    parsed = check_list(
        getattr(filesystem, "policies", None) or [],
        PLACE_ROOT,
        "the root filesystem",
    )
    return ENTRY_METADATA in parsed


def share_value(
    role: str,
    access: Optional[str] = None,
    tag: Optional[str] = None,
    env: Optional[str] = None,
    network: Optional[str] = None,
) -> bytes:
    """The canonical value of a ``share`` policy. Keys that are ``None`` are not written."""
    pairs = {"role": role, "access": access, "tag": tag, "env": env, "network": network}
    return component_formal({k: v for k, v in pairs.items() if v is not None})


def set_policy(policies, policy: celaut.Policy) -> None:
    """Put ``policy`` in a ``repeated Policy`` field, replacing one of the same
    ``formal``, and leave the list sorted by ``formal`` bytes."""
    kept = [p for p in policies if bytes(p.formal) != bytes(policy.formal)]
    kept.append(policy)
    kept.sort(key=lambda p: bytes(p.formal))
    copies = [celaut.Policy.FromString(p.SerializeToString()) for p in kept]
    del policies[:]
    policies.extend(copies)


@dataclass(frozen=True)
class SlotAccess:
    """The settings of one slot, with the default of each one that the slot does not set."""
    access: str = ACCESS_ANY
    tunnel_auth: str = TUNNEL_AUTH_TOKEN
    wake: str = WAKE_NEVER


def slot_access(slot: celaut.Service.Api.Slot) -> SlotAccess:
    """The settings of ``slot`` (#669). Raises :class:`PolicyError` if the slot is not valid.

    Two pairs are not valid, because the tunnel is the only way for both: ``wake=on_tunnel``
    and ``tunnel_auth=address`` need an ``access`` other than ``direct``.
    """
    where = f"slot {slot.port}"
    parsed = check_list(slot.policies, PLACE_API_SLOT, where)
    result = SlotAccess(
        access=parsed.get(ACCESS, ACCESS_ANY),
        tunnel_auth=parsed.get(TUNNEL_AUTH, TUNNEL_AUTH_TOKEN),
        wake=parsed.get(WAKE, WAKE_NEVER),
    )
    if result.access == ACCESS_DIRECT:
        if result.wake == WAKE_ON_TUNNEL:
            raise PolicyError(f"{where}: wake=on_tunnel is not valid with access=direct")
        if result.tunnel_auth == TUNNEL_AUTH_ADDRESS:
            raise PolicyError(f"{where}: tunnel_auth=address is not valid with access=direct")
    return result


def check_service_slots(service: celaut.Service) -> None:
    """Refuse a service that has a slot that this node does not accept (#669)."""
    for slot in service.api.slot:
        slot_access(slot)
