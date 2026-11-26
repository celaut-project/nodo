# Ledgers

A ledger: the network, its consensus, its units, and the keys of a reputation proof on
it.

Class: `ledger`.

## Places

`Contract.ledger` of a payment contract or of a pointer to a reputation proof
(`ReputationProof.ledger_proofs`).

## Value

None: a ledger is a `Descriptor`. A node that does not know the ledger does not pay
on it and does not credit a proof on it.
