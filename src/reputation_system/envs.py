from protos import celaut_pb2


LEDGER = "ergo" # or "ergo-testnet" for Ergo testnet.
CONTRACT = open("src/reputation_system/contracts/ergo/reputation_proof.es", "r").read()

PROSE = """
Ergo Blockchain is a decentralized digital ledger architecture based on an efficient Proof-of-Work (PoW) consensus protocol. It integrates advanced principles from cryptographic theory, formal logic, and computational economics to facilitate the execution of secure, auditable, and low-cost smart contracts. Built upon an implementation of the extended UTXO (eUTXO) model, Ergo provides a robust platform for the issuance, verification, and transfer of digital assets and contractual conditions, while preserving participant privacy through techniques such as Sigma Protocol cryptographic signatures and zero-knowledge proofs.

Additionally, Ergo emphasizes resistance to technological obsolescence through a self-amending design and implicit governance mechanisms based on miner consensus, ensuring its technical and economic viability without reliance on external dependencies or centralized actors.
"""

FORMAL = """
Let the sextuple

$$
\mathbf{E} = \bigl(\;\mathcal{G},\;V,\;\mu,\;\Delta,\;\mathcal{E},\;\Pi\;\bigr)
$$

denote the Ergo system, where:

---

## 1. $\mathcal{G}$ — Block Graph (Directed Acyclic Graph)

Each block $b \in \mathcal{G}$ is the 4‑tuple

```text
b = (h_{\text{prev}},\, \tau,\, \rho,\, m)
```

* **$h_{\text{prev}}$** (Parent Hash)

  * The SHA‑256 hash of the previous block’s header.
  * Secures immutability and enforces chaining.
* **$\tau$** (Timestamp)

  * UNIX time (seconds since 1970‑01‑01 UTC).
  * Used for block ordering and difficulty adjustment.
* **$\rho$** (Nonce)

  * An integer such that
    $\mathrm{Hash}(\text{header}) < D_t$.
  * Discovered by Proof‑of‑Work search.
* **$m$** (Merkle Root)

  * Root hash of the Merkle tree over all transactions in the block.
  * Enables compact inclusion proofs in $O(\log n)$.

> **Linearization:**
> Although defined abstractly as a DAG, Ergo’s PoW consensus selects the “heaviest” chain (highest cumulative difficulty), yielding a linear history without cycles.

---

## 2. $V$ — State‑Verification Function

$$
V: \mathcal{G} \times \Sigma^* \;\longrightarrow\; \{0,1\},
$$

where $\Sigma^*$ is a sequence of proposed transactions.

**Validation steps:**

1. **Syntax check**

   * Each transaction $\sigma$ conforms to the grammar:

     ```ebnf
     tx     = inputs, outputs, scripts, metadata? ;
     input  = prev_txid, index, script_witness ;
     output = value, script_pubkey ;
     ```
2. **eUTXO model enforcement**

   * Every referenced UTXO must exist and be unspent in the global state of $\mathcal{G}$.
   * No double‑spending within the same block sequence.
3. **Script execution**

   * For each input, evaluate `script_witness` against the corresponding `script_pubkey`:

     $$
       \mathrm{Eval}(\text{witness},\,\text{pubkey}) = \text{true}.
     $$
   * Implemented via Sigma protocols and zero‑knowledge proofs.
4. **Balance check**

   * $\sum$ (inputs) ≥ $\sum$ (outputs).
   * The surplus becomes miner fees.
5. **Chain consistency**

   * The containing block must meet PoW difficulty and correctly reference a valid parent.

> **Determinism:**
> Any full node replaying $V$ over the same inputs and chain will reach the identical resulting state.

---

## 3. $\mu$ — “Autolaborable” PoW Consensus

Ergo employs **Autolykos v2**, an ASIC‑resistant Proof‑of‑Work:

1. **Memory‑light design**

   * Avoids heavy RAM requirements (inspired by Cuckoo Cycle).
2. **Difficulty adjustment**

   $$
     D_{t+1} \;=\; D_t \times \exp\!\Bigl(\alpha\,\frac{T_{\mathrm{obs}} - T_{\mathrm{tgt}}}{T_{\mathrm{tgt}}}\Bigr),
   $$

   where

   * $T_{\mathrm{tgt}} = 120$ s (target block time),
   * $T_{\mathrm{obs}}$ = observed average block time,
   * $\alpha$ = smoothing factor.
3. **Block propagation**

   * Gossip‑style P2P broadcast within ≤ 1 s of validation.
   * Six confirmations yield practical finality.
4. **Economic incentives**

   * Miner reward = block subsidy $R_t$ + collected fees.
   * Subsidy decays over time toward fee‑only security.

---

## 4. $\Delta$ — Sigma Protocols & Zero‑Knowledge Primitives

$$
\Delta = \bigl\{\Sigma_{\mathrm{DL}},\,\Sigma_{\mathrm{OR}},\,\Sigma_{\mathrm{AND}},\,\dots\bigr\}
$$

| Protocol                | Purpose                                  | Typical Use Case                   |
| ----------------------- | ---------------------------------------- | ---------------------------------- |
| $\Sigma_{\mathrm{DL}}$  | Proof of discrete‑log knowledge          | Key ownership authentication       |
| $\Sigma_{\mathrm{OR}}$  | Logical OR composition of proofs         | Multi‑path spending conditions     |
| $\Sigma_{\mathrm{AND}}$ | Logical AND composition                  | Conditional contract clauses       |
| zk‑SNARK (experimental) | Succinct zero‑knowledge integrity proofs | Confidential transactions (future) |

> **Script architecture:**
>
> * Written in a lightweight functional DSL.
> * Compiled to minimal bytecode (non‑Turing complete), ensuring guaranteed termination and auditability.

---

## 5. $\mathcal{E}$ — Token Economics & Issuance

1. **Initial supply**

   $$
     E_0 = 97{,}200{,}000\;\mathrm{ERG}
   $$
2. **Unit granularity**

   $$
     1\;\mathrm{ERG} = 10^9\;\text{nanoERG}
   $$
3. **Block subsidy schedule**

   $$
     R_{t+1} = R_t - \tfrac{R_0}{N_{\mathrm{halvings}}},\quad R_0 = E_0/\,N_{\mathrm{blocks}}
   $$

   (a linear‑decrease model over a predetermined number of blocks).
4. **Fee model**

   * **Burn rate**: 20 % of fees are burned to reduce supply.
   * **Miner rate**: 80 % paid to the block producer.
5. **Anti‑spam measure**

   * Minimum fee per byte of transaction payload to prevent block spamming.

> **Long‑term security:**
> As block subsidy $R_t$ approaches zero, sustained fees ensure continued miner participation without runaway inflation.

---

## 6. $\Pi$ — Implicit, Self‑Amendable Governance

1. **Hard‑fork activation**

   * New protocol release is signaled by miners.
   * Activation requires ≥ 66.7 % of total hash power signaling readiness within a two‑week window.
2. **No off‑chain voting**

   * All governance decisions are encoded and observed on‑chain via mining power.
3. **Soft‑fork upgrades**

   * Certain non‑breaking changes can activate with ≥ 90 % miner signaling.
4. **Transparency**

   * All version signals and activation metrics are recorded in block headers for audit.

> **Coda:**
> This fully decentralized, proof‑based governance model ensures that Ergo evolves strictly by actual network consensus, with no reliance on privileged actors or treasury funds.
"""

ergo_ledger = celaut_pb2.Contract.Ledger(
    tags=[LEDGER],
    prose=PROSE,
    formal=FORMAL.encode("utf-8"),
)