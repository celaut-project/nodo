import requests
from protos import celaut_pb2 as celaut

from protos import gateway_pb2_grpc, gateway_pb2
from grpcbigbuffer.client import client_grpc
import grpc
import random
import string

from src.reputation_system.envs import CONTRACT, LEDGER
from src.reputation_system.bip_wallet_verification import bip_ecdsa_verify, get_public_key_hex, bip_ecdsa_sign
from src.database.access_functions.peers import get_peer_directions
from src.utils.logger import LOGGER as log
from src.utils.env import EnvManager

def __get_single_address_with_all_tokens(token_id) -> str | None:
    ergo_node = EnvManager().get_env("ERGO_NODE_URL")
    url = f"{ergo_node}/api/v1/boxes/unspent/byTokenId/{token_id}"
    params = {
        "offset": 0,
        "limit": 100  # Adjust the limit as needed
    }
    
    response = requests.get(url, params=params)
    if response.status_code != 200:
        log(f"Failed to fetch data from API for token_id {token_id}. Status code: {response.status_code}")
        return None
    
    data = response.json()
    addresses = set()
    
    for item in data.get('items', []):
        for output in item.get('outputs', []):
            addresses.add(output.get('address'))
    
    # If only one unique address contains all the tokens, return it
    if len(addresses) == 1:
        return addresses.pop()
    
    log(f"Multiple addresses found for token_id {token_id}, or no addresses found.")
    return None

def validate_contract_ledger(contract_ledger: celaut.Service.Api.ContractLedger, peer_id: str) -> bool:
    """
    Validates the contract ledger by checking compatibility with predefined contract and ledger,
    generating a random message, signing it using the peer's public key, and verifying the signature.
    """
    # Check compatibility of the contract ledger
    compatibility = contract_ledger.ledger == LEDGER and contract_ledger.contract == CONTRACT.encode("utf-8")
    
    if not compatibility: 
        log(f"Contract ledger not compatible: {contract_ledger}")
        return False
    
    # Generate a random message
    message = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    log(f"Generated random message: {message}")
    
    # Get public key from explorer
    public_key = __get_single_address_with_all_tokens(contract_ledger.contract_addr)
    if not public_key:
        log("Failed to obtain public key.")
        return False
    
    try:
        # Get peer directions
        ip, port = next(get_peer_directions(peer_id=peer_id))
        log(f"Connecting to peer at {ip}:{port} to validate reputation proof.")
        
        # Request signature of the message
        sign_response = next(client_grpc(
            method=gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(f"{ip}:{str(port)}")
            ).SignPublicKey,
            indices_parser=gateway_pb2.SignResponse,
            partitions_message_mode_parser=True,
            input=gateway_pb2.SignRequest(
                public_key=public_key,
                to_sign=message
            )
        )).signed
        
        log(f"Peer {ip}:{port} sign response {sign_response}")
        
        # Verify the signature
        is_valid = bip_ecdsa_verify(message=message, signature_hex=sign_response, public_key_hex=public_key)
        log(f"Signature verification: {'successful' if is_valid else 'failed'}")
        return is_valid
    
    except Exception as e:
        log(f"Error during contract validation: {e}")
        return False

def sign_message(public_key, message) -> str | None:
    """
    Signs a message using the private key associated with the provided public key.
    
    Args:
        public_key (str): The public key to verify against the wallet's public key.
        message (str): The message to be signed.
    
    Returns:
        str | None: The signed message if the public key matches the wallet's public key, otherwise None.
    """
    # Retrieve the mnemonic phrase from environment variables
    mnemonic_phrase = EnvManager().get_env("ERGO_WALLET_MNEMONIC")
    
    # Get the public key associated with the mnemonic phrase
    address = get_public_key_hex(mnemonic_phrase=mnemonic_phrase)
    
    # Check if the provided public key matches the wallet's public key
    if public_key == address:
        # Sign the message using the mnemonic phrase
        signed_msg = bip_ecdsa_sign(mnemonic_phrase=mnemonic_phrase, message=message)
        log(f"Message signed successfully for public key: {public_key}")
        return signed_msg
    else:
        log(f"Public key mismatch: provided {public_key}, expected {address}")
        return None
