import sqlite3
import random
import string
import sys
from hashlib import sha3_256

from src.utils.env import SHA3_256_ID, EnvManager

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")


def generate_random_data(n):
    # Generate a random string of given length
    def random_string(length):
        letters = string.ascii_letters
        return ''.join(random.choice(letters) for _ in range(length))

    # Generate random data for peer table
    peer_data = []
    for _ in range(n):
        id = random_string(6)
        token = random_string(10)
        metadata = random_string(20)
        app_protocol = random_string(8)
        peer_data.append((id, token, metadata, app_protocol))

    # Generate random data for slot table
    slot_data = []
    for _ in range(n):
        id = _+1
        internal_port = random.randint(8000, 9000)
        transport_protocol = random.choice(['tcp', 'udp'])
        peer_id = random.choice(peer_data)[0]
        slot_data.append((id, internal_port, transport_protocol, peer_id))

    # Generate random data for uri table
    uri_data = []
    for _ in range(n):
        id = _+1
        ip = f"{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}"
        port = random.randint(1000, 9999)
        slot_id = random.choice(slot_data)[0]
        uri_data.append((id, ip, port, slot_id))

    # Generate random data for contract table
    contract_data = []
    for _ in range(n):
        hash_type: str = SHA3_256_ID.hex()
        contract: bytes = bytes(random_string(1000), "utf-8")
        _hash: str = sha3_256(contract).hexdigest()
        contract_data.append((_hash, hash_type, contract))

    # Generate random data for ledger table
    ledger_data = []
    for _ in range(n):
        id = random_string(6)
        private_key = random_string(16)
        ledger_data.append((id, private_key))

    # Generate random data for ledger_provider table
    ledger_provider_data = []
    for _ in range(n):
        id = _+1
        uri = f"http://{random_string(10)}.com"
        ledger_id = random.choice(ledger_data)[0]
        ledger_provider_data.append((id, uri, ledger_id))

    # Generate random data for contract_instance table
    contract_instance_data = []
    for _ in range(n):
        id = _+1
        address = f"0x{random_string(40)}"
        ledger_id = random.choice(ledger_data)[0]
        contract_hash = random.choice(contract_data)[0]
        peer_id = random.choice(peer_data)[0]
        contract_instance_data.append((id, address, ledger_id, contract_hash, peer_id))

    return peer_data, slot_data, uri_data, contract_data, ledger_data, ledger_provider_data, contract_instance_data


def seed_database(num_rows):
    # Connect to the SQLite database
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Generate random data
    peer_data, slot_data, uri_data, contract_data, ledger_data, \
        ledger_provider_data, contract_instance_data = \
        generate_random_data(
            num_rows
        )

    # Seed the "peer" table
    cursor.executemany("INSERT INTO peer (id, token, metadata, app_protocol) VALUES (?, ?, ?, ?)", peer_data)

    # Seed the "slot" table
    cursor.executemany("INSERT INTO slot (id, internal_port, transport_protocol, peer_id) VALUES (?, ?, ?, ?)",
                       slot_data)

    # Seed the "uri" table
    cursor.executemany("INSERT INTO uri (id, ip, port, slot_id) VALUES (?, ?, ?, ?)", uri_data)

    # Seed the "contract" table
    cursor.executemany("INSERT INTO contract (hash, hash_type, contract) VALUES (?, ?, ?)", contract_data)

    # Seed the "ledger" table
    cursor.executemany("INSERT INTO ledger (id, private_key) VALUES (?, ?)", ledger_data)

    # Seed the "ledger_provider" table
    cursor.executemany("INSERT INTO ledger_provider (id, uri, ledger_id) VALUES (?, ?, ?)", ledger_provider_data)

    # Seed the "contract_instance" table
    cursor.executemany(
        "INSERT INTO contract_instance (id, address, ledger_id, contract_hash, peer_id) VALUES (?, ?, ?, ?, ?)",
        contract_instance_data
    )

    # Commit the changes and close the connection
    conn.commit()
    conn.close()
    print("Database seeding completed.")


if __name__ == '__main__':
    seed_database(num_rows=int(sys.argv[2]) if len(sys.argv) > 2 else 200)
