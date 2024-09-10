from typing import Generator, Tuple


def ledger_balancer(ledger_generator: Generator[Tuple[str, str], None, None]) \
        -> Generator[Tuple[str, str], None, None]:
    # TODO Implement a ledger balancer to decide which instance of the contract to use.
    # Also, filter between those supported by itself (by this node).
    yield from ledger_generator
