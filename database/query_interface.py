import sqlite3
from typing import Generator, Tuple, Any


def query_interface(query: str, params: tuple = ()) \
        -> Generator[
             Any, # TODO python3.10 Tuple[str | bytes | bytearray | memoryview | int | float | None],
            None, None
        ]:
    try:
        # Connect to the database
        conn = sqlite3.connect('database.sqlite')
        cursor = conn.cursor()

        # Execute the query
        cursor.execute(query, params)

        while True:
            result = cursor.fetchone()
            if not result:
                break
            yield result
        conn.close()

    except Exception as e:
        print(f'EXCEPCION NO CONTROLADA {str(e)}')
        pass
