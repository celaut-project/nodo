from typing import Generator, List
from commands.__interface import command
from src.utils.utils import peers_id_iterator


def generator() -> Generator[List[str], None, None]:
    for peer in peers_id_iterator():
        yield [peer]


def peers():
    command(f=generator, headers=['PAR'])


def delete(peer_id):
    from database.query_interface import query_interface

    query_interface(query='''
                            DELETE FROM peer
                            WHERE id = ?
                    ''', params=(peer_id,)
                    )
