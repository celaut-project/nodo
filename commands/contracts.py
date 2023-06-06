from typing import Generator, List
from commands.__interface import table_command
from src.utils.utils import get_ledgers
from web3.auto import w3
import binascii


def generator() -> Generator[List[str], None, None]:
    for ledger_id, private_key in get_ledgers():
        yield [
            ledger_id,
            #binascii.hexlify(
            #    w3.eth.account.privateKeyToAccount(private_key).public_key.to_bytes()
            #).decode('utf-8'),
            private_key
        ]


def contracts():
    table_command(
        f=generator,
        headers=[
            'LEDGER',
            # 'PUBLIC KEY',
            'PRIVATE KEY'
        ]
    )
