# Proofs of work

The rule of a proof of work that a node asks for before it gives a new client, and the
unit of its difficulty.

Class: `proof-of-work`.

## Places

`PoWRequired.descriptor`, in the answer of `GenerateClient`.

## Value

None: the rule is a `Descriptor`. The challenge and the difficulty are fields of
`PoWRequired`.
