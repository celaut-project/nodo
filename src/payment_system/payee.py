"""What the payee of the payment in progress declared (#626).

A payer pays through a peer's payment protocol, and some values of that protocol are the
payee's own choice: the payee states them in its declaration
(``payment_protocol.PAYEE_PARAMETERS``).
The payment process knows which peer it pays; the contract that waits for the
transaction does not. This module carries the payee's declaration from one to the
other, for the duration of one payment, without a change to the contract interface
(``process_payment`` and the awaiters keep their signatures).
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from protos import celaut_pb2

_payee_protocol: ContextVar = ContextVar("payee_protocol", default=None)


@contextmanager
def paying(protocol: Optional[celaut_pb2.Contract.Protocol]):
    """Make ``protocol`` the payee's declaration while the block runs.

    ``None`` is allowed: a peer whose declaration is not known. A contract then uses its
    own values, as it did before #626.
    """
    token = _payee_protocol.set(protocol)
    try:
        yield
    finally:
        _payee_protocol.reset(token)


def protocol() -> Optional[celaut_pb2.Contract.Protocol]:
    """The payment protocol declared by the payee of the payment in progress, or None."""
    return _payee_protocol.get()


def declared_protocol(peer_id: str, ledger_tag: str, contract_hash: str,
                      connection=None) -> Optional[celaut_pb2.Contract.Protocol]:
    """The protocol ``peer_id`` declared for the contract ``contract_hash`` on ``ledger_tag``.

    Read from the peer's last stored announcement (``peer.advertisement``), which is
    the message exactly as it arrived and is refreshed on every update of the peer. None
    when there is no announcement or no such contract in it. Never raises: a payment
    must not fail because this cannot be read.
    """
    from src.database.sql_connection import SQLConnection
    from src.utils.payment_protocol import protocol_hash

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
            hashed = protocol_hash(contract.protocol)
            if hashed is None or (contract_hash and hashed != contract_hash):
                continue
            return contract.protocol
    except Exception:
        return None
    return None
