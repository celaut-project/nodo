# Usage of Ergo Platform

### Why Ergo was Selected as the Path Forward for Celaut

Ergo was selected because its principles align with those of Celaut, as reflected in the Ergo Manifesto (available in [Ergo Manifesto](https://ergoplatform.org/en/blog/2021-04-26-the-ergo-manifesto/)).
Furthermore, its advanced technology and a community dedicated to these ideals reinforce its suitability.

It has been observed that no other network genuinely upholds these principles, as many tend to corporatize the products built on them, centralizing control in one way or another *(according to the developers of celaut-project/node).*

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

- Each node shares a **public wallet** (a payment address) with its clients.
- Clients (other nodes or external entities) register with the node and receive a **private key** to authenticate themselves.

#### 2. ERG Deposits (Gas)

- To increase their gas amount, the client generates a **deposit token** and creates an **Ergo transaction** that includes this token and a certain amount of ERGs.
- The client then notifies the node once the deposit token has been transferred.

#### 3. Deposit Verification

- The node verifies that the **deposit token** belongs to the client.
- If valid and the funds have been transferred to the node's **public wallet**, the client’s gas is increased according to the amount of ERGs received.
- The deposit token is then marked as **approved**.

#### 4. Wallet Management in the Nodo

The node uses two wallets to manage its funds and transactions:

- **Public wallet** (also called *hot wallet*): This is the address where clients send their payments. The node cannot spend directly from this wallet, but a **maintenance thread** transfers processed funds to the **sending wallet** or to a **cold address**.

- **Sending wallet** (also hot): This is the wallet the node uses to pay and increase its gas on other nodes. It has a funds limit defined by **ERGO_ERG_HOT_WALLET_LIMITS**. If the node operator has configured a **cold address**, the excess funds are sent to that address.

#### 5. Cold Address

- The **cold address** is a **public address** provided by the node operator. It is where ERGs that the node doesn't need for immediate operations are sent. It serves as a secure reserve for long-term management.

#### 6. Maintenance and Transfers

- The **maintenance thread** moves funds from the **public wallet** to the **sending wallet** only when the deposit tokens have been verified and approved.
- Funds are transferred to the **sending wallet** until it reaches the **ERGO_ERG_HOT_WALLET_LIMITS**.
- If this limit is exceeded, the excess ERGs are sent to the **cold address** provided by the node operator.
- Additionally, a configurable percentage (default set to 0%) is transferred to a **donation wallet** for the **celaut-project/node** to support project development.

#### Difference Between Wallet and Address

- **Wallet**: When the node has access to the **seed** of a wallet, it can sign transactions.
- **Address**: The node only holds the **public key** for an address, meaning it can only send funds to it, without the ability to spend received funds.

The node operator can manually provide the seed for the **sending wallet** in case the node has been reinstalled. This wallet is also used to add reputation proofs to the network.

### Sharing Information Between Nodos

When a node shares information with another, it provides two key elements:

1. The **ID of its reputation proof**.
2. The **payment address** (its public wallet) where funds should be sent.
