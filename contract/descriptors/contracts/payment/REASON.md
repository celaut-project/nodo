# Payment protocols

The kind of a payment contract on a ledger: what the attributes of the contract mean,
and how to pay.

Class: `payment-protocol`.

## Places

`Contract.protocol` of each payment contract that a node announces
(`Peer.payment_contracts`, `Api.payment_contracts`).

## Rules

- The formal defines the keys of `Contract.xattrs`: their meaning, their encoding, and
  which are required (`payment.xattr.*`). It also defines the way to pay.
- A node selects the payment validator by the formal. A node that does not know the
  formal cannot pay to the contract: it does not store the contract and does not use
  it. A payment contract with no protocol, or with an empty formal, is not valid.
- A payee parameter is a line that the payee selects for itself and states for the
  payer (`payment.min_confirmations`, `payment.too_early`). Nodes do not compare these
  lines, and they are not in the identity of the protocol. The files here are the
  identity, without them.
- The files here came before `../../REASON.md` (rule 6.4).
