import grpc
from typing import Final, Generator
from protos import gateway_pb2, gateway_pb2_grpc
from src.utils.logger import LOGGER
from src.utils.env import EnvManager
from tests.main import sc, service_tunnel

"""
    ¡¡¡NOT TESTED!!!
"""

env_manager = EnvManager()

def test_service_tunnel():
    """
    Test the ServiceTunnel endpoint by verifying bidirectional communication.
    """
    try:
        TOKEN_ID: Final[str] = "example-token-id"  # Replace with a valid token
        SLOT_ID: Final[int] = 12345  # Replace with a valid slot
        TEST_DATA: Final[bytes] = b"Hello, ServiceTunnel!"
    except Exception as e:
        LOGGER(f"Error initializing test parameters: {e}")
        return

    # Mock an iterator to simulate the input stream
    def input_stream() -> Generator:
        yield gateway_pb2.TokenMessage(token=TOKEN_ID, slot=str(SLOT_ID))
        yield TEST_DATA

    # Create a gRPC channel and stub for the Gateway service
    g_stub = gateway_pb2_grpc.GatewayStub(
        grpc.insecure_channel(env_manager.get_env("GATEWAY"))
    )

    # Simulate the ServiceTunnel call
    try:
        responses = service_tunnel(input_stream())

        for response in responses:
            if isinstance(response, bytes):
                LOGGER(f"Response from service: {response.decode('utf-8')}")
            else:
                LOGGER(f"Unexpected response type: {type(response)}")
    except Exception as e:
        LOGGER(f"Error during ServiceTunnel communication: {e}")
