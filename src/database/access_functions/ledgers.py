import typing
from typing import Generator

from src.database.sql_connection import SQLConnection


def get_peer_contract_instances(contract_hash: str, peer_id: str = "LOCAL",
                                asset: typing.Optional[str] = None) \
        -> Generator[typing.Tuple[bytes, str, str], None, None]:
    """``(script, ledger_tag, asset)`` per stored instance of ``contract_hash``.

    The ledger is its tag (``"ergo"``), which is the whole of its identity here: the
    contract interfaces check it against their own ``LEDGER`` constant, and everything
    else a chain needs -- a node URL, an explorer, a rate -- comes from this node's
    config, never from what a peer said about the chain.

    ``asset`` narrows it to one payment method: a method is ledger + contract + asset,
    and on Ergo the assets of one contract share a script and an address, differing
    only there. A payer settling in one asset handed another's rows would build its
    output against the right address for the wrong money.
    """
    db_connection = SQLConnection()
    yield from db_connection.get_peer_contract_instances(contract_hash, peer_id, asset)
