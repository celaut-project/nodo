from hashlib import sha3_256
from typing import Generator
from protos import celaut_pb2 as celaut
from src.database.access_functions.ledgers import get_peer_contract_instances
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount
from src.utils.logger import LOGGER
from src.utils.contract_xattrs import set_contract_type, set_script, set_token_id

GAS_PER_ERG = int(ConfigManager().get("ledgers.ergo.GAS_PER_ERG"))
CONTRACT = "proveDlog(decodePoint())"
CONTRACT_HASH = sha3_256(CONTRACT.encode("utf-8")).hexdigest()


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


def local_payment_methods() -> Generator[celaut.GasPrice, None, None]:
    """Advertise this node's payment contracts, in the form peers must receive.

    ``get_peer_contract_instances`` yields the stored instance value as raw bytes:
    for Ergo that is the wallet's ErgoTree/propositionBytes, exactly what
    ``interface.init()`` registered. That value is what a paying peer feeds to
    ``ergo_contract_from_proposition_bytes`` to build the output box, and what
    this node's own ``payment_process_validator`` turns back into an address to
    check the payment landed on its wallet — so it must travel as the ``script``
    xattr, untouched. See the contract in src/utils/ergo_tree.py: the exchanged
    value is never an ErgoScript source string and never a base58 address.

    ``contract_type`` carries the stable, wallet-independent identity instead, so
    the receiving peer's ``add_contract`` derives the same ``contract_hash`` this
    node looks the instance up by.

    This is a plain read of what is registered; recovering a missing registration is
    the caller's job (see ``src.gateway.utils``), so answering GetPeerInfo never
    depends on the ledger runtime being reachable.
    """
    for script, ledger in get_peer_contract_instances(CONTRACT_HASH):

        ledger_tag = ledger.tags[0] if ledger.tags else "unknown"
        LOGGER(f"Using ledger {ledger_tag} with script {script.hex()} for contract {CONTRACT_HASH}")

        contract_ledger = celaut.Contract()
        contract_ledger.ledger.CopyFrom(ledger)
        set_script(contract_ledger, script)
        set_contract_type(contract_ledger, CONTRACT.encode("utf-8"))
        set_token_id(contract_ledger, "ERG")

        gas_price = celaut.GasPrice(
            contract=contract_ledger,
            gas_amount=to_gas_amount(GAS_PER_ERG)
        )

        yield gas_price
