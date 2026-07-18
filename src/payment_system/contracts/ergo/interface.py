from src.reputation_system.envs import ergo_ledger
from typing import Optional, List, Tuple
from protos import celaut_pb2, celaut_pb2
import requests
from hashlib import sha3_256
from src.database import sql_connection
from src.payment_system.exceptions import DoubleSpendingAttempt
from src.utils.logger import LOGGER
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_address, set_script, set_token_id
from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module
from threading import Lock
from time import sleep

def clamp(value: float, maximum: float, minimum: float) -> float:
    return max(minimum, min(value, maximum))


def _ergo_runtime():
    ensure_ergpy_jvm(feature="Ergo payments")
    appkit = require_java_module("ergpy.appkit", feature="Ergo payments")
    helper_functions = require_java_module("ergpy.helper_functions", feature="Ergo payments")
    jpype = require_java_module("jpype", feature="Ergo payments")
    org_appkit = jpype.JPackage("org").ergoplatform.appkit
    return appkit, helper_functions.simple_send, jpype, org_appkit


# Initialize environment and global variables
env_manager = ConfigManager()
DEFAULT_FEE = 1_000_000  # Fee for the transaction in nanoErgs
LEDGER = "ergo" # or "ergo-testnet" for Ergo testnet.  TODO Ergo ledger actually should be the serialized protobuf.  -> But must be an id, and be defined on a yalm config file with the rest of envs. 
CONTRACT = "proveDlog(decodePoint())"  # Ergo tree template script
ERGO_NODE_URL = lambda: env_manager.get("ledgers.ergo.NODE_URL")
COLD_WALLET = lambda: env_manager.get('PAYMENTS_RECIVER_WALLET')
ERGO_DONATION_WALLET = lambda: env_manager.get('ledgers.ergo.DONATION_WALLET')
DONATION_PERCENTAGE = lambda: clamp(float(env_manager.get('ledgers.ergo.DONATION_PERCENTAGE')), 1.0, 0.0)  # type: ignore
HOT_LIMITS = int(env_manager.get("ledgers.ergo.HOT_WALLET_LIMITS"))  # type: ignore
# Receiver wallet. The documented/config key is AUXILIARY_MNEMONIC (see config.example.yaml
# and the auto-generation in src/utils/config.py); fall back to the legacy AUXILIAR_MNEMONIC
# spelling for back-compat with any node that already set the misspelled key.
AUXILIAR_MNEMONIC = (
    env_manager.get("ledgers.ergo.AUXILIARY_MNEMONIC")
    or env_manager.get("ledgers.ergo.AUXILIAR_MNEMONIC")
)
WALLET_MNEMONIC = lambda: env_manager.get('ledgers.ergo.WALLET_MNEMONIC')  # Sender wallet
GAS_PER_ERG_L = lambda: int(env_manager.get("ledgers.ergo.GAS_PER_ERG"))
WAIT_TX_TIME = 240  # 20 minutes (each 5 seconds)
WAT_TX_SLEEP_TIME = 5

CONTRACT_HASH = sha3_256(CONTRACT.encode("utf-8")).hexdigest()

"""

CLIENT_WALLET -> AUXILIAR_WALLET -> MAIN_WALLET -> COLD_WALLET

"""

payment_lock = Lock()  # Ensures that the same input box is no spent with more amount that it has. (could be more efficient ...)

def __gas_to_nanoerg(amount: int) -> int:
    gas_price = 1/GAS_PER_ERG_L()
    return int(round(amount*gas_price))

def __nanoerg_to_erg(amount: int) -> float:
    return amount / 1_000_000_000  # type: ignore

def __init_ergo():
    appkit, _, _, _ = _ergo_runtime()
    node_url = ERGO_NODE_URL()
    if not node_url.endswith('/'):
        node_url += '/'
    return appkit.ErgoAppKit(node_url=node_url)

def __get_sender_addr(mnemonic: str):
    # Initialize ErgoAppKit and get the sender's address
    ergo = __init_ergo()

    _m = ergo.getMnemonic(wallet_mnemonic=mnemonic, mnemonic_password=None)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=_m[1], wallet_password=_m[2])
    return sender_address

def __balance_total(address) -> Optional[dict]:
    # Initialize ErgoAppKit and fetch unspent UTXOs for the contract address
    ergo = __init_ergo()
    explorer_api = ergo.get_api_url()

    # Construct the API URL to fetch unspent UTXOs for the contract address
    url = f"{explorer_api}/api/v1/addresses/{str(address.toString())}/balance/total"
    response = requests.get(url)

    if response.status_code != 200:
        LOGGER(f"Error fetching the total balance: {response.status_code} - {response.text}")
        return None

    # Parse the response from the API
    return response.json()

def get_amount_by_addr(mnemonic: str) -> int:
    return __balance_total(__get_sender_addr(mnemonic=mnemonic))["confirmed"]["nanoErgs"]

def get_balances(only_sender: bool=False) -> Tuple[Tuple[str, float], Tuple[str, float]]:
    
    # Sender wallet
    _addr = __get_sender_addr(WALLET_MNEMONIC())
    _amount = __balance_total(address=_addr)["confirmed"]["nanoErgs"]

    if only_sender:
        return (
            str(_addr.toString()), 
            __nanoerg_to_erg(_amount)
        ), ("", 0.0)
        
        
    # Receiver wallet
    _aux_addr = __get_sender_addr(AUXILIAR_MNEMONIC)
    _aux_amount = __balance_total(address=_aux_addr)["confirmed"]["nanoErgs"]
    
    return (
            str(_addr.toString()), 
            __nanoerg_to_erg(_amount)
        ), (
            str(_aux_addr.toString()), 
            __nanoerg_to_erg(_aux_amount)
        )


def init():
    sender_addr = str(__get_sender_addr(AUXILIAR_MNEMONIC).toString())
    sql = sql_connection.SQLConnection()
    contract = celaut_pb2.Contract(ledger=ergo_ledger)
    set_token_id(contract, "ERG")
    set_address(contract, sender_addr)
    set_script(contract, CONTRACT.encode("utf-8"))
    sql.add_contract(
        contract=contract
    )

def check_sender_balance(amount: int) -> bool:
    try:
        check = get_balances(only_sender=True)[0][1] > __gas_to_nanoerg(amount)
        if not check:
            LOGGER(f"Insufficient balance for the sender wallet. Required: {__gas_to_nanoerg(amount)}, Available: {get_balances(only_sender=True)[0][1]}")
        return check
    except Exception as e:
        LOGGER(f"Error checking sender balance: {str(e)}")
        return False

def manager():
    LOGGER("Exec ergo interface manager")
    # Move the available outputs from AUXILIAR_MNEMONIC to WALLET_MNEMONIC.
    try:
        _, simple_send, _, _ = _ergo_runtime()
        aux_confirmed_amount = __balance_total(__get_sender_addr(AUXILIAR_MNEMONIC))["confirmed"]["nanoErgs"]
        # Funds that may have been sent in the iteration prior to the main wallet but have not yet been confirmed on the network are taken into account.
        wallet_unconfirmed_amount = __balance_total(__get_sender_addr(WALLET_MNEMONIC()))["unconfirmed"]["nanoErgs"]
        # Check if send is needed.
        if aux_confirmed_amount - wallet_unconfirmed_amount > 2*DEFAULT_FEE:
            # Normalize to ergs.
            aux_confirmed_amount = __nanoerg_to_erg(aux_confirmed_amount)
            fee = __nanoerg_to_erg(DEFAULT_FEE)
            wallet_confirmed_amount = __nanoerg_to_erg(__balance_total(__get_sender_addr(WALLET_MNEMONIC()))["confirmed"]["nanoErgs"])

            aux_total = aux_confirmed_amount - fee
            amounts = [aux_total]
            receiver_addresses = [str(__get_sender_addr(WALLET_MNEMONIC()).toString())]
            # Check if send to cold wallet is need.
            cold_wallet = COLD_WALLET()
            donation_percentage = DONATION_PERCENTAGE()
            donation_wallet = ERGO_DONATION_WALLET()
            if cold_wallet and aux_total + wallet_confirmed_amount > HOT_LIMITS:
                to_hot_amount = min(aux_total, max(0, HOT_LIMITS - wallet_confirmed_amount))
                to_cold_amount = aux_total - to_hot_amount

                if to_cold_amount > DEFAULT_FEE:
                    # Calculate donation amount based on percentage
                    donation_amount = to_cold_amount * donation_percentage

                    if donation_wallet and donation_amount > DEFAULT_FEE:
                        # Deduct the donation amount from the cold wallet amount
                        to_cold_amount -= donation_amount
                        amounts = [to_hot_amount, to_cold_amount, donation_amount]
                        receiver_addresses.append(cold_wallet)
                        receiver_addresses.append(donation_wallet)
                        LOGGER(f"Send {to_cold_amount} erg from receiver-node-wallet to cold-wallet.")
                        LOGGER(f"Send {donation_amount} erg from receiver-node-wallet to donation-wallet.")
                    else:
                        # If the donation amount is less than DEFAULT_FEE, send everything to the cold wallet
                        amounts = [to_hot_amount, to_cold_amount]
                        receiver_addresses.append(cold_wallet)
                        LOGGER(f"Send {to_cold_amount} erg from receiver-node-wallet to cold-wallet.")
            else:
                to_hot_amount = aux_total

            LOGGER(f"Send {to_hot_amount} erg from receiver-node-wallet to main-node-wallet.")
            tx = simple_send(
                ergo=__init_ergo(),
                amount=amounts, receiver_addresses=receiver_addresses,
                wallet_mnemonic=AUXILIAR_MNEMONIC, fee=fee
            )
            LOGGER(f"Simple send tx -> {tx}")
    except Exception as e:
        LOGGER(f"Exception on simple send -> {str(e)}")


# Function to process the payment, generating a transaction with the token in register R4
def process_payment(amount: int, deposit_token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> celaut_pb2.Contract:
    with payment_lock:
        amount = __gas_to_nanoerg(amount)
        LOGGER(f"Process ergo platform payment for token {deposit_token} of {amount}")

        try:
            _, _, jpype, org_appkit = _ergo_runtime()
            # Initialize ErgoAppKit and get the sender's address
            ergo = __init_ergo()
            sender_address = __get_sender_addr(WALLET_MNEMONIC())

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
                            org_appkit.ErgoValue.of(jpype.JString(deposit_token).getBytes("utf-8"))  # Store token in R4
                        ]) \
                        .contract(org_appkit.Address.create(script.decode('utf-8')).toErgoContract()) \
                        .build()  # Build the output box

            # Create the unsigned transaction
            unsigned_tx = ergo.buildUnsignedTransaction(
                input_box=input_utxo,  # Input UTXO
                outBox=[out_box],  # Output box
                fee=DEFAULT_FEE / 10**9,  # Fee for the transaction
                sender_address=sender_address  # Sender's address
            )

            # Sign the transaction
            w_mnemonic = ergo.getMnemonic(wallet_mnemonic=WALLET_MNEMONIC(), mnemonic_password=None)[0]
            signed_tx = ergo.signTransaction(unsigned_tx, w_mnemonic, prover_index=0)

            # Submit the transaction and get the transaction ID
            try:
                tx_id = ergo.txId(signed_tx)
                LOGGER(f"Transaction submitted: {tx_id} for token {deposit_token}")
            except Exception as e:
                if "Double spending attempt" in str(e):
                    raise DoubleSpendingAttempt(LEDGER)
                else:
                    raise e

            for sec in range(0, WAIT_TX_TIME):
                sleep(WAT_TX_SLEEP_TIME)
                response = requests.get(f"{ergo.get_api_url()}/api/v1/transactions/{tx_id}")
                if response.status_code != 200:
                    if response.status_code != 404:
                        LOGGER(f"{ergo.get_api_url()} requests to check tx {tx_id} failed with status code {response.status_code}")
                    continue

                obj = response.json()
                if obj["numConfirmations"] > 1:
                    LOGGER(f"Tx {tx_id} verified.")
                    contract = celaut_pb2.Contract(ledger=ledger)
                    set_token_id(contract, "ERG")
                    set_script(contract, CONTRACT.encode("utf-8"))
                    set_address(contract, script.decode("utf-8"))
                    return contract

            raise Exception(f"Can't verify the tx {tx_id}")

        except Exception as e:
            raise e


# Function to validate the payment process by checking if there is an unspent box with the token in register R4
def payment_process_validator(amount: int, token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> bool:
    try:
        address = script.decode("utf-8")
        assert LEDGER in ledger.tags, "Ledger does not match"
        assert address == str(__get_sender_addr(AUXILIAR_MNEMONIC).toString()), "Contract address does not match"

        # Initialize ErgoAppKit and fetch unspent UTXOs for the contract address
        ergo = __init_ergo()
        explorer_api = ergo.get_api_url()

        # Construct the API URL to fetch unspent UTXOs for the contract address
        url = f"{explorer_api}/api/v1/boxes/unspent/unconfirmed/byAddress/{address}"
        response = requests.get(url)

        if response.status_code != 200:
            LOGGER(f"Error fetching UTXOs: {response.status_code} - {response.text}")
            return False

        # Parse the response from the API
        utxos = response.json()

        for box_dict in utxos:
            # Check if the box has additionalRegisters and specifically R4
            if "additionalRegisters" in box_dict and "R4" in box_dict["additionalRegisters"]:
                r4_value = box_dict["additionalRegisters"]["R4"]["renderedValue"]
                decoded_r4 = bytes.fromhex(r4_value).decode("utf-8")

                # Check if the decoded value matches the token
                if decoded_r4 == token:
                    # Validate correct amount.
                    if "value" in box_dict and box_dict["value"] == __gas_to_nanoerg(amount):
                        return True
                    else:
                        LOGGER(f"Incorrect amount for token {token}. Value was {box_dict} but should be {__gas_to_nanoerg(amount)}")
                        return False

        # If no match found
        LOGGER(f"Token {token} not found in R4.")
        return False

    except Exception as e:
        LOGGER(f"Error validating payment process: {str(e)}")
        return False
