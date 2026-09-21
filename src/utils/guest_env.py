"""Validating ``Configuration.environment_variables`` before it also becomes a
Linux environment variable inside the guest (#405).

Every entry here already reaches the guest inside ``__config__`` regardless of
what this module decides -- ``ConfigurationFile.config.environment_variables``
(``src/virtualizers/microvm/rootfs.py::build_configuration_file``) is
unconditional and unfiltered, exactly as before this module existed. What this
module decides is the second, additive path: whether an entry is also safe to
hand the entrypoint process as a real ``KEY=VALUE`` in its own environment,
delivered through the ``.__nodo_envs`` metadata file and exported by
``bash/build_ch_initramfs.sh`` just before ``switch_root``.

An entry that fails validation is left out of that second path only. It is
never dropped from ``__config__``, and a launch never fails because of it: a
badly-named or oversized variable is something the instantiator can notice
from inside the guest (by falling back to parsing ``__config__``), not a
reason to refuse them a service.
"""
from __future__ import annotations

import re
from typing import Dict, Mapping

from src.utils import logger as log

#: The C identifier shape every environment variable already has. The one
#: definition of that shape: ``src/manager/network_templates.py`` imports it
#: for its own ``${VAR}`` placeholder grammar rather than writing it out a
#: second time, so the two can never quietly drift apart.
IDENTIFIER_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"

#: The guest's ``/init`` interpolates a kept name literally into
#: ``export "$name=$value"``; restricting it to :data:`IDENTIFIER_PATTERN` is
#: what keeps that interpolation from ever needing to be anything cleverer
#: than a literal.
NAME_RE = re.compile(f"^{IDENTIFIER_PATTERN}$")

#: Names that would change how the entrypoint's *own* process resolves symbols,
#: shared libraries or interpreter code before it ever runs a line of its own
#: logic, plus the two names ``bash/build_ch_initramfs.sh``'s ``/init`` needs
#: intact for itself after this module's output is exported into its shell
#: (``PATH``, to keep resolving every command the rest of ``/init`` still runs,
#: and ``ENTRYPOINT``, which it reads right after for the exec check and
#: ``switch_root``). This is not a defence against the instantiator -- they
#: already own everything inside their own instance -- it is defence against a
#: ``service.json`` ``envs`` declaration, or a launch config copied from
#: elsewhere, accidentally handing a dangerous knob, or a name ``/init`` itself
#: depends on, to a process nobody meant to hand one to.
DENIED_NAMES = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "ENV", "IFS", "GCONV_PATH",
    "PATH", "ENTRYPOINT",
})

#: Not an ``execve()``/``ARG_MAX`` limit -- each kept entry becomes one line of
#: base64 in ``.__nodo_envs``, decoded and exported by a busybox ash script with
#: no streaming, so this is defence in depth against a single "variable" being
#: asked to carry an oversized blob through that path.
MAX_VALUE_BYTES = 32 * 1024


def linux_env_vars(environment_variables: Mapping[str, bytes]) -> Dict[str, bytes]:
    """The subset of ``environment_variables`` safe to also expose as Linux env vars.

    Each entry is kept only if its name matches :data:`NAME_RE`, is not one of
    :data:`DENIED_NAMES`, and its value both contains no ``NUL`` byte and is at
    most :data:`MAX_VALUE_BYTES` long. A ``NUL`` byte disqualifies a value
    outright rather than being delivered truncated: a Linux environment
    variable is a ``NUL``-terminated string at the ``execve()`` level, so a
    value that already contains one could not be reproduced faithfully, and a
    truncated Linux env var silently disagreeing with the untruncated one in
    ``__config__`` would be worse than the variable simply not being there.

    Every rejection is logged once, naming the variable and the reason, since
    from inside the guest a missing Linux env var otherwise looks like a nodo
    bug rather than a validation outcome.
    """
    kept: Dict[str, bytes] = {}
    for name, value in environment_variables.items():
        if not NAME_RE.match(name):
            log.LOGGER(
                f"[ENV] '{name}' not delivered as a Linux env var: illegal name "
                f"(must match {NAME_RE.pattern}). Still available via __config__."
            )
            continue
        if name in DENIED_NAMES:
            log.LOGGER(
                f"[ENV] '{name}' not delivered as a Linux env var: reserved name. "
                "Still available via __config__."
            )
            continue
        if b"\x00" in value:
            log.LOGGER(
                f"[ENV] '{name}' not delivered as a Linux env var: value contains "
                "a NUL byte. Still available via __config__."
            )
            continue
        if len(value) > MAX_VALUE_BYTES:
            log.LOGGER(
                f"[ENV] '{name}' not delivered as a Linux env var: value is "
                f"{len(value)} bytes, over the {MAX_VALUE_BYTES}-byte ceiling. "
                "Still available via __config__."
            )
            continue
        kept[name] = value
    return kept
