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

#### 2. Deposits (ERG, and any configured token)

- To increase their balance, the client generates a **deposit token** — a **local identifier (UUID)** created and stored in the client's SQLite database, **not** an on-chain EIP-4 asset — and creates a normal **Ergo transaction** transferring a certain amount of native ERG, embedding that identifier in register **R4** of the transaction.
- The client then notifies the node once the transaction carrying the deposit token has been transferred.

#### 3. Deposit Verification

- The node verifies that the **deposit token** (the R4 identifier) belongs to the client.
- If valid and the funds have been transferred to the node's **wallet**, the client's balance is increased according to the amount of ERGs received. The node's unit of account is pegged at 1 MU = 1 nanoERG, so the credit is exact (see [`PRICING.md`](PRICING.md)).
- The deposit token is then marked `payed` (the legal states are `pending` / `payed` / `rejected`).

#### 3b. Native tokens

An Ergo P2PK address holds ERG **and** every EIP-4 token sent to it. One address, one
script, one `contract_hash` — several currencies. So a node with tokens configured under
`ledgers.ergo.payments.ASSETS` does not gain a second payment contract: it gains a
**payment method** per asset on the contract it already had, each advertised with its own
`mu_per_unit`. A payment method is `(ledger, contract, asset)`, and peers pick between
them by price.

An asset is identified by its **64-hex token id**, never by a name. Anyone can mint a
token called `SigUSD`, so nodo never resolves a name, never asks an explorer for "the
token called X", and never accepts a box because a name matched. `DECIMALS` is configured
too, rather than read from the minter's EIP-4 registers: those are self-declared, and a
wrong one misprices the node by a power of ten.

**Being paid in a token needs no ERG at all.** The payer supplies both the fee and the
`SAFE_MIN_BOX_VALUE` nanoERG box the token travels in. Everything else does need ERG of
this node's own:

| Operation | Needs ERG in this wallet? |
| --- | --- |
| Accept a token payment | No — the payer pays the fee and the carrier box |
| Pay a peer in a token | Yes — the fee, the carrier box, and a change box |
| Sweep tokens to the cold wallet | Yes — the fee and the box they move in |
| Pay a token donation | Yes — the fee, plus a carrier box per donation wallet |

The node warns at startup when tokens are configured and the wallet cannot cover a fee
and a box, naming exactly those three things it will not be able to do. `nodo pay` names
whichever side is short rather than saying "insufficient balance" on a wallet visibly
holding the token.

`MU_PER_NANOERG` stays **required** for an operator who prices nothing in ERG: a token
method's fee floor is denominated in ERG while its minimum output is one base unit of the
token, so deposit sizing needs both rates on the same scale. The node warns when the two
imply an implausible ERG price for the token — their ratio *is* its opinion about that
price, and no market data is needed to see that one of them is out by a power of ten.

Proving an incoming token payment scans the **whole** asset list of each unspent box for
the configured id. Never position zero: nodo builds its own reputation proof boxes and so
may read `assets[0]` there, but a payment box is built by the payer, and its asset order
is the payer's choice. Anything else the box carries is **kept, never returned** — the
same rule an ERG overpayment already gets. Returning an asset would mean building and
paying for a transaction nobody asked for.

Sweeping and donating are per **contract**, not per asset: one Ergo output carries several
assets, so one tick moves everything over its limits in one transaction and pays
everything it owes in one more. `HOT_WALLET_LIMITS` and `COLD_WALLET_MIN_TRANSFER` are per
asset, in whole units of it; `COLD_WALLET` is shared, because an Ergo address receives
anything.

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
`COLD_WALLET` is empty, nothing is swept. Amounts, destination, and transaction id are
logged; mnemonics never are.

**Donations are not part of this.** They used to be a cut of the swept amount, which
meant a node with no cold wallet — the default — donated nothing whatever its percentage
said. A donation is now a share of *incoming payments*, accrued as it is earned and paid
on the same periodic tick, out of a weighted list of wallets, with no relation to
`COLD_WALLET` or `HOT_WALLET_LIMITS`. The debt is paid before the sweep runs, because the
debt is owed and the sweep is discretionary. See [`DONATIONS.md`](DONATIONS.md).

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
would fix every celaut node's identity as an Ergo key forever.

What connects the two is an **owner attestation**: this wallet signs the node's
`peer_id`, and the pair rides in the announced proof's own `xattrs`. A reader checks that
R7 is the attested owner and that the owner signed this `peer_id`, both from the proof
box alone. So a proof is attributed to a node without the node's name having to be an
on-chain object, and the node can change wallets without changing its name.

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

The implementation is pure Python (`src/reputation_system/ergo_schnorr.py`) — no JVM and no Ergo node,
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

###### Reading the reputation held on a node

The same registers, read the other way round. Every proof in the ecosystem lives on one
P2S contract, so "what does the network think of this node" is a filter over that one
contract: the boxes whose R4 is `CELAUT_NODE_TYPE_NFT_ID` and whose R5 is the node's
identity public key. `nodo reputation` does exactly that
(`src/reputation_system/contracts/ergo/opinions.py`), and
`reputation_system.interface.get_node_reputation` is the ledger-neutral way in.

The explorer applies that filter itself, through
`POST /api/v1/boxes/unspent/search`, so the usual case is one request rather than a page
per hundred boxes on a contract that grows with the whole ecosystem. Two details decide
whether it filters at all, and **both fail by returning nothing rather than by
erroring** — so a filter written either way wrong reads as "nobody has an opinion about
this node":

- `ergoTreeTemplateHash` is **required**. Omitting it (or sending `null`) is an HTTP 400.
  It is `sha256` of the ErgoTree *template* — the root expression with the segregated
  constants stripped — derived from the pinned tree by
  `utils.ergo_tree_template_hash`, and pinned in the test suite against the value
  checked on mainnet.
- register values are matched in their **rendered** form: the raw payload hex, with no
  `0e`/length prefix. The serialized form matches zero boxes.

A template hash names a contract's *code*, not its exact tree: constant segregation
makes it shared by every tree with the same code and different constants. So what comes
back is still filtered client-side on the canonical `ergoTree` — at the time of writing
the template matched 311 unspent boxes where the canonical address held 307. A box on a
look-alike tree is invisible to every other reader of the chain, and crediting it would
report reputation nobody else can see. Scanning the contract address
(`GET /api/v1/boxes/unspent/byAddress`) remains as a fallback for when the search cannot
be used at all, and both paths run through that same client-side filter.

What such a box is *worth* is not its token count. A reputation proof has a fixed
supply, and its owner decides how much of it to stake on each thing it has an opinion
about, so an opinion is worth the share of its publisher committed here, in `0..1`.

The share is of the supply the proof has **assigned to opinions** — everything it holds
except what sits in a box pointing at its own token id. That self-pointing box is how a
proof declares itself (`profileFetch`'s `is_self_defined` selects on it) and where it
parks what it has not assigned; it is a reserve, not a judgement about anything, and it
is never itself counted as an opinion.

That reserve is nearly the whole supply in practice. Measured on mainnet: every live
profile holds ~99,999,9xx of its 99,999,999 tokens in one self-pointing box and spends
**one token per opinion**.

| proof | boxes | reserved | assigned | one token, of assigned | of minted |
|---|---|---|---|---|---|
| `2e743564…` | 96 | 99,999,904 | 95 | 1.0526 % | 0.0000010 % |
| `758eb796…` | 79 | 99,999,922 | 78 | 1.2821 % | 0.0000010 % |
| `cd37aa0f…` | 39 | 99,999,961 | 38 | 2.6316 % | 0.0000010 % |

So dividing by `emissionAmount` makes every real opinion in the system 0.000001 % — the
same unreadable figure for all of them. This is where nodo parts from
`ReputationProof.compute` in `reputation-systems/reputation-system`, which uses the
minted supply: same numerator, a denominator that excludes the reserve. It is summed
from the proof's own boxes rather than as `emissionAmount - reserved`, so tokens parked
outside the contract cannot dilute the opinions either; on the live proofs the two agree
exactly (99,999,904 + 95 = 99,999,999).

Two further consequences:

- **Polarity is a register, not a sign.** R8 says whether the stake is for or against,
  so what is staked for and what is staked against are reported apart and netted only
  where one figure is asked for. A box that declares no polarity is counted neither way.
- **One proof counts once.** A proof holding several boxes about the same node is worth
  the sum of their signed shares, capped by construction at what it has assigned, so
  splitting a stake across ten boxes buys no extra weight.

But a share on its own cannot be read, because **minting a proof is free**. What is not
free is the ERG behind it, and the contract makes that one-way: `nativeErgIsPreserved`
requires `totalNativeOut >= totalNativeIn` across the proof's boxes on the *owner's own*
spending path, not only on the public top-up path, so ERG put into a proof can never be
taken back out (`sacrifice_assets` in the reference library is the deliberate act of
adding to it, and anyone may top a box up without a signature). That sunk cost is the
system's only Sybil resistance.

So every opinion also carries the total ERG burned into the proof that published it —
`total_burned` in the reference library, the sum of `value` over the proof's unspent
boxes — and `nodo reputation` reports **`share × burned`**: the portion of that
unrecoverable value standing behind this node. A proof that sacrificed 10 ERG and
commits half of itself puts 5 ERG behind you; one sitting at the min-box value each of
its boxes needs to exist has had *nothing* sacrificed into it, and 100 % of it is worth
0.001 ERG. Read the two figures together: the share says how much of a proof is
committed, the backing says what that commitment cost.

Multiplying the *share* is deliberate, and differs from the reference web app's profile
score (`Profile.svelte`), which multiplies the raw `token_amount` by `burned / 1e9`. A
token supply is chosen freely by whoever mints the proof, so weighing the raw count
rewards minting a larger supply — which costs nothing. The share is supply-independent,
which is what makes two proofs comparable.

The **subject's** own proof is reported separately and left out of the totals — a node
vouching for itself is not reputation. Which proofs those are comes from the subject: the
config for this node, the proofs the peer announced in its signed advertisement for a
peer. Reading a peer against our own proof id instead counted its self-vouch as the
network's verdict, which mattered because every node that has submitted holds such a box
(`submit_to_ledger` always publishes one, addressed to its own identity key) and a node
with no peers assigns its **whole** supply to it.

Being announced, that list is voluntary. A proof kept off the advertisement is
indistinguishable from a third party's here, however much was burned into it, and minting
a proof is free — so this separation is what makes the report honest, not what would make
the figure safe to route on (issue #353).

###### Why there is no "reputation earned this week"

`nodo reputation` reports a standing and what backs it, and **no per-window breakdown**.
That is a deliberate absence, not a gap: the chain cannot answer the question.

Dates come from the block each box was created in, and revising an opinion spends its
box and writes a new one, so the chain keeps no earlier date. On top of that,
`submit_to_ledger` re-splits the node's **whole** supply across its current peers on
every submission — proportionally to internal reputation, with one token kept for the
self-opinion — and `__create_reputation_proof_tx` gathers input boxes covering that whole
supply, so every submission spends every box and mints new ones. A single peer
accumulating `LEDGER_REPUTATION_SUBMISSION_THRESHOLD` reputation events (10, counted per
event, so a handful of interactions) triggers it, and once triggered every peer already
on the proof is re-included.

So a nodo proof re-dates all of its opinions at once. A window over those dates would
measure how often the publisher republishes and present it as reputation earned that
week — which, next to the real money flows the TUI's EARNINGS page draws above it, would
read like one. Each opinion still carries the age of its own box, which is all that date
honestly supports. Answering the question properly would mean walking a token's whole
box history in height order and reconstructing the share per target at each step; that
is not done.

That whole-supply split is also why a nodo proof holds **nothing in reserve**: it has no
self-pointing box at all (its self-opinion is addressed to its own identity key, like
any other opinion), so its assigned supply is its minted supply and the shares it
publishes are exactly its normalised internal reputation.

##### Payment System
The payment system implements these fields:
- `contract`/`script` xattr: the raw ErgoTree/propositionBytes of the box that receives each payment
- `ledger`: Identifies the ledger system as `"ergo"`
- `script`: the raw **ErgoTree / propositionBytes** of the wallet's P2PK payment box
- `token_id`: which asset this method settles in — the reserved symbol `"ERG"` for
  native-ERG payments, or a token's 64-hex id. This is the third dimension of a payment
  method, and it is why a node can advertise `N + 1` `ContractRate`s that share
  `ledger`, `contract_type` and `script` and differ only here and in `mu_per_unit`.
  Advertising a token needs no proto change: it is another row, not a nested field, and
  ERG's row stays byte-identical to what it always was.