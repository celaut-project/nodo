# Usage of Ergo Platform

### Why Ergo was Selected as the Path Forward for Celaut

Ergo was selected because its principles align with those of Celaut, as reflected in the Ergo Manifesto (available in [Ergo Manifesto](https://ergoplatform.org/en/blog/2021-04-26-the-ergo-manifesto/)).
Furthermore, its advanced technology and a community dedicated to these ideals reinforce its suitability.

It has been observed that no other network genuinely upholds these principles, as many tend to corporatize the products built on them, centralizing control in one way or another *(according to the developers of celaut-project/nodo).*

For this reason, Ergo is considered the path forward.

**P.D.**  Ergo was chosen as the initial network to implement the necessary contracts for Celaut, although it is not necessarily the only ledger to be used. Celaut allows the simultaneous use of multiple ledgers, providing flexibility in its implementation across different networks.


### Reputation system implementation

The reputation system in the Nodo allows nodes to share their opinion about other nodes in the network. This system leverages the **Ergo** blockchain to manage **reputation proofs**. Here's how it works:

- Each node has a **reputation proof**, represented by a **token in Ergo**.
- The boxes containing this token record the node's opinions about other reputation proofs in the network.
- In this way, each node assigns a different reputation to its peers, enabling a decentralized and transparent evaluation system.

### Payment System implementation

The payment system between nodes is also implemented on **Ergo**. Here's how it is structured:

#### 1. Client Registration and Authentication

- Each node shares its **wallet** payment address with its clients.
- Clients (other nodes or external entities) register with the node and receive a **private key** to authenticate themselves.

#### 2. ERG Deposits

- To increase their balance, the client generates a **deposit token** — a **local identifier (UUID)** created and stored in the client's SQLite database, **not** an on-chain EIP-4 asset — and creates a normal **Ergo transaction** transferring a certain amount of native ERG, embedding that identifier in register **R4** of the transaction.
- The client then notifies the node once the transaction carrying the deposit token has been transferred.

#### 3. Deposit Verification

- The node verifies that the **deposit token** (the R4 identifier) belongs to the client.
- If valid and the funds have been transferred to the node's **wallet**, the client's balance is increased according to the amount of ERGs received. The node's unit of account is pegged at 1 MU = 1 nanoERG, so the credit is exact (see [`PRICING.md`](PRICING.md)).
- The deposit token is then marked `payed` (the legal states are `pending` / `payed` / `rejected`).

#### 4. Wallet Management in the Nodo

The node controls a **single wallet**, derived from `ledgers.ergo.WALLET_MNEMONIC`.
The flow is:

```
client  ->  single wallet  ->  cold wallet (when both thresholds are met)
```

- **Single wallet** (hot): the address clients pay directly to, and the wallet the
  node signs with (payments to other nodes, reputation proofs). There is no separate
  receiver/auxiliary wallet and no intermediate transfer between node wallets.
- **Cold wallet** (`ledgers.ergo.payments.COLD_WALLET`): a **public address only** —
  never a mnemonic inside Nodo. Excess funds are swept here for safekeeping.

#### 5. Cold-wallet sweep

A maintenance thread periodically computes, in integer nanoERG:

```
excess = balance - HOT_WALLET_LIMITS - fee
```

and sweeps `excess` to the cold wallet only when it is at least
`COLD_WALLET_MIN_TRANSFER` **and** a valid Ergo output. The hot-wallet limit, the
transaction fee, and the technical minimum box value are always retained. When
`COLD_WALLET` is empty, nothing is swept. A configurable percentage
(`DONATION_PERCENTAGE`, default 0%) of the swept amount may go to a donation wallet.
Amounts, destination, and transaction id are logged; mnemonics never are.

`HOT_WALLET_LIMITS` and `COLD_WALLET_MIN_TRANSFER` are decimal ERG strings, parsed once
with `Decimal` into nanoERG; all subsequent arithmetic is integer nanoERG.

#### Difference Between Wallet and Address

- **Wallet**: when the node has the **mnemonic**, it can sign transactions.
- **Address**: a **public address** (e.g. the cold wallet) the node can only send to.

The node operator can manually provide the mnemonic for the single wallet if the node
has been reinstalled. This same wallet is used to add reputation proofs to the network.

### Node identity signatures

A node's identity keypair is derived from `ledgers.ergo.WALLET_MNEMONIC` on Ergo's
derivation path (`m/44'/429'/0'/0/0`), and its 33-byte SEC-compressed public key is both
the node's `peer_id` and the owner recorded in a reputation proof's R7
(`0008cd` + public key). See `src/reputation_system/node_identity.py`.

What that key signs — the `GetPeerInfo` response (`Peer.signature`) and the
ownership challenge for a reputation proof — is signed with the **Schnorr scheme over
secp256k1 that ChainCash/Basis use off-chain**: the same `proveDlog` sigma protocol
Ergo's P2PK proofs are built on, in the encoding a reserve contract verifies explicitly.
It is not an on-chain P2PK spending proof — sigmastate truncates the challenge to 192
bits and serialises 56 bytes with `a` recomputed rather than sent:

```
signature = a || z            (65 bytes: 33-byte compressed point, 32-byte scalar)
a = compress(k*G)             k random per signature
e = blake2b256(a || message || public_key)      read as a SIGNED big-endian integer
z = (k + e*s) mod n
```

Verification is the group identity `z*G == A + e*P`. Two encoding rules are not optional,
because they are what the on-chain verifier does — ErgoScript's `byteArrayToBigInt` reads a
32-byte value as two's-complement:

* `e` is interpreted **signed**, matching the reserve contract's
  `g.exp(z) == a.multiply(pk.exp(e))`.
* When signing, the nonce is redrawn until the top byte of both `e` and `z` is `< 0x80`, so
  neither is read as negative and the signature verifies under the unsigned convention too.

The implementation is pure Python (`src/utils/ergo_schnorr.py`) — no JVM and no Ergo node,
so a node can sign from first boot. It is checked against the Scala reference
implementation's cross-validation vectors in `tests/test_ergo_schnorr.py`.

**An identity is mandatory.** A `Peer` that carries no public key, or whose signature does
not verify against it, is refused: `add_peer_instance` returns nothing and stores nothing,
and `accept_peer_refresh` rejects a `GetPeerInfo` response not signed by the peer whose
address it was fetched from. Every peer id in the database is therefore a public key.

### Sharing Information Between Celaut Nodes

When a node shares information with another, it provides two key elements:

1. The **ID of its reputation proof**.
2. The payment **contract**, whose `script` is the raw **ErgoTree / propositionBytes** of
   the wallet's P2PK payment boxes. A readable address is derived only locally, for UI or
   logs; it is never the value exchanged between nodes.


#### Contract Definition

```protobuf
message Contract {
    message ScriptTemplate {
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3;
    }
    message Ledger {
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3;
    }
    ScriptTemplate template = 1;
    bytes script = 2;  // Or Contract address on Ethereum-like networks.
    string token_id = 3;
    Ledger ledger = 4;
}
```

##### Reputation System
The reputation system utilizes the following fields:
- `contract`: Contains the sigma script of the box that holds each proof
- `ledger`: Specifies the ledger system in use, which is set to `"ergo"`
- `token_id`: Maps to the reputation proof ID, corresponding to the `token_id` in Ergo

##### Payment System
The payment system implements these fields:
- `contract`/`script` xattr: the raw ErgoTree/propositionBytes of the box that receives each payment
- `ledger`: Identifies the ledger system as `"ergo"`
- `script`: the raw **ErgoTree / propositionBytes** of the wallet's P2PK payment box
- `token_id`: `"ERG"` for native-ERG payments