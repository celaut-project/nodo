from src.database.sql_connection import SQLConnection
from typing import Generator, Tuple, Set

# TODO Implement a ledger balancer to decide which instance of the contract to use.
# Also, filter between those supported by itself (by this node).

def ledger_balancer(ledger_generator: Generator[Tuple[str, str], None, None]) \
        -> Generator[Tuple[str, str], None, None]:
    """
    Balances the usage of ledgers by filtering out those that are available.
    Avoids redundant queries to the database by tracking checked ledgers.

    Args:
        ledger_generator (Generator[Tuple[str, str], None, None]): 
            A generator yielding tuples containing contract addresses and ledgers.

    Yields:
        Generator[Tuple[str, str], None, None]: 
            A filtered generator yielding only the available contract addresses and ledgers.
    """
    sc = SQLConnection()
    checked_ledgers: Set[str] = set()  # Set to track checked ledgers

    for contract_addr, ledger in ledger_generator:
        if ledger not in checked_ledgers:
            # Check if the ledger is available and mark it as checked
            is_available = sc.check_if_ledger_is_available(ledger=ledger)
            checked_ledgers.add(ledger)  # Mark this ledger as checked

            if is_available:
                yield (contract_addr, ledger)
        else:
            # If the ledger was already checked, simply yield if it was available
            yield (contract_addr, ledger)
