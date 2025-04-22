import sqlite3
from src.utils.env import EnvManager

env_manager = EnvManager()
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

def list_instances():
    """
    Lists all internal service instances stored in the database, showing ID, IP, and computed gas value.
    If the table does not exist, prints a warning.
    """
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    try:
        # Check if 'internal_services' table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='internal_services';"
        )
        if not cursor.fetchone():
            print("Warning: The 'internal_services' table does not exist in the database.")
            return

        # Query the internal_services table
        cursor.execute(
            '''
            SELECT id, ip, gas_mantissa, gas_exponent
            FROM internal_services
            '''
        )
        rows = cursor.fetchall()

        print("Instances:\n")
        if rows:
            for row in rows:
                instance_id, ip, gas_mantissa, gas_exponent = row
                gas_value = gas_mantissa * (10 ** gas_exponent)
                gas_formatted = f"{gas_value:.6e}"

                print(f"""
ID: {instance_id}
Internal IP: {ip}
Gas Mantissa: {gas_mantissa}
Gas Exponent: {gas_exponent}
Computed Gas: {gas_formatted}
                """
                )
        else:
            print("No instances found.")

    except sqlite3.Error as e:
        print(f"An error occurred while listing instances: {e}")
    finally:
        connection.close()
