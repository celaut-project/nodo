"""A bitcoind this node runs itself, seeded from a mnemonic it holds.

The third way of reaching Bitcoin, and the one that finally lets a node **pay** in it
without asking anything of its operator's infrastructure:

* ``explorer`` is a public HTTP API. No key, nothing to run, and the node can only be
  paid -- which is the half that matters when you are earning.
* ``core`` is a bitcoind the operator installed and trusts with a wallet. It signs, so
  the node can pay, but somebody has to run it and back it up.
* ``service`` is this one: the same bitcoind, run by the node as a **core service**,
  with its wallet derived from ``ledgers.bitcoin.WALLET_MNEMONIC``. The operator backs
  up one mnemonic, the way they already do for Ergo, and the node brings the rest up.

Nothing about the payment flow changes: this is a different way to *reach* Core, not a
different contract. The JSON-RPC client is the same :class:`ChainBackend`, so what signs
a sweep to cold storage or a donation payout is Core -- nodo still builds no raw Bitcoin
transaction, which is what made local signing "not here" until now.

**What is nodo's and what is the service's.** Nodo holds the mnemonic and hands it over
at launch; the service derives the wallet from it and holds the keys. Nodo never derives
a Bitcoin key, never asks for one, and asks Core for a receiving address like any other
``core`` deployment. The derivation is therefore the *service's* contract, and it is
stated in docs/BITCOIN.md so the operator can open the same wallet in any BIP-84 tool
with nothing but the mnemonic: ``m/84'/0'/0'`` on mainnet, ``m/84'/1'/0'`` on the test
networks, P2WPKH, which is the address type ``new_address`` asks Core for.

**Where the secret goes, said plainly.** The mnemonic lives in ``config.yaml`` (like
Ergo's) and is passed to the instance as an environment variable, which the node also
records against the instance -- redacted, see ``local_execution._serialize_envs``. It is
never sent to a peer, never logged, and never leaves this machine. A node that would
rather hold no Bitcoin key at all should stay on ``explorer``: it can still be paid.

**Attaching versus launching**, because the difference is a hung payment path. This
module's ``backend()`` only *attaches* to an instance that is already running: it is
called per payment and on every advertisement, and launching a bitcoind there would
block the payment path on a service download. Launching happens where a node is allowed
to take its time -- ``prepare()``, which the contract calls from ``init()`` at boot and
from the periodic ``manager`` tick, so a service that never came up or has since died is
brought back without any payment waiting on it.
"""
from __future__ import annotations

from base64 import b64encode
from typing import Dict, Optional

from src.payment_system.contracts.bitcoin.backend import (
    BackendUnavailable,
    ChainBackend,
)
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

#: The environment the published service reads, and where each value comes from in
#: ``config.yaml``. One table rather than a chain of ``config.get`` calls: it is what
#: the documented env contract is checked against, and a name that exists in only one of
#: the two places is the way a service silently comes up misconfigured.
ENVIRONMENT: Dict[str, str] = {
    # The wallet. 12 or 24 BIP-39 words; the service derives, Core signs.
    "BITCOIN_MNEMONIC": "ledgers.bitcoin.WALLET_MNEMONIC",
    # Optional BIP-39 passphrase. A different wallet for the same words, and a secret
    # the mnemonic alone cannot recover -- so it is only worth setting for somebody who
    # knows they want it.
    "BITCOIN_MNEMONIC_PASSPHRASE": "ledgers.bitcoin.WALLET_PASSPHRASE",
    # mainnet | testnet | signet | regtest. Also decides the derivation's coin type,
    # which is why it is the service's business and not just Core's.
    "BITCOIN_NETWORK": "ledgers.bitcoin.NETWORK",
    # How much block history to keep, in MiB. `0` means keep everything and run
    # `txindex`, which is the ~700 GB answer; anything else prunes to about that size.
    "BITCOIN_PRUNE": "ledgers.bitcoin.PRUNE_MIB",
    # What nodo will authenticate with. Passed in rather than read back out of the
    # service: a credential the node cannot predict is one it cannot use.
    "BITCOIN_RPC_USER": "ledgers.bitcoin.RPC_USER",
    "BITCOIN_RPC_PASSWORD": "ledgers.bitcoin.RPC_PASSWORD",
    # The wallet Core loads, and the one nodo scopes its wallet calls to.
    "BITCOIN_WALLET_NAME": "ledgers.bitcoin.WALLET_NAME",
}

#: Without these the service cannot come up, or nodo cannot talk to it once it has.
REQUIRED_ENVIRONMENT = (
    "BITCOIN_MNEMONIC",
    "BITCOIN_RPC_USER",
    "BITCOIN_RPC_PASSWORD",
)

#: This backend holds a wallet -- in the service it runs -- so it can sign and broadcast.
can_pay = True


def _configured(key: str) -> str:
    return str(ConfigManager().get(key) or "").strip()


def launch_envs() -> Optional[Dict[str, str]]:
    """The environment to launch the service with, or ``None`` when it cannot be built.

    ``None`` means a required value is missing, and the caller degrades rather than
    launching a bitcoind that would come up with no wallet or refuse every call this
    node makes to it. Empty optional values are dropped rather than passed as empty
    strings: an unset BIP-39 passphrase and one set to "" are different wallets.
    """
    envs = {
        name: _configured(key)
        for name, key in ENVIRONMENT.items()
        if _configured(key)
    }
    missing = [name for name in REQUIRED_ENVIRONMENT if name not in envs]
    if missing:
        LOGGER(
            "Not launching the bitcoin-node core service: "
            f"{', '.join(missing)} has nothing to come from "
            f"({', '.join(ENVIRONMENT[name] for name in missing)})."
        )
        return None
    return envs


def _service_id() -> Optional[str]:
    from src.core_services import BITCOIN_NODE, get_core_service_id

    return get_core_service_id(BITCOIN_NODE)


def _endpoint(launch: bool) -> Optional[str]:
    """``http://<ip>:<port>`` of the running instance, launching it only when asked."""
    service_id = _service_id()
    if not service_id:
        return None
    from src.core_services.runtime import (
        ensure_core_service_running,
        find_running_endpoint,
    )

    if not launch:
        # The local instances table and nothing else. `ensure_core_service_running`
        # with `launch=False` still tries to *download* the service first, which is a
        # network round trip -- per payment, and per advertisement, for as long as the
        # service happens to be down.
        return find_running_endpoint(service_id)
    envs = launch_envs()
    if envs is None:
        return None
    return ensure_core_service_running(service_id, envs=envs)


def prepare() -> Optional[str]:
    """Bring the service up if it is not, and return its endpoint.

    The optional hook a backend module may expose for "make my infrastructure ready".
    Called from the contract's ``init()`` at boot and from its periodic tick -- the two
    places a node is allowed to take as long as a service download needs -- and never
    from the payment path.

    Best-effort by design, like everything in ``core_services.runtime``: a missing
    service id, an unavailable download, an architecture this host cannot run, or a
    launch that fails all yield ``None``. The contract carries on and
    :func:`configuration_reason` is what tells an operator why nothing can be paid.
    """
    endpoint = _endpoint(launch=True)
    if endpoint:
        LOGGER(f"The bitcoin-node core service is running at {endpoint}.")
    return endpoint


def backend() -> ChainBackend:
    """Core over JSON-RPC, at whatever address the running instance was given.

    Attaches; never launches. See the note in this module's docstring: this is called
    per payment and per advertisement, and a service download on that path would hold a
    payment for as long as the download takes.
    """
    service_id = _service_id()
    if not service_id:
        raise BackendUnavailable(
            "no bitcoin-node core service is configured: set its published service id "
            "under core_services.bitcoin-node"
        )
    endpoint = _endpoint(launch=False)
    if not endpoint:
        raise BackendUnavailable(
            "the bitcoin-node core service is not running yet. The node starts it at "
            "boot and on the payment manager tick; `nodo instances` shows whether it "
            "came up, and its own log says why it did not"
        )
    user = _configured("ledgers.bitcoin.RPC_USER")
    password = _configured("ledgers.bitcoin.RPC_PASSWORD")
    if not user or not password:
        # Not the cookie file: Core writes that inside the service's own filesystem,
        # where nodo cannot read it -- and a stale `~/.bitcoin/.cookie` left by some
        # other node would authenticate nodo against the wrong wallet. So this backend
        # deliberately does not go through `_auth_from_config`.
        raise BackendUnavailable(
            "ledgers.bitcoin.RPC_USER and RPC_PASSWORD are what nodo and the service "
            "agree on; the service's cookie file is inside it and unreadable from here"
        )
    return ChainBackend(
        url=endpoint,
        wallet=_configured("ledgers.bitcoin.WALLET_NAME"),
        auth=b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii"),
    )


def configuration_reason() -> Optional[str]:
    """Why this node cannot reach its own bitcoind, or ``None`` when it can try.

    Config only -- no socket, no launch. The registry asks this on the payment path and
    on every advertisement, so it may look at the configuration and nothing else. That
    a configured service is not *running* is deliberately not an answer here: the node
    brings it up itself, and a contract dropped from the advertisement between two ticks
    would make this node unpayable for reasons its own operator cannot see.
    """
    if not _service_id():
        return (
            "core_services.bitcoin-node is not set to a published service id, so there "
            "is no bitcoind for this node to run"
        )
    missing = [
        ENVIRONMENT[name] for name in REQUIRED_ENVIRONMENT if not _configured(ENVIRONMENT[name])
    ]
    if missing:
        return f"the bitcoin-node core service has nothing to read from: {', '.join(missing)}"
    return None
