# Implementation Plan: Single Ergo Wallet, Raw ErgoTrees, and Reputation Validation

## Confirmed decisions

- No migration or backward compatibility will be provided. There is no
  production deployment yet: configurations containing `AUXILIARY_MNEMONIC`,
  `AUXILIAR_MNEMONIC`, or `PAYMENTS_RECEIVER_WALLET` will no longer be valid and
  must be updated manually.
- Ergo will use a single wallet controlled by the node:
  `ledgers.ergo.WALLET_MNEMONIC`.
- Clients will pay directly to the address derived from that mnemonic. There
  will be no *Receiver Wallet* or intermediate wallet transfer.
- `Contract.script` will always contain the raw **ErgoTree/proposition bytes**
  of the box or contract it represents; never ErgoScript source code or an Ergo
  address encoded as text.
  - For payments, it will contain the raw `propositionBytes` of the wallet's
    P2PK payment boxes.
  - For reputation, it will contain the compiled ErgoTree of the reputation
    contract; owner identity will remain in `R7` as raw `propositionBytes`.
- A readable Ergo address may be derived only for local UI, logs, or APIs that
  require it. It will not be exchanged between nodes as the contract
  representation.
- `payments.PAYMENTS_RECEIVER_WALLET` is removed. Its replacement is
  `ledgers.ergo.payments.COLD_WALLET`, a public address that does not require or
  accept a mnemonic inside Nodo.

## Target configuration

Move all Ergo-specific configuration under `ledgers.ergo`:

```yaml
ledgers:
  ergo:
    tags: [ergo]
    NODE_URL: "https://node.sigmaspace.io"
    WALLET_MNEMONIC: ""
    GAS_PER_ERG: 1.0e+58
    HTTP_PEERS_PATH: "${main.STORAGE}/ergo_http_peers.json"
    reputation:
      LEDGER_REPUTATION_SUBMISSION_THRESHOLD: 10
      TOTAL_REPUTATION_TOKEN_AMOUNT: 1000000000
      REPUTATION_PROOF_ID: ""
      PLAIN_TEXT_TYPE_NFT_ID: "..."
      CELAUT_NODE_TYPE_NFT_ID: "..."
    payments:
      PAYMENT_MANAGER_ITERATION_TIME: 86400
      HOT_WALLET_LIMITS: "100"
      COLD_WALLET: ""
      COLD_WALLET_MIN_TRANSFER: "1"
      DONATION_WALLET: "..."
      DONATION_PERCENTAGE: "0.00"
```

`HOT_WALLET_LIMITS` is the maximum balance retained in the operational wallet.
`COLD_WALLET_MIN_TRANSFER` is the minimum sweep amount. Both are expressed as
decimal strings in ERG and converted once to nanoERG using `Decimal`; all
subsequent monetary arithmetic uses integers. This prevents tiny transfers and
floating-point errors.

## Implementation phases

### 1. Restructure Ergo configuration

1. Move the root-level `reputation` and `payments` blocks from `config.yaml`
   and `config.example.yaml` to `ledgers.ergo.reputation` and
   `ledgers.ergo.payments`.
2. Update every configuration read/write to use the new paths, including
   reputation transactions, validation, commands, the daemon, and tests.
   Remove fallbacks to root-level keys.
3. Remove `AUXILIARY_MNEMONIC` and `AUXILIAR_MNEMONIC` from examples, active
   configuration, automatic generation, and code. Keep only automatic
   generation of `WALLET_MNEMONIC`, if that feature remains enabled.
4. Remove `PAYMENTS_RECEIVER_WALLET`, including the historical typo
   `PAYMENTS_RECIVER_WALLET`; create and use only
   `ledgers.ergo.payments.COLD_WALLET`.
5. Update `bash/reconfig.sh` to edit the nested structure and explicitly ask
   for `Cold Wallet`, `Hot wallet limit`, and `Cold-wallet minimum transfer`.
6. Add configuration validation: require the mnemonic when payments or
   reputation are enabled, validate `COLD_WALLET` as an optional Ergo address,
   and require positive limits/thresholds representable in nanoERG.

### 2. Unify payment flow around one wallet

1. In `src/payment_system/contracts/ergo/interface.py`, replace both wallet
   sources with one wallet derived from `WALLET_MNEMONIC`.
2. Change `init()` to advertise one payment contract whose script is the raw
   `propositionBytes` of the single wallet.
3. Change `payment_process_validator()` to verify that same ErgoTree instead of
   the former auxiliary wallet.
4. Adapt `process_payment()` and `Gateway.Payable()` to transport and consume
   the raw script, converting it to `Address`/`ErgoContract` only at the AppKit
   boundary that requires it. Remove conversions such as
   `script.decode('utf-8')` that assume a textual address.
5. Replace the two-element `get_balances()` result with a single-wallet balance
   query. Simplify `tx_history` to query one address.
6. Replace `manager()` with an excess sweep from the single wallet:
   - Do nothing when `COLD_WALLET` is empty.
   - Read the confirmed balance in nanoERG.
   - Calculate `excess = balance - HOT_WALLET_LIMITS - required_fee`.
   - Send only when the excess is at least `COLD_WALLET_MIN_TRANSFER` and is a
     valid Ergo output.
   - Always retain the hot limit, the transaction fee, and any technical
     minimum required to build the transaction.
   - Log amount, destination, and transaction id without logging mnemonics.
7. Define tests for balance exactly at the limit, excess below the minimum,
   missing cold wallet, insufficient fee, and transaction failure/confirmation.

### 3. Normalize raw ErgoTrees in contracts and node communication

1. Create explicit, tested utilities to:
   - extract P2PK `propositionBytes` from an address;
   - serialize the compiled ErgoTree of a P2S contract;
   - reconstruct the AppKit object required from those bytes when an API needs
     an address or contract;
   - compare bytes, not text or hashes, using one canonical representation.
2. Establish the `Contract` convention:
   - `script`/`script` xattr: raw ErgoTree bytes;
   - `token_id`: token id when applicable;
   - `address`: optional local value derived for indexing/UI, not the source of
     truth and not the value advertised to peers.
3. Review all contract creators, serializers, and consumers (`payment_system`,
   `gateway`, `manager`, database, and `inspect`) so they propagate the binary
   `script` xattr without encoding/decoding a textual address.
4. When generating a reputation proof, put the compiled ErgoTree of `CONTRACT`
   in the shared `Contract` script, not the `reputation_proof.es` source. Keep
   each box's `R7` as the owner's raw `propositionBytes`.
5. Update reputation compatibility checks to compare the expected binary
   ErgoTree with the one obtained from on-chain boxes.

### 4. Resolve TODOs in the uncommitted diff: reputation, manager, and gateway

#### 4.1 `proof_validation.py`

1. Replace the current `get_script(...) == CONTRACT.encode(...)` comparison
   with comparison against the compiled raw ErgoTree. Cover incompatible
   script, ledger, and token-id cases.
2. Implement `_validate_box_structure` instead of the current `return True`:
   validate required register presence and types, reputation token data, and
   that `box.ergoTree` matches the ErgoTree of `CONTRACT`.
3. Complete peer ownership proof: obtain the peer's raw `R7`, create a random
   challenge with expiry, call `SignPublicKey` over gRPC using `peer_id`, and
   cryptographically verify the signature against that ErgoTree. Reject RPC,
   format, expiry, or verification failures.
4. Change `sign_message` to receive and validate raw `proposition_bytes`. It
   must sign only when they exactly match the bytes derived from the local
   mnemonic. Fix the current identity comparison using `is` and reject a
   mismatch instead of signing.
5. Remove `mnemonic_phrase` as a public parameter of
   `validate_reputation_proof_ownership`; read the single mnemonic from the
   Ergo configuration and keep an explicit internal helper only if tests need
   one.
6. Replace the full paginated box scan with a query filtered by `R7`/
   proposition bytes in `iter_unspent_boxes_by_address` (or a specialized
   helper). Define a fallback only when the Ergo endpoint lacks filtering, with
   pagination, timeout, and logging limits.
7. Decide and document the equivalence policy for `ledger.tags`, `prose`, and
   `formal`; if `formal` is canonical, validate only that field and remove the
   ambiguity represented by the current TODO.

#### 4.2 `gateway.py` and the gRPC protocol

1. Make `Gateway.SignPublicKey` the counterpart of the ownership challenge,
   rather than a generic signature operation over an arbitrary textual
   "public key".
2. Change `SignRequest.public_key` from `string` to `bytes proposition_bytes`
   (or rename it to express that meaning) and define an unambiguous binary
   encoding for challenge, signature, and response. Regenerate protobuf stubs;
   do not edit generated files manually.
3. Validate input size and format, handle gRPC errors explicitly, and return
   controlled status information instead of generic exceptions that hide the
   rejection reason.
4. Change `Gateway.Payable` to use `get_script(payment.contract)` as the raw
   binary ErgoTree rather than `get_address(...).encode('utf-8')`.

#### 4.3 `manager.py`

1. Keep `add_reputation_proof` tied to the complete validation path: do not
   persist a proof until ErgoTree compatibility, box structure, and R7
   ownership challenge validation pass.
2. Keep the removed debug `print(peer)` removed.
3. Add tests ensuring that a peer with foreign R7, an invalid signature, or an
   incompatible tree never reaches `sc.add_reputation_proof`.

### 5. State, TUI, CLI, and documentation

1. Make `nodo info` print one line such as
   `Wallet: <address>, Amount: <balance> ERGs`; it may show the configured cold
   wallet separately without mixing it with operational balance.
2. Remove `receiver_address` and `receiver_balance` from the TUI, including
   parsing, rendering, and tests. Keep one wallet card.
3. Update `nodo tx_history`, completion, error messages, and comments to remove
   the "Sending Wallet"/"Receiver Wallet" pair and use simply "Wallet".
4. Rewrite `docs/ERGO.md`, `docs/KyA.md`, README, and examples to describe:
   `client -> single wallet -> cold wallet when both thresholds are met`.
5. Run a final search to ensure no references remain to
   `AUXILIARY_MNEMONIC`, `AUXILIAR_MNEMONIC`, `Receiver Wallet`,
   `PAYMENTS_RECEIVER_WALLET`, or `PAYMENTS_RECIVER_WALLET`.

### 6. Local data and published contracts

1. Do not implement configuration or data migration: a checkout using the new
   code must start with the new configuration.
2. During development, clean or recreate the local database used by tests
   before verifying contract advertisements. Do not add code that preserves or
   translates contracts belonging to the former receiver wallet.
3. Verify that a new node advertises exactly one payment contract with the
   correct ErgoTree and that peers/clients receive that binary value.

## Tests and acceptance criteria

1. Unit tests for ErgoTree conversion: P2PK -> bytes -> AppKit representation,
   compiled P2S contract -> bytes, and stable binary comparisons.
2. Configuration tests for the new nested paths and rejection of removed keys;
   do not add migration tests.
3. Simulated payment tests: the advertised contract and valid deposit use the
   same ErgoTree derived from `WALLET_MNEMONIC`.
4. Sweep tests using nanoERG amounts for all threshold cases, including the
   minimum transfer amount.
5. Integration tests with Ergo node/explorer mocks for reputation: box
   ErgoTree, `R7`, valid/invalid challenge signatures, and filtered lookup.
6. Parsing tests for `nodo info`, TUI, and `tx_history` with one wallet.
7. Run the relevant test suite, formatting/lint checks, and `git diff --check`.
   The final result must not add secrets or mnemonics to fixtures, logs, or
   documentation.

## Recommended execution order

1. Configuration and binary ErgoTree utilities.
2. Single-wallet payment flow, including cold-wallet sweeps.
3. Gateway/protobuf and reputation/R7 validation.
4. Manager, UI/CLI, documentation, and reference cleanup.
5. Full test suite and manual verification against an Ergo test node.

Implementation note: treat this document as the acceptance contract, inspect the latests committed changes in `proof_validation.py`, `manager.py`, and `gateway.py` before editing them, and preserve useful work while resolving every TODO described above. This is intentionally a breaking pre-production change: do not add migration paths, legacy aliases, textual-address fallbacks, or a second wallet. Keep raw ErgoTree/proposition bytes as the canonical value exchanged in contracts and peer validation; derive display addresses only at API/UI boundaries. Use integer nanoERG arithmetic after parsing configuration, never log mnemonics, regenerate protobuf code rather than editing generated stubs, and finish only after focused tests plus the relevant full suite pass with `git diff --check` clean.
