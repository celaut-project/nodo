import requests
from protos import celaut_pb2 as celaut

from protos import gateway_pb2_grpc, gateway_pb2
from bee_rpc.client import client_grpc
import grpc
import random
import string

from src.reputation_system.contracts.ergo.utils import get_public_key, addr_to_pub_key_hex  #, pub_key_hex_to_addr
from src.reputation_system.envs import CONTRACT, LEDGER
from src.reputation_system.bip_wallet_verification import bip_ecdsa_verify, bip_ecdsa_sign
from src.database.access_functions.peers import get_peer_directions
from src.utils.logger import LOGGER as log
from src.utils.env import EnvManager

from typing import Optional

def __get_single_address_with_all_tokens(token_id: str) -> Optional[str]:
    ergo_node = EnvManager().get_env("ERGO_NODE_URL")
    if not ergo_node:
        log("No ergo node available.")
        return None

    url = f"{ergo_node}/blockchain/box/unspent/byTokenId/{token_id}"
    params = {
        "offset": 0,
        "limit": 100  # Adjust the limit as needed
    }
    
    try:
        response = requests.get(url, params=params)
        if response.status_code != 200:
            log(f"Failed to fetch data from API for token_id {token_id}. Status code: {response.status_code}")
            return None
        
        data = response.json()

        # Ensure data is a list of boxes
        if not isinstance(data, list):
            log(f"Unexpected response structure: {data}")
            return None

        # Extract addresses from all boxes
        addresses = {box.get("additionalRegisters").get("R7")[4:] for box in data if "additionalRegisters" in box and "R7" in box["additionalRegisters"]}

        # Return the address if all boxes have the same address
        if len(addresses) == 1:
            pub_key_hex = addresses.pop()
            return pub_key_hex  #pub_key_hex_to_addr(pub_key_hex)

        log(f"Multiple or no addresses found for token_id {token_id}.")
        return None

    except requests.RequestException as e:
        log(f"HTTP request failed: {e}")
        return None
    except ValueError as e:
        log(f"Failed to parse JSON response for token_id {token_id}: {e}")
        return None

def validate_contract_ledger(contract_ledger: celaut.ContractLedger, peer_id: str) -> bool:
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
    address = get_public_key(mnemonic_phrase=mnemonic_phrase)
    
    # Check if the provided public key matches the wallet's public key
    if public_key == address:
        # Sign the message using the mnemonic phrase
        signed_msg = bip_ecdsa_sign(mnemonic_phrase=mnemonic_phrase, message=message)
        log(f"Message signed successfully for public key: {public_key}")
        return signed_msg
    else:
        log(f"Public key mismatch: provided {public_key}, expected {address}")
        return None

def validate_reputation_proof_ownership() -> bool:
    # Retrieve the mnemonic phrase from environment variables
    mnemonic_phrase = EnvManager().get_env("ERGO_WALLET_MNEMONIC")
    proof_id = EnvManager().get_env("REPUTATION_PROOF_ID")
    
    # Get the public key associated with the mnemonic phrase
    address = get_public_key(mnemonic_phrase=mnemonic_phrase)
    addr_pk = addr_to_pub_key_hex(address)
    
    # Get public key associated with the reputation proof.
    proof_owner_pk = __get_single_address_with_all_tokens(proof_id)
    
    # Validate that the retrieved public key matches the expected proof owner public key
    valid = addr_pk == proof_owner_pk
    
    if not valid: 
        log((
            f"Validation failed: The derived public key ({addr_pk}) does not match "
            f"the proof owner's public key ({proof_owner_pk})."
        ))
        
    """
    Validation failed: The derived public key 
    (ProveDlog(Ecp((8696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0,515cdaf7ae08876901334938490b625a4cf1792cd188d5c36f94ecd5c63a6c9d,1)))) 
    does not match the proof owner's public key (038696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0).

    """
    
    return valid
