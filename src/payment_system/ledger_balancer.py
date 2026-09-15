from src.database.sql_connection import SQLConnection
from typing import Dict, Generator, Tuple
from src.utils.logger import LOGGER as log

# TODO Implement a ledger balancer to decide which instance of the contract to use.
# Also, filter between those supported by itself (by this node).

LedgerInstance = Tuple[bytes, str]


def ledger_balancer(ledger_generator: Generator[LedgerInstance, None, None]) \
        -> Generator[LedgerInstance, None, None]:
    """
    Balances the usage of ledgers by filtering out those that are available.
    Avoids redundant queries to the database by tracking checked ledgers.

    Args:
        ledger_generator: yields (script, ledger_tag) pairs as
            ``get_peer_contract_instances`` produces them -- the script is the raw
            contract value and the ledger its tag (``"ergo"``).

    Yields:
        Only the pairs whose ledger is available.
    """
    sc: SQLConnection = SQLConnection()
    # Keyed by the ledger tag, which is both hashable and readable. The value keeps the
    # verdict, so a ledger found unavailable stays skipped instead of being yielded on
    # its next occurrence.
    availability: Dict[str, bool] = {}

    # The asset rides through untouched: whether a *ledger* is reachable says nothing
    # about which of its assets a payment is in, and dropping it here would leave the
    # payer unable to tell two methods of one contract apart.
    for script, ledger, *asset in ledger_generator:
        if ledger not in availability:
            availability[ledger] = sc.check_if_ledger_is_available(ledger=ledger)
            if not availability[ledger]:
                log(f"Ledger {ledger} is not available for script {script[:6].hex()}.")

        if availability[ledger]:
            yield (script, ledger, asset[0] if asset else "")
