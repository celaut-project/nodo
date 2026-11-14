"""What the payee of the payment in progress declared (#626).

A payer pays into a peer's ledger, and some values of that ledger are the payee's own
choice: the payee states them in its declaration (``ledger_descriptors.PAYEE_PARAMETERS``).
The payment process knows which peer it pays; the contract that waits for the
transaction does not. This module carries the payee's declaration from one to the
other, for the duration of one payment, without a change to the contract interface
(``process_payment`` and the awaiters keep their signatures).
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from protos import celaut_pb2

_payee_ledger: ContextVar = ContextVar("payee_ledger", default=None)


@contextmanager
def paying(ledger: Optional[celaut_pb2.Contract.Ledger]):
    """Make ``ledger`` the payee's declaration while the block runs.

    ``None`` is allowed: a peer whose declaration is not known. A contract then uses its
    own values, as it did before #626.
    """
    token = _payee_ledger.set(ledger)
    try:
        yield
    finally:
        _payee_ledger.reset(token)


def ledger() -> Optional[celaut_pb2.Contract.Ledger]:
    """The declaration of the payee of the payment in progress, or None."""
    return _payee_ledger.get()


def declared_ledger(peer_id: str, ledger_tag: str, contract_hash: str,
                    connection=None) -> Optional[celaut_pb2.Contract.Ledger]:
    """The ledger ``peer_id`` declared for the contract ``contract_hash`` on ``ledger_tag``.

    Read from the peer's last stored announcement (``peer.advertisement``), which is
    the message exactly as it arrived and is refreshed on every update of the peer. None
    when there is no announcement or no such contract in it. Never raises: a payment
    must not fail because this cannot be read.
    """
    from hashlib import sha3_256

    from src.database.sql_connection import SQLConnection
    from src.utils.contract_xattrs import contract_shape_bytes, get_contract_type, get_script

    try:
        blob = (connection or SQLConnection()).get_peer_advertisement(peer_id)
        if not blob:
            return None
        peer = celaut_pb2.Peer()
        peer.ParseFromString(blob)
        for rate in peer.payment_contracts:
            contract = rate.contract
            tags = list(contract.ledger.tags)
            if not tags or tags[0] != ledger_tag:
                continue
            # The hash `SQLConnection.add_contract` keyed the stored contract by.
            type_bytes = (get_contract_type(contract) or get_script(contract)
                          or contract_shape_bytes(contract))
            if contract_hash and sha3_256(type_bytes).hexdigest() != contract_hash:
                continue
            return contract.ledger
    except Exception:
        return None
    return None
