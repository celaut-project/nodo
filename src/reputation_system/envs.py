from protos import celaut_pb2


LEDGER = "ergo"

# ---------------------------------------------------------------------------
# Canonical reputation-proof contract identity.
#
# nodo does NOT compile the reputation contract locally. AppKit compiles it to
# ErgoTree v0, which lands the proof box at a different P2S address than the one
# the reputation-systems/reputation-system contracts publish to and read from,
# making nodo's proofs invisible to that system. Instead we pin the exact
# compiled contract it uses.
#
# INTEGRITY — re-derive and verify every value below directly from
# github.com/reputation-systems/reputation-system (see its src/lib/envs.ts):
#   1. Compile src/lib/contracts/digital_public_good.es with @fleet-sdk/compiler
#      at ErgoTree version 1.
#      DIGITAL_PUBLIC_GOOD_SCRIPT_HASH == hex(blake2b256(dpgErgoTree.bytes)).
#   2. In src/lib/contracts/reputation_proof.es, replace the placeholder
#      `+DIGITAL_PUBLIC_GOOD_SCRIPT_HASH+` with that hash and compile at ErgoTree
#      version 1. Then:
#      REPUTATION_PROOF_ERGO_TREE == tree.toHex()
#      REPUTATION_PROOF_ADDRESS   == ErgoAddress.fromErgoTree(tree.toHex(),
#                                       Network.Mainnet).toString()
# The values below reproduce the on-chain contract that holds the live proofs.
# ---------------------------------------------------------------------------

DIGITAL_PUBLIC_GOOD_SCRIPT_HASH = "ceea52651b6b206381ea28a2e59f775367cef567c0c2f089dc7e09356b64ef61"

REPUTATION_PROOF_ADDRESS = "6axptaZbz6n5h3MUjsWMf4ptPTFjqF6BKoyqsrN4VNTs1impDs2DbLzcL12u8mPqDSm6sauaabcnyVoaxW7fcqqZdMisMRFwTgGcgkQrCWdsoRmvCGGynHMtMb6Ygp51BT6WF2GkezH95xzBCmpYPuTcoSBqbSacHhSJuUaTLLfR5j5q4Gnfeej7UFDJdvrXURpg6pF2EJjYNwFumeNobppRGuAbnVchWcVrPTRwuGZKjNwUaPwKZnDSA7mxm5kKBro5JEXCS2o9LaPr5kks26Qt3fdmmci3V44dwbE8FBnuMA3vUHzxHBia1bsLR862ykfAddtzP8XAt2NMa7aaJpXf5cMyK4gFqAdwnHLjsTeZnR6zQHL2UvNWujmaooZMZ26tvV4YvPQnbynACNVe5tUpRfFyrPb4WFKk6C4oby7R9aELDD2MpiZ2Yq4picCvUXPiCw6Dvd4JbGxayVSTNsa3c7EF88NvFuKbfmkCGGrozhqBokg1zmc2iGSi5ucjA2yWJ5F29gJWuEwUXrm1qrzw9ZxUbpZm37AJswhU9g1dnHSaXHSniQPxdrwysn838A8tS2KyCFfk3tAfsD2eQDjhYpRLMVEuJXGy7QMnnUMvbMF6zNCwASuRAGVRLyyYY6MLzrus1VxcWFS3vi7zjTMBoTgWdKRZTw5VJ5QrxEQgXDXB8kD94RWh5dmgAXBfGDCFKZoFVM7W1c6xqsfwduFWQApnvqQXedapoqCxs7qERarqfPa6ykRPkJpzrcXppof2YiG8d7oVnZndJgHTixkGUaSKrXmF6V9mvqJ48kLfjzBMbjyPeohk6e6v8Td1YQbFjWL89ocDUfdDtxCTQ67NiaU3T53fwivHukYrSz4EVah7PLjjC9PvZdgGs5CQoMBNqpHMz5WaefUcGEovZf9G9t2Lm96pjufto58DGPu8M2TvRjUwLqkCwc8VdkpHhcyuvrFNUoQtfX7m3oMJVvcWhrVujtMpiyByATK9939XFuZUe92fkpbyTKhEcGdeJhDXjgJgvKi6jMEpvgcHRtrqm5kykb19iHWnDeCH5T78GZpShYWCuFJpTEFSdRzptUfguXQiRVz8p38H71PMtG8sxYyCDTLrBD8Lf4cSELUKxscVj8Z4Pxjmsg1v88DS2Z3H4avf81iNGMme7eJfjtsHScH3jX623WKy3N6wVgvHpqzcdjGR"

REPUTATION_PROOF_ERGO_TREE = "19ea061c040004000400040004000400040004000402040205000400050004000402040205000500050005000e20ceea52651b6b206381ea28a2e59f775367cef567c0c2f089dc7e09356b64ef610400040001010100040204000100d803d601c6a7070ed602e47201d603b5a5d9010363eded93c27203c2a791b1db630872037300938cb2db63087203730100018cb2db6308a773020001d1ec95aea4d901046393c272047202d807d604cbc2a7d605b2db6308a7730300d6068c720501d607b5a4d9010763edededededed93cbc27207720491b1db630872077304938cb2db6308720773050001720693e4c67207070e7202e6c67207040ee6c67207050ee6c672070801d608b5a5d9010863edededededed93cbc27208720491b1db630872087306938cb2db6308720873070001720693e4c67208070e7202e6c67208040ee6c67208050ee6c672080801d609dc0c0f720701d9010963d802d60bdb63087209d60cb1720b9591720c7308b4720b7309720c83004d0ed60ae4c6a7040eededed93b07207730ad9010b41639a8c720b018cb2db63088c720b02730b0002b07208730cd9010b41639a8c720b018cb2db63088c720b02730d0002edafb0720983000ed9010b3c1a4d0ed802d60d8c720b01d60e8c8c720b020195ae720dd9010f0e93720f720e720db3720d83010e720ed9010b0e92b0b5dc0c0f720801d9010d63d802d60fdb6308720dd610b1720f95917210730eb4720f730f721083004d0ed9010d4d0e938c720d01720b7310d9010d414d0e9a8c720d018c8c720d0202b0b57209d9010d4d0e938c720d01720b7311d9010d414d0e9a8c720d018c8c720d020292b072087312d9010b41639a8c720b01c18c720b02b072077313d9010b41639a8c720b01c18c720b02aeadb5db6501fed9010b63ed93cbc2720b73148f8cc7720b01a3d9010b638cb2db6308720b73150001d9010b0e93720b720a95e4c6a70601ae7208d9010b63edededed928cb2db6308720b731600028c72050293e4c6720b040e720a93e4c6720b050ee4c6a7050ee4c6720b060193e4c6720b090ee4c6a7090e731773189593b172037319d801d604b27203731a00ededededededed93c67204040ec6a7040e93c67204050ec6a7050e93c672040601c6a7060193c67204070e720193c672040801c6a7080193c67204090ec6a7090e93db63087204db6308a792c17204c1a7731b"

PROSE = "Ergo system: PoW blockchain using Autolykos with verifiable eUTXO model, non-Turing-complete Sigma scripts, finite emission with linear reduction, on-chain miner-signaled governance, and cryptographic security via Merkle trees, proof-of-work, and zero-knowledge proofs."

ergo_ledger = celaut_pb2.Contract.Ledger(
    tags=[LEDGER],
    prose=PROSE,
    formal="".encode("utf-8"),
)
