import sqlite3

from src.utils.env import EnvManager

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

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
        print(f'EXCEPCION NO CONTROLADA {str(e)} en fetch_query')
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
        print(f'EXCEPCION NO CONTROLADA {str(e)} en commit_query')
        pass
