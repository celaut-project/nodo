from typing import Generator, List
from commands.__interface import table_command
from contracts.eth_main.utils import check_provider_availability
from src.utils.utils import get_ledgers, get_ledger_providers
from eth_account import Account


def private_key_to_public_key(private_key: str) -> str:
    return Account.from_key(private_key).address


def generator() -> Generator[List[str], None, None]:
    for ledger_id, private_key in get_ledgers():
        yield [
            ledger_id,
            private_key_to_public_key(private_key),
            private_key,
            str(any(check_provider_availability(i) for i in get_ledger_providers(ledger=ledger_id)))
        ]


def ledgers(stream: bool = True):
    table_command(
        f=generator,
        headers=[
            'LEDGER',
            'PUBLIC KEY',
            'PRIVATE KEY',
            'AVALIABLE'
        ],
        stream=stream
    )


def view(ledger: str):
    from database.query_interface import fetch_query
    private_key: str = next(fetch_query(
        query="SELECT private_key FROM ledger WHERE id = ?",
        params=(ledger,)
    ))[0]
    print(f"LEDGER -> {ledger}")
    print(f"PUBLIC KEY -> {private_key_to_public_key(private_key)}")
    print(f"PRIVATE KEY -> {private_key}")
