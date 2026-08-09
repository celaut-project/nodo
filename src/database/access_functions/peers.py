from typing import Generator, Tuple

from src.database.query_interface import fetch_query


def get_peer_ids() -> Generator[str, None, None]:
    for row in fetch_query(query="SELECT id FROM peer"):
        yield str(row[0])


def get_peer_id_by_ip(ip: str, port: int = None) -> str:
    """The peer registered at ``ip`` (optionally at that exact ``port``).

    Matching on the IP alone is ambiguous once nodes may announce private addresses:
    every Docker host has ``172.17.0.1``, so the same string legitimately belongs to
    several unrelated peers and ``next(...)`` would return an arbitrary one. Callers
    that know the port should pass it; the identity path (a signed Peer) does not rely
    on this lookup at all any more.
    """
    if port is None:
        return next(fetch_query(
            query="SELECT id FROM peer "
                  "WHERE id IN (SELECT peer_id FROM uri WHERE ip = ?)",
            params=(ip,)
        ))[0]

    return next(fetch_query(
        query="SELECT id FROM peer "
              "WHERE id IN (SELECT peer_id FROM uri WHERE ip = ? AND port = ?)",
        params=(ip, port)
    ))[0]


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
