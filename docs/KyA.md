# Know Your Assumptions (KyA) for **Nodo**

**Nodo** is P2P software that follows the [CELAUT](https://celaut-project.github.io/paradigm) paradigm, allowing nodes to offer and request services. All payments, reputation submissions, and service remunerations are handled decentralized on the Ergo blockchain.

## By accepting this KyA, you acknowledge and agree that:

1. The use of **Nodo** is at your own risk.
2. You are solely responsible for your assets and decisions within the network.
3. Services offered and requested in **Nodo** are remunerated through Ergo, and all transactions are final and irreversible.

## Please note that:

- **Alpha Version**: **Nodo** is in its alpha phase, meaning there may be bugs and issues. Full functionality and security of the software are not guaranteed.
- **Payment System**: All transactions are processed via the Ergo platform, and each can be viewed through the Ergo explorer.
- **Reputation System**: Nodes generate reputation proofs stored on the Ergo platform. These proofs influence the level of trust within the network.
- **Donation Models**: A node donates a share of its **earnings** — 2% by default — to the wallets listed in `ledgers.ergo.payments.DONATION_WALLETS`. The installer asks for this figure and shows the suggested value; setting `DONATION_PERCENTAGE` to `0` opts out, and one edit of `config.yaml` changes it at any time. Nothing is enforced: what a donation buys is a bounded bonus in *other* nodes' peer selection, computed from the chain. See `docs/DONATIONS.md`.
- **Alternative Payments**: Although Ergo is currently used for transactions, other payment systems may be integrated into **Nodo** in the future.

**Nodo** developers cannot offer assistance in cases of asset loss, hacking, or theft of private keys.

**Check how and why Nodo uses [Ergo](ERGO.md).**
