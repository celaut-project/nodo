import typing
from typing import Generator

from protos import celaut_pb2 as celaut
from src.database.query_interface import fetch_query
from src.database.sql_connection import SQLConnection


def get_ledgers() -> Generator[typing.Tuple[str, str], None, None]:
    yield from fetch_query(query="SELECT id, private_key FROM ledger")


def get_peer_contract_instances(contract_hash: str, peer_id: str = "LOCAL") \
        -> Generator[typing.Tuple[bytes, str], None, None]:
    db_connection = SQLConnection()
    yield from db_connection.get_peer_contract_instances(contract_hash, peer_id)

class NonUsedLedgerException(Exception):
    pass


def get_private_key_from_ledger(ledger: str) -> str:
    try:
        return next(fetch_query(
            query="SELECT private_key FROM ledger WHERE id = ?",
            params=(ledger,)
        ))[0]

    except Exception:
        raise NonUsedLedgerException()
