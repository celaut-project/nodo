# Bitcoin

`nodo` can be paid in BTC. This mirrors [`ERGO.md`](ERGO.md), and the first thing to
read is not how to configure it but what it is *for*.

## On-chain Bitcoin is for coarse, infrequent deposits

A transaction here costs real money, and that changes what the feature is. With an
illustrative market — **example arithmetic, not a quoted price** — of ERG at $0.50 and
BTC at $100,000, and a 184 vB transaction at 5 sat/vB:

| | Ergo | Bitcoin |
|---|---|---|
| transaction fee | 0.001 ERG ≈ $0.0005 | ~920 sat ≈ $0.92 |
| minimum output | 0.001 ERG ≈ $0.0005 | 294 sat ≈ $0.29 |
| smallest settleable deposit | ≈ $0.001 | ≈ **$1.21** |
| full deposit at 2 % fee overhead | 0.05 ERG ≈ $0.025 | 46,000 sat ≈ **$46** |

Two conclusions follow, and both are shipped as defaults rather than left to be
discovered:

- **This is node-to-node, not client-topping-up.** The prepaid-deposit model already
  fits — one large deposit spent down by many maintenance ticks — but
  `MAX_FEE_OVERHEAD` has to be far looser than Ergo's or the node demands years of
  runtime up front. Bitcoin's block ships at `0.25`, not `0.02`.
- **The free tier is unaffected.** A new client's credit is granted locally and settled
  by nobody.

Lightning is what would make BTC usable at this node's ordinary charge sizes. It is a
separate payment contract with its own rate, and it slots into the same registry — the
work in issue #340 is what makes it a small change instead of a rewrite. It is not
implemented.

## What you need, and what for

Two different asks, and which one applies depends on whether you want to be **paid** in
BTC or to **pay** in it. `ledgers.bitcoin.BACKEND` chooses.

### `esplora` — to be paid. Nothing to run, no key anywhere.

A public HTTP API: blockstream.info, mempool.space, or one you host. `nodo` reads the
chain through it and holds no Bitcoin key at all, so it can be paid in BTC and **cannot
pay in it**. That is not a half-working state: the payer walks the payment systems it
shares with a peer and settles through the first one it can fund, so a node with a
read-only Bitcoin backend simply pays in something else. Nothing is broadcast and
nothing fails halfway through a payment.

You must set `payments.RECEIVING_ADDRESS` yourself — a read-only API cannot be asked
for an address, and one invented later would strand payments aimed at the one peers were
already told. The contract is not offered until it is set.

This is the shipped default, because being paid is the side that matters to a node that
is earning.

### `core` — to pay. A bitcoind you trust with your wallet.

Bitcoin Core over JSON-RPC. Core signs, broadcasts, counts confirmations and keeps the
wallet, so `nodo` still holds no key — but the node has to be one you would hand your
wallet to. `RPC_URL` is a URL, so it may be **remote**: your own machine over a LAN or a
VPN. Not somebody else's public node.

Back up **bitcoind's wallet**, not `config.yaml`. `nodo` neither generates nor stores a
seed for this chain, and `WALLET_KEYS_EXTERNAL: true` is what tells it not to.

### How this compares to Ergo

Ergo's posture is neither of these: `ledgers.ergo.NODE_URL` defaults to somebody else's
public node and the wallet mnemonic lives in `config.yaml`, so the node runs no Ergo
infrastructure *and* can both send and receive. The equivalent for Bitcoin would mean a
seed in `config.yaml` plus raw segwit construction, BIP-143 sighashes and UTXO selection
— every line of it money-moving, and none of it needed to be paid. It is not
implemented; `esplora` is what gets the receiving side to the same "nothing to run".

Neither backend needs a JVM, so a node that will not run one can be paid in BTC even
though it cannot be paid in ERG.

## Configuration

```yaml
ledgers:
  bitcoin:
    tags: [ bitcoin ]
    NETWORK: mainnet                 # mainnet | testnet | signet | regtest
    BACKEND: esplora                 # esplora (be paid) | core (also pay)
    ESPLORA_URL: "https://blockstream.info/api"
    RPC_URL: "http://127.0.0.1:8332"        # BACKEND: core only
    RPC_COOKIE_PATH: "~/.bitcoin/.cookie"   # or RPC_USER / RPC_PASSWORD
    WALLET_NAME: "nodo"
    WALLET_KEYS_EXTERNAL: true       # the keys are Core's, not this file's
    payments:
      MU_PER_SATOSHI: ""             # you must set this -- see below
      MIN_CONFIRMATIONS: 1
      TARGET_CONF: 6
      MAX_FEE_RATE_SAT_VB: 100
      HOT_WALLET_LIMITS: "0.05"
      COLD_WALLET: ""
      COLD_WALLET_MIN_TRANSFER: "0.01"
      MAX_FEE_OVERHEAD: 0.25
      RECEIVING_ADDRESS: ""          # filled in by the node
```

The cookie is preferred and is what Core writes on every start, so the ordinary setup
keeps no credential in `config.yaml` at all. `RPC_USER` / `RPC_PASSWORD` are the
fallback. The RPC password is never written to a log, a URL or an error message.

### `MU_PER_SATOSHI` has no default, on purpose

This is the one setting you cannot skip, and leaving it empty is a working state: **the
node simply does not offer Bitcoin.**

MU is `nodo`'s unit of account and has no intrinsic value; each payment contract says
what one MU is worth in its own money. A satoshi and a nanoERG are about **six orders of
magnitude apart**, so copying Ergo's `MU_PER_NANOERG: 1` by analogy would sell an hour
of compute for roughly a millionth of its price. That is the exact failure
[`PRICING.md`](PRICING.md) exists to prevent, and the one the old gas model shipped
with — so there is no borrowed default to fall into.

Work it out against your own market:

```
MU_PER_SATOSHI = MU_PER_NANOERG × (value of one nanoERG / value of one satoshi)
```

At $0.50/ERG and $100,000/BTC with `MU_PER_NANOERG: 1`, that is about `2000000`. The
node warns at startup if you set it to `1`, because 1 is `MU_PER_NANOERG`'s value and
copying it is the specific mistake worth naming.

Until it is set: the contract is not registered, nothing is advertised to peers, `btc`
is not offered as a display unit, and `nodo donations` shows no Bitcoin block. Nothing
half-works.

## How a payment is tied to its deposit

Ergo writes the deposit token into register `R4` of the box it pays. Bitcoin has no
register, so `nodo` uses the literal translation: **one static receiving address, plus an
`OP_RETURN` output carrying the token.**

- The advertised `script` xattr is one fixed `scriptPubKey` — the bytes, never a
  human-readable address, exactly as Ergo advertises propositionBytes.
- The receiving address is asked of Core once and written back to
  `payments.RECEIVING_ADDRESS`, so what peers are told stays the same across restarts.
- It costs ~43 extra vB and reuses one address. That is the same privacy posture Ergo
  already has here.

The alternative — an xpub advertised, one derived address per deposit — is better for
privacy and cheaper, but it publishes that account's whole receiving history to every
peer that reads `GetPeerInfo`. Not in this version.

### Validation, and what "at least" means

A payment is accepted when a transaction with at least `MIN_CONFIRMATIONS`
confirmations carries the deposit token in an `OP_RETURN` and pays **at least** the
expected amount to this node's script.

At least, not exactly: the payer converts our MU figure from its own scale and has to
round down to a whole MU of ours, so a correct payment routinely carries a little more
than the credit it asks for. Demanding equality would reject payments with the money
already on-chain. Anything extra is simply kept — the same rule Ergo applies to an
overpayment.

**No transaction index is needed, and none of these reads asks for one.** Every
transaction the validator looks at pays an address of this node's own wallet, so it is
read with `gettransaction` rather than `getrawtransaction` — which Core answers from the
mempool and otherwise only under `-txindex=1` or when given the block. Since the payer
waits for confirmations before saying anything, the transaction has always left the
mempool by the time it is validated, and the wrong RPC would answer "not found" for
precisely the payments that did arrive.

The two reads behind that answer are deliberately unwilling to conclude "no payment":
address history is walked to the end rather than one page deep, and a transaction that
carries the token but pays too little does not settle the question either, because the
same token can appear in more than one confirmed transaction. Whatever cannot be read
leaves the verdict open rather than closing it — the money is already in this node's
wallet, so answering "nothing arrived" would keep somebody else's BTC and credit them
nothing, and nothing revisits that answer later.

## Confirmations, and the two Ergo-shaped constants around them

The payer waits, exactly as it does on Ergo: `process_payment` polls until
`MIN_CONFIRMATIONS` and only then tells the peer. The receiver therefore validates
something already final and answers accepted-or-rejected in one call, with no
intermediate deposit-token state. That also disposes of RBF for free — a confirmed
transaction cannot be replaced — leaving only a shallow reorg, which is what
`MIN_CONFIRMATIONS` is the knob for. A transaction Core reports with *negative*
confirmations has been replaced or reorged out; that is not "not yet", and `nodo` stops
rather than polling until the deposit token expires.

What is genuinely different is only how *long* the wait is, and that broke two constants
sized for Ergo:

- **The deposit-token deadline.** One hour is generous on Ergo and short enough on
  Bitcoin to expire an honest payer's token before their transaction confirms. This
  contract declares six hours, and the node takes the **maximum** across the payment
  systems it offers. A token is issued before the payer has chosen a system — the row
  has no contract on it — so there is one deadline, and it has to be long enough for the
  slowest chain someone might pay on. Too short refuses money that is already on-chain;
  too long only delays this node's own sweep.
- **The sweep pause.** Ergo's validator needs the paid box still *unspent*, so no sweep
  may run while a deposit is in flight. Bitcoin proves payment from a confirmed
  transaction, so it needs no pause — and must not be given one, since its confirmations
  routinely take longer than that wait is bounded by. Contracts declare this
  (`needs_unspent_proof`), and the ones that do not need it run their tick regardless.

## Fees

Bitcoin's fee is a market price, not a constant, so this contract's floors move:
`settlement_floors_mu()` reports `estimatesmartfee(TARGET_CONF) × 184 vB` and the
network's dust threshold, read per call and never cached.

`MAX_FEE_RATE_SAT_VB` is a ceiling, and above it a payment is **refused rather than
clamped**. A transaction built below the market rate does not fail — it sits unconfirmed
until its deposit token expires, which is worse than not sending it. Raise the ceiling
or wait for fees to fall.

Because the floors move, reading them can fail — an unreachable node, or a rate above
that ceiling — and sizing a deposit is on the manager's path. A failure there costs that
peer its top-up for that tick and says so in the log; it never propagates, because the
manager thread also bills instances, sweeps and pays donations, and it has no supervisor
to restart it.

## Paying a peer

```
nodo pay <peer_id> <amount> --ledger bitcoin
```

The amount is in BTC, whatever `ui.DISPLAY_UNIT` says: what moves is an on-chain
transfer, and the ledger denominates it. `--ledger` is only required when this node
offers more than one payment system — and then it is required, because two systems are
two currencies and guessing would move money on a chain nobody named.

**A named ledger is where the payment settles, or it does not happen.** The amount was
read in that ledger's unit and checked against that ledger's floors and wallet, so
carrying it to another chain would move a figure typed in one currency over a different
one. A peer that does not share the named system is a clean refusal naming what it does
offer, never a quiet fall back to the other chain.

Where **no** ledger is named — the automatic refill, `nodo increase_peer_deposit` — the
choice is decided by **funding**: `nodo` walks the systems it shares with the peer and
uses the first one whose wallet can cover the amount. So a node holding ERG and no BTC
tops up a peer that accepts both in ERG, with no setting to that effect.

## Cold storage

Same shape as Ergo's, in integer satoshi:

```
excess = balance - HOT_WALLET_LIMITS - fee
```

swept only when it is at least `COLD_WALLET_MIN_TRANSFER` **and** above the dust
threshold. `COLD_WALLET` must be a valid address **for the configured network** — an
address valid on another one is refused, because sweeping savings to it would send funds
nobody on this chain can spend. The check is bech32/base58check arithmetic and needs no
node, so a node that cannot reach `bitcoind` still refuses a typo.

## Donations

**Half the circuit, and the half that costs money.** Bitcoin *pays* donations exactly as
[`DONATIONS.md`](DONATIONS.md) describes: it declares its own percentage, its own minimum
payout and its own wallet lists, accrues its debt in satoshi, and pays it on this
contract's tick against this contract's floors. A debt in BTC is not a debt in ERG and is
never paid out of it. Each funded wallet's share is a share of everything this node has
ever earned in BTC, and what has reached it is on `nodo donations`.

**It does not yet *count* them.** The other half of that circuit reads a chain for
donations other peers paid, and that needs a Bitcoin scanner --
`contracts/bitcoin/donation_scan.py`, answering the five calls the indexer makes, which
`contracts/ergo/donation_scan.py` answers for Ergo. There is none, so
`envs.donation_scanners()` has no Bitcoin entry, no peer's BTC donation is ever indexed,
and `DONATION_CREDIT_WALLETS` on this ledger credits nobody.

That asymmetry is worth knowing before setting a Bitcoin percentage above zero: what
makes donating worth anything is that *other* nodes weigh it when they route work. They
weigh what they can read, and a node whose peers only scan Ergo reads nothing on
Bitcoin. Donating in BTC today is a transfer, not a position in anybody's routing.

## What is not here

- **Reputation stays on Ergo.** A node's identity key is not an Ergo wallet key either;
  proofs are Ergo boxes. A node can accept BTC and publish reputation on Ergo, or accept
  BTC and publish none.
- **Counting donations paid on Bitcoin**, above: paying works, indexing what others
  paid needs a scanner that is not written.
- **Lightning**. It is a separate payment contract with its own rate and it slots into
  the same registry.
- **Local signing.** Paying out needs a key, and this node does not hold a Bitcoin one:
  the `core` backend delegates that to bitcoind. A seed in `config.yaml`, the way Ergo
  does it, would need raw transaction construction and is not implemented.
- **Per-deposit derived addresses**, above.

## See also

- [`ERGO.md`](ERGO.md) — the other payment system, and the single-wallet model.
- [`PRICING.md`](PRICING.md) — MU, what one is worth, and how a quote is built.
- [`CONFIG.md`](CONFIG.md) — every key.
- [`DONATIONS.md`](DONATIONS.md) — the donation circuit this contract ships.
