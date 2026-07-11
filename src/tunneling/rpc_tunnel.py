import socket
from typing import Generator, Optional

from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER as logger
from protos.celaut_pb2 import TokenMessage

sc = SQLConnection()

def service_tunnel(iterator) -> Generator[bytes, None, None]:
    token_id: Optional[str] = None
    slot_id: Optional[str] = None

    # Extract token_id and slot_id from the iterator
    for c in iterator:  # could use next instead
        if type(c) == TokenMessage:
            token_id = c.token
            slot_id = c.slot
            logger(f"Received token_id: {token_id}, slot_id: {slot_id}")
            break

        else:
            logger(f"The first chunk must be a token message")
            return None

    if not token_id or not slot_id:
        logger("No token id or slot id provided")
        return None  # Could also raise an exception if preferred

    # Get the internal IP address of the container
    logger(f"Fetching internal IP for token_id: {token_id}")
    container_ip = sc.get_internal_ip(id=token_id)

    if not container_ip:
        logger(f"No internal IP found for token_id: {token_id}")
        return None  # Could also raise an exception if preferred

    logger(f"Internal IP resolved: {container_ip}")

    try:
        port = int(slot_id)
        logger(f"Resolved port: {port}")
    except ValueError:
        logger(f"Invalid port number: {slot_id}")
        return None  # Invalid port number

    try:
        # Should work for udp and virtrio too
        logger(f"Attempting connection to {container_ip}:{port}")
        with socket.create_connection((container_ip, port)) as conn:  # TCP?
            logger(f"Connection established to {container_ip}:{port}")
            conn.setblocking(False)  # Use non-blocking mode for bidirectional communication

            while True:
                # Send data from the iterator to the container
                try:
                    data = next(iterator, None)
                    if data is None:  # End of iterator
                        logger("End of iterator, closing connection")
                        break
                    if isinstance(data, bytes):
                        logger(f"Sending data: {len(data)} bytes")
                        conn.sendall(data)
                except StopIteration:
                    logger("Iterator exhausted, stopping transmission")
                    break  # Iterator is exhausted

                # Receive data from the container
                try:
                    response = conn.recv(4096)  # 4 KB buffer
                    if response:
                        logger(f"Received response: {len(response)} bytes")
                        yield response
                except socket.error:
                    logger("No data received yet, continuing loop")
                    pass  # No data to read yet, continue the loop

    except (socket.error, ValueError) as e:
        logger(f"Error during socket operation: {e}")
        return None
