from typing import Generator, List

from commands.__interface import command
from src.utils.utils import get_ledgers


def generator() -> Generator[List[str], None, None]:
    for ledger_id, private_key in get_ledgers():
        yield [ledger_id, private_key]


def contracts():
    command(f=generator, headers=['LEDGER', 'PRIVATE KEY'])
