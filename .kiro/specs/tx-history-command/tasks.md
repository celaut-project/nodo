# Implementation Plan

- [ ] 1. Create core transaction history command module
  - Create `src/commands/tx_history.py` file with main command function
  - Import required dependencies (requests, datetime, existing Ergo utilities)
  - Define main `tx_history()` function that will be called from nodo.py
  - _Requirements: 1.1, 2.1_

- [ ] 2. Implement transaction fetching functionality
  - [ ] 2.1 Create API client function for fetching address transactions
    - Write `get_address_transactions(address: str, limit: int = 10) -> List[dict]` function
    - Implement HTTP request to Ergo Explorer API endpoint `/api/v1/addresses/{address}/transactions`
    - Add proper error handling for API failures (404, 500, timeout)
    - Parse JSON response and return transaction list
    - _Requirements: 1.1, 1.4, 4.1, 4.2, 4.3_

  - [ ] 2.2 Implement wallet address retrieval
    - Create helper function to get both wallet addresses using existing `__get_sender_addr()` function
    - Handle cases where wallet configuration is missing or invalid
    - Return tuple of (sending_address, receiver_address) with proper error handling
    - _Requirements: 2.2, 4.2_

- [ ] 3. Implement transaction data processing and formatting
  - [ ] 3.1 Create transaction direction determination logic
    - Write `determine_transaction_direction(tx: dict, address: str) -> str` function
    - Analyze transaction inputs and outputs to determine if incoming or outgoing
    - Return "Incoming", "Outgoing", or "Internal" based on address involvement
    - _Requirements: 5.5_

  - [ ] 3.2 Implement utility functions for data conversion
    - Create `format_timestamp(timestamp: int) -> str` function to convert Unix timestamp to readable format
    - Reuse existing `__nanoerg_to_erg()` function for amount conversion
    - Add function to format confirmation count display
    - _Requirements: 5.2, 5.3, 5.4_

- [ ] 4. Implement transaction display formatting
  - [ ] 4.1 Create transaction list formatter
    - Write `format_transaction_display(transactions: List[dict], wallet_type: str, address: str)` function
    - Format each transaction with ID, amount, timestamp, confirmations, and direction
    - Use consistent formatting that matches existing CLI command patterns
    - Handle empty transaction lists with appropriate messaging
    - _Requirements: 1.2, 1.3, 2.1, 2.2, 2.3, 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ] 4.2 Implement wallet section headers and organization
    - Create clear section headers for "Sending Wallet" and "Receiver Wallet"
    - Display wallet addresses in each section header
    - Add proper spacing and separators between sections
    - _Requirements: 2.1, 2.2, 2.3_

- [ ] 5. Implement comprehensive error handling
  - [ ] 5.1 Add API error handling
    - Handle HTTP errors (404, 500, timeout) with user-friendly messages
    - Implement graceful degradation when one wallet fails but other succeeds
    - Add logging for technical error details while showing clean user messages
    - _Requirements: 1.4, 4.1, 4.3, 4.4_

  - [ ] 5.2 Add configuration error handling
    - Handle missing or invalid wallet mnemonic configuration
    - Display clear error messages for wallet setup issues
    - Provide guidance on how to resolve configuration problems
    - _Requirements: 4.2_

- [ ] 6. Integrate command into main CLI interface
  - Add new case 'tx_history' to the match statement in nodo.py
  - Import and call the tx_history function from src.commands.tx_history
  - Update the help text in nodo.py to include the new tx_history command
  - _Requirements: 1.1_

- [ ] 7. Implement transaction limit and ordering
  - [ ] 7.1 Add transaction limiting logic
    - Implement default limit of 10 transactions per wallet
    - Ensure transactions are fetched in reverse chronological order (newest first)
    - Add parameter to API request for proper offset and limit handling
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ] 7.2 Handle pagination and result limiting
    - Ensure only the most recent transactions are displayed when more than 10 exist
    - Implement proper API parameter handling for transaction limiting
    - Add logic to handle cases where fewer than 10 transactions exist
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 8. Create comprehensive unit tests
  - [ ] 8.1 Write tests for transaction fetching functions
    - Create test cases for successful API responses with mock data
    - Test error handling for various API failure scenarios (404, 500, timeout)
    - Test response parsing and data extraction logic
    - _Requirements: 1.1, 1.4, 4.1, 4.3, 4.4_

  - [ ] 8.2 Write tests for data formatting functions
    - Test transaction display formatting with various transaction types
    - Test timestamp conversion and amount conversion functions
    - Test transaction direction determination logic
    - Test empty transaction list handling
    - _Requirements: 1.2, 1.3, 2.1, 2.3, 5.1, 5.2, 5.3, 5.4, 5.5_

- [ ] 9. Add integration testing and validation
  - [ ] 9.1 Create end-to-end command tests
    - Write integration tests that mock the complete command flow
    - Test command execution with various wallet configurations
    - Validate output formatting and structure matches requirements
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3_

  - [ ] 9.2 Add error scenario integration tests
    - Test command behavior with network connectivity issues
    - Test command behavior with invalid wallet configurations
    - Test graceful degradation when one wallet fails
    - _Requirements: 1.4, 4.1, 4.2, 4.3, 4.4_