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
            # Display each transaction
            for i, tx in enumerate(transactions):
                _display_transaction(tx, address)
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


def _display_transaction(tx: Dict, address: str):
    """
    Display a single transaction in a formatted manner.
    
    Args:
        tx: Transaction dictionary from API response
        address: Wallet address to determine transaction direction
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
        
    except Exception as e:
        LOGGER(f"Error displaying transaction: {str(e)}")
        print(f"Error displaying transaction: {str(e)}")


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
