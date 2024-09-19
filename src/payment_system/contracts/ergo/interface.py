from protos import celaut_pb2, gateway_pb2
import requests
from hashlib import sha3_256
from ergpy import appkit
from src.database import sql_connection
from src.utils.logger import LOGGER
from src.utils.env import EnvManager
import json
from time import sleep

from jpype import *
import java.lang

import jpype
from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *

# Initialize environment and global variables
env_manager = EnvManager()
DEFAULT_FEE = 1_000_000  # Fee for the transaction in nanoErgs
LEDGER = "ergo" # or "ergo-testnet" for Ergo testnet.
CONTRACT = "proveDlog(decodePoint())".encode('utf-8')  # Ergo tree script
CONTRACT_HASH = sha3_256(CONTRACT).hexdigest()
RECIVER_ADDR = env_manager.get_env('ERGO_PAYMENTS_RECIVER_WALLET')
WAIT_TX_TIME = 240

def __to_nanoerg(amount: int) -> int:
    return int(amount/(10**58)) if amount > 10**58 else amount

def __get_sender_addr() -> Address:
    # Initialize ErgoAppKit and get the sender's address
    ergo = appkit.ErgoAppKit(node_url=env_manager.get_env('ERGO_NODE_URL'))

    mnemonic = ergo.getMnemonic(wallet_mnemonic=env_manager.get_env('ERGO_WALLET_MNEMONIC'), mnemonic_password=None)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])
    return sender_address

def init():
    LOGGER("Make a sql query ergo.")
    sender_addr = str(__get_sender_addr().toString())
    LOGGER(f"sender address -> {sender_addr}")
    sql = sql_connection.SQLConnection()
    sql.add_contract(contract=gateway_pb2.celaut__pb2.Service.Api.ContractLedger(
        ledger=LEDGER,
        contract_addr=sender_addr,
        contract=CONTRACT
    ))

    LOGGER("ERGO PAYMENT INTERFACE INITIATED.")

# Function to process the payment, generating a transaction with the token in register R4
def process_payment(amount: int, deposit_token: str, ledger: str, contract_address: str) -> celaut_pb2.Service.Api.ContractLedger:
    amount = __to_nanoerg(amount)
    LOGGER(f"Process ergo platform payment for token {deposit_token} of {amount}")

    try:
        # Initialize ErgoAppKit and get the sender's address
        ergo = appkit.ErgoAppKit(node_url=env_manager.get_env('ERGO_NODE_URL'))
        sender_address = __get_sender_addr()

        # Fetch UTXO from the contract's address
        input_utxo = ergo.getInputBoxCovering(
            amount_list=[amount],
            sender_address=sender_address
        )

        if not input_utxo:
            raise Exception("No UTXO found for the contract address with the required token.")

        # Build the output box with the token in register R4
        out_box = ergo._ctx.newTxBuilder() \
                    .outBoxBuilder() \
                    .value(amount) \
                    .registers([
                        ErgoValue.of(jpype.JString(deposit_token).getBytes("utf-8"))  # Store token in R4
                    ]) \
                    .contract(Address.create(contract_address).toErgoContract()) \
                    .build()  # Build the output box

        # Create the unsigned transaction
        unsigned_tx = ergo.buildUnsignedTransaction(
            input_box=input_utxo,  # Input UTXO
            outBox=[out_box],  # Output box
            fee=DEFAULT_FEE / 10**9,  # Fee for the transaction
            sender_address=sender_address  # Sender's address
        )

        # Sign the transaction
        signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index=0)

        # Submit the transaction and get the transaction ID
        tx_id = ergo.txId(signed_tx)
        LOGGER(f"Transaction submitted: {tx_id}")

        for sec in range(0, WAIT_TX_TIME):
            sleep(1)
            response = requests.get(f"{ergo.get_api_url()}/api/v1/transactions/{tx_id}")
            if response.status_code != 200:
                LOGGER(f"Error fetching UTXOs: {response.status_code} - {response.text}")  # TODO should be del.
                continue

            obj = response.json()
            if obj["numConfirmations"] > 1:
                LOGGER(f"Tx {tx_id} verified.")
                return gateway_pb2.celaut__pb2.Service.Api.ContractLedger(
                    ledger=ledger,
                    contract_addr=contract_address,
                    contract=CONTRACT
                )

        err = f"Can't verify the tx {tx_id}"
        LOGGER(err)
        raise Exception(err)

    except Exception as e:
        LOGGER(f"Error processing payment: {str(e)}")
        raise e

# Function to validate the payment process by checking if there is an unspent box with the token in register R4
def payment_process_validator(amount: int, token: str, ledger: str, contract_addr: str) -> bool:
    LOGGER(f"Validating token {token}")
    try:
        assert ledger == LEDGER, "Ledger does not match"
        assert contract_addr == __get_sender_addr(), "Contract address does not match"

        # Initialize ErgoAppKit and fetch unspent UTXOs for the contract address
        ergo = appkit.ErgoAppKit(node_url=env_manager.get_env('ERGO_NODE_URL'))
        explorer_api = ergo.get_api_url()

        # Construct the API URL to fetch unspent UTXOs for the contract address
        url = f"{explorer_api}/api/v1/boxes/unspent/unconfirmed/byAddress/{contract_addr}"
        response = requests.get(url)

        if response.status_code != 200:
            LOGGER(f"Error fetching UTXOs: {response.status_code} - {response.text}")
            return False

        # Parse the response from the API
        utxos = response.json()

        for box_dict in utxos:
            LOGGER(f"aux log {box_dict}")

            # Check if the box has additionalRegisters and specifically R4
            if "additionalRegisters" in box_dict and "R4" in box_dict["additionalRegisters"]:
                r4_value = box_dict["additionalRegisters"]["R4"]["renderedValue"]
                decoded_r4 = bytes.fromhex(r4_value).decode("utf-8")
                LOGGER(f"aux decoded log {decoded_r4}")

                # Check if the decoded value matches the token
                if decoded_r4 == token:
                    LOGGER(f"Token {token} found in R4.")

                    # Validate correct amount.
                    if "value" in box_dict and box_dict["value"] == __to_nanoerg(amount):
                        LOGGER(f"Correct amount for token {token}")
                        return True
                    else:
                        LOGGER(f"Incorrect amount for token {token}. Value was {box_dict} but should be {__to_nanoerg(amount)}")
                        return False

        # If no match found
        LOGGER(f"Token {token} not found in R4.")
        return False

    except Exception as e:
        LOGGER(f"Error validating payment process: {str(e)}")
        return False
