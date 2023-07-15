from typing import Generator, Tuple

from src.database.query_interface import fetch_query


def get_peer_ids() -> Generator[str, None, None]:
    for row in fetch_query(query="SELECT id FROM peer"):
        yield str(row[0])


def get_peer_id_by_ip(ip: str) -> str:
    return next(fetch_query(
        query="SELECT id FROM peer "
              "WHERE id IN ("
              "   SELECT peer_id FROM slot "
              "   WHERE id IN ("
              "       SELECT slot_id FROM uri "
              "       WHERE ip = ?"
              "   )"
              ")",
        params=(ip,)
    ))[0]


def get_peer_directions(peer_id) -> Generator[Tuple[str, int], None, None]:
    for ip, port in fetch_query(
            query="SELECT ip, port FROM uri "
                  "WHERE slot_id IN ("
                  "   SELECT id FROM slot "
                  "   WHERE peer_id = ?"
                  ")",
            params=(peer_id,)
    ):
        yield ip, port
