# Requirements Document

## Introduction

The tx_history command is a new CLI command for the Celaut node that displays the latest Ergo blockchain transactions for both the sending wallet and receiver wallet. This command will provide users with visibility into recent transaction activity, helping them monitor payment flows, deposits, and transfers between the node's wallets. The command will integrate with the existing Ergo payment system infrastructure and follow the established CLI patterns used throughout the application.

## Requirements

### Requirement 1

**User Story:** As a node operator, I want to view recent Ergo transactions for both my sending and receiving wallets, so that I can monitor payment activity and verify transaction history.

#### Acceptance Criteria

1. WHEN the user runs `python nodo.py tx_history` THEN the system SHALL display the latest transactions for both the sending wallet and receiver wallet
2. WHEN displaying transactions THEN the system SHALL show transaction ID, amount in ERG, timestamp, and transaction status for each transaction
3. WHEN no transactions are found THEN the system SHALL display an appropriate message indicating no recent transactions
4. WHEN the Ergo node is unreachable THEN the system SHALL display an error message and gracefully handle the connection failure

### Requirement 2

**User Story:** As a node operator, I want to see transactions organized by wallet type, so that I can easily distinguish between sending and receiving wallet activities.

#### Acceptance Criteria

1. WHEN displaying transaction history THEN the system SHALL clearly separate transactions by wallet type (Sending Wallet vs Receiver Wallet)
2. WHEN showing wallet information THEN the system SHALL display the wallet address for each wallet section
3. WHEN formatting the output THEN the system SHALL use consistent formatting that matches the existing CLI command patterns

### Requirement 3

**User Story:** As a node operator, I want to limit the number of transactions displayed, so that the output remains manageable and readable.

#### Acceptance Criteria

1. WHEN fetching transactions THEN the system SHALL limit results to the 10 most recent transactions per wallet by default
2. WHEN displaying transactions THEN the system SHALL show transactions in reverse chronological order (newest first)
3. IF there are more than 10 transactions available THEN the system SHALL only display the 10 most recent ones

### Requirement 4

**User Story:** As a node operator, I want the command to handle errors gracefully, so that I receive clear feedback when issues occur.

#### Acceptance Criteria

1. WHEN the Ergo explorer API is unavailable THEN the system SHALL display a clear error message and exit gracefully
2. WHEN wallet configuration is missing or invalid THEN the system SHALL display an appropriate error message
3. WHEN API responses are malformed THEN the system SHALL handle the error and display a user-friendly message
4. WHEN network timeouts occur THEN the system SHALL display a timeout error message

### Requirement 5

**User Story:** As a node operator, I want the transaction display to include relevant transaction details, so that I can understand the context of each transaction.

#### Acceptance Criteria

1. WHEN displaying each transaction THEN the system SHALL show the transaction ID (hash)
2. WHEN displaying each transaction THEN the system SHALL show the amount in ERG (converted from nanoERGs)
3. WHEN displaying each transaction THEN the system SHALL show the timestamp in a human-readable format
4. WHEN displaying each transaction THEN the system SHALL show the confirmation status
5. WHEN displaying each transaction THEN the system SHALL show the transaction direction (incoming/outgoing) where applicable