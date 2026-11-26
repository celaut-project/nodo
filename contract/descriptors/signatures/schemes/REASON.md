# Signature schemes

A signature scheme: the algorithm, the form of the public key (the signer), and the
form of the signature. A hybrid scheme is one formal that names its algorithms.

Class: `signature-scheme`.

## Places

`Signed.Body.scheme`, and the identity of a peer. A node that does not know the scheme
of a message cannot verify it, and it never accepts a signature that nobody verified.
It can look for a service that verifies the scheme (REASON 1.6).

## Value

None: a scheme is a `Descriptor`.
