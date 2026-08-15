import requests
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from src.utils.logger import LOGGER


def tx_history():
    """
    Main command function to display transaction history for both sending and receiving wallets.
    This function will be called from nodo.py when the tx_history command is executed.
    """
    print("Transaction History")
    print("=" * 50)
    
    try:
        # Single wallet.
        address = _get_wallet_address()
        _display_wallet_transactions("Wallet", address)

    except Exception as e:
        LOGGER(f"Error in tx_history command: {str(e)}")
        print(f"Error retrieving transaction history: {str(e)}")


def _get_wallet_address() -> str:
    """
    Retrieve the single wallet address using existing Ergo utilities.

    Raises:
        Exception: If wallet configuration is missing or invalid
    """
    try:
        from src.payment_system.contracts.ergo.interface import get_wallet_address

        return get_wallet_address()

    except Exception as e:
        raise Exception(f"Failed to retrieve wallet address: {str(e)}")


def _display_wallet_transactions(wallet_type: str, address: str):
    """
    Display transaction history for a specific wallet.
    
    Args:
        wallet_type: Type of wallet (e.g., "Wallet")
        address: Wallet address to fetch transactions for
    """
    print(f"[{wallet_type}] - Address: {address}")
    print("-" * 60)
    
    try:
        # Fetch transactions for this address
        transactions = _get_address_transactions(address)

        if not transactions:
            print("No recent transactions found.")
        else:
            # Resolved once for the page, not per transaction: both lookups are a single
            # query each, and the second (every deposit token this node ever issued) is
            # the only way an incoming payment can be attributed at all.
            payments = _payments_by_tx_id(transactions)
            clients_by_token = _clients_by_deposit_token()

            # Display each transaction
            for i, tx in enumerate(transactions):
                _display_transaction(tx, address, payments, clients_by_token)
                if i < len(transactions) - 1:
                    print()  # Add spacing between transactions

    except Exception as e:
        LOGGER(f"Error fetching transactions for {wallet_type}: {str(e)}")
        print(f"Error fetching transactions: {str(e)}")


def _get_address_transactions(address: str, limit: int = 10) -> List[Dict]:
    """
    Fetch transaction history from Ergo Explorer API for a given address.
    
    Args:
        address: Wallet address to fetch transactions for
        limit: Maximum number of transactions to fetch (default: 10)
        
    Returns:
        List of transaction dictionaries
        
    Raises:
        Exception: If API request fails or returns invalid data
    """
    try:
        from src.payment_system.contracts.ergo.interface import __init_ergo

        # Get Explorer API URL from existing Ergo utilities
        ergo = __init_ergo()
        explorer_api = ergo.get_api_url()
        
        # Construct API URL for address transactions
        url = f"{explorer_api}/api/v1/addresses/{address}/transactions"
        params = {
            'offset': 0,
            'limit': limit
        }
        
        # Make API request
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code == 404:
            # Address not found or no transactions
            return []
        elif response.status_code != 200:
            raise Exception(f"API request failed with status {response.status_code}: {response.text}")
        
        # Parse JSON response
        data = response.json()
        
        # Return the items list, or empty list if not present
        return data.get('items', [])
        
    except requests.exceptions.Timeout:
        raise Exception("Request timed out while fetching transactions")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Network error while fetching transactions: {str(e)}")
    except ValueError as e:
        raise Exception(f"Invalid JSON response from API: {str(e)}")


def _display_transaction(tx: Dict, address: str,
                         payments: Optional[Dict[str, Dict]] = None,
                         clients_by_token: Optional[Dict[str, str]] = None):
    """
    Display a single transaction in a formatted manner.

    Args:
        tx: Transaction dictionary from API response
        address: Wallet address to determine transaction direction
        payments: Local payment rows keyed by transaction id, for naming the peer
        clients_by_token: client id per deposit token, for naming the payer
    """
    try:
        from src.payment_system.contracts.ergo.interface import __nanoerg_to_erg

        # Extract transaction details
        tx_id = tx.get('id', 'N/A')
        timestamp = tx.get('timestamp', 0)
        confirmations = tx.get('numConfirmations', 0)

        # Format timestamp
        formatted_time = _format_timestamp(timestamp)

        # Determine transaction direction and amount
        direction, amount_nanoerg = _determine_transaction_direction(tx, address)
        amount_erg = __nanoerg_to_erg(amount_nanoerg) if amount_nanoerg else 0.0

        # Display transaction information
        print(f"Transaction ID: {tx_id}")
        print(f"Amount: {amount_erg:.9f} ERG")
        print(f"Timestamp: {formatted_time}")
        print(f"Confirmations: {confirmations}")
        print(f"Direction: {direction}")
        for line in _counterparty_lines(tx, address, direction,
                                        payments or {}, clients_by_token or {}):
            print(line)

    except Exception as e:
        LOGGER(f"Error displaying transaction: {str(e)}")
        print(f"Error displaying transaction: {str(e)}")


def _payments_by_tx_id(transactions: List[Dict]) -> Dict[str, Dict]:
    """Local payment rows for the transactions on screen, keyed by transaction id.

    The chain knows addresses; only this node knows which peer an address belonged to
    when it was paid. A checkout with no such rows yet -- or a wallet with activity
    nodo never made -- just gets nothing back, and the raw address is shown instead.
    """
    try:
        from src.database.sql_connection import SQLConnection

        return SQLConnection().get_payments_by_tx_ids(
            [tx.get('id') for tx in transactions if tx.get('id')]
        )
    except Exception as e:
        LOGGER(f"Could not read local payment records: {str(e)}")
        return {}


def _clients_by_deposit_token() -> Dict[str, str]:
    """client id per deposit token, for attributing incoming payments.

    A client pays by putting its deposit token in R4 of the box it sends us -- that
    register is how the node validates the payment in the first place, so it is also
    the one honest way to say who a received transaction came from. The payer's own
    address says nothing: a client is not an address, and nothing on chain links them.
    """
    try:
        from src.database.sql_connection import SQLConnection

        return {
            token['id']: token['client_id']
            for token in SQLConnection().get_deposit_tokens()
            if token.get('client_id')
        }
    except Exception as e:
        LOGGER(f"Could not read deposit tokens: {str(e)}")
        return {}


def _counterparty_lines(tx: Dict, address: str, direction: str,
                        payments: Dict[str, Dict],
                        clients_by_token: Dict[str, str]) -> List[str]:
    """Who was on the other side, named when this node can name them.

    Three sources, most trustworthy first: the payment this node recorded when it made
    it (exact -- it holds the peer id), the deposit token in R4 of an incoming box
    (exact -- it holds the client id), and failing both, the raw address, which is
    still more than the previous output gave.
    """
    lines: List[str] = []
    outgoing = direction.startswith("Outgoing")

    # Only outgoing rows can match here: an incoming payment is recorded without a
    # transaction id, because the box proving it is not the transaction that made it.
    payment = payments.get(tx.get('id') or '')
    if payment:
        # A row means this node signed the transaction, which settles the direction
        # more firmly than matching addresses does -- `_determine_transaction_direction`
        # reports "Unknown" whenever the explorer hands back inputs without addresses.
        outgoing = payment.get('direction', 'out') == 'out'
        if payment.get('peer_id'):
            lines.append(f"To: peer {payment['peer_id']}")

    if not outgoing:
        for token in _deposit_tokens_in(tx):
            client_id = clients_by_token.get(token)
            if client_id:
                lines.append(f"From: client {client_id} (deposit token {token})")
                break
            lines.append(f"From: an unknown deposit token {token}")
            break

    counterparties = _counterparty_addresses(tx, address, outgoing)
    if counterparties:
        label = "To address" if outgoing else "From address"
        lines.append(f"{label}: {', '.join(counterparties)}")
    elif not lines:
        lines.append("Counterparty: unknown")

    if payment and payment.get('status') == 'unacknowledged':
        lines.append("Note: broadcast, but the peer never acknowledged it, so no "
                     "balance was credited for it.")

    return lines


def _counterparty_addresses(tx: Dict, address: str, outgoing: bool) -> List[str]:
    """Every address on the other side of this transaction, ours excluded.

    Change goes back to the sender, so an outgoing transaction lists our own address
    among its outputs; dropping it is what leaves the recipient.
    """
    boxes = tx.get('outputs', []) if outgoing else tx.get('inputs', [])
    seen: List[str] = []
    for box in boxes:
        other = box.get('address')
        if other and other != address and other not in seen:
            seen.append(other)
    return seen


def _deposit_tokens_in(tx: Dict) -> List[str]:
    """Deposit tokens carried in R4 of this transaction's outputs.

    Mirrors what `payment_process_validator` reads: the register holds the token as
    UTF-8 bytes, rendered by the explorer as hex. Anything that does not decode is
    some other application's register and is skipped.
    """
    tokens: List[str] = []
    for box in tx.get('outputs', []):
        registers = box.get('additionalRegisters') or {}
        rendered = (registers.get('R4') or {}).get('renderedValue')
        if not rendered:
            continue
        try:
            tokens.append(bytes.fromhex(rendered).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
    return tokens


def _format_timestamp(timestamp: int) -> str:
    """
    Convert Unix timestamp to human-readable format.
    
    Args:
        timestamp: Unix timestamp in milliseconds
        
    Returns:
        Formatted timestamp string
    """
    try:
        if timestamp == 0:
            return "N/A"
        
        # Convert from milliseconds to seconds
        timestamp_seconds = timestamp / 1000
        dt = datetime.fromtimestamp(timestamp_seconds)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
        
    except Exception:
        return "Invalid timestamp"


def _determine_transaction_direction(tx: Dict, address: str) -> Tuple[str, int]:
    """
    Determine if transaction is incoming or outgoing for the given address.
    
    Args:
        tx: Transaction dictionary
        address: Wallet address to check against
        
    Returns:
        Tuple of (direction_string, amount_in_nanoerg)
    """
    try:
        inputs = tx.get('inputs', [])
        outputs = tx.get('outputs', [])
        
        # Check if address is in inputs (outgoing transaction)
        outgoing_amount = 0
        for inp in inputs:
            if inp.get('address') == address:
                outgoing_amount += inp.get('value', 0)
        
        # Check if address is in outputs (incoming transaction)
        incoming_amount = 0
        for out in outputs:
            if out.get('address') == address:
                incoming_amount += out.get('value', 0)
        
        # Determine direction based on amounts
        if outgoing_amount > 0 and incoming_amount > 0:
            # Both incoming and outgoing (internal transaction)
            net_amount = incoming_amount - outgoing_amount
            if net_amount > 0:
                return "Incoming (Internal)", net_amount
            elif net_amount < 0:
                return "Outgoing (Internal)", abs(net_amount)
            else:
                return "Internal", 0
        elif outgoing_amount > 0:
            return "Outgoing", outgoing_amount
        elif incoming_amount > 0:
            return "Incoming", incoming_amount
        else:
            return "Unknown", 0
            
    except Exception as e:
        LOGGER(f"Error determining transaction direction: {str(e)}")
        return "Unknown", 0
