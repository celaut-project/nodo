# Design Document

## Overview

The tx_history command will be implemented as a new CLI command that integrates with the existing Ergo payment system infrastructure. The command will fetch transaction history from the Ergo Explorer API for both the sending wallet (ERGO_WALLET_MNEMONIC) and receiver wallet (ERGO_AUXILIAR_MNEMONIC), format the data for display, and present it to the user in a clear, organized manner following the established CLI patterns used throughout the application.

The design leverages the existing Ergo interface components and follows the same architectural patterns as other commands in the system, ensuring consistency and maintainability.

## Architecture

The tx_history command will follow the established command architecture pattern:

```
nodo.py (CLI entry point)
    ↓
src/commands/tx_history.py (main command implementation)
    ↓
src/payment_system/contracts/ergo/interface.py (existing Ergo utilities)
    ↓
Ergo Explorer API (external service)
```

The command will reuse existing infrastructure components:
- Ergo wallet address generation functions
- ErgoAppKit initialization
- Environment variable management
- Error handling patterns
- CLI output formatting conventions

## Components and Interfaces

### 1. Command Entry Point
- **Location**: `src/commands/tx_history.py`
- **Function**: `tx_history()`
- **Purpose**: Main command implementation that orchestrates the transaction fetching and display

### 2. Transaction Fetcher
- **Function**: `get_address_transactions(address: str, limit: int = 10) -> List[dict]`
- **Purpose**: Fetch transaction history from Ergo Explorer API for a given address
- **API Endpoint**: `{explorer_api}/api/v1/addresses/{address}/transactions`
- **Parameters**: 
  - `offset=0` (start from most recent)
  - `limit=10` (default limit)

### 3. Transaction Formatter
- **Function**: `format_transaction_display(transactions: List[dict], wallet_type: str, address: str) -> None`
- **Purpose**: Format and display transaction data in a user-friendly format
- **Output Format**:
  ```
  [Wallet Type] - Address: {address}
  
  Transaction ID: {tx_id}
  Amount: {amount} ERG
  Timestamp: {formatted_timestamp}
  Confirmations: {confirmations}
  Direction: {incoming/outgoing}
  
  ---
  ```

### 4. Utility Functions
- **Function**: `nanoerg_to_erg(amount: int) -> float`
- **Purpose**: Convert nanoERG amounts to ERG for display (reuse existing function)
- **Function**: `format_timestamp(timestamp: int) -> str`
- **Purpose**: Convert Unix timestamp to human-readable format
- **Function**: `determine_transaction_direction(tx: dict, address: str) -> str`
- **Purpose**: Determine if transaction is incoming or outgoing for the given address

## Data Models

### Transaction Data Structure
Based on Ergo Explorer API response format:
```python
{
    "id": "transaction_hash",
    "timestamp": 1234567890000,  # Unix timestamp in milliseconds
    "confirmationsCount": 100,
    "inputs": [
        {
            "address": "sender_address",
            "value": 1000000000  # nanoERGs
        }
    ],
    "outputs": [
        {
            "address": "receiver_address", 
            "value": 1000000000  # nanoERGs
        }
    ]
}
```

### Wallet Configuration
The command will access wallet addresses using existing functions:
- **Sending Wallet**: `__get_sender_addr(ERGO_WALLET_MNEMONIC())`
- **Receiver Wallet**: `__get_sender_addr(ERGO_AUXILIAR_MNEMONIC)`

## Error Handling

### API Connection Errors
- **HTTP 404**: Address not found or no transactions
- **HTTP 500**: Explorer API server error
- **Connection Timeout**: Network connectivity issues
- **Malformed Response**: Invalid JSON or unexpected data structure

### Configuration Errors
- **Missing Wallet Configuration**: Handle cases where wallet mnemonics are not configured
- **Invalid Wallet Address**: Handle cases where wallet address generation fails

### Error Response Strategy
- Display user-friendly error messages
- Log technical details for debugging
- Graceful degradation (show available data if one wallet fails)
- Exit with appropriate status codes

## Testing Strategy

### Unit Tests
1. **Transaction Fetching**
   - Test successful API responses
   - Test API error responses (404, 500, timeout)
   - Test response parsing and data extraction

2. **Data Formatting**
   - Test transaction display formatting
   - Test timestamp conversion
   - Test amount conversion (nanoERG to ERG)
   - Test transaction direction determination

3. **Error Handling**
   - Test wallet configuration errors
   - Test API connectivity errors
   - Test malformed response handling

### Integration Tests
1. **End-to-End Command Execution**
   - Test complete command flow with mock API responses
   - Test command with real wallet addresses (if available in test environment)
   - Test command output formatting and structure

### Manual Testing
1. **Real Environment Testing**
   - Test with actual configured wallets
   - Verify transaction data accuracy against Ergo Explorer web interface
   - Test error scenarios (network disconnection, invalid configuration)

## Implementation Details

### API Integration
The command will use the existing `__init_ergo()` function to get the Explorer API URL and make HTTP requests using the `requests` library, following the same pattern as existing functions in the Ergo interface.

### CLI Integration
The command will be integrated into `nodo.py` following the existing pattern:
```python
case 'tx_history':
    from src.commands.tx_history import tx_history
    tx_history()
```

### Output Formatting
The command will follow the established CLI formatting patterns used in other commands like `clients`, `instances`, and `peers`, using consistent spacing, separators, and color coding where appropriate.

### Performance Considerations
- Limit API requests to 10 transactions per wallet by default
- Implement request timeouts to prevent hanging
- Cache wallet addresses to avoid repeated calculations
- Use concurrent requests for both wallets to improve response time