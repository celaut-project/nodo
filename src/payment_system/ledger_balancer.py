from src.database.sql_connection import SQLConnection
from typing import Dict, Generator, Tuple
from protos import celaut_pb2
from src.utils.logger import LOGGER as log

# TODO Implement a ledger balancer to decide which instance of the contract to use.
# Also, filter between those supported by itself (by this node).

LedgerInstance = Tuple[bytes, celaut_pb2.Contract.Ledger]


def ledger_balancer(ledger_generator: Generator[LedgerInstance, None, None]) \
        -> Generator[LedgerInstance, None, None]:
    """
    Balances the usage of ledgers by filtering out those that are available.
    Avoids redundant queries to the database by tracking checked ledgers.

    Args:
        ledger_generator: yields (script, ledger) pairs as
            ``get_peer_contract_instances`` produces them — the script is the raw
            contract value and the ledger a deserialized ``Contract.Ledger``
            message, NOT a string.

    Yields:
        Only the pairs whose ledger is available.
    """
    sc: SQLConnection = SQLConnection()
    # Keyed by ledger hash: a Contract.Ledger message is not hashable, so it cannot
    # go into a set — that raised `unhashable type: 'Ledger'` on every payment
    # attempt. The value keeps the verdict, so a ledger found unavailable stays
    # skipped instead of being yielded on its next occurrence.
    availability: Dict[str, bool] = {}

    for script, ledger in ledger_generator:
        key = SQLConnection.ledger_key(ledger)

        if key not in availability:
            availability[key] = sc.check_if_ledger_is_available(ledger=ledger)
            if not availability[key]:
                log(f"Ledger {key[:16]}… is not available for script {script[:6].hex()}.")

        if availability[key]:
            yield (script, ledger)
