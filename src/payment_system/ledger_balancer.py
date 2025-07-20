from src.database.sql_connection import SQLConnection
from typing import Generator, Tuple, Set
from src.utils.logger import LOGGER as log

# TODO Implement a ledger balancer to decide which instance of the contract to use.
# Also, filter between those supported by itself (by this node).

def ledger_balancer(ledger_generator: Generator[Tuple[bytes, str], None, None]) \
        -> Generator[Tuple[bytes, str], None, None]:
    """
    Balances the usage of ledgers by filtering out those that are available.
    Avoids redundant queries to the database by tracking checked ledgers.

    Args:
        ledger_generator (Generator[Tuple[bytes, str], None, None]): 
            A generator yielding tuples containing scripts and ledgers.

    Yields:
        Generator[Tuple[bytes, str], None, None]: 
            A filtered generator yielding only the available script and ledgers.
    """
    sc: SQLConnection = SQLConnection()
    checked_ledgers: Set[str] = set()  # Set to track checked ledgers

    for script, ledger in ledger_generator:
        if ledger not in checked_ledgers:
            # Check if the ledger is available and mark it as checked
            is_available = sc.check_if_ledger_is_available(ledger=ledger)
            checked_ledgers.add(ledger)  # Mark this ledger as checked

            if is_available:
                yield (script, ledger)
            else:
                # If the ledger is not available, log it and skip yielding
                log(f"Ledger {ledger} is not available for script {script[:6]}.")
        else:
            # If the ledger was already checked, simply yield if it was available
            yield (script, ledger)
