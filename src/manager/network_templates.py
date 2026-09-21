"""``${VAR}`` placeholders in a ``Service.Network.formal`` body (issue #385).

A network's ``formal`` carries two kinds of key, by convention and not by schema:

* **identity keys**, fixed by the service author, saying *what the protocol is* --
  Ergo's consensus algorithm, its ledger model, its target block time. They are the
  same for every instantiation, because they describe the thing, not the choice.
* **selection keys**, saying *which concrete instance of that protocol* to join --
  mainnet or testnet, which block, how much cumulative work. That decision belongs
  to whoever instantiates the service, not to the service's own spec.

Before this module there was no way to write the second kind down without also
deciding it: ``pow.block_id`` had to be a concrete hash at pack time, so a service
either pinned one chain state forever or left the key out and accepted any peer that
called itself Ergo. ``${VAR}`` is the missing spelling. A selection key whose value
is ``${MIN_DIFF}`` says "this ask is not complete until my instantiator completes
it", and the node reads that as an instruction about *when* to resolve:

* every placeholder answered by ``config.environment_variables`` -> substitute and
  resolve at launch, exactly as before;
* any placeholder unanswered -> do not resolve that network at all, omit it from
  ``__config__``, and leave it to ``Gateway.ResolveNetwork`` later.

Neither outcome is an error. A missing variable must never abort a launch -- the only
declared-network condition that does is an operator policy violation
(``network_policy.enforce_network_policy``), which is judged on the *declared*
network, before any of this runs.

One module, one regex, three readers: ``rootfs.build_network_resolution`` (which
substitutes), the packer (which validates that a templated ask would still be
well-formed once filled), and ``networks.check_network_request`` (which uses the
placeholder positions to decide what a ``ResolveNetwork`` caller is allowed to fill
in). Writing the grammar three times is how the three would come to disagree.

Grammar
-------

``${NAME}`` where NAME is ``[A-Za-z_][A-Za-z0-9_]*`` -- the C identifier shape every
environment variable already has, so nothing that is a legal variable name is
unspellable here.

**A value is either entirely a placeholder or contains none.** ``abc${X}`` is
refused, and the reason is not aesthetic: a formal value is compared byte for byte
by ``match_networks``, and the subset check in ``check_network_request`` has to be
able to say "this key was left open" or "this key was fixed" about a whole value. A
partially templated value is neither, and deciding what a caller may put in the
``abc`` half is a question with no good answer. Concatenation is also the shape that
turns a string substitution into an injection: the one place a guest-controlled
value meets a structured document is exactly the place not to allow gluing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Mapping, Tuple, Union

from src.identity.node_identity import (
    ComponentFormalError,
    component_formal,
    parse_component_formal,
)
from src.utils.guest_env import IDENTIFIER_PATTERN

#: The one regex. ``NAME`` is the C identifier shape, which is what every shell and
#: every ``environment_variables`` map already uses, so no legal variable name is
#: unspellable and no illegal one is silently accepted. The shape itself lives in
#: ``src.utils.guest_env.IDENTIFIER_PATTERN``, not here, so the two never drift.
PLACEHOLDER = re.compile(rf"\$\{{({IDENTIFIER_PATTERN})\}}")


@dataclass(frozen=True)
class Missing:
    """Why a templated ``formal`` could not be completed. Returned, never raised.

    Two different ways of being incomplete, kept apart because they are different
    mistakes by different people:

    * ``variables`` -- the ask is fine and the instantiator did not answer it. The
      network is deferred; this is the normal, expected outcome the feature exists
      for.
    * ``malformed`` -- the *spec* is wrong: a value that is partly a placeholder
      (``abc${X}``), or one whose answer could not be carried in a ``formal`` body.
      The packer refuses these outright, so reaching one at launch means the service
      was not packed by this code path. It is still not an abort: an unreadable ask
      is an ask this node cannot satisfy, and the network is deferred exactly as an
      unanswered one is, with the reason said out loud.
    """

    variables: Tuple[str, ...] = ()
    malformed: Tuple[str, ...] = ()

    def __str__(self) -> str:
        parts = []
        if self.variables:
            parts.append("unanswered ${}: " + ", ".join(self.variables))
        if self.malformed:
            parts.append("unusable: " + ", ".join(self.malformed))
        return "; ".join(parts) or "nothing missing"


def _pairs(formal: bytes) -> Dict[str, str]:
    """``formal`` as key/value pairs, or ``{}`` for a body that cannot be read.

    Swallowing ``ComponentFormalError`` here is deliberate and narrow: every caller
    of this module is asking "is there a template in here", and a body that is not a
    ``key=value`` document has no template in it by definition. The body is read
    again, properly, by whoever actually consumes it (``parse_pow_formal``,
    ``match_networks``), and *that* reader is the one that reports it as malformed.
    Raising here would move a formal-syntax error into the template layer, which did
    not find it and cannot explain it.
    """
    try:
        return parse_component_formal(formal)
    except ComponentFormalError:
        return {}


def find_placeholders(formal: bytes) -> Dict[str, str]:
    """``{formal key: variable name}`` for every value that is *entirely* a placeholder.

    The mapping, not a bare set of names, because both remaining readers need the
    key: the launch path logs which key is unanswered, and the ``ResolveNetwork``
    subset check needs to know precisely which keys the author left open for a caller
    to fill.

    Partially templated values are **not** here -- they are not placeholders under
    this grammar. :func:`find_partial_placeholders` reports them.
    """
    found: Dict[str, str] = {}
    for key, value in _pairs(formal).items():
        match = PLACEHOLDER.fullmatch(value.strip())
        if match:
            found[key] = match.group(1)
    return found


def find_partial_placeholders(formal: bytes) -> Tuple[str, ...]:
    """Formal keys whose value contains ``${...}`` without being one, e.g. ``abc${X}``.

    Its own function rather than an exception out of :func:`find_placeholders`,
    because the two callers want opposite things from the answer: the packer refuses
    the pack (the author is looking at the file and can fix it), and the launch path
    defers the network (there is nobody to tell, and a spec is not worth aborting a
    launch over -- the network simply resolves to nothing).
    """
    return tuple(
        key
        for key, value in _pairs(formal).items()
        if PLACEHOLDER.search(value) and not PLACEHOLDER.fullmatch(value.strip())
    )


def has_placeholders(formal: bytes) -> bool:
    """Whether this ``formal`` defers at all -- any placeholder, whole or partial.

    "Declare at least one templated key" is how a service author opts into deferred
    resolution (issue #385), so the question "does this network defer?" is asked on
    its own and is answered here rather than by two callers each spelling out the
    same disjunction.
    """
    return bool(find_placeholders(formal) or find_partial_placeholders(formal))


def _usable_value(raw: bytes) -> Union[str, None]:
    """One ``environment_variables`` value as a ``formal`` value, or None if it cannot be.

    ``Configuration.environment_variables`` is ``map<string, bytes>``; a ``formal`` is
    UTF-8 ``key=value`` lines. Two things therefore disqualify a value, and both are
    returned as "unusable" rather than raised:

    * **not UTF-8** -- there is no text to put in the document.
    * **contains a newline or the document's own structure** -- this is the injection
      guard, and it is the reason this function exists at all. ``formal`` separates
      pairs with ``\\n``; an instantiator answering ``MIN_DIFF`` with
      ``0\\npow.block_id=deadbeef`` would otherwise be *adding a key* to someone
      else's declaration, and the firewall would open what that added key resolved
      to. A value is one value.

    An ``=`` is fine: ``parse_component_formal`` splits on the first one, so a value
    may contain them and a key may not.
    """
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    if "\n" in text or "\r" in text:
        return None
    return text


def substitute(
    formal: bytes,
    env: Mapping[str, bytes],
) -> Union[bytes, Missing]:
    """``formal`` with every ``${VAR}`` replaced from ``env``, or :class:`Missing`.

    Returns the input bytes **unchanged** when there is no placeholder in them. That
    is not an optimisation, it is the compatibility guarantee: a ``formal`` is
    compared byte for byte by ``match_networks`` and hashed into a service id, and a
    round trip through ``parse_component_formal``/``component_formal`` would
    re-sort and re-join a hand-written body that may not have been sorted or joined
    that way. A network that declares no template has to be handled exactly as it was
    before this module existed, down to the byte, and the only way to promise that is
    not to touch it.

    When there *is* a placeholder, the result is re-encoded with
    ``component_formal`` -- sorted, UTF-8, canonical by construction -- because at
    that point the bytes are being authored here and there is no earlier spelling to
    preserve. Substituting into the raw text instead would carry the author's
    incidental key order into a value the node generated.

    ``env`` is the launcher-provided ``Configuration.environment_variables``. A
    variable that is absent, empty of usable text, or carries a value that cannot
    live in a ``formal`` body is reported in :class:`Missing`; nothing is guessed and
    nothing raises.
    """
    placeholders = find_placeholders(formal)
    partial = find_partial_placeholders(formal)

    if not placeholders and not partial:
        return bytes(formal)

    if partial:
        return Missing(
            variables=tuple(sorted(set(placeholders.values()))),
            malformed=tuple(
                f"{key} is only partly a placeholder; a value is either entirely "
                "${VAR} or contains none"
                for key in partial
            ),
        )

    pairs = _pairs(formal)
    missing_vars = []
    unusable = []
    for key, variable in placeholders.items():
        if variable not in env:
            missing_vars.append(variable)
            continue
        value = _usable_value(env[variable])
        if value is None:
            unusable.append(
                f"{key}: the value given for {variable} is not usable as a formal "
                "value (not UTF-8, or it contains a newline)"
            )
            continue
        pairs[key] = value

    if missing_vars or unusable:
        return Missing(
            variables=tuple(sorted(set(missing_vars))),
            malformed=tuple(unusable),
        )

    return component_formal(pairs)
