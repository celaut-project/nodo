"""Test helper: the `share` policy of a directory, from the keys of the old xattr form.

Before #670 a share was bare xattrs (`shared`, `guest`, `access`, `share_tag`,
`share_env`, `share_network`). The fixtures of the share tests still read best in that
shape, so this turns one dict of them into the `share` policy that a node reads now.
It checks nothing: a dict with no role, for example, becomes a value that a node refuses.
"""
from protos import celaut_pb2 as celaut
from src.utils import policies

_TRUE = (b"true", b"1")


def share_value_from_keys(keys):
    pairs = {}
    if keys.get("shared") in _TRUE:
        pairs["role"] = "shared"
    elif keys.get("guest") in _TRUE:
        pairs["role"] = "guest"
    elif "share_network" in keys:
        pairs["role"] = "network"
    for key, name in (("access", "access"), ("share_tag", "tag"),
                      ("share_env", "env"), ("share_network", "network")):
        if key in keys:
            pairs[name] = keys[key].decode("utf-8")
    return policies.component_formal(pairs)


def add_share(branch, keys):
    """Give ``branch`` the share policy for ``keys`` (the old xattr names)."""
    if not keys:
        return
    policies.set_policy(
        branch.policies, policies.make(policies.SHARE, share_value_from_keys(keys))
    )


def declare_entry_metadata(filesystem):
    """Declare ``entry_metadata`` on a root ``filesystem``, as the packer does; return it."""
    policies.set_policy(filesystem.policies, policies.make(policies.ENTRY_METADATA, b""))
    return filesystem
