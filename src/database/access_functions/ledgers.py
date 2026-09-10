import typing
from typing import Generator

from protos import celaut_pb2 as celaut
from src.database.query_interface import fetch_query
from src.database.sql_connection import SQLConnection


def get_peer_contract_instances(contract_hash: str, peer_id: str = "LOCAL",
                                asset: typing.Optional[str] = None) \
        -> Generator[typing.Tuple[bytes, celaut.Contract.Ledger, str], None, None]:
    """``(script, ledger, asset)`` per stored instance of ``contract_hash``.

    ``asset`` narrows it to one payment method: a method is ledger + contract + asset,
    and on Ergo the assets of one contract share a script and an address, differing
    only there. A payer settling in one asset handed another's rows would build its
    output against the right address for the wrong money.
    """
    db_connection = SQLConnection()
    yield from db_connection.get_peer_contract_instances(contract_hash, peer_id, asset)
