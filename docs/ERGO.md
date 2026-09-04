# Usage of Ergo Platform

### Why Ergo was Selected as the Path Forward for Celaut

Ergo was selected because its principles align with those of Celaut, as reflected in the Ergo Manifesto (available in [Ergo Manifesto](https://ergoplatform.org/en/blog/2021-04-26-the-ergo-manifesto/)).
Furthermore, its advanced technology and a community dedicated to these ideals reinforce its suitability.

It has been observed that no other network genuinely upholds these principles, as many tend to corporatize the products built on them, centralizing control in one way or another *(according to the developers of celaut-project/nodo).*

For this reason, Ergo is considered the path forward.

**P.D.**  Ergo was chosen as the initial network to implement the necessary contracts for Celaut, although it is not necessarily the only ledger to be used. Celaut allows the simultaneous use of multiple ledgers, providing flexibility in its implementation across different networks.


### Reputation system implementation

The reputation system in the Nodo allows nodes to share their opinion about other nodes in the network. This system leverages the **Ergo** blockchain to manage **reputation proofs**. Here's how it works:

- A node publishes its opinions through a **reputation proof**, represented by a **token in Ergo**.
- Each box holding that token is **one opinion**: whom it is about (register **R5**), how much of the token backs it (its weight), and what kind of thing the target is (register **R4**). The boxes are the opinions; the token is what carries their weight.
- An opinion is **about a node**, so R5 holds that node's **identity public key** — the same key that is its `peer_id` and the R7 owner of its own proofs (see [Node identity signatures](#node-identity-signatures)). One box per peer rated, plus one addressed to the publishing node's own key.
- In this way, each node assigns a different reputation to its peers, enabling a decentralized and transparent evaluation system.

**A proof is the author's opinions, not a score awarded to its holder.** This is the
part that reads backwards at first: when a peer announces a proof to us
(`Peer.reputation_proofs`), it is showing us *what it thinks of others*, not a
credential we granted it or a rating we should read off it. That also means one
identity key may hold **several** proofs at once, so nothing on our side records
"the" proof of a peer — the proofs a peer announced live in the signed
advertisement we store verbatim.

R5 used to hold the target's *reputation proof token id* instead of its public key.
That made every opinion an opinion about one of the target's proofs rather than about
the target: reputation did not survive the target minting a new proof, and a node
could shed its accumulated on-chain standing by doing exactly that (issue #281).

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

### The wallet, the identity, and the attestation between them

The wallet derived from `ledgers.ergo.WALLET_MNEMONIC` on Ergo's derivation path
(`m/44'/429'/0'/0/0`) is what this node is paid into, what publishes its reputation
proofs, and the owner recorded in a proof's R7 (`0008cd` + its 33-byte SEC-compressed
public key).

It is **not** the node's `peer_id`. That is an Ed25519 key of its own, from
`identity.MNEMONIC`, on no ledger at all (see
[Node identity](CONCEPTS.md#node-identity)). R7 is the reputation contract's spending
clause, so it can only ever hold an Ergo proposition — reading it as the peer's id
would have fixed every celaut node's identity as an Ergo key forever.

What connects the two is an **attestation**: this wallet signs the node's `peer_id`
once, and the pair is announced in `Peer.ledger_attestations`. A reader checks that R7
is the attested wallet and that the wallet signed this `peer_id`, both from the proof
box alone. So a proof is still attributed to a node, and the node can change wallets
without changing its name.

What this wallet signs — the attestation, and the ownership challenge for a reputation
proof — uses the **Schnorr scheme over secp256k1 that ChainCash/Basis use off-chain**:
the same `proveDlog` sigma protocol
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
so a node can attest from first boot. It is checked against the Scala reference
implementation's cross-validation vectors in `tests/test_ergo_schnorr.py`.

The `GetPeerInfo` response (`Peer.signature`) is a different signature in a different
scheme: Ed25519 by the identity key, which is what `Peer.signature_scheme` declares.
None of the above applies to it.

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

A `Peer` carries these as `repeated Contract reputation_proofs` — repeated because the
peer may hold several proofs, and each entry is one of the peer's own published
opinion sets rather than a rating of the peer (see [Reputation system
implementation](#reputation-system-implementation)). `nodo peers` lists every proof id
a peer announced; `nodo verify_reputation <peer_id>` checks, for each of them, that
the peer actually controls it on-chain.

The registers of an opinion box itself:
- **R4** — `typeNftTokenId`: what kind of object R5 names. A node is `CELAUT_NODE_TYPE_NFT_ID`.
- **R5** — `uniqueObjectData`: the target of the opinion. For a node, its **identity public key**.
- **R6** — `isLocked`
- **R7** — the owner's `propositionBytes` (`0008cd` + the *author's* public key)
- **R8** — `customFlag`, carrying the sign of the amount
- **R9** — free-form content; for a self-opinion, the node's signed `Peer` message

##### Payment System
The payment system implements these fields:
- `contract`/`script` xattr: the raw ErgoTree/propositionBytes of the box that receives each payment
- `ledger`: Identifies the ledger system as `"ergo"`
- `script`: the raw **ErgoTree / propositionBytes** of the wallet's P2PK payment box
- `token_id`: `"ERG"` for native-ERG payments