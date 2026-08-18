from typing import Generator, Tuple

from src.database.query_interface import fetch_query


def get_peer_ids() -> Generator[str, None, None]:
    for row in fetch_query(query="SELECT id FROM peer"):
        yield str(row[0])


def get_peer_directions(peer_id) -> Generator[Tuple[str, int, str], None, None]:
    """Every address announced by ``peer_id``, as ``(ip, port, transport)``.

    ``transport`` is the tag the peer declared for that address ("tcp"/"udp"), or an
    empty string for a legacy row that predates per-address transports.
    """
    for ip, port, transport in fetch_query(
            query="SELECT ip, port, transport FROM uri WHERE peer_id = ?",
            params=(peer_id,)
    ):
        yield ip, port, transport or ""
