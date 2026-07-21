import typing
from typing import Generator

from protos import celaut_pb2 as celaut
from src.database.query_interface import fetch_query
from src.database.sql_connection import SQLConnection


def get_peer_contract_instances(contract_hash: str, peer_id: str = "LOCAL") \
        -> Generator[typing.Tuple[bytes, str], None, None]:
    db_connection = SQLConnection()
    yield from db_connection.get_peer_contract_instances(contract_hash, peer_id)
