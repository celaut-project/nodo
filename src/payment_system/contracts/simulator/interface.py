from typing import Tuple

from protos import celaut_pb2
from hashlib import sha3_256
from src.identity.node_identity import component_formal
from src.utils.contract_xattrs import set_script, set_token_id
from src.utils.logger import LOGGER

# The payment protocol of a simulated payment (#671). Nothing is paid, but the formal
# still defines the xattrs that `process_payment` writes, as every payment protocol does.
_PROTOCOL = celaut_pb2.Contract.Protocol(
    tags=["simulated"],
    prose=(
        "Simulated payments: nothing settles, nothing moves. The script attribute is "
        "the script that the payer gives. Nothing is paid to it. The token_id "
        "attribute is empty. The two attributes are always present."
    ),
    formal=component_formal({
        "payment.kind": "simulated",
        "payment.xattr.script": "the script bytes that the payer gives; nothing is paid "
                                "to them",
        "payment.xattr.token_id": "empty: there is no asset",
        "payment.xattr.required": "script, token_id",
    }),
)
CONTRACT_HASH = sha3_256(_PROTOCOL.formal).hexdigest()


def protocol() -> celaut_pb2.Contract.Protocol:
    return _PROTOCOL


# The TAG is the identity, here as everywhere else; `PROSE` and `FORMAL` only describe.
LEDGER = "simulated"
PROSE = "Simulated payments: nothing settles, nothing moves."
# There is no chain, so there is nothing a formal specification could describe.
FORMAL = b""

simulated_ledger = celaut_pb2.Contract.Ledger(tags=[LEDGER], prose=PROSE, formal=FORMAL)
# A simulated payment settles on no chain, so nothing here can be double-spent and
# there is no box for a sweep to consume.
needs_unspent_proof = False
is_demo = True


def ledger() -> celaut_pb2.Contract.Ledger:
    """A ledger tag that names itself for what it is.

    Carried so the registry can treat this contract like any other; nothing resolves a
    node URL or a chain from it, because there is neither.
    """
    return simulated_ledger


def init() -> None:
    """Register nothing, deliberately.

    Every other contract writes a LOCAL row here so peers can read what this node
    accepts. This one must not: a simulated payment moves no money, so a peer that read
    it out of `GetPeerInfo` and paid through it would have paid into nothing. The flag
    exists to let a node exercise its own payment path, not to offer that path to
    others -- which is why `envs.DEMOS` exists on the *payer's* side only.
    """
    return None


def manager() -> None:
    """Nothing to sweep and nothing owed: no wallet, no chain, no fee."""
    return None


def manager_iteration_time() -> int:
    """Daily, like the others, though the job it schedules does nothing."""
    return 86400


def process_payment(amount: int, deposit_token: str, ledger: str, script: bytes) -> celaut_pb2.Contract:
    """Settle nothing, and hand back the `Contract` the peer is told about.

    ``token_id``, ``script`` and ``contract`` are xattrs, not fields of the message --
    passing them as keyword arguments raised ``ValueError`` before the caller ever saw
    the result, so this path could not complete a simulated payment at all. They are
    set through the same helpers every other contract uses.
    """
    LOGGER(f"Process simulated payment for token {deposit_token} of {amount}")
    contract = celaut_pb2.Contract(ledger=simulated_ledger, protocol=_PROTOCOL)
    set_token_id(contract, "")
    set_script(contract, script)
    return contract


def payment_process_validator(amount: int, token: str, ledger: str, script: bytes) -> bool:
    return True

def check_sender_balance(amount: int) -> bool:
    return True


def settlement_floors_mu() -> Tuple[int, int]:
    """``(fee, smallest payable output)``, both zero: nothing here reaches a chain.

    A simulated payment costs nothing and has no minimum output, so this contract imposes
    no floor on how small a deposit may be. Answering honestly rather than borrowing
    Ergo's box-value floor is the point of asking the contract at all -- deposit sizing
    used to import Ergo's constants directly, so a node running purely on this contract
    still sized its deposits against a chain it never touched.
    """
    return 0, 0