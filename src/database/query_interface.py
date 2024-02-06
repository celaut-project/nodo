import sqlite3

from src.utils.env import STORAGE

DATABASE_FILE = f'{STORAGE}/database.sqlite'


def fetch_query(query: str, params: tuple = ()):
    # -> Generator[
    #    # TODO python3.10 Tuple[str | bytes | bytearray | memoryview | int | float | None],
    #    None, None
    # ]:
    try:
        # Connect to the database
        conn = sqlite3.connect(DATABASE_FILE)
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


def commit_query(query: str, params: tuple = ()):
    try:
        # Connect to the database
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # Execute the query
        cursor.execute(query, params)

        conn.commit()
        conn.close()

    except Exception as e:
        print(f'EXCEPCION NO CONTROLADA {str(e)}')
        pass
