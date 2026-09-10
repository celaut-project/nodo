from typing import Generator
from protos import celaut_pb2 as celaut
from src.database.access_functions.ledgers import get_peer_contract_instances
from src.utils.utils import to_amount
from src.utils.logger import LOGGER
from src.utils.contract_xattrs import set_contract_type, set_script, set_token_id


def register_local_contracts() -> None:
    """Re-run the ledger interfaces' ``init()`` so the LOCAL contract rows exist.

    ``init()`` normally runs once at daemon boot (see
    ``payment_process.init_interfaces``), but it is *skipped* when its runtime
    dependency is unavailable at that instant — e.g. Java installed after the
    daemon started. The row it writes is the only source for what this node
    advertises, so without a retry the node keeps announcing zero payment methods
    until someone happens to restart it, and no peer can ever pay it.

    ``add_contract`` is INSERT OR IGNORE, so calling this again is a no-op.
    """
    from src.payment_system.contracts import envs
    from src.utils.java_dependency import (
        JavaDependencyMissing,
        log_java_dependency_warning,
    )

    try:
        interfaces = envs.init_interfaces()
    except JavaDependencyMissing:
        log_java_dependency_warning(LOGGER, feature="Ergo payments or reputation")
        return

    for contract_hash, _init in interfaces.items():
        try:
            _init()
        except JavaDependencyMissing:
            log_java_dependency_warning(LOGGER, feature="Ergo payments or reputation")
        except Exception as e:
            LOGGER(f"Could not register the local contract {contract_hash[:6]}: {e}")


def local_payment_methods() -> Generator[celaut.ContractRate, None, None]:
    """Advertise this node's payment contracts, in the form peers must receive.

    One ``ContractRate`` per registered contract per stored instance, each with its own
    ``token_id`` and its own rate. It used to yield Ergo's and only Ergo's, which is why
    "nodo allows the simultaneous use of multiple ledgers" had never been true.

    ``get_peer_contract_instances`` yields the stored instance value as raw bytes:
    for Ergo that is the wallet's ErgoTree/propositionBytes, exactly what
    ``interface.init()`` registered. That value is what a paying peer feeds to
    ``ergo_contract_from_proposition_bytes`` to build the output box, and what
    this node's own ``payment_process_validator`` turns back into an address to
    check the payment landed on its wallet — so it must travel as the ``script``
    xattr, untouched. See the contract in src/payment_system/contracts/ergo/ergo_tree.py: the exchanged
    value is never an ErgoScript source string and never a base58 address.

    ``contract_type`` carries the stable, wallet-independent identity instead, so
    the receiving peer's ``add_contract`` derives the same ``contract_hash`` this
    node looks the instance up by.

    This is a plain read of what is registered; recovering a missing registration is
    the caller's job (see ``src.gateway.utils``), so answering GetPeerInfo never
    depends on the ledger runtime being reachable.
    """
    from src.payment_system.contracts.registry import attribute, contracts

    for contract in contracts().values():
        # A contract that settles on no chain is never advertised: a peer that read it
        # out of GetPeerInfo and paid through it would have paid into nothing.
        if attribute(contract, "is_demo"):
            continue
        try:
            rate = int(contract.mu_per_unit())
        except Exception as e:
            # A malformed rate is a configuration error, and advertising the contract
            # without one would have peers convert this node's prices by guessing.
            LOGGER(
                f"Not advertising {contract.LEDGER}: its MU rate could not be read ({e})."
            )
            continue
        if rate <= 0:
            LOGGER(f"Not advertising {contract.LEDGER}: its MU rate is not positive.")
            continue

        for script, ledger in get_peer_contract_instances(contract.CONTRACT_HASH):

            contract_ledger = celaut.Contract()
            contract_ledger.ledger.CopyFrom(ledger)
            set_script(contract_ledger, script)
            set_contract_type(contract_ledger, contract.CONTRACT.encode("utf-8"))
            set_token_id(contract_ledger, getattr(contract, "NATIVE_ASSET", ""))

            # What one unit of this contract is worth, in this node's MU. This is the
            # only thing that makes a price quoted in MU actionable to whoever reads
            # it, so it travels with every advertisement.
            yield celaut.ContractRate(
                contract=contract_ledger,
                mu_per_unit=to_amount(rate),
            )
