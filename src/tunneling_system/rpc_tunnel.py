import socket
from typing import Generator, Optional

from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER as log
from protos.gateway_pb2 import TokenMessage

sc = SQLConnection()

def service_tunnel(iterator) -> Generator[bytes, None, None]:
    token_id: Optional[str] = None
    slot_id: Optional[str] = None

    # Extract token_id and slot_id from the iterator
    for c in iterator:
        if type(c) == TokenMessage:
            token_id = c.token
            slot_id = c.slot
            log(f"Received token_id: {token_id}, slot_id: {slot_id}")
            break

        else:
            log(f"The first chunk must be a token message")
            return None

    if not token_id or not slot_id:
        log("No token id or slot id provided")
        return None  # Could also raise an exception if preferred

    # Get the internal IP address of the container
    log(f"Fetching internal IP for token_id: {token_id}")
    container_ip = sc.get_internal_ip(id=token_id)

    if not container_ip:
        log(f"No internal IP found for token_id: {token_id}")
        return None  # Could also raise an exception if preferred

    log(f"Internal IP resolved: {container_ip}")

    try:
        port = int(slot_id)
        log(f"Resolved port: {port}")
    except ValueError:
        log(f"Invalid port number: {slot_id}")
        return None  # Invalid port number

    try:
        log(f"Attempting connection to {container_ip}:{port}")
        with socket.create_connection((container_ip, port)) as conn:
            log(f"Connection established to {container_ip}:{port}")
            conn.setblocking(False)  # Use non-blocking mode for bidirectional communication

            while True:
                # Send data from the iterator to the container
                try:
                    data = next(iterator, None)
                    if data is None:  # End of iterator
                        log("End of iterator, closing connection")
                        break
                    if isinstance(data, bytes):
                        log(f"Sending data: {len(data)} bytes")
                        conn.sendall(data)
                except StopIteration:
                    log("Iterator exhausted, stopping transmission")
                    break  # Iterator is exhausted

                # Receive data from the container
                try:
                    response = conn.recv(4096)  # 4 KB buffer
                    if response:
                        log(f"Received response: {len(response)} bytes")
                        yield response
                except socket.error:
                    log("No data received yet, continuing loop")
                    pass  # No data to read yet, continue the loop

    except (socket.error, ValueError) as e:
        log(f"Error during socket operation: {e}")
        return None
