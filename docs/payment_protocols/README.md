# Payment protocols

A **payment protocol** is the kind of payment contract on a ledger. It is the message
`celaut.Contract.Protocol` (`tags`, `prose`, `formal`), in the field `Contract.protocol`
of each payment contract that a node announces (#671). It replaces the xattr
`contract_type`.

The `Ledger` of a payment contract describes the network: the consensus, the
computation, the units and the rules. The protocol describes one kind of contract on that
network. A ledger can have more than one kind.

## Rules

- The `formal` of the protocol defines the keys of `Contract.xattrs`: what they mean,
  their encoding and which of them are required (`payment.xattr.*`). It also defines the
  way to pay.
- Two payment protocols are the same only if their `formal` fields have the same bytes.
  `tags` help a reader. `prose` never decides.
- A node chooses the payment validator by the `formal`. The `contract_hash` of the
  database is the SHA3-256 of it.
- A node that does not know the `formal` cannot pay to the contract. It does not store
  the contract and does not use it. A payment contract with no protocol, or with an empty
  `formal`, is not valid.
- A published `formal` never changes. A new meaning is a new `formal`.
- A **payee parameter** is a line that the payee selects for itself and states for the
  payer (`payment.min_confirmations`, `payment.too_early`). The nodes do not compare these
  lines, and they are not in the identity of the protocol (`protocol_id`). The files here
  are the identity, without them.
- A reputation proof has no protocol. Its ledger declares its keys, with the attestation
  of the owner (`owner_public_key`, `owner_signature`).

## Protocols

The file of each protocol is the exact bytes of its `formal`, without payee parameters.
The code of the node holds the same bytes, and a test pins them to these files.

| Protocol | Tag | `formal` | Ledger |
| --- | --- | --- | --- |
| Ergo pay-to-public-key | `ergo-p2pk` | [`ergo-p2pk.formal`](ergo-p2pk.formal) | Ergo |
| Bitcoin p2wpkh | `bitcoin-p2wpkh` | [`bitcoin-p2wpkh.formal`](bitcoin-p2wpkh.formal) | Bitcoin |

The Bitcoin protocol also has two payee parameters on the wire: `payment.min_confirmations`
and `payment.too_early` (see [#626](https://github.com/celaut-project/nodo/issues/626)).
